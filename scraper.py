from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

SOURCE_URL = (
    "https://www.gamedevmap.com/index.php?"
    "location=&country=Cyprus&state=&city=&query=&type="
)
OUTPUT_DIR = Path("data")
JSON_PATH = OUTPUT_DIR / "studios.json"
CSV_PATH = OUTPUT_DIR / "studios.csv"
MIN_EXPECTED_STUDIOS = 50

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CyprusGamedevJobs/0.1; "
        "+https://github.com/Legion-91/cyprus-gamedev-jobs)"
    )
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def normalize_header(value: str) -> str:
    value = clean_text(value).lower()
    value = value.replace("_", " ")
    return value


def canonical_header(value: str) -> str | None:
    header = normalize_header(value)

    if header == "company":
        return "company"
    if header == "type":
        return "type"
    if header == "city":
        return "city"
    if "state" in header or "province" in header:
        return "state"
    if "country" in header or "region" in header:
        return "country"

    return None


def fetch_page() -> str:
    response = requests.get(SOURCE_URL, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.text


def find_results_table(soup: BeautifulSoup) -> tuple[Tag, list[str | None], int]:
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row_index, row in enumerate(rows[:6]):
            cells = row.find_all(["th", "td"], recursive=False)
            if not cells:
                cells = row.find_all(["th", "td"])

            headers = [canonical_header(cell.get_text(" ", strip=True)) for cell in cells]
            found = {header for header in headers if header}

            if {"company", "type", "city", "country"}.issubset(found):
                return table, headers, row_index

    raise RuntimeError("Could not find the GameDevMap results table")


def normalize_website(href: str) -> str:
    href = href.strip()
    if not href:
        return ""
    return urljoin(SOURCE_URL, href)


def website_domain(url: str) -> str:
    if not url:
        return ""
    domain = urlparse(url).netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]
    return domain


def parse_studios(html: str) -> list[dict[str, str | int]]:
    soup = BeautifulSoup(html, "html.parser")
    table, headers, header_row_index = find_results_table(soup)
    rows = table.find_all("tr")

    studios: list[dict[str, str | int]] = []

    for source_row, row in enumerate(rows[header_row_index + 1 :], start=1):
        cells = row.find_all("td", recursive=False)
        if not cells:
            cells = row.find_all("td")

        if len(cells) < len(headers):
            continue

        values: dict[str, str] = {}
        company_link = None

        for index, header in enumerate(headers):
            if header is None or index >= len(cells):
                continue

            cell = cells[index]
            values[header] = clean_text(cell.get_text(" ", strip=True))

            if header == "company":
                company_link = cell.find("a", href=True)

        company = values.get("company", "")
        if not company:
            continue

        website = normalize_website(company_link["href"]) if company_link else ""

        studios.append(
            {
                "source_row": source_row,
                "company": company,
                "type": values.get("type", ""),
                "city": values.get("city", ""),
                "state": values.get("state", ""),
                "country": values.get("country", ""),
                "website": website,
                "domain": website_domain(website),
            }
        )

    if len(studios) < MIN_EXPECTED_STUDIOS:
        raise RuntimeError(
            f"Parsed only {len(studios)} studios. "
            "GameDevMap layout may have changed, refusing to overwrite data."
        )

    return studios


def save(studios: list[dict[str, str | int]]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "source_url": SOURCE_URL,
        "country_filter": "Cyprus",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "count": len(studios),
        "studios": studios,
    }

    JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    fieldnames = [
        "source_row",
        "company",
        "type",
        "city",
        "state",
        "country",
        "website",
        "domain",
    ]

    with CSV_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(studios)


def main() -> None:
    html = fetch_page()
    studios = parse_studios(html)
    save(studios)

    unique_domains = {studio["domain"] for studio in studios if studio["domain"]}
    print(f"Parsed {len(studios)} GameDevMap rows")
    print(f"Unique website domains: {len(unique_domains)}")
    print(f"Wrote {JSON_PATH} and {CSV_PATH}")


if __name__ == "__main__":
    main()
