from __future__ import annotations

import csv
import re
from pathlib import Path

SEED = Path("data/studios.csv")
COMPANIES = Path("data/companies.csv")
SOURCES = Path("data/job_sources.csv")
JOBS = Path("data/jobs.csv")


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "company"


def merge_values(old: str, new: str) -> str:
    values = [item.strip() for item in old.split("|") if item.strip()]
    if new.strip() and new.strip() not in values:
        values.append(new.strip())
    return " | ".join(values)


def load_companies() -> list[dict[str, str]]:
    by_name: dict[str, dict[str, str]] = {}

    with SEED.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            name = row["company"].strip()
            key = name.casefold()

            if key not in by_name:
                by_name[key] = {
                    "company_id": slugify(name),
                    "company": name,
                    "website": row["website"].strip(),
                    "domain": row["domain"].strip(),
                    "type": row["type"].strip(),
                    "city": row["city"].strip(),
                    "state": row["state"].strip(),
                    "country": row["country"].strip(),
                }
            else:
                company = by_name[key]
                company["city"] = merge_values(company["city"], row["city"])
                company["state"] = merge_values(company["state"], row["state"])

    companies = sorted(by_name.values(), key=lambda row: row["company"].casefold())

    used: dict[str, int] = {}
    for company in companies:
        base = company["company_id"]
        used[base] = used.get(base, 0) + 1
        if used[base] > 1:
            company["company_id"] = f"{base}-{used[base]}"

    return companies


def write_companies(companies: list[dict[str, str]]) -> None:
    fields = [
        "company_id",
        "company",
        "website",
        "domain",
        "type",
        "city",
        "state",
        "country",
    ]
    with COMPANIES.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(companies)


def load_existing_sources() -> dict[str, dict[str, str]]:
    if not SOURCES.exists():
        return {}

    with SOURCES.open(encoding="utf-8", newline="") as handle:
        return {
            row["company_id"]: row
            for row in csv.DictReader(handle)
            if row.get("company_id")
        }


def write_sources(companies: list[dict[str, str]]) -> None:
    fields = [
        "company_id",
        "company",
        "status",
        "source_url",
        "method",
        "format",
        "notes",
    ]
    existing = load_existing_sources()

    with SOURCES.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()

        for company in companies:
            saved = existing.get(company["company_id"], {})
            writer.writerow(
                {
                    "company_id": company["company_id"],
                    "company": company["company"],
                    "status": saved.get("status", "not_checked"),
                    "source_url": saved.get("source_url", ""),
                    "method": saved.get("method", ""),
                    "format": saved.get("format", ""),
                    "notes": saved.get("notes", ""),
                }
            )


def ensure_jobs() -> None:
    if JOBS.exists():
        return

    fields = [
        "company_id",
        "company",
        "source_job_id",
        "title",
        "location",
        "url",
    ]
    with JOBS.open("w", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=fields).writeheader()


def main() -> None:
    companies = load_companies()
    write_companies(companies)
    write_sources(companies)
    ensure_jobs()
    print(f"Companies: {len(companies)}")
    print(f"Wrote {COMPANIES}")
    print(f"Wrote {SOURCES}")
    print(f"Ready {JOBS}")


if __name__ == "__main__":
    main()
