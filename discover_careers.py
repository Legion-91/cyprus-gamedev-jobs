from __future__ import annotations

import csv
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

INPUT_PATH = Path("data/studios.json")
OUTPUT_JSON = Path("data/careers.json")
OUTPUT_CSV = Path("data/careers.csv")
MAX_WORKERS = 8
TIMEOUT = 12

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; CyprusGamedevJobs/0.2; "
        "+https://github.com/Legion-91/cyprus-gamedev-jobs)"
    )
}

CAREER_WORDS = {
    "careers": 35,
    "career": 30,
    "vacancies": 35,
    "vacancy": 30,
    "open positions": 35,
    "open roles": 35,
    "openings": 25,
    "jobs": 30,
    "job": 18,
    "join us": 30,
    "join our team": 35,
    "work with us": 30,
    "hiring": 20,
    "вакансии": 35,
    "вакансия": 30,
}

PAGE_SIGNALS = [
    "open positions",
    "open roles",
    "current vacancies",
    "job openings",
    "available positions",
    "join our team",
    "we are hiring",
    "we're hiring",
    "apply now",
    "view jobs",
    "view vacancies",
    "career opportunities",
    "вакансии",
]

COMMON_PATHS = [
    "/careers",
    "/jobs",
    "/vacancies",
    "/career",
    "/join-us",
    "/work-with-us",
]

ATS_DOMAINS = {
    "greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "jobs.lever.co": "lever",
    "ashbyhq.com": "ashby",
    "jobs.ashbyhq.com": "ashby",
    "recruitee.com": "recruitee",
    "smartrecruiters.com": "smartrecruiters",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "teamtailor.com": "teamtailor",
    "workable.com": "workable",
    "apply.workable.com": "workable",
    "bamboohr.com": "bamboohr",
    "personio.com": "personio",
    "personio.de": "personio",
    "comeet.com": "comeet",
    "workdayjobs.com": "workday",
    "myworkdayjobs.com": "workday",
}


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_url(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    parsed = parsed._replace(fragment="")
    return urlunparse(parsed)


def host(url: str) -> str:
    value = (urlparse(url).hostname or "").lower()
    if value.startswith("www."):
        value = value[4:]
    return value


def detect_ats(url: str) -> str | None:
    hostname = host(url)
    for suffix, ats in ATS_DOMAINS.items():
        if hostname == suffix or hostname.endswith("." + suffix):
            return ats
    return None


def is_same_site(a: str, b: str) -> bool:
    ha = host(a)
    hb = host(b)
    return bool(ha and hb and (ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)))


def career_score(url: str, anchor: str) -> int:
    value = f"{url} {anchor}".lower().replace("-", " ").replace("_", " ")
    score = 0
    for word, points in CAREER_WORDS.items():
        if word in value:
            score += points

    ats = detect_ats(url)
    if ats:
        score += 45

    lowered = url.lower()
    if any(bad in lowered for bad in ["privacy", "cookie", "blog", "news", "press", "contact"]):
        score -= 30
    return score


def page_signal_score(html: str, final_url: str) -> tuple[int, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    text = clean_text(soup.get_text(" ", strip=True)).lower()
    title = clean_text(soup.title.get_text(" ", strip=True) if soup.title else "").lower()
    combined = f"{title} {text[:100000]}"

    evidence = []
    score = 0

    for signal in PAGE_SIGNALS:
        if signal in combined:
            score += 12
            evidence.append(f"page:{signal}")

    if re.search(r'"@type"\s*:\s*"JobPosting"', html, flags=re.IGNORECASE):
        score += 45
        evidence.append("schema:JobPosting")

    url_value = final_url.lower().replace("-", " ").replace("_", " ")
    if any(word in url_value for word in ["career", "jobs", "vacan", "join us", "work with us"]):
        score += 20
        evidence.append("career-like-url")

    ats = detect_ats(final_url)
    if ats:
        score += 45
        evidence.append(f"ats:{ats}")

    return score, evidence


def request(session: requests.Session, url: str) -> requests.Response | None:
    try:
        response = session.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if response.status_code >= 400:
            return None
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type and "xml" not in content_type and "text/plain" not in content_type:
            return None
        return response
    except requests.RequestException:
        return None


def homepage_candidates(html: str, base_url: str) -> list[tuple[int, str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    candidates = []

    for link in soup.find_all("a", href=True):
        href = normalize_url(urljoin(base_url, link.get("href", "")))
        if not href or href in seen:
            continue
        seen.add(href)

        anchor = clean_text(link.get_text(" ", strip=True))
        score = career_score(href, anchor)
        if score >= 25:
            candidates.append((score, href, anchor))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[:10]


def sitemap_candidates(session: requests.Session, website: str) -> list[str]:
    sitemap_url = urljoin(website.rstrip("/") + "/", "sitemap.xml")
    response = request(session, sitemap_url)
    if not response:
        return []

    soup = BeautifulSoup(response.text, "xml")
    urls = []
    for loc in soup.find_all("loc"):
        candidate = normalize_url(clean_text(loc.get_text()))
        if candidate and career_score(candidate, "") >= 25:
            urls.append(candidate)

    return urls[:10]


def verify_candidate(session: requests.Session, candidate_url: str, base_score: int, reason: str) -> dict | None:
    response = request(session, candidate_url)
    if not response:
        return None

    final_url = normalize_url(response.url)
    page_score, evidence = page_signal_score(response.text, final_url)
    total = min(100, base_score + page_score)

    if total < 45:
        return None

    ats = detect_ats(final_url)
    return {
        "careers_url": final_url,
        "status": "found" if total >= 65 else "possible",
        "source_type": "ats" if ats else "custom",
        "ats": ats or "",
        "confidence": total,
        "evidence": [reason] + evidence,
    }


def discover_for_website(website: str) -> dict:
    session = requests.Session()
    website = normalize_url(website)

    if not website:
        return {
            "careers_url": "",
            "status": "no_website",
            "source_type": "",
            "ats": "",
            "confidence": 0,
            "evidence": [],
        }

    homepage = request(session, website)
    if not homepage:
        return {
            "careers_url": "",
            "status": "unreachable",
            "source_type": "",
            "ats": "",
            "confidence": 0,
            "evidence": ["homepage-unreachable"],
        }

    final_home = normalize_url(homepage.url)
    candidates = homepage_candidates(homepage.text, final_home)

    for score, candidate, anchor in candidates:
        reason = f"homepage-link:{anchor[:80]}" if anchor else "homepage-link"
        result = verify_candidate(session, candidate, score, reason)
        if result:
            return result

    base = final_home.rstrip("/") + "/"
    for path in COMMON_PATHS:
        candidate = urljoin(base, path.lstrip("/"))
        result = verify_candidate(session, candidate, 35, f"common-path:{path}")
        if result:
            return result

    for candidate in sitemap_candidates(session, final_home):
        result = verify_candidate(session, candidate, 35, "sitemap")
        if result:
            return result

    homepage_page_score, homepage_evidence = page_signal_score(homepage.text, final_home)
    if homepage_page_score >= 45:
        ats = detect_ats(final_home)
        return {
            "careers_url": final_home,
            "status": "possible",
            "source_type": "ats" if ats else "custom",
            "ats": ats or "",
            "confidence": min(100, homepage_page_score),
            "evidence": ["homepage-itself"] + homepage_evidence,
        }

    return {
        "careers_url": "",
        "status": "not_found",
        "source_type": "",
        "ats": "",
        "confidence": 0,
        "evidence": [],
    }


def load_studios() -> list[dict]:
    payload = json.loads(INPUT_PATH.read_text(encoding="utf-8"))
    return payload["studios"]


def main() -> None:
    studios = load_studios()

    websites = []
    seen = set()
    for studio in studios:
        website = normalize_url(str(studio.get("website", "")))
        key = host(website) or website
        if website and key not in seen:
            seen.add(key)
            websites.append(website)

    print(f"Studios: {len(studios)}")
    print(f"Unique websites to inspect: {len(websites)}")

    by_domain = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(discover_for_website, website): website for website in websites}
        for future in as_completed(futures):
            website = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "careers_url": "",
                    "status": "error",
                    "source_type": "",
                    "ats": "",
                    "confidence": 0,
                    "evidence": [f"error:{type(exc).__name__}"],
                }
            by_domain[host(website)] = result
            print(f"{host(website):35} {result['status']:12} {result['careers_url']}")

    rows = []
    for studio in studios:
        result = by_domain.get(host(str(studio.get("website", ""))), {})
        rows.append(
            {
                "company": studio.get("company", ""),
                "type": studio.get("type", ""),
                "city": studio.get("city", ""),
                "website": studio.get("website", ""),
                "domain": studio.get("domain", ""),
                "careers_url": result.get("careers_url", ""),
                "status": result.get("status", "not_checked"),
                "source_type": result.get("source_type", ""),
                "ats": result.get("ats", ""),
                "confidence": result.get("confidence", 0),
                "evidence": result.get("evidence", []),
            }
        )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "studio_count": len(rows),
        "unique_websites_checked": len(websites),
        "found": sum(row["status"] == "found" for row in rows),
        "possible": sum(row["status"] == "possible" for row in rows),
        "not_found": sum(row["status"] == "not_found" for row in rows),
        "unreachable": sum(row["status"] == "unreachable" for row in rows),
        "results": rows,
    }

    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    csv_fields = [
        "company",
        "type",
        "city",
        "website",
        "domain",
        "careers_url",
        "status",
        "source_type",
        "ats",
        "confidence",
        "evidence",
    ]
    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            csv_row["evidence"] = " | ".join(row["evidence"])
            writer.writerow(csv_row)

    print(
        "Summary: "
        f"found={payload['found']}, possible={payload['possible']}, "
        f"not_found={payload['not_found']}, unreachable={payload['unreachable']}"
    )
    print(f"Wrote {OUTPUT_JSON} and {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
