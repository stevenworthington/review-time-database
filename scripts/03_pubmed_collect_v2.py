#!/usr/bin/env python3
"""
03_pubmed_collect_v2.py

Collect peer review dates from PubMed for journals in journal_list_full.csv.
Expanded version targeting ~29,500 journals with richer metadata extraction.

Key design:
  - Uses query_pubmed boolean column (not hardcoded field list)
  - Targets 100 articles WITH review dates per journal
  - Fetches up to 200 PMIDs per search, pages if needed
  - APPENDs to existing pubmed_articles.csv (preserves Phase 1 data)
  - Extracts extended metadata: revision_date, pub_types, grant_agencies,
    keywords, mesh_terms, pmc_release_date, etc.

Usage:
    python scripts/03_pubmed_collect_v2.py [--resume] [--email EMAIL]
                                            [--api-key KEY] [--journal-list PATH]
"""

import argparse
import csv
import json
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
EUTILS_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
TARGET_WITH_DATES = 100     # Stop after this many articles with review dates
SEARCH_BATCH = 200          # PMIDs per esearch call
FETCH_BATCH = 200           # PMIDs per efetch call
OUTPUT_FILE = Path("data/pubmed_articles_v2.csv")
CHECKPOINT_FILE = Path("data/pubmed_checkpoint_v2.json")
PHASE1_FILE = Path("data/pubmed_articles.csv")  # original Phase 1 data
DEFAULT_JOURNAL_LIST = Path("data/journal_list_full.csv")

CSV_FIELDS = [
    "journal_name", "issn", "field", "pmid", "doi", "title",
    "received_date", "accepted_date", "revised_date", "published_date",
    "days_submission_to_acceptance", "days_acceptance_to_publication",
    "days_total", "data_source",
    "n_authors", "pub_types", "grant_agencies",
    "has_abstract", "keywords", "mesh_terms", "pmc_release_date",
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


def search_pmids(session, issn, retmax=SEARCH_BATCH, retstart=0):
    """Search PubMed for recent articles by ISSN."""
    params = {
        "db": "pubmed",
        "term": f"{issn}[journal]",
        "retmax": retmax,
        "retstart": retstart,
        "sort": "pub_date",
        "retmode": "json",
    }
    try:
        r = session.get(f"{EUTILS_BASE}/esearch.fcgi", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        result = data.get("esearchresult", {})
        total_count = int(result.get("count", 0))
        pmids = result.get("idlist", [])
        return pmids, total_count
    except Exception as e:
        print(f"    ERROR searching PMIDs: {e}")
        return [], 0


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
    """Parse a PubMedPubDate or PubDate element into a datetime."""
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


def fmt_date(dt):
    """Format a datetime as YYYY-MM-DD string, or empty string if None."""
    return dt.strftime("%Y-%m-%d") if dt else ""


def parse_articles(xml_text):
    """Parse PubMed XML and extract dates and extended metadata for each article.

    Returns list of dicts with all fields needed for CSV output.
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
            "revised_date": None,
            "published_date": None,
            "pmc_release_date": None,
            "n_authors": "",
            "pub_types": "",
            "grant_agencies": "",
            "has_abstract": "0",
            "keywords": "",
            "mesh_terms": "",
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
            # ArticleTitle may have mixed content (child tags like <i>)
            record["title"] = ("".join(title_elem.itertext()) or "")[:200]

        # Dates from PubMedPubDate elements
        for pub_date in article.findall(".//PubmedData/History/PubMedPubDate"):
            status = pub_date.get("PubStatus", "")
            dt = parse_pubmed_date(pub_date)
            if status == "received":
                record["received_date"] = dt
            elif status == "accepted":
                record["accepted_date"] = dt
            elif status == "revised":
                record["revised_date"] = dt
            elif status == "epublish":
                record["published_date"] = dt
            elif status == "pmc-release":
                record["pmc_release_date"] = dt

        # Fallback for published date: use PubDate from journal issue
        if record["published_date"] is None:
            pub_date_elem = article.find(".//Article/Journal/JournalIssue/PubDate")
            if pub_date_elem is not None:
                record["published_date"] = parse_pubmed_date(pub_date_elem)

        # Number of authors
        author_list = article.find(".//AuthorList")
        if author_list is not None:
            authors = author_list.findall("Author")
            record["n_authors"] = len(authors) if authors else ""

        # Publication types
        pub_type_elems = article.findall(".//PublicationTypeList/PublicationType")
        if pub_type_elems:
            pub_types = [pt.text for pt in pub_type_elems if pt.text]
            record["pub_types"] = "; ".join(pub_types)

        # Grant agencies (unique)
        grant_elems = article.findall(".//GrantList/Grant")
        if grant_elems:
            agencies = []
            seen = set()
            for grant in grant_elems:
                agency_elem = grant.find("Agency")
                if agency_elem is not None and agency_elem.text:
                    ag = agency_elem.text.strip()
                    if ag not in seen:
                        agencies.append(ag)
                        seen.add(ag)
            record["grant_agencies"] = "; ".join(agencies)

        # Has abstract
        abstract_elem = article.find(".//Abstract")
        record["has_abstract"] = "1" if abstract_elem is not None else "0"

        # Keywords
        keyword_elems = article.findall(".//KeywordList/Keyword")
        if keyword_elems:
            kws = [kw.text for kw in keyword_elems if kw.text]
            record["keywords"] = "; ".join(kws)

        # MeSH terms
        mesh_elems = article.findall(".//MeshHeadingList/MeshHeading/DescriptorName")
        if mesh_elems:
            terms = [m.text for m in mesh_elems if m.text]
            record["mesh_terms"] = "; ".join(terms)

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
    """Load checkpoint dict: {issn: {n_with_dates, n_fetched, done}}."""
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


def load_existing_pmids(output_file):
    """Load set of PMIDs already in the output CSV to avoid duplicates."""
    existing = set()
    if not output_file.exists():
        return existing
    try:
        df = pd.read_csv(output_file, usecols=["pmid"], dtype=str)
        existing = set(df["pmid"].dropna())
    except Exception as e:
        print(f"  Warning: could not read existing PMIDs from {output_file}: {e}")
    return existing


def main():
    parser = argparse.ArgumentParser(description="Collect PubMed review dates (v2)")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--email", default="reviewtimedb@example.com",
                        help="Email for NCBI E-utilities")
    parser.add_argument("--api-key", default=None,
                        help="NCBI API key for higher rate limit (10/sec vs 3/sec)")
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

    # Filter to query_pubmed == True
    if "query_pubmed" in journals.columns:
        journals = journals[journals["query_pubmed"].astype(str).str.strip().str.lower() == "true"].copy()
        print(f"  {len(journals)} journals with query_pubmed=True")

    # Load checkpoint if resuming
    checkpoint = load_checkpoint() if args.resume else {}
    if checkpoint:
        print(f"Resuming: {len(checkpoint)} journals in checkpoint")

    # Count existing articles per ISSN (from both Phase 1 and v2 files)
    existing_counts = count_existing_articles(PHASE1_FILE)
    existing_counts_v2 = count_existing_articles(OUTPUT_FILE)
    for issn, count in existing_counts_v2.items():
        existing_counts[issn] = existing_counts.get(issn, 0) + count
    existing_pmids = load_existing_pmids(PHASE1_FILE)
    existing_pmids.update(load_existing_pmids(OUTPUT_FILE))
    if existing_counts:
        print(f"  {sum(existing_counts.values())} existing articles across {len(existing_counts)} journals")

    # Set up output CSV — always append, write header only if file doesn't exist
    write_header = not OUTPUT_FILE.exists()
    csv_file = open(OUTPUT_FILE, "a", newline="")
    writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
    if write_header:
        writer.writeheader()

    session = get_session(args.email, args.api_key)
    rate_delay = 0.35 if args.api_key else 1.1  # 10/sec with key, 3/sec without

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

        # Check checkpoint — skip if done
        cp = checkpoint.get(issn, {})
        if cp.get("done", False):
            continue

        # Check if already have enough from existing data
        existing_n = existing_counts.get(issn, 0)
        if existing_n >= TARGET_WITH_DATES:
            checkpoint[issn] = {"n_with_dates": existing_n, "n_fetched": existing_n, "done": True}
            save_checkpoint(checkpoint)
            continue

        journals_processed_this_run += 1
        n_with_dates = existing_n
        needed = TARGET_WITH_DATES - n_with_dates

        print(f"[{loop_idx+1}/{total_journals}] {journal_name} (ISSN: {issn})"
              f"  need={needed}")

        # Search for PMIDs — try both ISSNs
        pmids, total_count = search_pmids(session, issn, retmax=SEARCH_BATCH)
        if not pmids and issn_alt:
            pmids, total_count = search_pmids(session, issn_alt, retmax=SEARCH_BATCH)
        time.sleep(rate_delay)

        if not pmids:
            print(f"    No PubMed articles found")
            checkpoint[issn] = {"n_with_dates": 0, "n_fetched": 0, "done": True}
            save_checkpoint(checkpoint)
            continue

        print(f"    Found {len(pmids)} PMIDs (total available: {total_count})")

        # Filter out already-collected PMIDs
        pmids = [p for p in pmids if p not in existing_pmids]
        if not pmids:
            print(f"    All PMIDs already collected")
            checkpoint[issn] = {"n_with_dates": n_with_dates, "n_fetched": 0, "done": True}
            save_checkpoint(checkpoint)
            continue

        # Fetch XML in batches
        journal_new_with_dates = 0
        journal_new_articles = 0

        for batch_start in range(0, len(pmids), FETCH_BATCH):
            if n_with_dates >= TARGET_WITH_DATES:
                break

            batch = pmids[batch_start:batch_start + FETCH_BATCH]
            xml_text = fetch_articles_xml(session, batch)
            time.sleep(rate_delay)

            if not xml_text:
                continue

            articles = parse_articles(xml_text)

            for art in articles:
                if n_with_dates >= TARGET_WITH_DATES:
                    break

                # Skip duplicates
                if art["pmid"] in existing_pmids:
                    continue

                has_review_data = (art["received_date"] is not None
                                   and art["accepted_date"] is not None)
                if not has_review_data:
                    continue

                deltas = compute_deltas(art)
                csv_record = {
                    "journal_name": journal_name,
                    "issn": issn,
                    "field": field,
                    "pmid": art["pmid"],
                    "doi": art["doi"],
                    "title": art["title"],
                    "received_date": fmt_date(art["received_date"]),
                    "accepted_date": fmt_date(art["accepted_date"]),
                    "revised_date": fmt_date(art["revised_date"]),
                    "published_date": fmt_date(art["published_date"]),
                    "days_submission_to_acceptance": deltas["days_submission_to_acceptance"] if deltas["days_submission_to_acceptance"] is not None else "",
                    "days_acceptance_to_publication": deltas["days_acceptance_to_publication"] if deltas["days_acceptance_to_publication"] is not None else "",
                    "days_total": deltas["days_total"] if deltas["days_total"] is not None else "",
                    "data_source": "pubmed",
                    "n_authors": art["n_authors"],
                    "pub_types": art["pub_types"],
                    "grant_agencies": art["grant_agencies"],
                    "has_abstract": art["has_abstract"],
                    "keywords": art["keywords"],
                    "mesh_terms": art["mesh_terms"],
                    "pmc_release_date": fmt_date(art["pmc_release_date"]),
                }
                writer.writerow(csv_record)
                existing_pmids.add(art["pmid"])
                n_with_dates += 1
                journal_new_with_dates += 1
                journal_new_articles += 1

        csv_file.flush()
        total_new_articles += journal_new_articles
        total_new_with_dates += journal_new_with_dates

        print(f"    Result: {n_with_dates} total with dates ({journal_new_with_dates} new this run)")

        checkpoint[issn] = {
            "n_with_dates": journal_new_with_dates,
            "n_fetched": len(pmids),
            "done": True,
        }
        save_checkpoint(checkpoint)

    csv_file.close()

    print(f"\nDone!")
    print(f"  Journals processed this run: {journals_processed_this_run}")
    print(f"  New articles with review dates: {total_new_with_dates}")
    print(f"  Output: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
