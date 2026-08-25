from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

JOBS = Path("data/jobs.csv")
TEMPLATE = Path("site/index.html")
PUBLIC = Path("docs")


def main() -> None:
    PUBLIC.mkdir(exist_ok=True)
    shutil.copyfile(TEMPLATE, PUBLIC / "index.html")

    rows = []
    if JOBS.exists() and JOBS.stat().st_size:
        with JOBS.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row.get("active", "true").lower() != "true":
                    continue
                if row.get("company") == "Pixonic":
                    continue
                rows.append(
                    {
                        "company": row.get("company", ""),
                        "title": row.get("title", ""),
                        "location": row.get("location", ""),
                        "url": row.get("url", ""),
                        "first_seen": row.get("first_seen", ""),
                        "last_seen": row.get("last_seen", ""),
                    }
                )

    rows.sort(key=lambda row: (row["first_seen"], row["company"], row["title"]), reverse=True)
    (PUBLIC / "jobs.json").write_text(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    (PUBLIC / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Built mobile site with {len(rows)} active vacancies")


if __name__ == "__main__":
    main()
