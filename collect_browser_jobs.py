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
    "Artstorm",
    "Burny Games",
    "Mundfish",
    "Nexters (GDEV)",
    "Owlcat Games",
}

JOB_COLUMNS = [
    "company_id", "company", "job_id", "title", "location", "url",
    "description", "first_seen", "last_seen", "active",
]

BAD_TITLES = {
    "jobs", "job", "careers", "career", "vacancies", "vacancy",
    "open positions", "all jobs", "apply", "apply now", "read more",
}


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


def stable_id(company_id, url):
    digest = hashlib.sha1(canonical(url).encode("utf-8")).hexdigest()[:20]
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


def candidate(company, base_url, href, text):
    url = canonical(urljoin(base_url, href or ""))
    if not url:
        return ""
    p = urlparse(url)
    path = p.path.rstrip("/")
    text = clean(text).lower()

    if company == "Mundfish":
        return url if path.startswith("/en/careers/") and path not in {"/en/careers/projects"} else ""
    if company == "Artstorm":
        return url if "/en-US/careers/" in path and path != "/en-US/careers" else ""
    if company == "Burny Games":
        return url if "/jobs/" in path and not path.endswith("/new") else ""
    if company == "Nexters (GDEV)":
        return url if "vacancy=" in p.query else ""
    if company == "Owlcat Games":
        if "/careers/" in path and path != "/careers":
            return url
        if any(k in text for k in ("designer", "manager", "qa", "engineer", "developer", "artist", "producer", "sound")):
            return url if urlparse(url).netloc.endswith("owlcat.games") else ""
    return ""


def title_from_page(soup, fallback):
    for selector in ("h1", "[class*=title]", "[class*=position]"):
        node = soup.select_one(selector)
        if node:
            t = clean(node.get_text(" ", strip=True))
            if t and t.lower() not in BAD_TITLES and len(t) <= 180:
                return t
    t = clean(fallback)
    return t if t.lower() not in BAD_TITLES else ""


def location_from_page(soup, text):
    for selector in ("[class*=location]", "[class*=city]", "[class*=geo]", "[data-testid*=location]"):
        node = soup.select_one(selector)
        if node:
            v = clean(node.get_text(" ", strip=True))
            if 1 < len(v) < 140:
                return v
    m = re.search(r"\b(Remote|Cyprus|Limassol|Nicosia|Yerevan|Abu Dhabi|Ukraine|EU \+ Non EU)\b[^\n]{0,80}", text, re.I)
    return clean(m.group(0)) if m else ""


def collect_company(page, source):
    company = source["company"]
    company_id = source["company_id"]
    source_url = source["source_url"]

    page.goto(source_url, wait_until="domcontentloaded", timeout=45000)
    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2500)

    anchors = page.locator("a").evaluate_all(
        "els => els.map(a => ({href:a.href, text:(a.innerText||a.textContent||'').trim()}))"
    )

    links = {}
    for a in anchors:
        url = candidate(company, source_url, a.get("href"), a.get("text"))
        if url:
            links[url] = clean(a.get("text"))

    # Nexters sometimes renders vacancy URLs only after page scripts settle.
    if company == "Nexters (GDEV)":
        html = page.content()
        for m in re.findall(r"[?&]vacancy=([a-z0-9\-]+)", html, re.I):
            url = source_url.split("?")[0] + "?vacancy=" + m
            links[url] = ""

    rows = []
    for url, anchor_text in list(links.items())[:100]:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(700)
            soup = BeautifulSoup(page.content(), "html.parser")
            text = clean(soup.get_text(" ", strip=True))
            if not text:
                continue
            if company == "Burny Games" and "hidden from public view" in text.lower():
                continue
            title = title_from_page(soup, anchor_text)
            if not title:
                continue
            # Avoid obvious listing/navigation false positives.
            if title.lower() in BAD_TITLES or len(title) > 180:
                continue
            rows.append({
                "company_id": company_id,
                "company": company,
                "job_id": stable_id(company_id, url),
                "title": title,
                "location": location_from_page(soup, text),
                "url": canonical(page.url),
                "description": text[:12000],
            })
        except Exception as exc:
            print(f"DETAIL FAIL {company}: {url}: {type(exc).__name__}: {exc}")

    # Deduplicate by canonical URL.
    dedup = {}
    for row in rows:
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

    # Only deactivate an old company's rows after a non-empty successful browser result.
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
