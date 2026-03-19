#!/usr/bin/env python3
"""
03_pubmed_collect.py

Collect peer review dates from PubMed for biomedical/life science journals.
PubMed stores received/accepted dates in XML metadata under PubMedPubDate elements.
Coverage is ~55% of PubMed-indexed journals. Only covers biomedical/life science fields.

Strategy per journal:
  1. esearch: search by ISSN to get PMIDs
  2. efetch: get full XML for those PMIDs
  3. Parse received/accepted/epublish dates from XML

Usage:
    python scripts/03_pubmed_collect.py [--resume] [--email EMAIL] [--api-key KEY]
"""

import argparse
import csv
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
ARTICLES_PER_JOURNAL = 50
OUTPUT_FILE = Path("data/pubmed_articles.csv")
CHECKPOINT_FILE = Path("data/pubmed_checkpoint.json")
JOURNAL_LIST = Path("data/journal_list.csv")

# Fields that PubMed is likely to cover
BIOMEDICAL_AREAS = {
    "Medicine", "Biochemistry, Genetics and Molecular Biology",
    "Immunology and Microbiology", "Neuroscience", "Pharmacology, Toxicology and Pharmaceutics",
    "Health Professions", "Nursing", "Dentistry", "Veterinary",
    "Agricultural and Biological Sciences",
}

CSV_FIELDS = [
    "journal_name", "issn", "field", "pmid", "doi", "title",
    "received_date", "accepted_date", "published_date",
    "days_submission_to_acceptance", "days_acceptance_to_publication",
    "days_total", "data_source",
]

# Max delta thresholds
MAX_REVIEW_DAYS = 1095  # 3 years
MAX_PUB_DAYS = 365


def get_session(email, api_key=None):
    """Create a requests session."""
    session = requests.Session()
    session.params = {"email": email, "tool": "ReviewTimeDB"}
    if api_key:
        session.params["api_key"] = api_key
    return session


def search_pmids(session, issn, max_results=ARTICLES_PER_JOURNAL):
    """Search PubMed for recent articles by ISSN."""
    params = {
        "db": "pubmed",
        "term": f"{issn}[journal]",
        "retmax": max_results,
        "sort": "pub_date",
        "retmode": "json",
    }
    try:
        r = session.get(f"{EUTILS_BASE}/esearch.fcgi", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("esearchresult", {}).get("idlist", [])
    except Exception as e:
        print(f"    ERROR searching PMIDs: {e}")
        return []


def fetch_articles_xml(session, pmids):
    """Fetch full XML for a list of PMIDs."""
    if not pmids:
        return None
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "rettype": "xml",
        "retmode": "xml",
    }
    try:
        r = session.get(f"{EUTILS_BASE}/efetch.fcgi", params=params, timeout=60)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"    ERROR fetching XML: {e}")
        return None


def parse_pubmed_date(date_elem):
    """Parse a PubMedPubDate element into a datetime."""
    if date_elem is None:
        return None
    year = date_elem.findtext("Year")
    month = date_elem.findtext("Month")
    day = date_elem.findtext("Day")
    if not year:
        return None
    try:
        y = int(year)
        m = int(month) if month else 1
        d = int(day) if day else 1
        return datetime(y, m, d)
    except (ValueError, TypeError):
        return None


def parse_articles(xml_text):
    """Parse PubMed XML and extract dates for each article.

    Returns list of dicts with keys: pmid, doi, title, received_date,
    accepted_date, published_date.
    """
    if not xml_text:
        return []

    results = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"    XML parse error: {e}")
        return []

    for article in root.findall(".//PubmedArticle"):
        record = {
            "pmid": "",
            "doi": "",
            "title": "",
            "received_date": None,
            "accepted_date": None,
            "published_date": None,
        }

        # PMID
        pmid_elem = article.find(".//PMID")
        if pmid_elem is not None:
            record["pmid"] = pmid_elem.text or ""

        # DOI
        for id_elem in article.findall(".//ArticleId"):
            if id_elem.get("IdType") == "doi":
                record["doi"] = id_elem.text or ""
                break

        # Title
        title_elem = article.find(".//ArticleTitle")
        if title_elem is not None:
            record["title"] = (title_elem.text or "")[:200]

        # Dates from PubMedPubDate elements
        for pub_date in article.findall(".//PubmedData/History/PubMedPubDate"):
            status = pub_date.get("PubStatus", "")
            dt = parse_pubmed_date(pub_date)
            if status == "received":
                record["received_date"] = dt
            elif status == "accepted":
                record["accepted_date"] = dt
            elif status == "epublish":
                record["published_date"] = dt

        # Fallback for published date: use PubDate from article
        if record["published_date"] is None:
            pub_date_elem = article.find(".//Article/Journal/JournalIssue/PubDate")
            if pub_date_elem is not None:
                record["published_date"] = parse_pubmed_date(pub_date_elem)

        results.append(record)

    return results


def compute_deltas(record):
    """Compute day deltas between dates."""
    deltas = {
        "days_submission_to_acceptance": None,
        "days_acceptance_to_publication": None,
        "days_total": None,
    }

    received = record["received_date"]
    accepted = record["accepted_date"]
    published = record["published_date"]

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
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return set(json.load(f))
    return set()


def save_checkpoint(processed):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(sorted(processed), f)


def is_biomedical(areas_str):
    """Check if a journal's areas overlap with PubMed-covered fields."""
    if not areas_str or str(areas_str) == "nan":
        return False
    journal_areas = {a.strip() for a in str(areas_str).split(";")}
    return bool(journal_areas & BIOMEDICAL_AREAS)


def main():
    parser = argparse.ArgumentParser(description="Collect PubMed review dates")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--email", default="reviewtimedb@example.com",
                        help="Email for NCBI E-utilities")
    parser.add_argument("--api-key", default=None,
                        help="NCBI API key for higher rate limit (10/sec vs 3/sec)")
    args = parser.parse_args()

    if not JOURNAL_LIST.exists():
        print(f"ERROR: {JOURNAL_LIST} not found. Run 01_build_journal_list.py first.")
        sys.exit(1)

    import pandas as pd
    journals = pd.read_csv(JOURNAL_LIST)
    print(f"Loaded {len(journals)} journals from {JOURNAL_LIST}")

    # Filter to biomedical journals only
    journals["is_biomed"] = journals["areas"].apply(is_biomedical)
    biomed_journals = journals[journals["is_biomed"]].copy()
    print(f"  {len(biomed_journals)} are in biomedical/life science areas (PubMed-eligible)")

    processed = load_checkpoint() if args.resume else set()
    if processed:
        print(f"Resuming: {len(processed)} journals already processed")

    write_header = not (args.resume and OUTPUT_FILE.exists())
    csv_file = open(OUTPUT_FILE, "a" if args.resume else "w", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if write_header:
        writer.writeheader()

    session = get_session(args.email, args.api_key)
    rate_delay = 0.35 if args.api_key else 1.1  # 10/sec with key, 3/sec without

    total_articles = 0
    total_with_dates = 0
    total_journals = len(biomed_journals)

    for idx, row in biomed_journals.iterrows():
        issn = row["issn"]
        issn_alt = row.get("issn_alt", "")
        if pd.isna(issn_alt):
            issn_alt = ""
        journal_name = row["journal_name"]
        field = row["field"]

        if issn in processed:
            continue

        print(f"[{idx+1}/{total_journals}] {journal_name} (ISSN: {issn})")

        # Search for PMIDs — try both ISSNs
        pmids = search_pmids(session, issn)
        if not pmids and issn_alt:
            pmids = search_pmids(session, issn_alt)
        time.sleep(rate_delay)

        if not pmids:
            print(f"    No PubMed articles found")
            processed.add(issn)
            save_checkpoint(processed)
            continue

        print(f"    Found {len(pmids)} PMIDs, fetching metadata...")

        # Fetch XML in one batch
        xml_text = fetch_articles_xml(session, pmids)
        time.sleep(rate_delay)

        if not xml_text:
            processed.add(issn)
            save_checkpoint(processed)
            continue

        articles = parse_articles(xml_text)
        journal_with_dates = 0

        for article in articles:
            has_review_data = article["received_date"] is not None and article["accepted_date"] is not None
            if not has_review_data:
                continue

            deltas = compute_deltas(article)
            record = {
                "journal_name": journal_name,
                "issn": issn,
                "field": field,
                "pmid": article["pmid"],
                "doi": article["doi"],
                "title": article["title"],
                "received_date": article["received_date"].strftime("%Y-%m-%d"),
                "accepted_date": article["accepted_date"].strftime("%Y-%m-%d"),
                "published_date": article["published_date"].strftime("%Y-%m-%d") if article["published_date"] else "",
                "days_submission_to_acceptance": deltas["days_submission_to_acceptance"] if deltas["days_submission_to_acceptance"] is not None else "",
                "days_acceptance_to_publication": deltas["days_acceptance_to_publication"] if deltas["days_acceptance_to_publication"] is not None else "",
                "days_total": deltas["days_total"] if deltas["days_total"] is not None else "",
                "data_source": "pubmed",
            }
            writer.writerow(record)
            journal_with_dates += 1

        csv_file.flush()
        total_articles += len(articles)
        total_with_dates += journal_with_dates

        print(f"    Processed {len(articles)} articles, {journal_with_dates} with review dates")

        processed.add(issn)
        save_checkpoint(processed)

    csv_file.close()

    print(f"\nDone!")
    print(f"  Total articles processed: {total_articles}")
    print(f"  Articles with review dates: {total_with_dates}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
