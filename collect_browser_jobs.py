from __future__ import annotations

import csv
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

SOURCES = Path("data/job_sources.csv")
JOBS = Path("data/jobs.csv")

TARGET_COMPANIES = {
    "Amrita Studio", "Artstorm", "Awem Games", "Burny Games", "Critical Reflex",
    "Guli Games", "HolyDay Studios", "HypeTrain Digital", "Mundfish", "Murka Games",
    "Nexters (GDEV)", "Obelisk Studio", "Owlcat Games", "Playrix",
    "ReactGames Studio", "RJ Games (Nexters)", "Sunday Games", "Tamasenco",
    "Team Clout", "TrueMyth Games",
}

INLINE_COMPANIES = {
    "HypeTrain Digital", "ReactGames Studio", "Sunday Games", "Tamasenco"
}

JOB_COLUMNS = [
    "company_id", "company", "job_id", "title", "location", "url",
    "description", "first_seen", "last_seen", "active",
]

BAD_TITLES = {
    "jobs", "job", "careers", "career", "vacancies", "vacancy", "open positions",
    "all jobs", "apply", "apply now", "read more", "learn more", "join us",
    "join our team", "work with us", "our vacancies", "current openings",
}

ROLE_WORDS = (
    "designer", "manager", "qa", "engineer", "developer", "artist", "producer",
    "sound", "analyst", "marketing", "animator", "writer", "programmer",
    "director", "lead", "specialist", "recruiter", "product", "project",
    "community", "support", "backend", "frontend", "devops", "unity", "unreal",
    "game", "2d", "3d", "vfx", "technical", "accounting", "administrator",
)


def clean(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical(url):
    if not url:
        return ""
    p = urlparse(url)
    if p.scheme not in {"http", "https"}:
        return ""
    return p._replace(fragment="").geturl()


def stable_id(company_id, url, title=""):
    raw = canonical(url) + "|" + clean(title).lower()
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]
    return f"{company_id}:{digest}"


def load_sources():
    with SOURCES.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows if r.get("company") in TARGET_COMPANIES and r.get("status") == "found"]


def load_jobs():
    if not JOBS.exists() or not JOBS.stat().st_size:
        return {}
    with JOBS.open(encoding="utf-8", newline="") as f:
        return {r["job_id"]: r for r in csv.DictReader(f) if r.get("job_id")}


def save_jobs(rows):
    with JOBS.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOB_COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def is_role_text(text):
    t = clean(text).lower()
    if not t or t in BAD_TITLES or len(t) < 3 or len(t) > 180:
        return False
    return any(word in t for word in ROLE_WORDS)


def same_host(a, b):
    return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()


def candidate(company, base_url, href, text):
    url = canonical(urljoin(base_url, href or ""))
    if not url:
        return ""
    p = urlparse(url)
    path = p.path.rstrip("/")
    text_l = clean(text).lower()

    if company == "Critical Reflex":
        return url if p.netloc == "criticalreflex.bamboohr.com" and path.startswith("/careers/") else ""
    if company == "Amrita Studio":
        return url if p.netloc.endswith("amrita.studio") and path.startswith("/career/") else ""
    if company == "Mundfish":
        return url if path.startswith("/en/careers/") and path not in {"/en/careers/projects"} else ""
    if company == "Artstorm":
        return url if "/en-US/careers/" in path and path != "/en-US/careers" else ""
    if company == "Burny Games":
        return url if "/jobs/" in path and not path.endswith("/new") else ""
    if company == "Nexters (GDEV)":
        return url if "vacancy=" in p.query else ""
    if company == "RJ Games (Nexters)":
        return url if ("/vacs/" in path or "/vac" in path) and path != "/vacs" else ""
    if company == "Playrix":
        return url if ("/job/" in path or "/jobs/" in path) and path not in {"/job/open", "/job"} else ""
    if company == "Owlcat Games":
        return url if "/careers/" in path and path != "/careers" else ""
    if company == "Obelisk Studio":
        return url if same_host(base_url, url) and ("career" in path.lower() or "job" in path.lower() or "vac" in path.lower()) and path != urlparse(base_url).path.rstrip("/") else ""
    if company == "Murka Games":
        return url if same_host(base_url, url) and ("career" in path.lower() or "job" in path.lower() or "vac" in path.lower()) and path not in {"", "/"} else ""
    if company == "Awem Games":
        return url if same_host(base_url, url) and ("career" in path.lower() or "job" in path.lower() or "vac" in path.lower()) and path != urlparse(base_url).path.rstrip("/") else ""
    if company in {"Guli Games", "HolyDay Studios", "TrueMyth Games", "Team Clout"}:
        if same_host(base_url, url) and ("job" in path.lower() or "career" in path.lower() or "vac" in path.lower()):
            return url
    if is_role_text(text_l) and same_host(base_url, url):
        return url
    return ""


def title_from_page(soup, fallback):
    for selector in ("h1", "[class*=job-title]", "[class*=vacancy-title]", "[class*=position-title]", "[class*=title]"):
        node = soup.select_one(selector)
        if node:
            t = clean(node.get_text(" ", strip=True))
            if t and t.lower() not in BAD_TITLES and len(t) <= 180:
                return t
    t = clean(fallback)
    return t if t.lower() not in BAD_TITLES and len(t) <= 180 else ""


def location_from_page(soup, text):
    for selector in ("[class*=location]", "[class*=city]", "[class*=geo]", "[data-testid*=location]"):
        node = soup.select_one(selector)
        if node:
            v = clean(node.get_text(" ", strip=True))
            if 1 < len(v) < 140:
                return v
    m = re.search(r"\b(Remote|Cyprus|Limassol|Nicosia|Yerevan|Abu Dhabi|Ukraine|EU|Europe|Worldwide)\b[^|]{0,70}", text, re.I)
    return clean(m.group(0)) if m else ""


def inline_rows(company_id, company, source_url, soup):
    rows = []
    seen_titles = set()
    selectors = "h1,h2,h3,h4,[class*=title],[class*=position],[class*=vacancy],[class*=job]"
    for node in soup.select(selectors):
        title = clean(node.get_text(" ", strip=True))
        if not is_role_text(title):
            continue
        key = title.lower()
        if key in seen_titles:
            continue
        seen_titles.add(key)

        container = node
        for _ in range(4):
            parent = container.parent
            if not parent:
                break
            parent_text = clean(parent.get_text(" ", strip=True))
            if 80 <= len(parent_text) <= 5000:
                container = parent
                break
            container = parent

        body = clean(container.get_text(" ", strip=True))
        if len(body) < 40:
            continue
        rows.append({
            "company_id": company_id,
            "company": company,
            "job_id": stable_id(company_id, source_url, title),
            "title": title,
            "location": location_from_page(container, body),
            "url": canonical(source_url),
            "description": body[:12000],
        })
    return rows


def collect_company(page, source):
    company = source["company"]
    company_id = source["company_id"]
    source_url = source["source_url"]

    page.goto(source_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)

    soup = BeautifulSoup(page.content(), "html.parser")
    rows = []

    if company in INLINE_COMPANIES:
        rows.extend(inline_rows(company_id, company, source_url, soup))

    anchors = page.locator("a").evaluate_all(
        "els => els.map(a => ({href:a.href, text:(a.innerText||a.textContent||'').trim()}))"
    )
    links = {}
    for a in anchors:
        url = candidate(company, source_url, a.get("href"), a.get("text"))
        if url:
            links[url] = clean(a.get("text"))

    html = page.content()
    if company == "Nexters (GDEV)":
        for m in re.findall(r"[?&]vacancy=([a-z0-9\-]+)", html, re.I):
            links[source_url.split("?")[0] + "?vacancy=" + m] = ""
    if company == "Critical Reflex":
        for m in re.findall(r"https://criticalreflex\.bamboohr\.com/careers/(\d+)", html):
            links[f"https://criticalreflex.bamboohr.com/careers/{m}"] = ""

    for url, anchor_text in list(links.items())[:120]:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(900)
            detail_soup = BeautifulSoup(page.content(), "html.parser")
            text = clean(detail_soup.get_text(" ", strip=True))
            if not text:
                continue
            if company == "Burny Games" and "hidden from public view" in text.lower():
                continue
            title = title_from_page(detail_soup, anchor_text)
            if not title or title.lower() in BAD_TITLES:
                continue
            if not is_role_text(title) and company not in {"Critical Reflex", "Mundfish"}:
                continue
            rows.append({
                "company_id": company_id,
                "company": company,
                "job_id": stable_id(company_id, canonical(page.url), title),
                "title": title,
                "location": location_from_page(detail_soup, text),
                "url": canonical(page.url),
                "description": text[:12000],
            })
        except Exception as exc:
            print(f"DETAIL FAIL {company}: {url}: {type(exc).__name__}: {exc}")

    dedup = {}
    for row in rows:
        if row["title"].lower() in BAD_TITLES:
            continue
        dedup[row["job_id"]] = row
    return list(dedup.values())


def main():
    existing = load_jobs()
    timestamp = now_iso()
    successful = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140 Safari/537.36",
            viewport={"width": 1440, "height": 1200},
        )
        page = context.new_page()

        for source in load_sources():
            company = source["company"]
            try:
                rows = collect_company(page, source)
                if rows:
                    successful[source["company_id"]] = rows
                    print(f"BROWSER OK   {company:24} jobs={len(rows)}")
                else:
                    print(f"BROWSER SKIP {company:24} no jobs extracted")
            except Exception as exc:
                print(f"BROWSER FAIL {company:24} {type(exc).__name__}: {exc}")

        context.close()
        browser.close()

    for company_id in successful:
        for row in existing.values():
            if row.get("company_id") == company_id:
                row["active"] = "false"

    for company_id, rows in successful.items():
        for row in rows:
            old = existing.get(row["job_id"])
            row["first_seen"] = old.get("first_seen") if old and old.get("first_seen") else timestamp
            row["last_seen"] = timestamp
            row["active"] = "true"
            existing[row["job_id"]] = row

    output = sorted(
        existing.values(),
        key=lambda r: (r.get("active") != "true", r.get("company", "").lower(), r.get("title", "").lower()),
    )
    save_jobs(output)
    print(f"Browser collector updated {sum(len(v) for v in successful.values())} active rows")


if __name__ == "__main__":
    main()
