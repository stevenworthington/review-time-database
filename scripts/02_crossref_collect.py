#!/usr/bin/env python3
"""
02_crossref_collect.py

Collect peer review dates from Crossref for all journals in journal_list.csv.

The key insight from prototyping: Crossref's standard `accepted` field is almost
universally empty. Dates live in the `assertion` field, and only Springer Nature /
BMC reliably deposit them. We still query all journals to find whatever data exists.

Strategy per journal:
  1. GET /works?filter=issn:{issn},type:journal-article&sort=published&order=desc&rows=50&select=DOI
  2. For each DOI: GET /works/{doi} → check assertions for received/accepted dates
  3. Parse dates, compute deltas, save results

Usage:
    python scripts/02_crossref_collect.py [--resume] [--email EMAIL]
"""

import argparse
import csv
import json
import time
import sys
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CROSSREF_BASE = "https://api.crossref.org"
ARTICLES_PER_JOURNAL = 50
OUTPUT_FILE = Path("data/crossref_articles.csv")
CHECKPOINT_FILE = Path("data/crossref_checkpoint.json")
JOURNAL_LIST = Path("data/journal_list.csv")

# Date parsing patterns for assertion dates
DATE_FORMATS = [
    "%d %B %Y",       # "29 August 2025"
    "%B %d, %Y",      # "August 29, 2025"
    "%d %b %Y",       # "29 Aug 2025"
    "%b %d, %Y",      # "Aug 29, 2025"
    "%Y-%m-%d",        # "2025-08-29"
    "%d/%m/%Y",        # "29/08/2025"
    "%m/%d/%Y",        # "08/29/2025"
    "%d-%m-%Y",        # "29-08-2025"
    "%Y/%m/%d",        # "2025/08/29"
]

CSV_FIELDS = [
    "journal_name", "issn", "field", "doi", "title",
    "received_date", "accepted_date", "published_date",
    "days_submission_to_acceptance", "days_acceptance_to_publication",
    "days_total", "data_source",
]

# Max delta thresholds (filter bogus data)
MAX_REVIEW_DAYS = 1095  # 3 years
MAX_PUB_DAYS = 365


def parse_date(date_str):
    """Try multiple date formats to parse a date string."""
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def parse_crossref_date_parts(date_parts):
    """Parse Crossref date-parts format [[year, month, day]]."""
    if not date_parts or not date_parts[0]:
        return None
    parts = date_parts[0]
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return datetime(year, month, day)
    except (ValueError, IndexError):
        return None


def get_session(email):
    """Create a requests session with polite pool headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": f"ReviewTimeDB/1.0 (mailto:{email})",
        "Accept": "application/json",
    })
    return session


def fetch_dois_for_journal(session, issn, issn_alt="", max_articles=ARTICLES_PER_JOURNAL):
    """Fetch recent article DOIs for a journal. Tries primary ISSN first, then alternate."""
    for try_issn in [issn, issn_alt]:
        if not try_issn:
            continue
        url = f"{CROSSREF_BASE}/works"
        params = {
            "filter": f"issn:{try_issn},type:journal-article",
            "sort": "published",
            "order": "desc",
            "rows": max_articles,
            "select": "DOI",
        }
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()
            items = data.get("message", {}).get("items", [])
            dois = [item["DOI"] for item in items if "DOI" in item]
            if dois:
                return dois
        except Exception as e:
            print(f"    ERROR fetching DOIs with ISSN {try_issn}: {e}")
    return []


def fetch_article_metadata(session, doi):
    """Fetch full metadata for a single article by DOI."""
    url = f"{CROSSREF_BASE}/works/{doi}"
    try:
        r = session.get(url, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("message", {})
    except Exception as e:
        print(f"    ERROR fetching {doi}: {e}")
        return None


def extract_dates(msg):
    """Extract received, accepted, and published dates from article metadata.

    Returns dict with keys: received_date, accepted_date, published_date
    (as datetime objects or None).
    """
    result = {"received_date": None, "accepted_date": None, "published_date": None}

    # --- Check assertion field (primary source) ---
    assertions = msg.get("assertion", [])
    for assertion in assertions:
        name = assertion.get("name", "").lower()
        value = assertion.get("value", "")
        if name == "received" and value:
            result["received_date"] = parse_date(value)
        elif name == "accepted" and value:
            result["accepted_date"] = parse_date(value)

    # --- Fallback: check standard accepted field ---
    if result["accepted_date"] is None:
        accepted = msg.get("accepted")
        if accepted and "date-parts" in accepted:
            result["accepted_date"] = parse_crossref_date_parts(accepted["date-parts"])

    # --- Published date: prefer published-online, then issued ---
    pub_online = msg.get("published-online")
    if pub_online and "date-parts" in pub_online:
        result["published_date"] = parse_crossref_date_parts(pub_online["date-parts"])
    if result["published_date"] is None:
        issued = msg.get("issued")
        if issued and "date-parts" in issued:
            result["published_date"] = parse_crossref_date_parts(issued["date-parts"])

    return result


def compute_deltas(dates):
    """Compute day deltas between dates. Returns dict with delta fields."""
    deltas = {
        "days_submission_to_acceptance": None,
        "days_acceptance_to_publication": None,
        "days_total": None,
    }

    received = dates["received_date"]
    accepted = dates["accepted_date"]
    published = dates["published_date"]

    if received and accepted:
        delta = (accepted - received).days
        if 0 <= delta <= MAX_REVIEW_DAYS:
            deltas["days_submission_to_acceptance"] = delta

    if accepted and published:
        delta = (published - accepted).days
        if 0 <= delta <= MAX_PUB_DAYS:
            deltas["days_acceptance_to_publication"] = delta

    if received and published:
        delta = (published - received).days
        if 0 <= delta <= (MAX_REVIEW_DAYS + MAX_PUB_DAYS):
            deltas["days_total"] = delta

    return deltas


def load_checkpoint():
    """Load set of already-processed ISSNs."""
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(processed_issns):
    """Save set of processed ISSNs."""
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(sorted(processed_issns), f)


def main():
    parser = argparse.ArgumentParser(description="Collect Crossref review dates")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--email", default="reviewtimedb@example.com",
                        help="Email for Crossref polite pool")
    args = parser.parse_args()

    if not JOURNAL_LIST.exists():
        print(f"ERROR: {JOURNAL_LIST} not found. Run 01_build_journal_list.py first.")
        sys.exit(1)

    # Load journal list
    import pandas as pd
    journals = pd.read_csv(JOURNAL_LIST)
    print(f"Loaded {len(journals)} journals from {JOURNAL_LIST}")

    # Load checkpoint if resuming
    processed = load_checkpoint() if args.resume else set()
    if processed:
        print(f"Resuming: {len(processed)} journals already processed")

    # Set up output CSV
    write_header = not (args.resume and OUTPUT_FILE.exists())
    csv_file = open(OUTPUT_FILE, "a" if args.resume else "w", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if write_header:
        writer.writeheader()

    session = get_session(args.email)

    total_articles = 0
    total_with_dates = 0
    total_journals = len(journals)

    for idx, row in journals.iterrows():
        issn = row["issn"]
        issn_alt = row.get("issn_alt", "")
        if pd.isna(issn_alt):
            issn_alt = ""
        journal_name = row["journal_name"]
        field = row["field"]

        if issn in processed:
            continue

        print(f"[{idx+1}/{total_journals}] {journal_name} (ISSN: {issn})")

        # Step 1: Get DOIs (tries both ISSNs)
        dois = fetch_dois_for_journal(session, issn, issn_alt)
        if not dois:
            print(f"    No articles found")
            processed.add(issn)
            save_checkpoint(processed)
            continue

        print(f"    Found {len(dois)} articles, fetching metadata...")

        journal_articles = 0
        journal_with_dates = 0

        for doi in dois:
            msg = fetch_article_metadata(session, doi)
            if msg is None:
                time.sleep(0.1)
                continue

            dates = extract_dates(msg)
            deltas = compute_deltas(dates)

            has_review_data = dates["received_date"] is not None and dates["accepted_date"] is not None

            if has_review_data:
                title = msg.get("title", [""])[0] if msg.get("title") else ""
                record = {
                    "journal_name": journal_name,
                    "issn": issn,
                    "field": field,
                    "doi": doi,
                    "title": title[:200],
                    "received_date": dates["received_date"].strftime("%Y-%m-%d") if dates["received_date"] else "",
                    "accepted_date": dates["accepted_date"].strftime("%Y-%m-%d") if dates["accepted_date"] else "",
                    "published_date": dates["published_date"].strftime("%Y-%m-%d") if dates["published_date"] else "",
                    "days_submission_to_acceptance": deltas["days_submission_to_acceptance"] if deltas["days_submission_to_acceptance"] is not None else "",
                    "days_acceptance_to_publication": deltas["days_acceptance_to_publication"] if deltas["days_acceptance_to_publication"] is not None else "",
                    "days_total": deltas["days_total"] if deltas["days_total"] is not None else "",
                    "data_source": "crossref_assertion",
                }
                writer.writerow(record)
                journal_with_dates += 1

            journal_articles += 1
            # Brief pause to stay under rate limits
            time.sleep(0.05)

        csv_file.flush()
        total_articles += journal_articles
        total_with_dates += journal_with_dates

        print(f"    Processed {journal_articles} articles, {journal_with_dates} with review dates")

        processed.add(issn)
        save_checkpoint(processed)

    csv_file.close()

    print(f"\nDone!")
    print(f"  Total articles processed: {total_articles}")
    print(f"  Articles with review dates: {total_with_dates}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
