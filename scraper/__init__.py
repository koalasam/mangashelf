"""
Scraper registry for MangaShelf.

To add a new scraper:
  1. Drop the scraper .py file into this folder (e.g. mymangasource.py)
  2. Make sure it exposes a  download(url: str)  function at the module level
  3. Add an entry to SCRAPERS below:
       'source-name': ('mymangasource', 'Human Readable Name')

The key is what gets stored in monitored.json and shown in the UI.
"""

# Registry: scraper_key -> (module_filename_without_py, display_name)
SCRAPERS = {
    'weebcentral': ('weebcenteral', 'Weeb Central'),
}


def get_scraper(key: str):
    """Import and return the scraper module for the given key, or None."""
    import importlib
    import sys
    import os

    if key not in SCRAPERS:
        return None

    module_file, _ = SCRAPERS[key]

    # Ensure the scraper folder is on sys.path
    scraper_dir = os.path.dirname(__file__)
    if scraper_dir not in sys.path:
        sys.path.insert(0, os.path.dirname(scraper_dir))  # project root

    try:
        module = importlib.import_module(f'scraper.{module_file}')
        return module
    except ImportError as e:
        print(f'[Scraper] Failed to import {module_file}: {e}')
        return None


def list_scrapers():
    """Return list of (key, display_name) tuples for the UI."""
    return [(k, v[1]) for k, v in SCRAPERS.items()]