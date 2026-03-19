#!/usr/bin/env python3
"""
04_merge_and_summarize.py

Merge Crossref and PubMed article data, compute journal-level and field-level
summaries, and generate coverage reports.

Priority for dates: PubMed > Crossref assertion > Crossref accepted field.
When both sources have data for the same article (matched by DOI), PubMed wins.

Outputs:
  - data/journal_summary.csv — one row per journal with median timelines
  - data/field_summary.csv — one row per field with aggregated stats
  - data/coverage_report.csv — gap analysis by field and publisher
  - outputs/review_times.xlsx — formatted spreadsheet with all sheets
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

DATA_DIR = Path("data")
OUTPUT_DIR = Path("outputs")


def load_csv_sources(name, paths):
    """Load article data from one or more CSV files."""
    frames = []
    total = 0
    for path in paths:
        p = DATA_DIR / path
        if p.exists():
            try:
                df = pd.read_csv(p)
                frames.append(df)
                total += len(df)
            except Exception as e:
                print(f"  Warning: could not read {p}: {e}")
    if frames:
        combined = pd.concat(frames, ignore_index=True)
        print(f"  {name}: {total} articles with review dates (from {len(frames)} file(s))")
        return combined
    else:
        print(f"  No {name} data found — skipping")
        return pd.DataFrame()


def load_crossref():
    """Load Crossref article data from v1 and v2 files."""
    return load_csv_sources("Crossref", [
        "crossref_articles.csv",
        "crossref_articles_v2.csv",
    ])


def load_pubmed():
    """Load PubMed article data from v1 and v2 files."""
    return load_csv_sources("PubMed", [
        "pubmed_articles.csv",
        "pubmed_articles_v2.csv",
    ])


def load_scraped():
    """Load scraped article data."""
    return load_csv_sources("Scraped", ["scraped_articles.csv"])


def merge_sources(crossref_df, pubmed_df, scraped_df=None):
    """
    Merge all data sources. Priority: PubMed > Crossref > Scraped.
    When multiple sources have the same DOI, higher-priority source wins.
    """
    if scraped_df is None:
        scraped_df = pd.DataFrame()

    all_frames = [pubmed_df, crossref_df, scraped_df]
    non_empty = [df for df in all_frames if not df.empty]

    if not non_empty:
        print("ERROR: No data from any source!")
        sys.exit(1)

    if len(non_empty) == 1:
        return non_empty[0]

    # Deduplicate by DOI, keeping highest-priority source (PubMed > Crossref > Scraped)
    # Since we concat in priority order and drop duplicates keeping first, PubMed wins
    frames_to_merge = []
    seen_dois = set()

    for df in [pubmed_df, crossref_df, scraped_df]:
        if df.empty:
            continue
        df = df.copy()
        df["doi_lower"] = df["doi"].astype(str).str.lower().str.strip()

        # Keep only DOIs not seen in higher-priority sources
        unique = df[~df["doi_lower"].isin(seen_dois)]
        seen_dois.update(unique["doi_lower"].dropna())
        frames_to_merge.append(unique)

    merged = pd.concat(frames_to_merge, ignore_index=True)

    # Standardize: ensure 'pmid' column exists
    if "pmid" not in merged.columns:
        merged["pmid"] = ""

    # Clean up
    if "doi_lower" in merged.columns:
        merged.drop(columns=["doi_lower"], inplace=True)

    source_counts = merged["data_source"].value_counts()
    print(f"  After dedup: {len(merged)} total articles")
    for source, count in source_counts.items():
        print(f"    {source}: {count}")

    return merged


def compute_journal_summary(merged_df, journal_list_df):
    """Compute per-journal summary statistics."""
    # Convert days columns to numeric
    for col in ["days_submission_to_acceptance", "days_acceptance_to_publication", "days_total"]:
        merged_df[col] = pd.to_numeric(merged_df[col], errors="coerce")

    # Group by journal
    grouped = merged_df.groupby(["journal_name", "issn", "field"])

    summaries = []
    for (journal, issn, field), group in grouped:
        review_days = group["days_submission_to_acceptance"].dropna()
        pub_days = group["days_acceptance_to_publication"].dropna()
        total_days = group["days_total"].dropna()

        summary = {
            "journal_name": journal,
            "issn": issn,
            "field": field,
            "n_articles": len(group),
            "n_with_review_time": len(review_days),
            "median_days_submission_to_acceptance": review_days.median() if len(review_days) > 0 else None,
            "p25_review": review_days.quantile(0.25) if len(review_days) > 0 else None,
            "p75_review": review_days.quantile(0.75) if len(review_days) > 0 else None,
            "mean_days_submission_to_acceptance": review_days.mean() if len(review_days) > 0 else None,
            "median_days_acceptance_to_publication": pub_days.median() if len(pub_days) > 0 else None,
            "median_days_total": total_days.median() if len(total_days) > 0 else None,
            "data_sources": ", ".join(sorted(group["data_source"].unique())),
        }
        summaries.append(summary)

    summary_df = pd.DataFrame(summaries)

    # Merge in publisher info from journal list
    if not journal_list_df.empty:
        journal_info = journal_list_df[["issn", "publisher", "sjr_rank", "sjr_score"]].drop_duplicates(subset="issn")
        summary_df = summary_df.merge(journal_info, on="issn", how="left")

    return summary_df.sort_values(["field", "median_days_submission_to_acceptance"],
                                   ascending=[True, False], na_position="last")


def compute_field_summary(journal_summary_df):
    """Compute per-field aggregated statistics."""
    summaries = []

    for field, group in journal_summary_df.groupby("field"):
        with_data = group[group["n_with_review_time"] > 0]
        medians = with_data["median_days_submission_to_acceptance"].dropna()

        summary = {
            "field": field,
            "n_journals_total": len(group),
            "n_journals_with_data": len(with_data),
            "coverage_pct": round(len(with_data) / len(group) * 100, 1) if len(group) > 0 else 0,
            "field_median_review_days": medians.median() if len(medians) > 0 else None,
            "field_p25_review_days": medians.quantile(0.25) if len(medians) > 0 else None,
            "field_p75_review_days": medians.quantile(0.75) if len(medians) > 0 else None,
            "field_mean_review_days": medians.mean() if len(medians) > 0 else None,
            "slowest_journal": with_data.loc[medians.idxmax(), "journal_name"] if len(medians) > 0 else None,
            "fastest_journal": with_data.loc[medians.idxmin(), "journal_name"] if len(medians) > 0 else None,
        }
        summaries.append(summary)

    return pd.DataFrame(summaries).sort_values("field_median_review_days",
                                                ascending=False, na_position="last")


def compute_coverage_report(journal_list_df, journal_summary_df):
    """Generate coverage gap analysis by field and publisher."""
    all_issns = set(journal_list_df["issn"])
    covered_issns = set(journal_summary_df[journal_summary_df["n_with_review_time"] > 0]["issn"])
    missing_issns = all_issns - covered_issns

    missing_journals = journal_list_df[journal_list_df["issn"].isin(missing_issns)].copy()

    # By field
    field_coverage = []
    for field, group in journal_list_df.groupby("field"):
        total = len(group)
        covered = len(group[group["issn"].isin(covered_issns)])
        field_coverage.append({
            "field": field,
            "total_journals": total,
            "journals_with_data": covered,
            "journals_missing": total - covered,
            "coverage_pct": round(covered / total * 100, 1),
        })

    # By publisher
    publisher_coverage = []
    for pub, group in journal_list_df.groupby("publisher"):
        total = len(group)
        covered = len(group[group["issn"].isin(covered_issns)])
        if total >= 3:  # Only show publishers with 3+ journals
            publisher_coverage.append({
                "publisher": pub,
                "total_journals": total,
                "journals_with_data": covered,
                "journals_missing": total - covered,
                "coverage_pct": round(covered / total * 100, 1),
            })

    field_df = pd.DataFrame(field_coverage).sort_values("coverage_pct")
    publisher_df = pd.DataFrame(publisher_coverage).sort_values("coverage_pct")

    return field_df, publisher_df, missing_journals


def create_excel(journal_summary, field_summary, field_coverage, publisher_coverage, missing_journals):
    """Create formatted Excel workbook with all results."""
    out_path = OUTPUT_DIR / "review_times.xlsx"
    OUTPUT_DIR.mkdir(exist_ok=True)

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        # Field summary — the main deliverable
        field_summary.to_excel(writer, sheet_name="Fields", index=False)

        # Journal-level detail
        journal_summary.to_excel(writer, sheet_name="Journals", index=False)

        # Coverage by field
        field_coverage.to_excel(writer, sheet_name="Coverage by Field", index=False)

        # Coverage by publisher
        publisher_coverage.to_excel(writer, sheet_name="Coverage by Publisher", index=False)

        # Missing journals
        missing_journals[["field", "journal_name", "issn", "publisher"]].to_excel(
            writer, sheet_name="Missing Journals", index=False)

    print(f"  Excel output: {out_path}")


def main():
    print("Loading data sources...")
    crossref_df = load_crossref()
    pubmed_df = load_pubmed()
    scraped_df = load_scraped()

    print("\nMerging sources...")
    merged = merge_sources(crossref_df, pubmed_df, scraped_df)

    print("\nLoading journal list...")
    # Prefer full journal list if it exists
    if (DATA_DIR / "journal_list_full.csv").exists():
        journal_list = pd.read_csv(DATA_DIR / "journal_list_full.csv")
    else:
        journal_list = pd.read_csv(DATA_DIR / "journal_list.csv")
    print(f"  {len(journal_list)} journals in master list")

    print("\nComputing journal summaries...")
    journal_summary = compute_journal_summary(merged, journal_list)
    journal_summary.to_csv(DATA_DIR / "journal_summary.csv", index=False)
    print(f"  {len(journal_summary)} journals with data → data/journal_summary.csv")

    print("\nComputing field summaries...")
    field_summary = compute_field_summary(journal_summary)
    field_summary.to_csv(DATA_DIR / "field_summary.csv", index=False)
    print(f"  {len(field_summary)} fields → data/field_summary.csv")

    # Top 10 slowest fields
    print("\n  Top 10 slowest fields (median review days):")
    top10 = field_summary[field_summary["field_median_review_days"].notna()].head(10)
    for _, row in top10.iterrows():
        print(f"    {row['field']}: {row['field_median_review_days']:.0f} days "
              f"({row['n_journals_with_data']}/{row['n_journals_total']} journals covered)")

    print("\nComputing coverage report...")
    field_coverage, publisher_coverage, missing = compute_coverage_report(journal_list, journal_summary)
    field_coverage.to_csv(DATA_DIR / "coverage_report.csv", index=False)

    overall_covered = journal_summary[journal_summary["n_with_review_time"] > 0]
    print(f"  Overall coverage: {len(overall_covered)}/{len(journal_list)} journals "
          f"({len(overall_covered)/len(journal_list)*100:.1f}%)")

    print("\nCreating Excel output...")
    create_excel(journal_summary, field_summary, field_coverage, publisher_coverage, missing)

    print("\nDone!")


if __name__ == "__main__":
    main()
