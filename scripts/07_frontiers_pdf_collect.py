#!/usr/bin/env python3
"""
07_frontiers_pdf_collect.py

Extract peer review dates from Frontiers journal PDFs via Unpaywall OA links.

Frontiers PDFs contain dates in the format:
    RECEIVED 15 July 2024
    ACCEPTED 06 December 2024
    PUBLISHED 05 February 2025

Strategy per journal:
  1. Fetch DOIs from Crossref bulk ISSN endpoint (just need DOIs, not assertions)
  2. For each DOI, query Unpaywall for an OA PDF URL
  3. Download the PDF, extract text from first 2 pages
  4. Parse RECEIVED/ACCEPTED/PUBLISHED dates via regex
  5. Stop when 100 articles with dates, OR no more DOIs

Usage:
    python scripts/07_frontiers_pdf_collect.py [--resume]
"""

import argparse
import csv
import json
import re
import time
import sys
import tempfile
import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF (fitz) is required. Install with: pip install pymupdf")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CROSSREF_BASE = "https://api.crossref.org"
UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
EMAIL = "reviewtimedb@rapidpeer.com"
TARGET_WITH_DATES = 100       # Stop after this many articles with review dates
MAX_DOIS_CHECKED = 500        # Hard cap on DOIs checked per journal
PAGE_SIZE = 50                # DOIs per Crossref page
OUTPUT_FILE = Path("data/frontiers_articles.csv")
CHECKPOINT_FILE = Path("data/frontiers_checkpoint.json")
DEFAULT_JOURNAL_LIST = Path("data/journal_list_full.csv")

# Rate limiting (seconds)
UNPAYWALL_DELAY = 0.3
PDF_DOWNLOAD_DELAY = 0.5
CROSSREF_DELAY = 0.1

# Max delta thresholds (filter bogus data)
MAX_REVIEW_DAYS = 365         # Submission to acceptance
MAX_PUB_DAYS = 365            # Acceptance to publication

CSV_FIELDS = [
    "journal_name", "issn", "field", "doi", "title",
    "received_date", "accepted_date", "revised_date", "published_date",
    "published_online_date", "published_print_date",
    "days_submission_to_acceptance", "days_acceptance_to_publication",
    "days_total", "data_source",
]

# Regex for Frontiers date patterns (all-caps keyword + DD Month YYYY)
DATE_PATTERN = re.compile(
    r"(?:RECEIVED|Received)\s+(\d{1,2}\s+\w+\s+\d{4})"
    r"|(?:ACCEPTED|Accepted)\s+(\d{1,2}\s+\w+\s+\d{4})"
    r"|(?:PUBLISHED|Published)\s+(\d{1,2}\s+\w+\s+\d{4})",
    re.IGNORECASE,
)

DATE_FORMATS = [
    "%d %B %Y",       # "15 July 2024"
    "%d %b %Y",       # "15 Jul 2024"
]


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


def fmt_date(dt):
    """Format a datetime as YYYY-MM-DD string, or empty string if None."""
    return dt.strftime("%Y-%m-%d") if dt else ""


def get_session():
    """Create a requests session with polite pool headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": f"ReviewTimeDB/2.0 (mailto:{EMAIL})",
        "Accept": "application/json",
    })
    return session


def fetch_doi_page(session, issn, offset=0, rows=PAGE_SIZE):
    """Fetch a page of DOIs for a journal from Crossref bulk endpoint."""
    url = f"{CROSSREF_BASE}/works"
    params = {
        "filter": f"issn:{issn},type:journal-article",
        "sort": "published",
        "order": "desc",
        "rows": rows,
        "offset": offset,
        "select": "DOI,title",
    }
    try:
        r = session.get(url, params=params, timeout=30)
        if r.status_code == 404:
            return [], 0
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {})
        items = msg.get("items", [])
        total_results = msg.get("total-results", 0)
        results = []
        for item in items:
            doi = item.get("DOI", "")
            title = item.get("title", [""])[0] if item.get("title") else ""
            if doi:
                results.append((doi, title))
        return results, total_results
    except Exception as e:
        print(f"    ERROR fetching DOIs with ISSN {issn} offset={offset}: {e}")
        return [], 0


def get_pdf_url(session, doi):
    """Query Unpaywall for an OA PDF URL for a given DOI."""
    url = f"{UNPAYWALL_BASE}/{doi}"
    params = {"email": EMAIL}
    try:
        r = session.get(url, params=params, timeout=15)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()

        # Check best_oa_location first
        best = data.get("best_oa_location", {})
        if best:
            pdf_url = best.get("url_for_pdf") or best.get("url")
            if pdf_url:
                return pdf_url

        # Then check all oa_locations
        for loc in data.get("oa_locations", []):
            pdf_url = loc.get("url_for_pdf") or loc.get("url")
            if pdf_url:
                return pdf_url

        return None
    except Exception as e:
        print(f"    ERROR Unpaywall for {doi}: {e}")
        return None


def download_and_extract_text(session, pdf_url, max_pages=2):
    """Download a PDF and extract text from the first N pages."""
    try:
        r = session.get(pdf_url, timeout=30, stream=True)
        r.raise_for_status()

        # Check content type — skip if not PDF
        content_type = r.headers.get("Content-Type", "")
        if "html" in content_type.lower():
            return None

        # Read into memory (limit to 10MB to avoid huge files)
        content = b""
        for chunk in r.iter_content(chunk_size=8192):
            content += chunk
            if len(content) > 10 * 1024 * 1024:
                return None

        if len(content) < 1000:
            return None

        # Extract text with PyMuPDF
        doc = fitz.open(stream=content, filetype="pdf")
        text = ""
        for page_num in range(min(max_pages, len(doc))):
            text += doc[page_num].get_text()
        doc.close()
        return text

    except Exception as e:
        print(f"    ERROR downloading PDF from {pdf_url[:80]}...: {e}")
        return None


def extract_dates_from_text(text):
    """Extract RECEIVED, ACCEPTED, PUBLISHED dates from PDF text."""
    dates = {
        "received_date": None,
        "accepted_date": None,
        "published_date": None,
    }

    if not text:
        return dates

    # Find all matches
    for match in DATE_PATTERN.finditer(text):
        received_str, accepted_str, published_str = match.groups()
        if received_str:
            dates["received_date"] = parse_date(received_str)
        if accepted_str:
            dates["accepted_date"] = parse_date(accepted_str)
        if published_str:
            dates["published_date"] = parse_date(published_str)

    return dates


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


def load_existing_dois():
    """Load set of DOIs already in the output CSV to avoid duplicates."""
    existing = set()
    if not OUTPUT_FILE.exists():
        return existing
    try:
        df = pd.read_csv(OUTPUT_FILE, usecols=["doi"], dtype=str)
        existing = set(df["doi"].dropna())
    except Exception as e:
        print(f"  Warning: could not read existing DOIs from {OUTPUT_FILE}: {e}")
    return existing


def main():
    parser = argparse.ArgumentParser(description="Collect review dates from Frontiers PDFs")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--journal-list", default=str(DEFAULT_JOURNAL_LIST),
                        help="Path to journal list CSV")
    parser.add_argument("--limit", type=int, default=0,
                        help="Limit number of journals to process (0 = all)")
    args = parser.parse_args()

    journal_list_path = Path(args.journal_list)
    if not journal_list_path.exists():
        print(f"ERROR: {journal_list_path} not found.")
        sys.exit(1)

    # Load journal list, filter to Frontiers
    journals = pd.read_csv(journal_list_path)
    journals = journals[journals["publisher"].str.contains("Frontiers", case=False, na=False)].copy()
    journals = journals.sort_values("sjr_rank").reset_index(drop=True)
    print(f"Loaded {len(journals)} Frontiers journals from {journal_list_path}")

    if args.limit > 0:
        journals = journals.head(args.limit)
        print(f"  Limited to {len(journals)} journals")

    # Load checkpoint if resuming
    checkpoint = load_checkpoint() if args.resume else {}
    if checkpoint:
        print(f"Resuming: {len(checkpoint)} journals in checkpoint")

    # Load existing DOIs
    existing_dois = load_existing_dois()
    if existing_dois:
        print(f"  {len(existing_dois)} existing articles in output file")

    # Set up output CSV — always append, write header only if file doesn't exist
    write_header = not OUTPUT_FILE.exists()
    csv_file = open(OUTPUT_FILE, "a", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if write_header:
        writer.writeheader()

    session = get_session()

    total_new_with_dates = 0
    total_journals = len(journals)
    journals_processed = 0
    journals_skipped = 0

    for loop_idx, (_, row) in enumerate(journals.iterrows()):
        issn = str(row["issn"]).strip()
        journal_name = row["journal_name"]
        field = row["field"]

        # Check checkpoint — skip if done
        cp = checkpoint.get(issn, {})
        if cp.get("done", False):
            journals_skipped += 1
            continue

        journals_processed += 1
        n_with_dates = cp.get("n_with_dates", 0)
        n_checked = cp.get("n_checked", 0)
        n_no_pdf = cp.get("n_no_pdf", 0)
        needed = TARGET_WITH_DATES - n_with_dates

        print(f"\n[{loop_idx+1}/{total_journals}] {journal_name} (ISSN: {issn})"
              f"  need={needed} checked={n_checked}")

        # Paging through DOIs
        offset = cp.get("offset", 0)
        journal_done = False

        while not journal_done and n_checked < MAX_DOIS_CHECKED:
            # Fetch a page of DOIs from Crossref
            doi_page, total_available = fetch_doi_page(session, issn, offset=offset)
            time.sleep(CROSSREF_DELAY)

            if not doi_page:
                print(f"    No more DOIs available (offset={offset})")
                journal_done = True
                break

            for doi, title in doi_page:
                if n_checked >= MAX_DOIS_CHECKED:
                    journal_done = True
                    break
                if n_with_dates >= TARGET_WITH_DATES:
                    print(f"    Reached target: {n_with_dates} articles with dates")
                    journal_done = True
                    break

                # Skip if already collected
                if doi in existing_dois:
                    continue

                n_checked += 1

                # Step 1: Get PDF URL from Unpaywall
                pdf_url = get_pdf_url(session, doi)
                time.sleep(UNPAYWALL_DELAY)

                if not pdf_url:
                    n_no_pdf += 1
                    continue

                # Step 2: Download PDF and extract text
                text = download_and_extract_text(session, pdf_url)
                time.sleep(PDF_DOWNLOAD_DELAY)

                if not text:
                    n_no_pdf += 1
                    continue

                # Step 3: Parse dates from text
                dates = extract_dates_from_text(text)
                has_review_data = (dates["received_date"] is not None
                                   and dates["accepted_date"] is not None)

                if has_review_data:
                    deltas = compute_deltas(dates)

                    # Skip if submission_to_acceptance is bogus
                    if deltas["days_submission_to_acceptance"] is None and dates["received_date"] and dates["accepted_date"]:
                        # Delta was computed but filtered out as bogus
                        raw_delta = (dates["accepted_date"] - dates["received_date"]).days
                        if raw_delta < 0 or raw_delta > MAX_REVIEW_DAYS:
                            continue

                    record = {
                        "journal_name": journal_name,
                        "issn": issn,
                        "field": field,
                        "doi": doi,
                        "title": title[:200],
                        "received_date": fmt_date(dates["received_date"]),
                        "accepted_date": fmt_date(dates["accepted_date"]),
                        "revised_date": "",
                        "published_date": fmt_date(dates["published_date"]),
                        "published_online_date": fmt_date(dates["published_date"]),
                        "published_print_date": "",
                        "days_submission_to_acceptance": deltas["days_submission_to_acceptance"] if deltas["days_submission_to_acceptance"] is not None else "",
                        "days_acceptance_to_publication": deltas["days_acceptance_to_publication"] if deltas["days_acceptance_to_publication"] is not None else "",
                        "days_total": deltas["days_total"] if deltas["days_total"] is not None else "",
                        "data_source": "frontiers_pdf",
                    }
                    writer.writerow(record)
                    existing_dois.add(doi)
                    n_with_dates += 1
                    total_new_with_dates += 1

                # Progress logging every 10 articles
                if n_checked % 10 == 0:
                    print(f"    ... checked {n_checked}, found {n_with_dates} with dates, {n_no_pdf} no PDF")

            offset += PAGE_SIZE

            # Don't go beyond available results
            if offset >= total_available:
                journal_done = True

        csv_file.flush()

        print(f"    Result: {n_with_dates} with dates / {n_checked} checked / {n_no_pdf} no PDF")

        checkpoint[issn] = {
            "n_with_dates": n_with_dates,
            "n_checked": n_checked,
            "n_no_pdf": n_no_pdf,
            "offset": offset,
            "done": True,
        }
        save_checkpoint(checkpoint)

    csv_file.close()

    print(f"\nDone!")
    print(f"  Journals processed this run: {journals_processed}")
    print(f"  Journals skipped (already done): {journals_skipped}")
    print(f"  New articles with review dates: {total_new_with_dates}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
