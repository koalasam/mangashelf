import aiohttp
import asyncio
import os
from tqdm import tqdm
import aiofiles

# List of domains to try in order
DOMAINS = [
    "https://official.lowee.us",
    "https://scans.lastation.us",
    "https://scans-hot.planeptune.us",
    "https://hot.planeptune.us",
    "https://scans-hot.planeptune.us"
]

async def download_image(session, image_url, file_path, pbar):
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)

        async with session.get(image_url) as response:
            if response.status == 200:
                async with aiofiles.open(file_path, "wb") as f:
                    await f.write(await response.read())
                pbar.update(1)
                return True
            else:
                pbar.update(1)
                return False
    except Exception:
        pbar.update(1)
        return False

async def check_page_exists(session, test_url):
    try:
        async with session.head(test_url) as response:
            return response.status == 200
    except Exception:
        return False

async def find_working_domain_for_chapter(session, series_name, chapter):
    test_urls = [
        f"{domain}/manga/{series_name}/{str(chapter).zfill(4)}-001.png"
        for domain in DOMAINS
    ]

    for test_url in test_urls:
        if await check_page_exists(session, test_url):
            return test_url.split("/manga/")[0]

    return None

async def get_max_page(session, base_domain, series_name, chapter, pbar=None):
    max_page = 1

    while True:
        test_url = (
            f"{base_domain}/manga/{series_name}/"
            f"{str(chapter).zfill(4)}-{str(max_page).zfill(3)}.png"
        )
        if await check_page_exists(session, test_url):
            max_page += 1
        else:
            break

    if pbar:
        pbar.update(1)

    return max_page - 1

async def get_max_chapter(session, series_name):
    max_chapter = 1

    with tqdm(desc="Finding max chapter", unit="chapter") as pbar:
        while True:
            working_domain = await find_working_domain_for_chapter(
                session, series_name, max_chapter
            )

            if working_domain:
                max_chapter += 1
                pbar.update(1)
            else:
                break

    return max_chapter - 1

async def get_all_max_pages_and_domains(session, series_name, max_chapter):
    print("Finding max pages and domains for all chapters...")

    with tqdm(total=max_chapter, desc="Finding domains", unit="chapter") as pbar:
        domain_tasks = [
            find_working_domain_for_chapter(session, series_name, chapter)
            for chapter in range(1, max_chapter + 1)
        ]
        working_domains = await asyncio.gather(*domain_tasks)
        pbar.update(max_chapter)

    domain_dict = {
        chapter + 1: domain for chapter, domain in enumerate(working_domains)
    }

    print("Finding max pages for all chapters...")
    with tqdm(total=max_chapter, desc="Scanning chapters", unit="chapter") as pbar:
        tasks = [
            get_max_page(
                session,
                domain_dict[chapter],
                series_name,
                chapter,
                pbar,
            )
            for chapter in range(1, max_chapter + 1)
        ]

        max_pages = await asyncio.gather(*tasks)

    return {
        chapter + 1: (domain_dict[chapter + 1], max_page)
        for chapter, max_page in enumerate(max_pages)
    }

async def download_manga(url):
    prefix = "https://weebcentral.com/series/"
    if not url.startswith(prefix):
        print("Invalid Weeb Central URL")
        return

    series_name = url[len(prefix):].split("/", 1)[1].strip("/")
    print(f"Detected series name: {series_name}")

    async with aiohttp.ClientSession() as session:
        max_chapter = await get_max_chapter(session, series_name)
        print(f"Max chapter found: {max_chapter}")

        # Smart zero-fill width based on chapter count
        chapter_pad = len(str(max_chapter))

        chapter_data = await get_all_max_pages_and_domains(
            session, series_name, max_chapter
        )

        total_images = sum(data[1] for data in chapter_data.values())
        print(f"Total images to download: {total_images}")

        download_tasks = []

        with tqdm(total=total_images, desc="Downloading", unit="image") as pbar:
            for chapter in range(1, max_chapter + 1):
                base_domain, max_page = chapter_data[chapter]

                chapter_folder = f"chapter-{str(chapter).zfill(chapter_pad)}"

                for page in range(1, max_page + 1):
                    image_url = (
                        f"{base_domain}/manga/{series_name}/"
                        f"{str(chapter).zfill(4)}-{str(page).zfill(3)}.png"
                    )

                    file_path = (
                        f"./Manga/{series_name}/"
                        f"{chapter_folder}/"
                        f"{str(page).zfill(3)}.png"
                    )

                    download_tasks.append(
                        download_image(session, image_url, file_path, pbar)
                    )

            await asyncio.gather(*download_tasks)

def download(url):
    asyncio.run(download_manga(url))

if __name__ == "__main__":
    url = input("Enter weebcentral.com series URL: ")
    download(url)
