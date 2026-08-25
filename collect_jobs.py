from __future__ import annotations

import csv
import hashlib
import html as html_lib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

SOURCES_PATH = Path("data/job_sources.csv")
JOBS_PATH = Path("data/jobs.csv")
TIMEOUT = 20

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CyprusGamedevJobs/1.0; "
        "+https://github.com/Legion-91/cyprus-gamedev-jobs)"
    )
}

JOB_COLUMNS = [
    "company_id",
    "company",
    "job_id",
    "title",
    "location",
    "url",
    "description",
    "first_seen",
    "last_seen",
    "active",
]

JOB_WORDS = (
    "engineer", "developer", "designer", "artist", "producer", "manager",
    "analyst", "qa", "tester", "writer", "animator", "marketing",
    "recruiter", "hr", "lead", "director", "specialist", "support",
    "programmer", "architect", "product", "project", "community",
)

LISTING_TITLES = {
    "jobs", "job", "careers", "career", "vacancies", "vacancy",
    "open positions", "open roles", "all jobs", "all vacancies",
    "join us", "join our team", "apply", "apply now",
}


@dataclass
class Source:
    company_id: str
    company: str
    source_url: str
    method: str
    format: str
    notes: str


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def canonical_url(url: str) -> str:
    value = clean(url)
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    return parsed._replace(fragment="").geturl()


def strip_html(value: str) -> str:
    if not value:
        return ""
    return clean(BeautifulSoup(html_lib.unescape(value), "html.parser").get_text(" "))


def stable_id(company_id: str, native_id: str, url: str, title: str) -> str:
    if clean(native_id):
        return f"{company_id}:{clean(native_id)}"
    raw = canonical_url(url) or f"{company_id}|{clean(title).lower()}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"{company_id}:{digest}"


def get(session: requests.Session, url: str) -> requests.Response:
    response = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
    response.raise_for_status()
    return response


def load_sources() -> list[Source]:
    with SOURCES_PATH.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        Source(
            company_id=row["company_id"],
            company=row["company"],
            source_url=row["source_url"],
            method=row["method"],
            format=row["format"],
            notes=row.get("notes", ""),
        )
        for row in rows
        if row.get("status") == "found" and row.get("source_url")
    ]


def load_existing() -> dict[str, dict[str, str]]:
    if not JOBS_PATH.exists():
        return {}
    with JOBS_PATH.open(encoding="utf-8", newline="") as handle:
        return {row["job_id"]: row for row in csv.DictReader(handle) if row.get("job_id")}


def job_row(
    source: Source,
    *,
    native_id: str = "",
    title: str,
    location: str = "",
    url: str,
    description: str = "",
) -> dict[str, str] | None:
    title = clean(title)
    url = canonical_url(url)
    if not title or title.lower() in LISTING_TITLES or not url:
        return None
    if len(title) > 180:
        return None
    return {
        "company_id": source.company_id,
        "company": source.company,
        "job_id": stable_id(source.company_id, native_id, url, title),
        "title": title,
        "location": clean(location),
        "url": url,
        "description": clean(description),
    }


def scrape_ashby(session: requests.Session, source: Source) -> list[dict[str, str]]:
    board = urlparse(source.source_url).path.strip("/").split("/")[0]
    if not board:
        raise RuntimeError("Cannot determine Ashby board name")
    api_url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
    payload = get(session, api_url).json()
    result = []
    for item in payload.get("jobs", []):
        row = job_row(
            source,
            native_id=clean(item.get("id")) or clean(item.get("jobUrl")),
            title=item.get("title", ""),
            location=item.get("location", ""),
            url=item.get("jobUrl") or item.get("applyUrl") or "",
            description=strip_html(item.get("descriptionHtml", "")),
        )
        if row:
            result.append(row)
    return result


def greenhouse_board_token(source_url: str) -> str:
    path = urlparse(source_url).path.strip("/")
    return path.split("/")[0] if path else ""


def scrape_greenhouse(session: requests.Session, source: Source) -> list[dict[str, str]]:
    token = greenhouse_board_token(source.source_url)
    if not token:
        raise RuntimeError("Cannot determine Greenhouse board token")
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    payload = get(session, api_url).json()
    result = []
    for item in payload.get("jobs", []):
        title = clean(item.get("title"))
        location = clean((item.get("location") or {}).get("name"))
        content = strip_html(item.get("content", ""))

        if source.company == "Belka Games":
            evidence = f"{title} {location} {content}".lower()
            if "belka" not in evidence:
                continue

        row = job_row(
            source,
            native_id=clean(item.get("id")),
            title=title,
            location=location,
            url=item.get("absolute_url", ""),
            description=content,
        )
        if row:
            result.append(row)
    return result


def scrape_pinpoint(session: requests.Session, source: Source) -> list[dict[str, str]]:
    base = f"{urlparse(source.source_url).scheme}://{urlparse(source.source_url).netloc}"
    payload = get(session, f"{base}/postings.json").json()
    items = payload.get("data", payload.get("postings", payload if isinstance(payload, list) else []))
    if isinstance(items, dict):
        items = list(items.values())

    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        location_obj = item.get("location") or {}
        location = location_obj.get("name", "") if isinstance(location_obj, dict) else location_obj
        url = item.get("url") or item.get("job_url") or item.get("absolute_url") or ""
        if url and url.startswith("/"):
            url = urljoin(base, url)
        description = item.get("description") or item.get("description_html") or ""
        extra = " ".join(
            clean(item.get(key))
            for key in ("key_responsibilities", "skills_knowledge_expertise")
            if item.get(key)
        )
        row = job_row(
            source,
            native_id=clean(item.get("id")) or clean(item.get("slug")),
            title=item.get("title", ""),
            location=location,
            url=url,
            description=f"{strip_html(description)} {strip_html(extra)}",
        )
        if row:
            result.append(row)
    return result


def iter_jobposting(value: Any):
    if isinstance(value, dict):
        kind = value.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            yield value
        for child in value.values():
            yield from iter_jobposting(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_jobposting(child)


def jsonld_jobs(html: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except Exception:
            continue
        result.extend(iter_jobposting(payload))
    return result


def location_from_jsonld(item: dict[str, Any]) -> str:
    parts = []
    if item.get("jobLocationType"):
        parts.append(clean(item.get("jobLocationType")))
    locations = item.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        address = loc.get("address") or {}
        if isinstance(address, str):
            parts.append(clean(address))
        elif isinstance(address, dict):
            value = ", ".join(
                clean(address.get(key))
                for key in ("addressLocality", "addressRegion", "addressCountry")
                if clean(address.get(key))
            )
            if value:
                parts.append(value)
    return " | ".join(dict.fromkeys(parts))


def rows_from_jsonld(source: Source, page_url: str, html: str) -> list[dict[str, str]]:
    result = []
    for item in jsonld_jobs(html):
        row = job_row(
            source,
            native_id=clean(item.get("identifier", {}).get("value")) if isinstance(item.get("identifier"), dict) else "",
            title=item.get("title") or item.get("name") or "",
            location=location_from_jsonld(item),
            url=item.get("url") or page_url,
            description=strip_html(item.get("description", "")),
        )
        if row:
            result.append(row)
    return result


def looks_like_job_link(url: str, text: str, source_url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    source_path = urlparse(source_url).path.lower().rstrip("/")
    text_l = clean(text).lower()
    if not path or url == canonical_url(source_url):
        return False
    if text_l in LISTING_TITLES:
        return False
    if any(bad in path for bad in ("privacy", "cookie", "contact", "blog", "news", "press")):
        return False
    markers = ("/job/", "/jobs/", "/vacancy/", "/vacancies/", "/career/", "/position/")
    if any(marker in path + "/" for marker in markers):
        return True
    if source_path and path.startswith(source_path + "/") and len(text_l) >= 4:
        return True
    if any(word in text_l for word in JOB_WORDS) and len(text_l) <= 140:
        return True
    return False


def detail_fallback(source: Source, url: str, html: str, anchor: str) -> dict[str, str] | None:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True) if h1 else anchor)
    if not title or title.lower() in LISTING_TITLES:
        return None
    page_text = clean(soup.get_text(" ", strip=True))
    signals = ("responsibil", "requirements", "qualifications", "apply", "what you'll", "what you will", "we offer")
    if not any(signal in page_text.lower() for signal in signals):
        return None
    location = ""
    for selector in ("[class*=location]", "[class*=city]", "[data-testid*=location]"):
        node = soup.select_one(selector)
        if node:
            candidate = clean(node.get_text(" ", strip=True))
            if 1 < len(candidate) < 120:
                location = candidate
                break
    return job_row(
        source,
        title=title,
        location=location,
        url=url,
        description=page_text[:12000],
    )


def scrape_html(session: requests.Session, source: Source) -> list[dict[str, str]]:
    response = get(session, source.source_url)
    page_url = canonical_url(response.url)
    result: dict[str, dict[str, str]] = {}

    for row in rows_from_jsonld(source, page_url, response.text):
        result[row["job_id"]] = row

    soup = BeautifulSoup(response.text, "html.parser")
    links: list[tuple[str, str]] = []
    seen = set()
    for link in soup.find_all("a", href=True):
        url = canonical_url(urljoin(page_url, link.get("href", "")))
        text = clean(link.get_text(" ", strip=True))
        if not url or url in seen:
            continue
        seen.add(url)
        if looks_like_job_link(url, text, page_url):
            links.append((url, text))

    for url, anchor in links[:100]:
        try:
            detail = get(session, url)
        except requests.RequestException:
            continue
        detail_url = canonical_url(detail.url)
        parsed = rows_from_jsonld(source, detail_url, detail.text)
        if parsed:
            for row in parsed:
                result[row["job_id"]] = row
            continue
        fallback = detail_fallback(source, detail_url, detail.text, anchor)
        if fallback:
            result[fallback["job_id"]] = fallback

    if source.company == "Wargaming Cyprus":
        result = {
            key: row
            for key, row in result.items()
            if any(x in row["location"].lower() for x in ("nicosia", "cyprus"))
            or "nicosia" in row["description"].lower()
            or "cyprus" in row["description"].lower()
        }

    if source.company == "Pixonic":
        result = {
            key: row
            for key, row in result.items()
            if "pixonic" in f"{row['title']} {row['description']}".lower()
            or "war robots" in f"{row['title']} {row['description']}".lower()
        }

    return list(result.values())


def collect_source(source: Source) -> tuple[bool, list[dict[str, str]], str]:
    if source.method == "browser" or source.format == "js_rendered_html":
        return False, [], "browser source not collected yet"

    session = requests.Session()
    try:
        if source.method == "ashby":
            rows = scrape_ashby(session, source)
        elif source.method == "greenhouse":
            rows = scrape_greenhouse(session, source)
        elif source.method == "pinpoint":
            rows = scrape_pinpoint(session, source)
        elif source.method in {"html", "bamboohr", "huntflow"}:
            rows = scrape_html(session, source)
        else:
            return False, [], f"unsupported method: {source.method}"
        return True, rows, ""
    except Exception as exc:
        return False, [], f"{type(exc).__name__}: {exc}"


def merge(existing: dict[str, dict[str, str]], collected: dict[str, list[dict[str, str]]], successful_company_ids: set[str]) -> list[dict[str, str]]:
    timestamp = now_iso()
    output = {key: dict(value) for key, value in existing.items()}

    for row in output.values():
        if row.get("company_id") in successful_company_ids:
            row["active"] = "false"

    for company_id, rows in collected.items():
        for row in rows:
            old = output.get(row["job_id"])
            first_seen = old.get("first_seen") if old else timestamp
            output[row["job_id"]] = {
                **row,
                "first_seen": first_seen or timestamp,
                "last_seen": timestamp,
                "active": "true",
            }

    return sorted(
        output.values(),
        key=lambda row: (
            row.get("active") != "true",
            row.get("company", "").lower(),
            row.get("title", "").lower(),
        ),
    )


def save(rows: list[dict[str, str]]) -> None:
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JOBS_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=JOB_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    sources = load_sources()
    existing = load_existing()
    collected: dict[str, list[dict[str, str]]] = {}
    successful: set[str] = set()

    print(f"Sources: {len(sources)}")
    for source in sources:
        ok, rows, error = collect_source(source)
        if ok:
            successful.add(source.company_id)
            collected[source.company_id] = rows
            print(f"OK   {source.company:30} jobs={len(rows)}")
        else:
            print(f"SKIP {source.company:30} {error}")

    merged = merge(existing, collected, successful)
    save(merged)

    active = sum(row.get("active") == "true" for row in merged)
    companies = len({row["company_id"] for row in merged if row.get("active") == "true"})
    print(f"Active jobs: {active} from {companies} companies")
    print(f"Wrote {JOBS_PATH}")


if __name__ == "__main__":
    main()
