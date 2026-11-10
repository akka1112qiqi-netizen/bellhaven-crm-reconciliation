
import csv
import json
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://analyst-assessment-production.up.railway.app"
OUTPUT_DIR = Path("data")
OUTPUT_DIR.mkdir(exist_ok=True)

session = requests.Session()
session.headers.update({
    "User-Agent": "BellhavenAssessmentScraper/1.0"
})


def get_page(url):
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def find_community_links(soup):
    links = set()

    for a in soup.select("a[href]"):
        url = urljoin(BASE_URL, a["href"])
        parsed = urlparse(url)

        if parsed.netloc != urlparse(BASE_URL).netloc:
            continue

        if re.fullmatch(r"/communities/[^/]+/?", parsed.path):
            links.add(urljoin(BASE_URL, parsed.path))

    return links


def discover_communities():
    links = set()

    # Also inspect homepage for newly announced communities.
    home = get_page(BASE_URL)
    links.update(find_community_links(home))

    # Follow the community directory pagination.
    next_url = urljoin(BASE_URL, "/communities")
    visited = set()

    while next_url and next_url not in visited:
        visited.add(next_url)
        soup = get_page(next_url)

        links.update(find_community_links(soup))

        next_url = None

        for a in soup.select("a[href]"):
            label = a.get_text(" ", strip=True).lower()

            if label.startswith("next"):
                next_url = urljoin(BASE_URL, a["href"])
                break

    return sorted(links)


def section_values(texts, start, end_labels):
    try:
        start_index = texts.index(start) + 1
    except ValueError:
        return []

    end_labels = set(end_labels)
    values = []

    for item in texts[start_index:]:
        if item in end_labels:
            break
        values.append(item)

    return values


def scrape_community(url):
    soup = get_page(url)

    heading = soup.find("h1")
    if not heading:
        raise ValueError(f"Missing community name: {url}")

    name = heading.get_text(" ", strip=True)
    texts = list(soup.stripped_strings)

    address_lines = section_values(
        texts,
        "Address",
        ["Care Offerings", "Administrator", "Phone"]
    )

    offerings = section_values(
        texts,
        "Care Offerings",
        ["Administrator", "Phone"]
    )

    street = ""
    city = ""
    state = ""
    zip_code = ""

    if address_lines:
        street = " ".join(address_lines[:-1])
        city_line = address_lines[-1]

        match = re.fullmatch(
            r"(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)",
            city_line
        )

        if match:
            city, state, zip_code = match.groups()
        else:
            print(f"WARNING: Address needs review: {name}")

    if not street or not city or not offerings:
        print(f"WARNING: Incomplete details: {name}")

    return {
        "name": name,
        "address": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "care_offerings": offerings,
        "source_url": url
    }


def main():
    print("Discovering Bellhaven communities...")

    urls = discover_communities()
    print(f"Found {len(urls)} community pages.")

    records = []

    for index, url in enumerate(urls, start=1):
        record = scrape_community(url)
        records.append(record)
        print(f"[{index}/{len(urls)}] {record['name']}")

    records.sort(key=lambda row: row["name"].lower())

    json_path = OUTPUT_DIR / "bellhaven_locations.json"
    csv_path = OUTPUT_DIR / "bellhaven_locations.csv"

    with open(json_path, "w", encoding="utf-8") as file:
        json.dump(records, file, ensure_ascii=False, indent=2)

    with open(
        csv_path, "w", newline="", encoding="utf-8-sig"
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "name", "address", "city",
                "state", "zip", "care_offerings",
                "source_url"
            ]
        )
        writer.writeheader()

        for record in records:
            writer.writerow({
                **record,
                "care_offerings": "; ".join(
                    record["care_offerings"]
                )
            })

    print("\nSCRAPING COMPLETE")
    print("Total locations:", len(records))
    print("JSON saved to:", json_path)
    print("CSV saved to:", csv_path)

    if len(records) != 35:
        print("WARNING: Expected about 35 locations.")
        print("Please review the website and scraper output.")


if __name__ == "__main__":
    main()
