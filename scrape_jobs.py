from __future__ import annotations

import csv
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

CAREERS_PATH = Path("data/careers.json")
OUTPUT_JSON = Path("data/jobs.json")
OUTPUT_CSV = Path("data/jobs.csv")
TIMEOUT = 15
MAX_COMPANY_WORKERS = 6
MAX_DETAIL_WORKERS = 6
MAX_DETAIL_LINKS = 60

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CyprusGamedevJobs/0.5; "
        "+https://github.com/Legion-91/cyprus-gamedev-jobs)"
    )
}

JOB_PATH_MARKERS = (
    "/job/",
    "/jobs/",
    "/vacancy/",
    "/vacancies/",
    "/position/",
    "/positions/",
    "/opening/",
    "/openings/",
    "/postings/",
)

NON_JOB_WORDS = {
    "careers",
    "career",
    "jobs",
    "vacancies",
    "vacancy",
    "open positions",
    "open roles",
    "see all jobs",
    "view all jobs",
    "all jobs",
    "all vacancies",
    "all positions",
    "apply",
    "apply now",
    "learn more",
}


def clean_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_url(url: str) -> str:
    value = clean_text(url)
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def host(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def request(session: requests.Session, url: str) -> requests.Response | None:
    try:
        response = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if response.status_code >= 400:
            return None
        return response
    except requests.RequestException:
        return None


def stable_id(company: str, url: str, title: str) -> str:
    raw = f"{company}\n{normalize_url(url)}\n{clean_text(title).lower()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def is_listing_title(title: str) -> bool:
    value = clean_text(title).lower().strip(" :|-_")
    if not value:
        return True
    if value in NON_JOB_WORDS:
        return True
    if re.fullmatch(r"all\s+(jobs|vacancies|positions)(\s*\d+)?", value):
        return True
    if re.fullmatch(r"(jobs|vacancies|positions)\s*\d+", value):
        return True
    return False


def iter_jsonld_objects(value):
    if isinstance(value, dict):
        if value.get("@type") == "JobPosting" or (
            isinstance(value.get("@type"), list) and "JobPosting" in value.get("@type", [])
        ):
            yield value
        for child in value.values():
            yield from iter_jsonld_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_jsonld_objects(child)


def parse_jsonld(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[dict] = []

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text("", strip=True)
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        jobs.extend(iter_jsonld_objects(payload))

    return jobs


def jsonld_location(job: dict) -> str:
    parts: list[str] = []

    location_type = clean_text(str(job.get("jobLocationType", "")))
    if location_type:
        parts.append(location_type)

    locations = job.get("jobLocation")
    if isinstance(locations, dict):
        locations = [locations]

    if isinstance(locations, list):
        for item in locations:
            if not isinstance(item, dict):
                continue
            address = item.get("address", {})
            if isinstance(address, str):
                value = clean_text(address)
            elif isinstance(address, dict):
                value = ", ".join(
                    clean_text(str(address.get(key, "")))
                    for key in ("addressLocality", "addressRegion", "addressCountry")
                    if clean_text(str(address.get(key, "")))
                )
            else:
                value = ""
            if value:
                parts.append(value)

    applicant = job.get("applicantLocationRequirements")
    if isinstance(applicant, dict):
        applicant = [applicant]
    if isinstance(applicant, list):
        for item in applicant:
            if isinstance(item, dict):
                value = clean_text(str(item.get("name", "")))
                if value:
                    parts.append(value)

    return " | ".join(dict.fromkeys(parts))


def job_from_jsonld(company: str, source_url: str, job: dict) -> dict | None:
    title = clean_text(str(job.get("title") or job.get("name") or ""))
    if not title or is_listing_title(title):
        return None

    url = normalize_url(str(job.get("url") or source_url)) or source_url
    description = clean_text(
        BeautifulSoup(str(job.get("description", "")), "html.parser").get_text(" ")
    )

    hiring = job.get("hiringOrganization")
    hiring_name = ""
    if isinstance(hiring, dict):
        hiring_name = clean_text(str(hiring.get("name", "")))

    return {
        "id": stable_id(company, url, title),
        "company": company,
        "title": title,
        "location": jsonld_location(job),
        "department": "",
        "employment_type": clean_text(str(job.get("employmentType", ""))),
        "url": url,
        "description": description,
        "date_posted": clean_text(str(job.get("datePosted", ""))),
        "valid_through": clean_text(str(job.get("validThrough", ""))),
        "source": "jsonld",
        "hiring_organization": hiring_name,
    }


def looks_like_job_detail(url: str, text: str, careers_url: str) -> bool:
    normalized = normalize_url(url)
    if not normalized:
        return False

    parsed = urlparse(normalized)
    path = parsed.path.lower().rstrip("/")
    career_path = urlparse(careers_url).path.lower().rstrip("/")

    if path == career_path:
        return False

    if any(part in path for part in ["/filter/", "/category/", "/tag/"]):
        return False
    if path.endswith("/apply") or path.endswith("/apply-now"):
        return False

    anchor = clean_text(text)
    if is_listing_title(anchor):
        return False

    if any(marker in path + "/" for marker in JOB_PATH_MARKERS):
        segments = [segment for segment in path.split("/") if segment]
        return len(segments) >= 2

    if career_path and path.startswith(career_path + "/"):
        return len(anchor) >= 4 and not is_listing_title(anchor)

    return False


def detail_links(html: str, page_url: str, careers_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    candidates: list[tuple[int, str, str]] = []
    seen: set[str] = set()

    for link in soup.find_all("a", href=True):
        url = normalize_url(urljoin(page_url, link.get("href", "")))
        text = clean_text(link.get_text(" ", strip=True))
        if not url or url in seen:
            continue
        seen.add(url)

        if not looks_like_job_detail(url, text, careers_url):
            continue

        score = 0
        path = urlparse(url).path.lower()
        if any(marker in path for marker in JOB_PATH_MARKERS):
            score += 30
        if text and 4 <= len(text) <= 120:
            score += 20
        if any(
            word in text.lower()
            for word in [
                "engineer",
                "developer",
                "designer",
                "artist",
                "manager",
                "producer",
                "analyst",
                "qa",
                "marketing",
                "hr",
                "writer",
                "lead",
                "director",
            ]
        ):
            score += 15

        candidates.append((score, url, text))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [(url, text) for _, url, text in candidates[:MAX_DETAIL_LINKS]]


def fallback_detail_job(
    company: str,
    url: str,
    html: str,
    anchor_text: str = "",
) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    title = clean_text(h1.get_text(" ", strip=True) if h1 else "") or clean_text(anchor_text)

    if is_listing_title(title) or len(title) > 180:
        return None

    page_text = clean_text(soup.get_text(" ", strip=True))
    if not any(
        signal in page_text.lower()
        for signal in [
            "apply",
            "responsibil",
            "requirements",
            "what you",
            "we offer",
            "about the role",
            "your tasks",
            "qualifications",
        ]
    ):
        return None

    location = ""
    for selector in ["[class*=location]", "[data-testid*=location]", "[class*=city]"]:
        node = soup.select_one(selector)
        if node:
            candidate = clean_text(node.get_text(" ", strip=True))
            if 1 < len(candidate) < 120:
                location = candidate
                break

    return {
        "id": stable_id(company, url, title),
        "company": company,
        "title": title,
        "location": location,
        "department": "",
        "employment_type": "",
        "url": normalize_url(url),
        "description": "",
        "date_posted": "",
        "valid_through": "",
        "source": "html-detail",
        "hiring_organization": "",
    }


def scrape_ashby(company: str, careers_url: str) -> list[dict]:
    parsed = urlparse(careers_url)
    board = parsed.path.strip("/").split("/")[0]
    if not board:
        return []

    api_url = (
        f"https://api.ashbyhq.com/posting-api/job-board/{board}"
        "?includeCompensation=true"
    )
    session = requests.Session()
    response = request(session, api_url)
    if not response:
        return []

    try:
        payload = response.json()
    except ValueError:
        return []

    jobs = []
    for item in payload.get("jobs", []):
        title = clean_text(str(item.get("title", "")))
        url = normalize_url(str(item.get("jobUrl") or item.get("applyUrl") or ""))
        if not title or not url or is_listing_title(title):
            continue
        jobs.append(
            {
                "id": stable_id(company, url, title),
                "company": company,
                "title": title,
                "location": clean_text(str(item.get("location", ""))),
                "department": clean_text(str(item.get("department", ""))),
                "employment_type": clean_text(str(item.get("employmentType", ""))),
                "url": url,
                "description": clean_text(
                    BeautifulSoup(
                        str(item.get("descriptionHtml", "")), "html.parser"
                    ).get_text(" ")
                ),
                "date_posted": clean_text(str(item.get("publishedAt", ""))),
                "valid_through": "",
                "source": "ashby-api",
                "hiring_organization": company,
            }
        )
    return jobs


def scrape_pinpoint(company: str, careers_url: str) -> list[dict]:
    parsed = urlparse(careers_url)
    if not parsed.hostname:
        return []

    postings_url = f"{parsed.scheme}://{parsed.hostname}/postings.json"
    session = requests.Session()
    response = request(session, postings_url)
    if not response:
        return []

    try:
        payload = response.json()
    except ValueError:
        return []

    jobs = []
    for item in payload.get("data", []):
        title = clean_text(str(item.get("title", "")))
        url = normalize_url(str(item.get("url", "")))
        if not title or not url or is_listing_title(title):
            continue

        location_obj = item.get("location")
        location = ""
        if isinstance(location_obj, dict):
            location = clean_text(str(location_obj.get("name", "")))

        job_obj = item.get("job")
        department = ""
        if isinstance(job_obj, dict):
            department_obj = job_obj.get("department")
            if isinstance(department_obj, dict):
                department = clean_text(str(department_obj.get("name", "")))

        description = clean_text(
            BeautifulSoup(str(item.get("description", "")), "html.parser").get_text(" ")
        )
        workplace = clean_text(str(item.get("workplace_type_text", "")))
        if workplace and workplace.lower() not in location.lower():
            location = " | ".join(part for part in [location, workplace] if part)

        jobs.append(
            {
                "id": stable_id(company, url, title),
                "company": company,
                "title": title,
                "location": location,
                "department": department,
                "employment_type": clean_text(
                    str(item.get("employment_type_text") or item.get("employment_type") or "")
                ),
                "url": url,
                "description": description,
                "date_posted": "",
                "valid_through": clean_text(str(item.get("deadline_at", ""))),
                "source": "pinpoint-json",
                "hiring_organization": company,
            }
        )

    return jobs


def scrape_generic(company: str, careers_url: str) -> list[dict]:
    session = requests.Session()
    response = request(session, careers_url)
    if not response:
        return []

    final_url = normalize_url(response.url) or careers_url
    collected: dict[str, dict] = {}

    for obj in parse_jsonld(response.text):
        job = job_from_jsonld(company, final_url, obj)
        if job:
            collected[job["id"]] = job

    links = detail_links(response.text, final_url, careers_url)

    def fetch_detail(item: tuple[str, str]):
        url, anchor = item
        local = requests.Session()
        detail = request(local, url)
        if not detail:
            return []

        found = []
        final_detail_url = normalize_url(detail.url) or url
        for obj in parse_jsonld(detail.text):
            job = job_from_jsonld(company, final_detail_url, obj)
            if job:
                found.append(job)

        if not found:
            fallback = fallback_detail_job(company, final_detail_url, detail.text, anchor)
            if fallback:
                found.append(fallback)
        return found

    if links:
        with ThreadPoolExecutor(max_workers=MAX_DETAIL_WORKERS) as executor:
            futures = [executor.submit(fetch_detail, item) for item in links]
            for future in as_completed(futures):
                try:
                    for job in future.result():
                        collected[job["id"]] = job
                except Exception:
                    continue

    if not collected:
        fallback = fallback_detail_job(company, final_url, response.text)
        if fallback and any(
            marker in urlparse(final_url).path.lower() + "/"
            for marker in JOB_PATH_MARKERS
        ):
            collected[fallback["id"]] = fallback

    return list(collected.values())


def scrape_company(source: dict) -> tuple[list[dict], dict]:
    company = clean_text(str(source.get("company", "")))
    careers_url = normalize_url(str(source.get("careers_url", "")))
    ats = clean_text(str(source.get("ats", ""))).lower()

    if not company or not careers_url:
        return [], {
            "company": company,
            "status": "skipped",
            "reason": "no-careers-url",
        }

    try:
        if ats == "ashby":
            jobs = scrape_ashby(company, careers_url)
            if not jobs:
                jobs = scrape_generic(company, careers_url)
        elif ats == "pinpoint":
            jobs = scrape_pinpoint(company, careers_url)
            if not jobs:
                jobs = scrape_generic(company, careers_url)
        else:
            jobs = scrape_generic(company, careers_url)
    except Exception as exc:
        return [], {
            "company": company,
            "status": "error",
            "reason": f"{type(exc).__name__}:{exc}",
        }

    return jobs, {
        "company": company,
        "status": "ok",
        "reason": "",
        "jobs": len(jobs),
        "careers_url": careers_url,
        "careers_status": source.get("status", ""),
        "ats": ats,
    }


def load_sources() -> list[dict]:
    payload = json.loads(CAREERS_PATH.read_text(encoding="utf-8"))
    return [
        row
        for row in payload.get("results", [])
        if row.get("careers_url") and row.get("status") in {"found", "possible"}
    ]


def main() -> None:
    sources = load_sources()
    print(f"Career sources to inspect: {len(sources)}")

    jobs_by_id: dict[str, dict] = {}
    source_reports: list[dict] = []

    with ThreadPoolExecutor(max_workers=MAX_COMPANY_WORKERS) as executor:
        futures = {executor.submit(scrape_company, source): source for source in sources}
        for future in as_completed(futures):
            source = futures[future]
            company = source.get("company", "")
            try:
                jobs, report = future.result()
            except Exception as exc:
                jobs = []
                report = {
                    "company": company,
                    "status": "error",
                    "reason": f"{type(exc).__name__}:{exc}",
                }

            for job in jobs:
                jobs_by_id[job["id"]] = job
            source_reports.append(report)
            print(f"{company:35} {report.get('status'):8} jobs={len(jobs)}")

    jobs = sorted(
        jobs_by_id.values(),
        key=lambda row: (
            row.get("company", "").lower(),
            row.get("title", "").lower(),
        ),
    )

    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "generated_at": now,
        "source_count": len(sources),
        "job_count": len(jobs),
        "companies_with_jobs": len({job["company"] for job in jobs}),
        "sources": sorted(
            source_reports,
            key=lambda row: row.get("company", "").lower(),
        ),
        "jobs": jobs,
    }
    OUTPUT_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    fields = [
        "id",
        "company",
        "title",
        "location",
        "department",
        "employment_type",
        "url",
        "date_posted",
        "valid_through",
        "source",
        "hiring_organization",
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            writer.writerow({field: job.get(field, "") for field in fields})

    print(
        f"Collected {len(jobs)} jobs from "
        f"{payload['companies_with_jobs']} companies"
    )
    print(f"Wrote {OUTPUT_JSON} and {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
