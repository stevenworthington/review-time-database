#!/usr/bin/env python3
"""
09_scielo_collect.py

Scrape peer review dates from SciELO article pages (Latin American journals).

SciELO article pages contain dates in a "History" section:
    <listitem>05 June 2023<generic>Received</generic></listitem>
    <listitem>07 June 2024<generic>Accepted</generic></listitem>

Also checks for Portuguese labels: Recebido, Aceito, Aprovado, Revisado.

Strategy per journal:
  1. Fetch DOIs from Crossref bulk ISSN endpoint
  2. Resolve DOI to SciELO article URL
  3. Scrape article page for received/accepted dates
  4. Stop when 100 articles with dates, OR no more DOIs

Usage:
    python scripts/09_scielo_collect.py [--resume]
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
OUTPUT_FILE = Path("data/scielo_articles.csv")
CHECKPOINT_FILE = Path("data/scielo_checkpoint.json")
DEFAULT_JOURNAL_LIST = Path("data/journal_list_full.csv")

# Rate limiting
HTTP_DELAY = 1.0  # Be polite to SciELO
CROSSREF_DELAY = 0.1

# Max delta thresholds
MAX_REVIEW_DAYS = 365 * 3
MAX_PUB_DAYS = 365

CSV_FIELDS = [
    "journal_name", "issn", "field", "doi", "title",
    "received_date", "accepted_date", "revised_date", "published_date",
    "days_submission_to_acceptance", "days_acceptance_to_publication",
    "days_total", "data_source",
]

# Date labels in English, Portuguese, and Spanish
RECEIVED_LABELS = ["received", "recebido", "recibido"]
ACCEPTED_LABELS = ["accepted", "aceito", "aprovado", "aceptado", "aprobado"]
REVISED_LABELS = ["revised", "revisado"]
PUBLISHED_LABELS = ["published", "publicado", "publication in this collection"]

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

    # Portuguese month names mapping
    pt_months = {
        "janeiro": "January", "fevereiro": "February", "março": "March",
        "marco": "March", "abril": "April", "maio": "May", "junho": "June",
        "julho": "July", "agosto": "August", "setembro": "September",
        "outubro": "October", "novembro": "November", "dezembro": "December",
    }
    # Spanish month names
    es_months = {
        "enero": "January", "febrero": "February", "marzo": "March",
        "abril": "April", "mayo": "May", "junio": "June",
        "julio": "July", "agosto": "August", "septiembre": "September",
        "octubre": "October", "noviembre": "November", "diciembre": "December",
    }

    date_lower = date_str.lower()
    for month_map in [pt_months, es_months]:
        for foreign, english in month_map.items():
            if foreign in date_lower:
                date_str = re.sub(foreign, english, date_str, flags=re.IGNORECASE)
                break

    for fmt in [
        "%d %B %Y",        # 05 June 2023
        "%d %b %Y",        # 05 Jun 2023
        "%B %d, %Y",       # June 05, 2023
        "%b %d, %Y",       # Jun 05, 2023
        "%d/%m/%Y",        # 05/06/2023
        "%Y-%m-%d",        # 2023-06-05
        "%d %B, %Y",       # 05 June, 2023
    ]:
        try:
            return datetime.strptime(date_str.strip(), fmt)
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


def scrape_scielo_dates(doi):
    """Resolve DOI to SciELO page and extract review dates."""
    try:
        # Resolve DOI
        r = SESSION.head(f"https://doi.org/{doi}", allow_redirects=True, timeout=15)
        url = r.url

        if "scielo" not in url.lower():
            return None

        # Prefer English version
        if "lang=pt" in url:
            url = url.replace("lang=pt", "lang=en")
        elif "lang=es" in url:
            url = url.replace("lang=es", "lang=en")
        elif "lang=" not in url:
            sep = "&" if "?" in url else "?"
            url = url + sep + "lang=en"

        # Fetch article page
        r = SESSION.get(url, timeout=15)
        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "lxml")
        dates = {}

        # Method 1: Look for <listitem> elements with <generic> labels
        # This is SciELO's custom XML-in-HTML format
        for listitem in soup.find_all("listitem"):
            generic = listitem.find("generic")
            if not generic:
                continue

            label = generic.get_text(strip=True).lower()
            # Get date text (text before the generic element)
            date_text = listitem.get_text(strip=True).replace(generic.get_text(strip=True), "").strip()

            dt = parse_date(date_text)
            if not dt:
                continue

            if any(l in label for l in RECEIVED_LABELS):
                dates["received"] = dt
            elif any(l in label for l in ACCEPTED_LABELS):
                dates["accepted"] = dt
            elif any(l in label for l in REVISED_LABELS):
                dates["revised"] = dt
            elif any(l in label for l in PUBLISHED_LABELS):
                dates["published"] = dt

        # Method 2: Fallback — look for text patterns in the page
        if not dates.get("received") and not dates.get("accepted"):
            text = soup.get_text()
            for pattern_set, key in [
                (RECEIVED_LABELS, "received"),
                (ACCEPTED_LABELS, "accepted"),
                (REVISED_LABELS, "revised"),
            ]:
                for label in pattern_set:
                    match = re.search(
                        rf'{label}[:\s]+(\d{{1,2}}\s+\w+\s+\d{{4}}|\w+\s+\d{{1,2}},?\s+\d{{4}})',
                        text, re.IGNORECASE
                    )
                    if match:
                        dt = parse_date(match.group(1))
                        if dt:
                            dates[key] = dt
                            break

        if dates.get("received") or dates.get("accepted"):
            return dates
        return None

    except Exception:
        return None


def get_scielo_journals(journal_list_path):
    """Get journals that are likely on SciELO from the journal list."""
    jl = pd.read_csv(journal_list_path)
    jl["publisher"] = jl["publisher"].fillna("")

    # Latin American / SciELO publishers
    keywords = [
        "scielo", "brazil", "brasil", "mexico", "méxico", "argentina",
        "chile", "colombia", "peru", "perú", "venezuela", "cuba",
        "uruguay", "ecuador", "bolivia", "fiocruz", "usp", "unicamp",
        "unesp", "fapesp", "conicet", "unam", "latin",
        "ibero", "costa rica", "panama", "panamá",
        "sociedad", "sociedade", "asociación", "associação",
        "revista", "fundação", "fundación", "instituto",
        "universidade", "universidad",
    ]

    mask = jl["publisher"].apply(
        lambda p: any(k in str(p).lower() for k in keywords)
    )
    scielo_journals = jl[mask].copy()
    return scielo_journals


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
        Path("data/jstage_articles.csv"),
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
    parser = argparse.ArgumentParser(description="Scrape SciELO for review dates")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--journal-list", default=str(DEFAULT_JOURNAL_LIST))
    args = parser.parse_args()

    print("SciELO Review Date Collector")
    print("=" * 50)

    # Get SciELO journals
    journals = get_scielo_journals(args.journal_list)
    print(f"Latin American journals in list: {len(journals)}")

    # Load checkpoint
    checkpoint = load_checkpoint() if args.resume else {}
    if checkpoint:
        print(f"Resuming: {len(checkpoint)} journals in checkpoint")

    # Load existing DOIs
    existing_dois = load_existing_dois()
    print(f"Existing DOIs across all sources: {len(existing_dois)}")

    # Publisher-level skip after 3 journals with 0 dates
    PUBLISHER_PROBE_THRESHOLD = 3
    publisher_stats = {}
    for cp_issn, cp_info in checkpoint.items():
        if cp_info.get("done"):
            cp_row = journals[journals["issn"] == cp_issn]
            if len(cp_row):
                cp_pub = str(cp_row.iloc[0]["publisher"])
                if cp_pub not in publisher_stats:
                    publisher_stats[cp_pub] = {"checked": 0, "with_data": 0}
                publisher_stats[cp_pub]["checked"] += 1
                if cp_info.get("n_with_dates", 0) > 0:
                    publisher_stats[cp_pub]["with_data"] += 1

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

            # Publisher-level skip
            pub_key = publisher
            if pub_key not in publisher_stats:
                publisher_stats[pub_key] = {"checked": 0, "with_data": 0}
            ps = publisher_stats[pub_key]
            if ps["checked"] >= PUBLISHER_PROBE_THRESHOLD and ps["with_data"] == 0:
                checkpoint[issn] = {"n_with_dates": 0, "n_checked": 0, "done": True,
                                    "skipped": f"publisher_0pct_after_{ps['checked']}"}
                save_checkpoint(checkpoint)
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

                    # Scrape SciELO page
                    dates = scrape_scielo_dates(doi)
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
                            "data_source": "scielo",
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

            # Update publisher stats
            publisher_stats[pub_key]["checked"] += 1
            if n_with_dates > 0:
                publisher_stats[pub_key]["with_data"] += 1
            ps = publisher_stats[pub_key]
            if ps["checked"] == PUBLISHER_PROBE_THRESHOLD and ps["with_data"] == 0:
                print(f"    ⚠ Publisher '{pub_key[:40]}' 0/{ps['checked']} — skipping rest")

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
