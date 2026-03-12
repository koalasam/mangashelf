from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file
from datetime import timedelta
import json
import threading
import subprocess
import sys
import os
import re
from pathlib import Path
from PIL import Image
import hashlib

app = Flask(__name__)
app.secret_key = 'manga-reader-secret-key-change-me'
app.permanent_session_lifetime = timedelta(days=30)
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True

DATA_DIR = Path('data')
MANGA_DIR = Path('manga')  # Where manga folders live
COVERS_DIR = Path('static/covers')

DATA_DIR.mkdir(exist_ok=True)
MANGA_DIR.mkdir(exist_ok=True)
COVERS_DIR.mkdir(exist_ok=True)

USERS_FILE = DATA_DIR / 'users.json'
LIBRARY_FILE = DATA_DIR / 'library.json'
PROGRESS_FILE = DATA_DIR / 'progress.json'
SETTINGS_FILE = DATA_DIR / 'settings.json'
USER_SETTINGS_FILE = DATA_DIR / 'user_settings.json'
MONITORED_FILE = DATA_DIR / 'monitored.json'

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.avif'}

def hash_password(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)

def init_data():
    if not USERS_FILE.exists():
        save_json(USERS_FILE, {
            'admin': {
                'password': hash_password('admin'),
                'role': 'admin',
                'display_name': 'Admin'
            }
        })
    if not LIBRARY_FILE.exists():
        save_json(LIBRARY_FILE, {})
    if not PROGRESS_FILE.exists():
        save_json(PROGRESS_FILE, {})
    if not SETTINGS_FILE.exists():
        save_json(SETTINGS_FILE, {'manga_dir': 'manga'})
    if not USER_SETTINGS_FILE.exists():
        save_json(USER_SETTINGS_FILE, {})
    if not MONITORED_FILE.exists():
        save_json(MONITORED_FILE, {})

init_data()

def get_users():
    return load_json(USERS_FILE, {})

def get_library():
    return load_json(LIBRARY_FILE, {})

def get_progress():
    return load_json(PROGRESS_FILE, {})

def get_settings():
    return load_json(SETTINGS_FILE, {})

def get_user_settings():
    return load_json(USER_SETTINGS_FILE, {})

def get_monitored():
    return load_json(MONITORED_FILE, {})

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return redirect(url_for('login'))
        users = get_users()
        if users.get(session['username'], {}).get('role') != 'admin':
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated

def natural_sort_key(s):
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', str(s))]

def is_double_page(img_path):
    try:
        with Image.open(img_path) as img:
            w, h = img.size
            return w > h
    except:
        return False

def compute_page_pairs(pages, force_first_right=None):
    """
    pages: sorted list of image filenames for a chapter
    Returns list of pairs. Each pair is [left, right] by page index (1-based).
    Double pages (landscape) are stored as [page] alone.
    force_first_right: if True, force page 1 to right side; if False, force to left;
                       if None, auto-detect from double-page spreads.
    """
    n = len(pages)
    if n == 0:
        return []

    # Determine if each page is double
    double = []
    for p in pages:
        double.append(is_double_page(p))

    if force_first_right is not None:
        # Use the explicit override
        first_page_is_right = force_first_right
    else:
        # Auto-detect from first double-page spread anchor
        first_page_is_right = True  # default: page 1 on right
        for i, is_dp in enumerate(double):
            if is_dp:
                if i % 2 == 1:
                    first_page_is_right = False
                break

    # Now build pairs
    pairs = []
    i = 0
    right_start = first_page_is_right  # if True, page 0 goes right

    # Assign display positions
    # right_start=True: page 0=right(even pos), page 1=left(odd pos), page 2=right...
    # right_start=False: page 0=left(odd pos), page 1=right(even pos)...

    if not right_start:
        # Page 0 is alone on left (no right partner at start)
        pairs.append([1])  # page 1 alone on left
        i = 1

    while i < n:
        if double[i]:
            pairs.append([i + 1])  # double page, shown alone
            i += 1
        else:
            # Right page
            right = i + 1
            i += 1
            if i < n and not double[i]:
                left = i + 1
                pairs.append([left, right])  # [left, right] = later, earlier
                i += 1
            else:
                # Lone right page (end of chapter or next is double)
                pairs.append([right])

    return pairs

def scan_series(series_path, series_id, existing_chapters=None):
    """Scan a series folder and return structured data.
    existing_chapters: list of previously saved chapter dicts, used to preserve manual overrides.
    """
    existing_by_id = {c['id']: c for c in (existing_chapters or [])}
    series_path = Path(series_path)
    if not series_path.exists():
        return None

    # Find cover
    cover = None
    for ext in ['.jpg', '.jpeg', '.png', '.webp']:
        for name in ['cover', 'Cover', 'poster', 'Poster', 'folder', 'Folder']:
            candidate = series_path / f"{name}{ext}"
            if candidate.exists():
                cover = str(candidate)
                break
        if cover:
            break

    # Find chapters (subfolders)
    chapters = []
    for item in sorted(series_path.iterdir(), key=lambda x: natural_sort_key(x.name)):
        if item.is_dir():
            pages = sorted(
                [p for p in item.iterdir() if p.suffix.lower() in IMAGE_EXTS],
                key=lambda x: natural_sort_key(x.name)
            )
            if pages:
                existing = existing_by_id.get(item.name, {})
                force = existing.get('first_page_right', None)
                pairs = compute_page_pairs(pages, force_first_right=force)
                ch_data = {
                    'id': item.name,
                    'title': item.name,
                    'pages': [str(p) for p in pages],
                    'pairs': pairs,
                    'page_count': len(pages)
                }
                if force is not None:
                    ch_data['first_page_right'] = force
                chapters.append(ch_data)

    # If no subfolders, treat root images as single chapter
    if not chapters:
        pages = sorted(
            [p for p in series_path.iterdir() if p.suffix.lower() in IMAGE_EXTS],
            key=lambda x: natural_sort_key(x.name)
        )
        if pages:
            existing = existing_by_id.get('Chapter 1', {})
            force = existing.get('first_page_right', None)
            pairs = compute_page_pairs(pages, force_first_right=force)
            ch_data = {
                'id': 'Chapter 1',
                'title': 'Chapter 1',
                'pages': [str(p) for p in pages],
                'pairs': pairs,
                'page_count': len(pages)
            }
            if force is not None:
                ch_data['first_page_right'] = force
            chapters.append(ch_data)

    # Copy/find cover
    cover_dest = COVERS_DIR / f"{series_id}.jpg"
    if cover:
        try:
            img = Image.open(cover)
            img = img.convert('RGB')
            img.thumbnail((300, 450))
            img.save(cover_dest, 'JPEG')
        except:
            pass
    elif chapters and chapters[0]['pages']:
        # Use first page as cover
        try:
            img = Image.open(chapters[0]['pages'][0])
            img = img.convert('RGB')
            img.thumbnail((300, 450))
            img.save(cover_dest, 'JPEG')
        except:
            pass

    return {
        'id': series_id,
        'title': series_path.name,
        'path': str(series_path),
        'cover': str(cover_dest) if cover_dest.exists() else None,
        'chapters': chapters,
        'chapter_count': len(chapters)
    }

# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        users = get_users()
        if username in users and users[username]['password'] == hash_password(password):
            session.permanent = True  # always use permanent session with 30-day lifetime
            session['username'] = username
            session['role'] = users[username].get('role', 'user')
            return redirect(url_for('index'))
        error = 'Invalid username or password'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    library = get_library()
    progress = get_progress()
    user_progress = progress.get(session['username'], {})

    # Continue reading: find series with progress
    continue_reading = []
    for sid, sdata in library.items():
        if sid in user_progress:
            p = user_progress[sid]
            continue_reading.append({
                **sdata,
                'last_chapter': p.get('chapter'),
                'last_page': p.get('page', 1)
            })
    continue_reading = continue_reading[:5]

    # Highlighted: newest or random up to 6
    highlighted = list(library.values())[:6]

    return render_template('index.html', 
                           continue_reading=continue_reading,
                           highlighted=highlighted,
                           username=session['username'])

@app.route('/library')
@login_required
def library():
    lib = get_library()
    series_list = list(lib.values())
    return render_template('library.html', series_list=series_list, username=session['username'])

@app.route('/series/<series_id>')
@login_required
def series(series_id):
    library = get_library()
    if series_id not in library:
        return redirect(url_for('library'))
    s = library[series_id]
    order = request.args.get('order', 'asc')
    chapters = s.get('chapters', [])
    if order == 'desc':
        chapters = list(reversed(chapters))
    
    progress = get_progress()
    user_progress = progress.get(session['username'], {}).get(series_id, {})
    
    return render_template('series.html', series=s, chapters=chapters, order=order,
                           progress=user_progress, username=session['username'])

@app.route('/read/<series_id>/<chapter_id>')
@login_required
def reader(series_id, chapter_id):
    library = get_library()
    if series_id not in library:
        return redirect(url_for('library'))
    s = library[series_id]
    
    chapters = s.get('chapters', [])
    chapter = next((c for c in chapters if c['id'] == chapter_id), None)
    if not chapter:
        return redirect(url_for('series', series_id=series_id))
    
    chapter_index = next(i for i, c in enumerate(chapters) if c['id'] == chapter_id)
    prev_chapter = chapters[chapter_index - 1]['id'] if chapter_index > 0 else None
    next_chapter = chapters[chapter_index + 1]['id'] if chapter_index < len(chapters) - 1 else None
    
    page = request.args.get('page', 1, type=int)
    user_settings = get_user_settings()
    saved_mode = user_settings.get(session['username'], {}).get('reader_mode', 'single')
    mode = request.args.get('mode', saved_mode)
    progress = get_progress()
    user_progress = progress.get(session['username'], {}).get(series_id, {})
    initial_pair_idx = 0  # always resume from first page
    initial_page_idx = 0  # always resume from first page
    
    users = get_users()
    is_admin = users.get(session['username'], {}).get('role') == 'admin'
    fpr = chapter.get('first_page_right', None)
    first_page_side = 'right' if fpr is True else 'left' if fpr is False else 'auto'

    return render_template('reader.html',
                           series=s,
                           series_id=series_id,
                           chapter=chapter,
                           chapter_index=chapter_index,
                           prev_chapter=prev_chapter,
                           next_chapter=next_chapter,
                           initial_page=page,
                           initial_pair_idx=initial_pair_idx,
                           initial_page_idx=initial_page_idx,
                           mode=mode,
                           is_admin=is_admin,
                           first_page_side=first_page_side,
                           username=session['username'])

@app.route('/admin')
@admin_required
def admin():
    from scraper import list_scrapers
    users = get_users()
    settings = get_settings()
    library = get_library()
    monitored = get_monitored()
    scrapers = list_scrapers()
    return render_template('admin.html', users=users, settings=settings,
                           library=library, monitored=monitored,
                           scrapers=scrapers, username=session['username'])

# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route('/api/page/<path:filepath>')
@login_required
def serve_page(filepath):
    full_path = Path(filepath)
    if full_path.exists():
        return send_file(full_path)
    return 'Not found', 404

@app.route('/api/progress', methods=['POST'])
@login_required
def save_progress():
    data = request.json
    progress = get_progress()
    username = session['username']
    if username not in progress:
        progress[username] = {}
    series_id = data.get('series_id')
    progress[username][series_id] = {
        'chapter': data.get('chapter'),
        'page': data.get('page'),
        'pair_index': data.get('pair_index', 0),
        'page_index': data.get('page_index', 0)
    }
    save_json(PROGRESS_FILE, progress)
    return jsonify({'ok': True})

@app.route('/api/user_settings', methods=['GET', 'POST'])
@login_required
def user_settings_api():
    us = get_user_settings()
    username = session['username']
    if request.method == 'GET':
        return jsonify(us.get(username, {}))
    data = request.json
    if username not in us:
        us[username] = {}
    if 'reader_mode' in data:
        us[username]['reader_mode'] = data['reader_mode']
    save_json(USER_SETTINGS_FILE, us)
    return jsonify({'ok': True})

@app.route('/api/scan', methods=['POST'])
@admin_required
def scan():
    settings = get_settings()
    manga_dir = Path(settings.get('manga_dir', 'manga'))
    library = get_library()
    
    results = {'added': [], 'updated': [], 'errors': []}
    
    if not manga_dir.exists():
        return jsonify({'error': f'Manga directory {manga_dir} does not exist'}), 400
    
    for item in manga_dir.iterdir():
        if item.is_dir():
            series_id = re.sub(r'[^a-z0-9_-]', '_', item.name.lower())
            try:
                data = scan_series(item, series_id)
                if data:
                    if series_id in library:
                        results['updated'].append(item.name)
                    else:
                        results['added'].append(item.name)
                    library[series_id] = data
            except Exception as e:
                results['errors'].append(f"{item.name}: {str(e)}")
    
    save_json(LIBRARY_FILE, library)
    return jsonify(results)

@app.route('/api/users', methods=['POST'])
@admin_required
def manage_users():
    action = request.json.get('action')
    users = get_users()
    
    if action == 'add':
        username = request.json.get('username', '').strip()
        password = request.json.get('password', '')
        role = request.json.get('role', 'user')
        if not username or not password:
            return jsonify({'error': 'Username and password required'}), 400
        if username in users:
            return jsonify({'error': 'User already exists'}), 400
        users[username] = {'password': hash_password(password), 'role': role, 'display_name': username}
        save_json(USERS_FILE, users)
        return jsonify({'ok': True})
    
    elif action == 'delete':
        username = request.json.get('username')
        if username == session['username']:
            return jsonify({'error': 'Cannot delete yourself'}), 400
        if username in users:
            del users[username]
            save_json(USERS_FILE, users)
        return jsonify({'ok': True})
    
    elif action == 'change_password':
        username = request.json.get('username')
        new_password = request.json.get('password', '')
        if username in users and new_password:
            users[username]['password'] = hash_password(new_password)
            save_json(USERS_FILE, users)
            return jsonify({'ok': True})
        return jsonify({'error': 'Invalid request'}), 400
    
    return jsonify({'error': 'Unknown action'}), 400

@app.route('/api/settings', methods=['POST'])
@admin_required
def update_settings():
    data = request.json
    settings = get_settings()
    if 'manga_dir' in data:
        settings['manga_dir'] = data['manga_dir']
    save_json(SETTINGS_FILE, settings)
    return jsonify({'ok': True})

@app.route('/api/pairs/<series_id>/<chapter_id>', methods=['POST'])
@admin_required
def update_pairs(series_id, chapter_id):
    library = get_library()
    if series_id not in library:
        return jsonify({'error': 'Series not found'}), 404
    s = library[series_id]
    chapter = next((c for c in s['chapters'] if c['id'] == chapter_id), None)
    if not chapter:
        return jsonify({'error': 'Chapter not found'}), 404
    
    pairs = request.json.get('pairs')
    if pairs is not None:
        chapter['pairs'] = pairs
        save_json(LIBRARY_FILE, library)
    return jsonify({'ok': True, 'pairs': chapter['pairs']})

@app.route('/api/rescan_pairs/<series_id>/<chapter_id>', methods=['POST'])
@admin_required
def rescan_pairs(series_id, chapter_id):
    library = get_library()
    if series_id not in library:
        return jsonify({'error': 'Series not found'}), 404
    s = library[series_id]
    chapter = next((c for c in s['chapters'] if c['id'] == chapter_id), None)
    if not chapter:
        return jsonify({'error': 'Chapter not found'}), 404

    data = request.json or {}
    # Respect any stored override, or a newly supplied one
    if 'first_page_right' in data:
        force = data['first_page_right']  # True / False / None
        chapter['first_page_right'] = force
    else:
        force = chapter.get('first_page_right', None)  # None = auto

    pages = [Path(p) for p in chapter['pages']]
    pairs = compute_page_pairs(pages, force_first_right=force)
    chapter['pairs'] = pairs
    save_json(LIBRARY_FILE, library)
    return jsonify({'ok': True, 'pairs': pairs, 'first_page_right': force})

@app.route('/api/set_first_page_side/<series_id>/<chapter_id>', methods=['POST'])
@admin_required
def set_first_page_side(series_id, chapter_id):
    """Set which side page 1 goes on and recompute pairs."""
    library = get_library()
    if series_id not in library:
        return jsonify({'error': 'Series not found'}), 404
    s = library[series_id]
    chapter = next((c for c in s['chapters'] if c['id'] == chapter_id), None)
    if not chapter:
        return jsonify({'error': 'Chapter not found'}), 404

    data = request.json or {}
    # 'side': 'right', 'left', or 'auto'
    side = data.get('side', 'auto')
    if side == 'right':
        force = True
    elif side == 'left':
        force = False
    else:
        force = None  # auto-detect

    chapter['first_page_right'] = force
    pages = [Path(p) for p in chapter['pages']]
    pairs = compute_page_pairs(pages, force_first_right=force)
    chapter['pairs'] = pairs
    save_json(LIBRARY_FILE, library)
    return jsonify({'ok': True, 'pairs': pairs, 'first_page_right': force, 'side': side})


# ── Scraper / Monitor helpers ─────────────────────────────────────────────────

def run_scraper_for_series(monitor_key):
    """
    Run the scraper for a monitored series and trigger a library rescan.
    monitor_key: key in monitored.json
    Returns (success, message)
    """
    import datetime
    monitored = get_monitored()
    if monitor_key not in monitored:
        return False, 'Series not found in monitored list'

    entry = monitored[monitor_key]
    scraper_key = entry.get('scraper')
    url = entry.get('url')

    from scraper import get_scraper
    scraper = get_scraper(scraper_key)
    if not scraper:
        return False, f'Scraper "{scraper_key}" not found'

    if not hasattr(scraper, 'download'):
        return False, f'Scraper "{scraper_key}" has no download() function'

    # Run scraper from the manga directory so relative paths land correctly
    settings = get_settings()
    manga_dir = Path(settings.get('manga_dir', 'manga')).resolve()
    manga_dir.mkdir(exist_ok=True)

    try:
        import os
        old_cwd = os.getcwd()
        # Scrapers download to ./Manga/<series>/ relative to cwd
        # We cd to the parent of manga_dir so files land in the right place
        os.chdir(manga_dir.parent)
        try:
            scraper.download(url)
        finally:
            os.chdir(old_cwd)
    except Exception as e:
        return False, f'Scraper error: {str(e)}'

    # Update last_checked and chapter count, then rescan library
    library = get_library()
    monitored = get_monitored()  # reload after scrape

    # Re-scan just this series
    # Try to figure out series folder from url or existing library entry
    series_id = entry.get('series_id')
    if series_id:
        series_path = manga_dir / library.get(series_id, {}).get('path', '')
        # Use the stored path if available, otherwise search manga_dir
        stored_path = library.get(series_id, {}).get('path')
        if stored_path:
            sp = Path(stored_path)
            if sp.exists():
                existing_chs = library.get(series_id, {}).get('chapters', [])
                data = scan_series(sp, series_id, existing_chapters=existing_chs)
                if data:
                    library[series_id] = data
                    save_json(LIBRARY_FILE, library)

    # Also do a full scan to catch anything new
    run_auto_scan()

    monitored[monitor_key]['last_checked'] = datetime.datetime.now().isoformat()
    save_json(MONITORED_FILE, monitored)
    return True, 'Scrape completed successfully'


def check_monitored_updates():
    """Check all monitored series for new chapters by running their scrapers."""
    import datetime
    print('[Monitor] Checking for updates on all monitored series...')
    monitored = get_monitored()
    for key, entry in monitored.items():
        if not entry.get('enabled', True):
            continue
        print(f'[Monitor] Checking: {entry.get("title", key)}')
        success, msg = run_scraper_for_series(key)
        print(f'[Monitor] {key}: {msg}')
    print('[Monitor] Update check complete')


@app.route('/api/monitor', methods=['GET'])
@admin_required
def get_monitor():
    return jsonify(get_monitored())


@app.route('/api/monitor/add', methods=['POST'])
@admin_required
def add_monitor():
    import datetime
    data = request.json or {}
    url = data.get('url', '').strip()
    scraper_key = data.get('scraper', '').strip()
    title = data.get('title', '').strip()
    series_id = (data.get('series_id') or '').strip() or None

    if not url or not scraper_key:
        return jsonify({'error': 'URL and scraper required'}), 400

    from scraper import get_scraper, SCRAPERS
    if scraper_key not in SCRAPERS:
        return jsonify({'error': f'Unknown scraper: {scraper_key}'}), 400

    monitored = get_monitored()
    # Use URL as unique key (slugified)
    key = re.sub(r'[^a-z0-9_-]', '_', url.lower())[:80]

    monitored[key] = {
        'url': url,
        'scraper': scraper_key,
        'title': title or url,
        'series_id': series_id,
        'enabled': True,
        'last_checked': None,
        'added': datetime.datetime.now().isoformat()
    }
    save_json(MONITORED_FILE, monitored)
    return jsonify({'ok': True, 'key': key})


@app.route('/api/monitor/remove/<path:monitor_key>', methods=['POST'])
@admin_required
def remove_monitor(monitor_key):
    monitored = get_monitored()
    if monitor_key in monitored:
        del monitored[monitor_key]
        save_json(MONITORED_FILE, monitored)
    return jsonify({'ok': True})


@app.route('/api/monitor/toggle/<path:monitor_key>', methods=['POST'])
@admin_required
def toggle_monitor(monitor_key):
    monitored = get_monitored()
    if monitor_key not in monitored:
        return jsonify({'error': 'Not found'}), 404
    monitored[monitor_key]['enabled'] = not monitored[monitor_key].get('enabled', True)
    save_json(MONITORED_FILE, monitored)
    return jsonify({'ok': True, 'enabled': monitored[monitor_key]['enabled']})


@app.route('/api/monitor/run/<path:monitor_key>', methods=['POST'])
@admin_required
def run_monitor(monitor_key):
    """Manually trigger scraper for one monitored series."""
    success, msg = run_scraper_for_series(monitor_key)
    return jsonify({'ok': success, 'message': msg})


@app.route('/api/monitor/run_all', methods=['POST'])
@admin_required
def run_all_monitors():
    """Manually trigger all enabled monitored series."""
    monitored = get_monitored()
    results = {}
    for key, entry in monitored.items():
        if entry.get('enabled', True):
            success, msg = run_scraper_for_series(key)
            results[key] = {'ok': success, 'message': msg, 'title': entry.get('title', key)}
        else:
            results[key] = {'ok': None, 'message': 'Disabled', 'title': entry.get('title', key)}
    return jsonify({'ok': True, 'results': results})

# ── Scheduled Auto-Scan ───────────────────────────────────────────────────────

def run_auto_scan():
    """Scan all series in the configured manga directory."""
    try:
        settings = get_settings()
        manga_dir = Path(settings.get('manga_dir', 'manga'))
        library = get_library()
        if not manga_dir.exists():
            return
        for item in manga_dir.iterdir():
            if item.is_dir():
                series_id = re.sub(r'[^a-z0-9_-]', '_', item.name.lower())
                try:
                    existing_chs = library.get(series_id, {}).get('chapters', [])
                    data = scan_series(item, series_id, existing_chapters=existing_chs)
                    if data:
                        library[series_id] = data
                except Exception:
                    pass
        save_json(LIBRARY_FILE, library)
        print('[Auto-scan] Completed successfully')
    except Exception as e:
        print(f'[Auto-scan] Error: {e}')

def schedule_auto_scan():
    """Schedule scans at midnight and noon every day."""
    import datetime
    while True:
        now = datetime.datetime.now()
        # Next target: either today's noon or midnight (tomorrow)
        targets = [
            now.replace(hour=0, minute=0, second=0, microsecond=0),
            now.replace(hour=12, minute=0, second=0, microsecond=0),
        ]
        # Find next future target
        future = sorted([t for t in targets if t > now])
        if future:
            next_run = future[0]
        else:
            # Both already passed today — next is midnight tomorrow
            next_run = (now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        
        wait_seconds = (next_run - datetime.datetime.now()).total_seconds()
        print(f'[Auto-scan] Next scan scheduled at {next_run.strftime("%Y-%m-%d %H:%M")} (in {int(wait_seconds)}s)')
        threading.Event().wait(timeout=max(0, wait_seconds))
        run_auto_scan()
        check_monitored_updates()

# Start scheduler in background thread
_scheduler_thread = threading.Thread(target=schedule_auto_scan, daemon=True)
_scheduler_thread.start()


if __name__ == '__main__':
    app.run(host='0.0.0.0', debug=False, port=5000, threaded=True)