#!/usr/bin/env python3
"""
08_jstage_collect.py

Scrape peer review dates from J-STAGE article pages (Japanese journals).

J-STAGE article pages contain dates in spans with class "accodion_lic":
    Received: August 16, 2001
    Accepted: February 06, 2002
    Revised: -
    Published: 2002

Strategy per journal:
  1. Fetch DOIs from Crossref bulk ISSN endpoint
  2. Resolve DOI to J-STAGE article URL
  3. Scrape article page for received/accepted/revised dates
  4. Stop when 100 articles with dates, OR no more DOIs

Usage:
    python scripts/08_jstage_collect.py [--resume]
"""

import argparse
import csv
import json
import re
import time
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CROSSREF_BASE = "https://api.crossref.org"
EMAIL = "reviewtimedb@rapidpeer.com"
TARGET_WITH_DATES = 100
MAX_DOIS_CHECKED = 500
PAGE_SIZE = 50
OUTPUT_FILE = Path("data/jstage_articles.csv")
CHECKPOINT_FILE = Path("data/jstage_checkpoint.json")
DEFAULT_JOURNAL_LIST = Path("data/journal_list_full.csv")

# Rate limiting
HTTP_DELAY = 1.0  # Be polite to J-STAGE
CROSSREF_DELAY = 0.1

# Max delta thresholds
MAX_REVIEW_DAYS = 365 * 3   # Some humanities journals are very slow
MAX_PUB_DAYS = 365

CSV_FIELDS = [
    "journal_name", "issn", "field", "doi", "title",
    "received_date", "accepted_date", "revised_date", "published_date",
    "days_submission_to_acceptance", "days_acceptance_to_publication",
    "days_total", "data_source",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": f"ReviewTimeDB/1.0 (mailto:{EMAIL}; academic research)",
})


def load_checkpoint():
    if CHECKPOINT_FILE.exists():
        return json.loads(CHECKPOINT_FILE.read_text())
    return {}


def save_checkpoint(cp):
    CHECKPOINT_FILE.write_text(json.dumps(cp, indent=1))


def parse_date(date_str):
    """Parse various date formats into a datetime object."""
    if not date_str or date_str.strip() == "-":
        return None
    date_str = date_str.strip()
    for fmt in [
        "%B %d, %Y",      # August 16, 2001
        "%b %d, %Y",      # Aug 16, 2001
        "%d %B %Y",        # 16 August 2001
        "%d %b %Y",        # 16 Aug 2001
        "%Y-%m-%d",        # 2001-08-16
        "%Y/%m/%d",        # 2001/08/16
        "%Y",              # 2001 (year only)
    ]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def fmt_date(dt):
    return dt.strftime("%Y-%m-%d") if dt else ""


def compute_deltas(dates):
    received = dates.get("received")
    accepted = dates.get("accepted")
    published = dates.get("published")

    sub_to_acc = None
    acc_to_pub = None
    total = None

    if received and accepted:
        delta = (accepted - received).days
        if 0 <= delta <= MAX_REVIEW_DAYS:
            sub_to_acc = delta

    if accepted and published:
        delta = (published - accepted).days
        if 0 <= delta <= MAX_PUB_DAYS:
            acc_to_pub = delta

    if received and published:
        delta = (published - received).days
        if 0 <= delta <= (MAX_REVIEW_DAYS + MAX_PUB_DAYS):
            total = delta

    return {
        "days_submission_to_acceptance": sub_to_acc,
        "days_acceptance_to_publication": acc_to_pub,
        "days_total": total,
    }


def fetch_doi_page(issn, offset=0, rows=PAGE_SIZE):
    """Fetch a page of DOIs from Crossref for a journal ISSN."""
    url = f"{CROSSREF_BASE}/journals/{issn}/works"
    params = {
        "rows": rows,
        "offset": offset,
        "select": "DOI,title",
        "sort": "published",
        "order": "desc",
        "mailto": EMAIL,
    }
    try:
        r = SESSION.get(url, params=params, timeout=30)
        if r.status_code != 200:
            return [], 0
        data = r.json()["message"]
        items = data.get("items", [])
        total = data.get("total-results", 0)
        dois = []
        for item in items:
            doi = item.get("DOI")
            title = (item.get("title") or [""])[0]
            if doi:
                dois.append((doi, title))
        return dois, total
    except Exception as e:
        print(f"    Crossref error: {e}")
        return [], 0


def scrape_jstage_dates(doi):
    """Resolve DOI to J-STAGE page and extract review dates."""
    try:
        # Resolve DOI to get actual URL
        r = SESSION.head(f"https://doi.org/{doi}", allow_redirects=True, timeout=15)
        url = r.url

        if "jstage.jst.go.jp" not in url:
            return None

        # Make sure we're on the English version
        url = url.replace("-char/ja/", "-char/en/")
        if "-char/en" not in url:
            url = url.rstrip("/") + "/-char/en/"

        # Fetch article page
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "lxml")

        # Find date spans
        dates = {}
        spans = soup.select("span.accodion_lic")
        for span in spans:
            text = span.get_text(strip=True)
            if ":" not in text:
                continue
            label, _, value = text.partition(":")
            label = label.strip().lower()
            value = value.strip()

            if value == "-" or not value:
                continue

            dt = parse_date(value)
            if dt:
                if "received" in label:
                    dates["received"] = dt
                elif "accepted" in label:
                    dates["accepted"] = dt
                elif "revised" in label:
                    dates["revised"] = dt
                elif "published" in label:
                    dates["published"] = dt

        if dates.get("received") or dates.get("accepted"):
            return dates
        return None

    except Exception as e:
        return None


def get_jstage_journals(journal_list_path):
    """Get journals that are likely on J-STAGE from the journal list."""
    jl = pd.read_csv(journal_list_path)
    jl["publisher"] = jl["publisher"].fillna("")

    # Japanese publishers
    jp_keywords = [
        "japan", "japanese", "nihon", "nippon", "jstage",
        "tokyo", "osaka", "kyoto", "hokkaido",
    ]
    mask = jl["publisher"].apply(
        lambda p: any(k in str(p).lower() for k in jp_keywords)
    )
    jp_journals = jl[mask].copy()
    return jp_journals


def load_existing_dois():
    """Load DOIs from all existing data sources to avoid duplicates."""
    existing = set()
    for csv_path in [
        Path("data/crossref_articles.csv"),
        Path("data/crossref_articles_v2.csv"),
        Path("data/pubmed_articles.csv"),
        Path("data/pubmed_articles_v2.csv"),
        Path("data/scraped_articles_t1.csv"),
        Path("data/scraped_articles_t2.csv"),
        Path("data/frontiers_articles.csv"),
        OUTPUT_FILE,
    ]:
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path, usecols=["doi"], dtype=str)
                existing.update(df["doi"].dropna())
            except Exception:
                pass
    return existing


def main():
    parser = argparse.ArgumentParser(description="Scrape J-STAGE for review dates")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--journal-list", default=str(DEFAULT_JOURNAL_LIST))
    args = parser.parse_args()

    print("J-STAGE Review Date Collector")
    print("=" * 50)

    # Get J-STAGE journals
    journals = get_jstage_journals(args.journal_list)
    print(f"Japanese journals in list: {len(journals)}")

    # Load checkpoint
    checkpoint = load_checkpoint() if args.resume else {}
    if checkpoint:
        print(f"Resuming: {len(checkpoint)} journals in checkpoint")

    # Load existing DOIs
    existing_dois = load_existing_dois()
    print(f"Existing DOIs across all sources: {len(existing_dois)}")

    # Set up output CSV
    write_header = not OUTPUT_FILE.exists()
    csv_file = open(OUTPUT_FILE, "a", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if write_header:
        writer.writeheader()

    total_new = 0
    total_with_dates = 0

    try:
        for idx, (_, row) in enumerate(journals.iterrows()):
            issn = str(row["issn"]).strip()
            journal_name = row["journal_name"]
            field = row["field"]
            publisher = str(row["publisher"])

            # Check checkpoint
            cp = checkpoint.get(issn, {})
            if cp.get("done", False):
                continue

            n_with_dates = cp.get("n_with_dates", 0)
            n_checked = cp.get("n_checked", 0)
            needed = TARGET_WITH_DATES - n_with_dates

            print(f"\n[{idx+1}/{len(journals)}] {journal_name}")
            print(f"    ISSN: {issn}, pub: {publisher[:40]}")
            print(f"    need={needed}, checked={n_checked}")

            # Fetch DOIs from Crossref
            offset = n_checked
            journal_done = False

            while not journal_done and n_checked < MAX_DOIS_CHECKED:
                dois, total_available = fetch_doi_page(issn, offset=offset)
                time.sleep(CROSSREF_DELAY)

                if not dois:
                    journal_done = True
                    break

                for doi, title in dois:
                    if n_checked >= MAX_DOIS_CHECKED:
                        journal_done = True
                        break
                    if n_with_dates >= TARGET_WITH_DATES:
                        journal_done = True
                        break

                    n_checked += 1

                    if doi in existing_dois:
                        continue

                    # Scrape J-STAGE page
                    dates = scrape_jstage_dates(doi)
                    time.sleep(HTTP_DELAY)

                    if dates:
                        deltas = compute_deltas(dates)
                        record = {
                            "journal_name": journal_name,
                            "issn": issn,
                            "field": field,
                            "doi": doi,
                            "title": title[:200],
                            "received_date": fmt_date(dates.get("received")),
                            "accepted_date": fmt_date(dates.get("accepted")),
                            "revised_date": fmt_date(dates.get("revised")),
                            "published_date": fmt_date(dates.get("published")),
                            "days_submission_to_acceptance": deltas["days_submission_to_acceptance"] if deltas["days_submission_to_acceptance"] is not None else "",
                            "days_acceptance_to_publication": deltas["days_acceptance_to_publication"] if deltas["days_acceptance_to_publication"] is not None else "",
                            "days_total": deltas["days_total"] if deltas["days_total"] is not None else "",
                            "data_source": "jstage",
                        }
                        writer.writerow(record)
                        existing_dois.add(doi)
                        n_with_dates += 1
                        total_with_dates += 1

                    total_new += 1

                offset += PAGE_SIZE
                if offset >= total_available:
                    journal_done = True

            csv_file.flush()
            print(f"    Result: {n_with_dates} with dates / {n_checked} checked")

            checkpoint[issn] = {
                "n_with_dates": n_with_dates,
                "n_checked": n_checked,
                "done": True,
            }
            save_checkpoint(checkpoint)

    finally:
        csv_file.close()

    print(f"\nDone!")
    print(f"  Articles checked: {total_new}")
    print(f"  Articles with dates: {total_with_dates}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
