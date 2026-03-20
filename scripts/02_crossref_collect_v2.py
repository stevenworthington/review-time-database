#!/usr/bin/env python3
"""
02_crossref_collect_v2.py

Collect peer review dates from Crossref for all journals in journal_list_full.csv.
Expanded version targeting ~29,500 journals with richer metadata extraction.

Key design:
  - Targets 100 articles WITH review dates per journal (or 500 DOIs checked max)
  - Smart publisher probing: for unknown publishers, checks first 20 DOIs;
    if 0 have assertions, marks journal done early
  - APPENDs to existing crossref_articles.csv (preserves Phase 1 data)
  - Rich checkpoint: {issn: {n_with_dates: N, n_checked: N, done: bool}}

Strategy per journal:
  1. Fetch DOIs in pages of 50
  2. For each DOI: GET /works/{doi} -> check assertions for received/accepted dates
  3. Extract extended metadata columns
  4. Stop when 100 articles with dates, OR 500 DOIs checked, OR no more DOIs

Usage:
    python scripts/02_crossref_collect_v2.py [--resume] [--email EMAIL]
                                              [--journal-list PATH]
"""

import argparse
import csv
import json
import time
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CROSSREF_BASE = "https://api.crossref.org"
TARGET_WITH_DATES = 100      # Stop after this many articles with review dates
MAX_DOIS_CHECKED = 500       # Hard cap on DOIs checked per journal
PROBE_THRESHOLD = 20         # For unknown publishers, check this many first
PAGE_SIZE = 50               # DOIs per Crossref page
OUTPUT_FILE = Path("data/crossref_articles_v2.csv")
CHECKPOINT_FILE = Path("data/crossref_checkpoint_v2.json")
PHASE1_FILE = Path("data/crossref_articles.csv")  # original Phase 1 data for counting existing
DEFAULT_JOURNAL_LIST = Path("data/journal_list_full.csv")

# Publishers confirmed to NEVER deposit Crossref assertion dates (0% hit rate
# across 10+ journals already probed). Skip these entirely to save API calls.
SKIP_PUBLISHERS = {
    "Elsevier Ltd",
    "Elsevier Inc.",
    "SAGE Publications Ltd",
    "Cambridge University Press",
    "Academic Press Inc.",
    "Multidisciplinary Digital Publishing Institute (MDPI)",
    "American Psychological Association",
    "Frontiers Media SA",
    "W.B. Saunders",
    "Cell Press",
    "Royal Society of Chemistry",
    "Elsevier Ireland Ltd",
    "Academic Press",
    "S. Karger AG",
    "Mary Ann Liebert Inc.",
    "IOP Publishing Ltd.",
    "Elsevier Masson s.r.l.",
    "Emerald Publishing",
    "Elsevier GmbH",
    "American Physical Society",
    "Society for Industrial and Applied Mathematics Publications",
    "JMIR Publications Inc.",
    "Churchill Livingstone",
    "W.B. Saunders Ltd",
    "European Mathematical Society Publishing House",
    "KeAi Publishing Communications Ltd.",
    "Walter de Gruyter GmbH",
    "IEEE Computer Society",
    # Smaller publishers also confirmed 0% (5-9 journals checked)
    "Public Library of Science",
    "American Institute of Physics",
    "Dove Medical Press Ltd",
    "Dove Medical Press Ltd.",
    "American Meteorological Society",
    "American Mathematical Society",
    "Institute of Physics",
    "BioScientifica Ltd.",
    "Mathematical Sciences Publishers",
    "Portland Press Ltd",
    "Ubiquity Press",
    "American Accounting Association",
    "Cold Spring Harbor Laboratory Press",
    "Human Kinetics Publishers Inc.",
    "OAE Publishing Inc.",
}

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
    "received_date", "accepted_date", "revised_date", "published_date",
    "published_online_date", "published_print_date",
    "days_submission_to_acceptance", "days_acceptance_to_publication",
    "days_total", "data_source",
    "n_references", "n_authors", "funder", "license_url",
    "article_subtype", "citation_count", "page",
    "has_abstract", "language",
]

# Max delta thresholds (filter bogus data)
MAX_REVIEW_DAYS = 1095  # 3 years
MAX_PUB_DAYS = 365


def parse_date(date_str):
    """Try multiple date formats to parse a date string."""
    if not date_str:
        return None
    date_str = str(date_str).strip()
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


def fmt_date(dt):
    """Format a datetime as YYYY-MM-DD string, or empty string if None."""
    return dt.strftime("%Y-%m-%d") if dt else ""


def get_session(email):
    """Create a requests session with polite pool headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": f"ReviewTimeDB/2.0 (mailto:{email})",
        "Accept": "application/json",
    })
    return session


def fetch_doi_page(session, issn, issn_alt="", offset=0, rows=PAGE_SIZE):
    """Fetch a page of DOIs for a journal. Tries primary ISSN first, then alternate."""
    for try_issn in [issn, issn_alt]:
        if not try_issn:
            continue
        url = f"{CROSSREF_BASE}/works"
        params = {
            "filter": f"issn:{try_issn},type:journal-article",
            "sort": "published",
            "order": "desc",
            "rows": rows,
            "offset": offset,
            "select": "DOI",
        }
        try:
            r = session.get(url, params=params, timeout=30)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            data = r.json()
            msg = data.get("message", {})
            items = msg.get("items", [])
            total_results = msg.get("total-results", 0)
            dois = [item["DOI"] for item in items if "DOI" in item]
            if dois:
                return dois, total_results
        except Exception as e:
            print(f"    ERROR fetching DOIs with ISSN {try_issn} offset={offset}: {e}")
    return [], 0


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
    """Extract received, accepted, revised, and published dates from article metadata."""
    result = {
        "received_date": None,
        "accepted_date": None,
        "revised_date": None,
        "published_date": None,
        "published_online_date": None,
        "published_print_date": None,
    }

    # --- Check assertion field (primary source) ---
    assertions = msg.get("assertion", [])
    for assertion in assertions:
        name = assertion.get("name", "").lower()
        value = assertion.get("value", "")
        if name == "received" and value:
            result["received_date"] = parse_date(value)
        elif name == "accepted" and value:
            result["accepted_date"] = parse_date(value)
        elif ("revised" in name or "revision" in name) and value:
            result["revised_date"] = parse_date(value)

    # --- Fallback: check standard accepted field ---
    if result["accepted_date"] is None:
        accepted = msg.get("accepted")
        if accepted and "date-parts" in accepted:
            result["accepted_date"] = parse_crossref_date_parts(accepted["date-parts"])

    # --- Published dates ---
    pub_online = msg.get("published-online")
    if pub_online and "date-parts" in pub_online:
        result["published_online_date"] = parse_crossref_date_parts(pub_online["date-parts"])

    pub_print = msg.get("published-print")
    if pub_print and "date-parts" in pub_print:
        result["published_print_date"] = parse_crossref_date_parts(pub_print["date-parts"])

    # Best available published date: prefer online, then print, then issued
    result["published_date"] = result["published_online_date"]
    if result["published_date"] is None:
        result["published_date"] = result["published_print_date"]
    if result["published_date"] is None:
        issued = msg.get("issued")
        if issued and "date-parts" in issued:
            result["published_date"] = parse_crossref_date_parts(issued["date-parts"])

    return result


def extract_extended_metadata(msg):
    """Extract additional metadata fields from a Crossref work record."""
    meta = {}

    # Number of references
    meta["n_references"] = msg.get("reference-count", "")

    # Number of authors
    authors = msg.get("author", [])
    meta["n_authors"] = len(authors) if authors else ""

    # Funder: semicolon-separated names
    funders = msg.get("funder", [])
    if funders:
        funder_names = [f.get("name", "") for f in funders if f.get("name")]
        meta["funder"] = "; ".join(funder_names) if funder_names else ""
    else:
        meta["funder"] = ""

    # License URL: prefer "vor" (version of record), else first license
    licenses = msg.get("license", [])
    meta["license_url"] = ""
    if licenses:
        for lic in licenses:
            cv = lic.get("content-version", "")
            if cv == "vor":
                meta["license_url"] = lic.get("URL", "")
                break
        if not meta["license_url"]:
            meta["license_url"] = licenses[0].get("URL", "")

    # Article subtype
    meta["article_subtype"] = msg.get("subtype", msg.get("type", ""))

    # Citation count
    meta["citation_count"] = msg.get("is-referenced-by-count", "")

    # Page
    meta["page"] = msg.get("page", "")

    # Has abstract
    meta["has_abstract"] = "1" if msg.get("abstract") else "0"

    # Language
    meta["language"] = msg.get("language", "")

    return meta


def compute_deltas(dates):
    """Compute day deltas between dates."""
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
    """Load checkpoint dict: {issn: {n_with_dates, n_checked, done}}."""
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {}


def save_checkpoint(checkpoint):
    """Save checkpoint dict."""
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoint, f, indent=1)


def count_existing_articles(output_file):
    """Count articles per ISSN already in the output CSV."""
    counts = {}
    if not output_file.exists():
        return counts
    try:
        df = pd.read_csv(output_file, usecols=["issn"], dtype=str)
        for issn in df["issn"]:
            counts[issn] = counts.get(issn, 0) + 1
    except Exception as e:
        print(f"  Warning: could not read existing {output_file}: {e}")
    return counts


def load_existing_dois(output_file):
    """Load set of DOIs already in the output CSV to avoid duplicates."""
    existing = set()
    if not output_file.exists():
        return existing
    try:
        df = pd.read_csv(output_file, usecols=["doi"], dtype=str)
        existing = set(df["doi"].dropna().str.strip())
    except Exception as e:
        print(f"  Warning: could not read existing DOIs from {output_file}: {e}")
    print(f"  Loaded {len(existing)} existing DOIs from {output_file.name}")
    return existing


def main():
    parser = argparse.ArgumentParser(description="Collect Crossref review dates (v2)")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--email", default="reviewtimedb@example.com",
                        help="Email for Crossref polite pool")
    parser.add_argument("--journal-list", default=str(DEFAULT_JOURNAL_LIST),
                        help="Path to journal list CSV")
    args = parser.parse_args()

    journal_list_path = Path(args.journal_list)
    if not journal_list_path.exists():
        print(f"ERROR: {journal_list_path} not found.")
        sys.exit(1)

    # Load journal list
    journals = pd.read_csv(journal_list_path)
    print(f"Loaded {len(journals)} journals from {journal_list_path}")

    # Filter to query_crossref == True
    if "query_crossref" in journals.columns:
        journals = journals[journals["query_crossref"].astype(str).str.strip().str.lower() == "true"].copy()
        print(f"  {len(journals)} journals with query_crossref=True")

    # Load checkpoint if resuming
    checkpoint = load_checkpoint() if args.resume else {}
    if checkpoint:
        print(f"Resuming: {len(checkpoint)} journals in checkpoint")

    # Count existing articles per ISSN (from both Phase 1 and v2 files)
    existing_counts = count_existing_articles(OUTPUT_FILE)
    existing_dois = load_existing_dois(OUTPUT_FILE)
    if existing_counts:
        print(f"  {sum(existing_counts.values())} existing articles across {len(existing_counts)} journals")

    # Set up output CSV — always append, write header only if file doesn't exist
    write_header = not OUTPUT_FILE.exists()
    csv_file = open(OUTPUT_FILE, "a", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if write_header:
        writer.writeheader()

    session = get_session(args.email)

    total_new_articles = 0
    total_new_with_dates = 0
    total_journals = len(journals)
    journals_processed_this_run = 0

    for loop_idx, (_, row) in enumerate(journals.iterrows()):
        issn = str(row["issn"]).strip()
        issn_alt = str(row.get("issn_alt", "")).strip()
        if issn_alt == "nan":
            issn_alt = ""
        journal_name = row["journal_name"]
        field = row["field"]

        # Skip publishers known to never deposit assertion dates
        publisher = row.get("publisher", "")
        if publisher in SKIP_PUBLISHERS:
            if issn not in checkpoint:
                checkpoint[issn] = {"n_with_dates": 0, "n_checked": 0, "done": True, "skipped": "publisher_skip_list"}
                save_checkpoint(checkpoint)
            continue

        # Check checkpoint — skip if done
        cp = checkpoint.get(issn, {})
        if cp.get("done", False):
            continue

        # Check if already have enough from existing data
        existing_n = existing_counts.get(issn, 0)
        if existing_n >= TARGET_WITH_DATES:
            checkpoint[issn] = {"n_with_dates": existing_n, "n_checked": existing_n, "done": True}
            save_checkpoint(checkpoint)
            continue

        journals_processed_this_run += 1
        n_with_dates = cp.get("n_with_dates", 0) + existing_n
        n_checked = cp.get("n_checked", 0)
        needed = TARGET_WITH_DATES - n_with_dates

        print(f"[{loop_idx+1}/{total_journals}] {journal_name} (ISSN: {issn})"
              f"  need={needed} checked={n_checked}")

        # Paging through DOIs
        offset = n_checked  # Resume from where we left off approximately
        probe_mode = True  # Start in probe mode for unknown publishers
        probe_found = 0
        journal_done = False

        while not journal_done and n_checked < MAX_DOIS_CHECKED:
            # Fetch a page of DOIs
            dois_page, total_available = fetch_doi_page(
                session, issn, issn_alt, offset=offset, rows=PAGE_SIZE
            )

            if not dois_page:
                print(f"    No more DOIs available (offset={offset})")
                journal_done = True
                break

            for doi in dois_page:
                if n_checked >= MAX_DOIS_CHECKED:
                    journal_done = True
                    break
                if n_with_dates >= TARGET_WITH_DATES:
                    journal_done = True
                    break

                # Skip if already collected
                if doi in existing_dois:
                    continue

                msg = fetch_article_metadata(session, doi)
                n_checked += 1

                if msg is None:
                    time.sleep(0.1)
                    continue

                dates = extract_dates(msg)
                has_review_data = (dates["received_date"] is not None
                                   and dates["accepted_date"] is not None)

                if has_review_data:
                    deltas = compute_deltas(dates)
                    ext = extract_extended_metadata(msg)
                    title = msg.get("title", [""])[0] if msg.get("title") else ""

                    record = {
                        "journal_name": journal_name,
                        "issn": issn,
                        "field": field,
                        "doi": doi,
                        "title": title[:200],
                        "received_date": fmt_date(dates["received_date"]),
                        "accepted_date": fmt_date(dates["accepted_date"]),
                        "revised_date": fmt_date(dates["revised_date"]),
                        "published_date": fmt_date(dates["published_date"]),
                        "published_online_date": fmt_date(dates["published_online_date"]),
                        "published_print_date": fmt_date(dates["published_print_date"]),
                        "days_submission_to_acceptance": deltas["days_submission_to_acceptance"] if deltas["days_submission_to_acceptance"] is not None else "",
                        "days_acceptance_to_publication": deltas["days_acceptance_to_publication"] if deltas["days_acceptance_to_publication"] is not None else "",
                        "days_total": deltas["days_total"] if deltas["days_total"] is not None else "",
                        "data_source": "crossref_assertion",
                    }
                    record.update(ext)
                    writer.writerow(record)
                    existing_dois.add(doi)
                    n_with_dates += 1
                    probe_found += 1
                    total_new_with_dates += 1

                total_new_articles += 1
                # Brief pause to stay under rate limits
                time.sleep(0.05)

                # Smart publisher probing: if we've checked PROBE_THRESHOLD DOIs
                # and found 0 with dates, this publisher likely doesn't deposit them
                if probe_mode and n_checked >= PROBE_THRESHOLD and probe_found == 0:
                    print(f"    Probe: 0/{PROBE_THRESHOLD} DOIs had assertions, skipping journal")
                    journal_done = True
                    break

            # Done probing after first batch
            if n_checked >= PROBE_THRESHOLD:
                probe_mode = False

            offset += PAGE_SIZE

            # Don't go beyond available results
            if offset >= total_available:
                journal_done = True

        csv_file.flush()

        print(f"    Result: {n_with_dates} with dates / {n_checked} checked")

        checkpoint[issn] = {
            "n_with_dates": n_with_dates - existing_n,  # Only count new ones
            "n_checked": n_checked,
            "done": True,
        }
        save_checkpoint(checkpoint)

    csv_file.close()

    print(f"\nDone!")
    print(f"  Journals processed this run: {journals_processed_this_run}")
    print(f"  New articles checked: {total_new_articles}")
    print(f"  New articles with review dates: {total_new_with_dates}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
