#!/usr/bin/env python3
"""
05_scrape_publishers.py

Scrape peer review dates from publisher article HTML pages.
Targets journals not covered by Crossref assertions or PubMed.

Working publishers:
  Tier 1 (HTTP/curl — fast): Elsevier/ScienceDirect, MDPI
  Tier 2 (Playwright — slower): Taylor & Francis, Oxford UP, Frontiers

Strategy per journal:
  1. Get recent DOIs from Crossref (reuses same approach as 02_crossref_collect)
  2. Resolve DOI → publisher article URL
  3. Fetch page and extract dates using publisher-specific parsers
  4. Target: 100 articles with dates per journal, or 300 DOIs checked max

Usage:
    python scripts/05_scrape_publishers.py [--resume] [--email EMAIL]
                                            [--journal-list PATH]
                                            [--tier {1,2,all}]
"""

import argparse
import csv
import json
import time
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests

from publisher_parsers import (
    SCRAPEABLE_PUBLISHERS, get_parser_for_url,
    parse_http, parse_playwright_page, parse_date,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CROSSREF_BASE = "https://api.crossref.org"
TARGET_WITH_DATES = 100      # Stop after this many articles with review dates
MAX_DOIS_CHECKED = 300       # Hard cap on DOIs checked per journal
PAGE_SIZE = 50               # DOIs per Crossref page
OUTPUT_FILE = Path("data/scraped_articles.csv")  # default; overridden per-tier
CHECKPOINT_FILE = Path("data/scrape_checkpoint.json")  # default; overridden per-tier
DEFAULT_JOURNAL_LIST = Path("data/journal_list_full.csv")

# Delay between page fetches (seconds) — be polite
HTTP_DELAY = 1.0
PLAYWRIGHT_DELAY = 2.0

# Max delta thresholds (filter bogus data)
MAX_REVIEW_DAYS = 1095  # 3 years
MAX_PUB_DAYS = 365

CSV_FIELDS = [
    "journal_name", "issn", "field", "doi", "title",
    "received_date", "accepted_date", "revised_date", "published_date",
    "days_submission_to_acceptance", "days_acceptance_to_publication",
    "days_total", "data_source",
    "n_authors", "has_abstract",
]


def fmt_date(dt):
    """Format a datetime as YYYY-MM-DD string, or empty string if None."""
    return dt.strftime("%Y-%m-%d") if dt else ""


def compute_deltas(dates):
    """Compute day deltas between dates."""
    deltas = {
        "days_submission_to_acceptance": None,
        "days_acceptance_to_publication": None,
        "days_total": None,
    }
    received = dates.get("received_date")
    accepted = dates.get("accepted_date")
    published = dates.get("published_date")

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


def get_session(email):
    """Create a requests session with polite pool headers for Crossref."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": f"ReviewTimeDB/3.0 (mailto:{email})",
        "Accept": "application/json",
    })
    return session


def fetch_doi_page(session, issn, issn_alt="", offset=0, rows=PAGE_SIZE):
    """Fetch a page of DOIs for a journal from Crossref."""
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
            "select": "DOI,URL",
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
            doi_urls = []
            for item in items:
                doi = item.get("DOI", "")
                article_url = item.get("URL", f"https://doi.org/{doi}")
                if doi:
                    doi_urls.append((doi, article_url))
            if doi_urls:
                return doi_urls, total_results
        except Exception as e:
            print(f"    ERROR fetching DOIs with ISSN {try_issn} offset={offset}: {e}")
    return [], 0


def resolve_doi_url(doi):
    """
    Resolve a DOI to its actual publisher URL by following the redirect.
    Uses a separate session with browser-like headers (not Crossref API headers)
    to get the real publisher URL instead of the Crossref API transform URL.
    """
    try:
        r = requests.head(
            f"https://doi.org/{doi}",
            allow_redirects=True,
            timeout=10,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html",
            }
        )
        return r.url
    except Exception:
        return f"https://doi.org/{doi}"


def load_checkpoint():
    """Load checkpoint dict."""
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {}


def save_checkpoint(checkpoint):
    """Save checkpoint dict."""
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoint, f, indent=1)


def load_existing_dois():
    """Load set of DOIs already collected from all sources."""
    existing = set()
    for csv_path in [
        Path("data/crossref_articles.csv"),
        Path("data/crossref_articles_v2.csv"),
        Path("data/pubmed_articles.csv"),
        Path("data/pubmed_articles_v2.csv"),
        OUTPUT_FILE,
    ]:
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path, usecols=["doi"], dtype=str)
                existing.update(df["doi"].dropna())
            except Exception:
                pass
    return existing


def count_existing_articles_per_issn():
    """Count articles per ISSN already in the scrape output."""
    counts = {}
    if not OUTPUT_FILE.exists():
        return counts
    try:
        df = pd.read_csv(OUTPUT_FILE, usecols=["issn"], dtype=str)
        for issn in df["issn"]:
            counts[issn] = counts.get(issn, 0) + 1
    except Exception:
        pass
    return counts


def determine_publisher_tier(publisher_name):
    """
    Determine which scraping tier a publisher belongs to.
    Returns "http", "playwright", or None if not scrapeable.
    """
    if not publisher_name or (isinstance(publisher_name, float) and pd.isna(publisher_name)):
        return None
    pub = str(publisher_name).strip()

    # Elsevier group → HTTP
    elsevier_names = {
        "Elsevier B.V.", "Elsevier Ltd", "Elsevier Inc.", "Elsevier Ireland Ltd",
        "W.B. Saunders", "W.B. Saunders Ltd", "Academic Press", "Academic Press Inc.",
        "Cell Press",
    }
    if pub in elsevier_names:
        return "http"

    # MDPI → Playwright (Cloudflare blocks HTTP)
    if "MDPI" in pub:
        return "playwright"

    # T&F → Playwright
    tf_names = {"Taylor and Francis Ltd.", "Routledge", "Informa Healthcare", "Informa UK Limited"}
    if pub in tf_names:
        return "playwright"

    # OUP → Playwright
    if pub == "Oxford University Press":
        return "playwright"

    # Frontiers → Playwright
    if pub == "Frontiers Media SA":
        return "playwright"

    return None


def main():
    parser = argparse.ArgumentParser(description="Scrape publisher HTML for review dates")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--email", default="reviewtimedb@example.com",
                        help="Email for Crossref polite pool")
    parser.add_argument("--journal-list", default=str(DEFAULT_JOURNAL_LIST),
                        help="Path to journal list CSV")
    parser.add_argument("--tier", default="all", choices=["1", "2", "all"],
                        help="Which tier to run: 1=HTTP only, 2=Playwright only, all=both")
    args = parser.parse_args()

    # Use tier-specific output/checkpoint files so tiers can run in parallel
    global OUTPUT_FILE, CHECKPOINT_FILE
    if args.tier == "1":
        OUTPUT_FILE = Path("data/scraped_articles_t1.csv")
        CHECKPOINT_FILE = Path("data/scrape_checkpoint_t1.json")
    elif args.tier == "2":
        OUTPUT_FILE = Path("data/scraped_articles_t2.csv")
        CHECKPOINT_FILE = Path("data/scrape_checkpoint_t2.json")
    # else "all" uses the defaults

    journal_list_path = Path(args.journal_list)
    if not journal_list_path.exists():
        print(f"ERROR: {journal_list_path} not found.")
        sys.exit(1)

    # Load journal list
    journals = pd.read_csv(journal_list_path)
    print(f"Loaded {len(journals)} journals from {journal_list_path}")

    # Filter to scrapeable publishers
    journals["scrape_tier"] = journals["publisher"].apply(determine_publisher_tier)
    scrapeable = journals[journals["scrape_tier"].notna()].copy()

    if args.tier == "1":
        scrapeable = scrapeable[scrapeable["scrape_tier"] == "http"]
    elif args.tier == "2":
        scrapeable = scrapeable[scrapeable["scrape_tier"] == "playwright"]

    print(f"  {len(scrapeable)} scrapeable journals (tier filter: {args.tier})")

    http_count = len(scrapeable[scrapeable["scrape_tier"] == "http"])
    pw_count = len(scrapeable[scrapeable["scrape_tier"] == "playwright"])
    print(f"    HTTP: {http_count}, Playwright: {pw_count}")

    # Load checkpoint
    checkpoint = load_checkpoint() if args.resume else {}
    if checkpoint:
        print(f"Resuming: {len(checkpoint)} journals in checkpoint")

    # Load existing DOIs and per-ISSN counts
    existing_dois = load_existing_dois()
    existing_counts = count_existing_articles_per_issn()
    print(f"  {len(existing_dois)} existing DOIs across all sources")

    # Also count articles from other sources (crossref + pubmed) per ISSN
    # to skip journals that already have enough data
    all_source_counts = {}
    for csv_path in [
        Path("data/crossref_articles.csv"), Path("data/crossref_articles_v2.csv"),
        Path("data/pubmed_articles.csv"), Path("data/pubmed_articles_v2.csv"),
    ]:
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path, usecols=["issn"], dtype=str)
                for issn in df["issn"]:
                    all_source_counts[issn] = all_source_counts.get(issn, 0) + 1
            except Exception:
                pass
    # Add scrape counts
    for issn, count in existing_counts.items():
        all_source_counts[issn] = all_source_counts.get(issn, 0) + count

    # Set up output CSV
    write_header = not OUTPUT_FILE.exists()
    csv_file = open(OUTPUT_FILE, "a", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if write_header:
        writer.writeheader()

    session = get_session(args.email)

    # Initialize Playwright browser if needed
    pw_browser = None
    pw_context = None
    if args.tier in ("2", "all") and pw_count > 0:
        try:
            from playwright.sync_api import sync_playwright
            pw_manager = sync_playwright()
            pw = pw_manager.start()
            pw_browser = pw.chromium.launch(
                headless=True,
                args=[
                    '--disable-blink-features=AutomationControlled',
                ]
            )
            pw_context = pw_browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/131.0.0.0 Safari/537.36'
            )
            print("  Playwright browser initialized")
        except Exception as e:
            print(f"  WARNING: Could not initialize Playwright: {e}")
            print(f"  Skipping Playwright-based publishers")
            scrapeable = scrapeable[scrapeable["scrape_tier"] == "http"]

    total_journals = len(scrapeable)
    total_new_articles = 0
    total_new_with_dates = 0
    journals_processed = 0

    # Publisher-level tracking: skip publishers after 3 journals with 0 dates
    PUBLISHER_PROBE_THRESHOLD = 3
    publisher_stats = {}  # {publisher: {"checked": N, "with_data": N}}

    # Pre-populate from checkpoint
    for cp_issn, cp_info in checkpoint.items():
        if cp_info.get("done"):
            cp_row = scrapeable[scrapeable["issn"] == cp_issn]
            if len(cp_row):
                cp_pub = str(cp_row.iloc[0]["publisher"])
                if cp_pub not in publisher_stats:
                    publisher_stats[cp_pub] = {"checked": 0, "with_data": 0}
                publisher_stats[cp_pub]["checked"] += 1
                if cp_info.get("n_with_dates", 0) > 0:
                    publisher_stats[cp_pub]["with_data"] += 1

    try:
        for loop_idx, (_, row) in enumerate(scrapeable.iterrows()):
            issn = str(row["issn"]).strip()
            issn_alt = str(row.get("issn_alt", "")).strip()
            if issn_alt == "nan":
                issn_alt = ""
            journal_name = row["journal_name"]
            field = row["field"]
            publisher = row["publisher"]
            tier = row["scrape_tier"]

            # Check checkpoint
            cp = checkpoint.get(issn, {})
            if cp.get("done", False):
                continue

            # Publisher-level skip: if we've checked 3+ journals from this
            # publisher and none had dates, skip the rest
            pub_key = str(publisher)
            if pub_key not in publisher_stats:
                publisher_stats[pub_key] = {"checked": 0, "with_data": 0}
            ps = publisher_stats[pub_key]
            if ps["checked"] >= PUBLISHER_PROBE_THRESHOLD and ps["with_data"] == 0:
                checkpoint[issn] = {"n_with_dates": 0, "n_checked": 0, "done": True,
                                    "skipped": f"publisher_0pct_after_{ps['checked']}"}
                save_checkpoint(checkpoint)
                continue

            # Check if already have enough from all sources
            total_existing = all_source_counts.get(issn, 0)
            if total_existing >= TARGET_WITH_DATES:
                checkpoint[issn] = {"n_with_dates": total_existing, "n_checked": 0, "done": True}
                save_checkpoint(checkpoint)
                continue

            journals_processed += 1
            n_with_dates = cp.get("n_with_dates", 0)
            n_checked = cp.get("n_checked", 0)
            needed = TARGET_WITH_DATES - total_existing - n_with_dates

            print(f"[{loop_idx+1}/{total_journals}] {journal_name} (ISSN: {issn}, "
                  f"tier={tier}, pub={publisher[:30]})")
            print(f"    need={needed}, checked={n_checked}, existing={total_existing}")

            # Skip Playwright journals if browser not available
            if tier == "playwright" and pw_context is None:
                checkpoint[issn] = {"n_with_dates": 0, "n_checked": 0, "done": True,
                                    "skipped": "no_playwright"}
                save_checkpoint(checkpoint)
                continue

            # Paging through DOIs
            # Start offset past already-collected articles to find new ones
            offset = max(n_checked, total_existing)
            journal_done = False
            consecutive_failures = 0

            while not journal_done and n_checked < MAX_DOIS_CHECKED:
                doi_urls, total_available = fetch_doi_page(
                    session, issn, issn_alt, offset=offset, rows=PAGE_SIZE
                )

                if not doi_urls:
                    print(f"    No more DOIs available (offset={offset})")
                    journal_done = True
                    break

                new_in_page = 0
                for doi, crossref_url in doi_urls:
                    if n_checked >= MAX_DOIS_CHECKED:
                        journal_done = True
                        break
                    if n_with_dates >= needed:
                        journal_done = True
                        break

                    n_checked += 1

                    # Skip already collected
                    if doi in existing_dois:
                        continue

                    new_in_page += 1
                    dates = None

                    try:
                        if tier == "http":
                            # Resolve URL and parse
                            article_url = resolve_doi_url(doi)
                            _, parser_name = get_parser_for_url(article_url)
                            if parser_name:
                                dates = parse_http(article_url, parser_name)
                            time.sleep(HTTP_DELAY)

                        elif tier == "playwright":
                            # Navigate Playwright to the article
                            page = pw_context.new_page()
                            try:
                                article_url = f"https://doi.org/{doi}"
                                page.goto(article_url, wait_until='domcontentloaded',
                                          timeout=30000)
                                page.wait_for_timeout(5000)

                                # Check if we're on a real article page
                                current_url = page.url
                                _, parser_name = get_parser_for_url(current_url)
                                if parser_name:
                                    dates = parse_playwright_page(page, parser_name)
                            except Exception as e:
                                print(f"    Page error for {doi}: {e}")
                            finally:
                                page.close()
                            time.sleep(PLAYWRIGHT_DELAY)

                    except Exception as e:
                        print(f"    ERROR scraping {doi}: {e}")
                        consecutive_failures += 1
                        if consecutive_failures >= 10:
                            print(f"    Too many failures, skipping journal")
                            journal_done = True
                            break
                        continue

                    if dates:
                        consecutive_failures = 0
                        deltas = compute_deltas(dates)

                        record = {
                            "journal_name": journal_name,
                            "issn": issn,
                            "field": field,
                            "doi": doi,
                            "title": "",
                            "received_date": fmt_date(dates.get("received_date")),
                            "accepted_date": fmt_date(dates.get("accepted_date")),
                            "revised_date": fmt_date(dates.get("revised_date")),
                            "published_date": fmt_date(dates.get("published_date")),
                            "days_submission_to_acceptance": deltas["days_submission_to_acceptance"] if deltas["days_submission_to_acceptance"] is not None else "",
                            "days_acceptance_to_publication": deltas["days_acceptance_to_publication"] if deltas["days_acceptance_to_publication"] is not None else "",
                            "days_total": deltas["days_total"] if deltas["days_total"] is not None else "",
                            "data_source": f"scrape_{parser_name}",
                            "n_authors": "",
                            "has_abstract": "",
                        }
                        writer.writerow(record)
                        existing_dois.add(doi)
                        n_with_dates += 1
                        total_new_with_dates += 1
                    else:
                        consecutive_failures += 1

                    total_new_articles += 1

                # If entire page was already collected, no point fetching more
                if new_in_page == 0:
                    print(f"    All {len(doi_urls)} DOIs in page already collected, stopping")
                    journal_done = True

                offset += PAGE_SIZE
                if offset >= total_available:
                    journal_done = True

            csv_file.flush()

            print(f"    Result: {n_with_dates} with dates / {n_checked} checked")

            # Update publisher stats
            publisher_stats[pub_key]["checked"] += 1
            if n_with_dates > 0:
                publisher_stats[pub_key]["with_data"] += 1
            ps = publisher_stats[pub_key]
            if ps["checked"] == PUBLISHER_PROBE_THRESHOLD and ps["with_data"] == 0:
                print(f"    ⚠ Publisher '{pub_key[:40]}' has 0/{ps['checked']} journals with data — skipping rest")

            checkpoint[issn] = {
                "n_with_dates": n_with_dates,
                "n_checked": n_checked,
                "done": True,
            }
            save_checkpoint(checkpoint)

    finally:
        csv_file.close()
        if pw_browser:
            try:
                pw_browser.close()
            except Exception:
                pass

    print(f"\nDone!")
    print(f"  Journals processed: {journals_processed}")
    print(f"  New articles scraped: {total_new_articles}")
    print(f"  Articles with review dates: {total_new_with_dates}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
