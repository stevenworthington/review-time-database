#!/usr/bin/env python3
"""
06_build_dashboard.py

Generate an interactive Plotly.js dashboard (v2) from review time data.
Pre-computes all aggregations in Python, serializes as JSON, and renders
a single-file HTML dashboard via Jinja2 template.

Outputs:
    docs/index.html      — v2 dashboard
    docs/index_v1.html   — backup of previous dashboard (if it existed)

Usage:
    python scripts/06_build_dashboard.py

Prerequisites:
    - Run scripts 01-04 first (or this script will run 04 automatically)
    - pip install pandas numpy scipy jinja2
"""

import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from jinja2 import Environment, FileSystemLoader
from scipy.stats import gaussian_kde

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
TEMPLATE_DIR = ROOT / "templates"


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def ensure_summaries():
    """Run script 04 if summary CSVs don't exist."""
    needed = [DATA_DIR / "journal_summary.csv", DATA_DIR / "field_summary.csv"]
    if all(f.exists() for f in needed):
        print("  Summary CSVs already exist")
        return
    print("  Running 04_merge_and_summarize.py ...")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "04_merge_and_summarize.py")],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ERROR running script 04:\n{result.stderr}")
        sys.exit(1)
    print(result.stdout)


def load_data():
    """Load all required data files."""
    field_summary = pd.read_csv(DATA_DIR / "field_summary.csv")
    journal_summary = pd.read_csv(DATA_DIR / "journal_summary.csv")

    # Load article-level data from all sources
    article_files = [
        "crossref_articles.csv",
        "crossref_articles_v2.csv",
        "pubmed_articles.csv",
        "pubmed_articles_v2.csv",
        "frontiers_articles.csv",
        "jstage_articles.csv",
        "scielo_articles.csv",
        "scraped_articles.csv",
        "scraped_articles_t1.csv",
        "scraped_articles_t2.csv",
    ]

    dfs = []
    for fname in article_files:
        path = DATA_DIR / fname
        if path.exists():
            try:
                df = pd.read_csv(path, low_memory=False)
                dfs.append(df)
            except Exception as e:
                print(f"  Warning: could not load {fname}: {e}")

    if dfs:
        articles = pd.concat(dfs, ignore_index=True)
        # Ensure required columns exist
        for col in ["days_submission_to_acceptance", "days_acceptance_to_publication"]:
            if col in articles.columns:
                articles[col] = pd.to_numeric(articles[col], errors="coerce")
            else:
                articles[col] = np.nan
        # Filter bogus values
        articles = articles[
            (articles["days_submission_to_acceptance"] > 0)
            & (articles["days_submission_to_acceptance"] <= 730)
        ].copy()
        # Deduplicate by DOI (keep first occurrence)
        if "doi" in articles.columns:
            articles = articles.drop_duplicates(subset="doi", keep="first")
    else:
        articles = pd.DataFrame()

    return field_summary, journal_summary, articles


# ---------------------------------------------------------------------------
# Tier aggregation helpers
# ---------------------------------------------------------------------------
def compute_tier_summary(journal_summary, group_col):
    """Compute summary stats grouped by the given column."""
    js = journal_summary.copy()
    js_with_data = js[js["n_with_review_time"] > 0].dropna(
        subset=["median_days_submission_to_acceptance"]
    )

    records = []
    for name, grp in js.groupby(group_col, dropna=True):
        grp_data = grp[grp["n_with_review_time"] > 0].dropna(
            subset=["median_days_submission_to_acceptance"]
        )
        medians = grp_data["median_days_submission_to_acceptance"]
        n_total = len(grp)
        n_with = len(grp_data)

        if n_with == 0:
            continue

        records.append({
            "name": name,
            "median": float(medians.median()),
            "p25": float(medians.quantile(0.25)),
            "p75": float(medians.quantile(0.75)),
            "mean": float(medians.mean()),
            "n_journals_total": int(n_total),
            "n_journals_with_data": int(n_with),
            "coverage_pct": round(n_with / n_total * 100, 1) if n_total > 0 else 0,
            "slowest_journal": grp_data.loc[medians.idxmax(), "journal_name"] if n_with > 0 else "",
            "fastest_journal": grp_data.loc[medians.idxmin(), "journal_name"] if n_with > 0 else "",
        })

    return sorted(records, key=lambda r: r["median"])


def compute_kde(articles, journal_summary, group_col, n_points=200, bw=0.15):
    """Pre-compute KDE curves for each group at the given tier."""
    x_grid = np.linspace(0, 730, n_points).tolist()

    # Map journals to groups
    journal_group = journal_summary.set_index("issn")[group_col].to_dict()
    valid = articles.dropna(subset=["days_submission_to_acceptance"]).copy()
    if "issn" in valid.columns:
        valid["_group"] = valid["issn"].map(journal_group)
    elif "field" in valid.columns and group_col == "field":
        valid["_group"] = valid["field"]
    else:
        valid["_group"] = valid.get("issn", pd.Series(dtype=str)).map(journal_group)

    valid = valid.dropna(subset=["_group"])

    kde_data = {}
    for name, grp in valid.groupby("_group"):
        vals = grp["days_submission_to_acceptance"].values
        if len(vals) < 20:
            continue
        try:
            kde = gaussian_kde(vals, bw_method=bw)
            density = kde(np.array(x_grid))
            # Normalize so max = 1
            d_max = density.max()
            if d_max > 0:
                density = density / d_max
            kde_data[name] = density.tolist()
        except Exception:
            continue

    return {"x": x_grid, "curves": kde_data}


def compute_histogram_bins(articles, journal_summary, group_col, bin_size=14, max_days=730):
    """Pre-compute histogram bin counts for each group."""
    journal_group = journal_summary.set_index("issn")[group_col].to_dict()
    valid = articles.dropna(subset=["days_submission_to_acceptance"]).copy()

    if "issn" in valid.columns:
        valid["_group"] = valid["issn"].map(journal_group)
    elif "field" in valid.columns and group_col == "field":
        valid["_group"] = valid["field"]
    else:
        valid["_group"] = valid.get("issn", pd.Series(dtype=str)).map(journal_group)

    valid = valid.dropna(subset=["_group"])

    bin_edges = list(range(0, max_days + bin_size, bin_size))
    bin_centers = [(bin_edges[i] + bin_edges[i + 1]) / 2 for i in range(len(bin_edges) - 1)]

    hist_data = {}
    for name, grp in valid.groupby("_group"):
        vals = grp["days_submission_to_acceptance"].values
        if len(vals) < 10:
            continue
        counts, _ = np.histogram(vals, bins=bin_edges)
        median_val = float(np.median(vals))
        hist_data[name] = {
            "counts": counts.tolist(),
            "median": median_val,
            "n_articles": int(len(vals)),
        }

    return {"bin_centers": bin_centers, "bin_edges": bin_edges, "groups": hist_data}


def compute_heatmap(journal_summary, group_col, n_publishers=30):
    """Compute field x publisher median matrix."""
    js = journal_summary[journal_summary["n_with_review_time"] > 0].dropna(
        subset=["median_days_submission_to_acceptance", "publisher", group_col]
    ).copy()

    # Top publishers by journal count
    pub_counts = js["publisher"].value_counts().head(n_publishers)
    top_pubs = pub_counts.index.tolist()

    # Pivot: group × publisher → median of journal medians
    pivot = js[js["publisher"].isin(top_pubs)].pivot_table(
        index=group_col,
        columns="publisher",
        values="median_days_submission_to_acceptance",
        aggfunc="median",
    )

    # Sort rows by overall median
    row_medians = pivot.median(axis=1).sort_values()
    pivot = pivot.loc[row_medians.index]

    # Reorder columns by overall median
    col_medians = pivot.median(axis=0).sort_values()
    pivot = pivot[col_medians.index]

    return {
        "rows": pivot.index.tolist(),
        "cols": pivot.columns.tolist(),
        "values": [[None if pd.isna(v) else round(float(v), 1) for v in row] for row in pivot.values],
    }


def compute_coverage(journal_summary, group_col):
    """Compute coverage data for treemap."""
    records = []
    for name, grp in journal_summary.groupby(group_col, dropna=True):
        n_total = len(grp)
        n_with = len(grp[grp["n_with_review_time"] > 0])
        records.append({
            "name": name,
            "n_journals_total": int(n_total),
            "n_journals_with_data": int(n_with),
            "coverage_pct": round(n_with / n_total * 100, 1) if n_total > 0 else 0,
        })
    return records


def compute_journal_data(journal_summary):
    """Serialize journal-level data for scatter/dumbbell charts."""
    js = journal_summary[journal_summary["n_with_review_time"] > 0].dropna(
        subset=["median_days_submission_to_acceptance"]
    ).copy()

    records = []
    for _, row in js.iterrows():
        rec = {
            "name": str(row["journal_name"]),
            "issn": str(row["issn"]),
            "field": str(row["field"]),
            "median": float(row["median_days_submission_to_acceptance"]),
            "n": int(row["n_with_review_time"]),
        }
        # Optional numeric fields
        for col, key in [
            ("sjr_rank", "sjr_rank"),
            ("sjr_score", "sjr_score"),
            ("median_days_acceptance_to_publication", "median_accept_pub"),
            ("p25_review", "p25"),
            ("p75_review", "p75"),
        ]:
            if col in row.index and pd.notna(row[col]):
                rec[key] = float(row[col])

        for col, key in [
            ("primary_area", "area"),
            ("mega_domain", "domain"),
            ("publisher", "publisher"),
        ]:
            if col in row.index and pd.notna(row[col]):
                rec[key] = str(row[col])

        records.append(rec)

    return records


def build_mappings(journal_summary):
    """Build tier mapping lookups."""
    js = journal_summary.dropna(subset=["field", "primary_area", "mega_domain"])

    field_to_area = {}
    area_to_domain = {}

    for _, row in js[["field", "primary_area", "mega_domain"]].drop_duplicates().iterrows():
        field_to_area[row["field"]] = row["primary_area"]
        area_to_domain[row["primary_area"]] = row["mega_domain"]

    # Items per tier (sorted)
    field_items = sorted(js["field"].unique().tolist())
    area_items = sorted(js["primary_area"].unique().tolist())
    domain_items = sorted(js["mega_domain"].unique().tolist())

    return {
        "fieldToArea": field_to_area,
        "areaToDomain": area_to_domain,
        "fieldItems": field_items,
        "areaItems": area_items,
        "domainItems": domain_items,
    }


# ---------------------------------------------------------------------------
# Main data assembly
# ---------------------------------------------------------------------------
def build_data_json(field_summary, journal_summary, articles):
    """Assemble all pre-computed data into a single JSON-serializable dict."""
    print("  Computing tier summaries...")
    field_summ = compute_tier_summary(journal_summary, "field")
    area_summ = compute_tier_summary(journal_summary, "primary_area")
    domain_summ = compute_tier_summary(journal_summary, "mega_domain")

    print("  Computing KDE curves...")
    kde_field = compute_kde(articles, journal_summary, "field")
    kde_area = compute_kde(articles, journal_summary, "primary_area")
    kde_domain = compute_kde(articles, journal_summary, "mega_domain")

    print("  Computing histogram bins...")
    hist_field = compute_histogram_bins(articles, journal_summary, "field")
    hist_area = compute_histogram_bins(articles, journal_summary, "primary_area")
    hist_domain = compute_histogram_bins(articles, journal_summary, "mega_domain")

    print("  Computing heatmaps...")
    heatmap_field = compute_heatmap(journal_summary, "field")
    heatmap_area = compute_heatmap(journal_summary, "primary_area")
    heatmap_domain = compute_heatmap(journal_summary, "mega_domain")

    print("  Computing coverage...")
    cov_field = compute_coverage(journal_summary, "field")
    cov_area = compute_coverage(journal_summary, "primary_area")
    cov_domain = compute_coverage(journal_summary, "mega_domain")

    print("  Serializing journal data...")
    journal_data = compute_journal_data(journal_summary)

    print("  Building mappings...")
    mappings = build_mappings(journal_summary)

    # Count data sources
    sources = set()
    if not articles.empty and "data_source" in articles.columns:
        sources = set(articles["data_source"].dropna().unique())
    n_sources = len(sources) if sources else 2  # fallback

    # Total stats
    n_journals_total = len(journal_summary)
    n_journals_with_data = int((journal_summary["n_with_review_time"] > 0).sum())
    n_articles = len(articles)
    n_fields = len(field_summary)

    # Journal list count (total tracked)
    journal_list_path = DATA_DIR / "journal_list.csv"
    n_tracked = len(pd.read_csv(journal_list_path)) if journal_list_path.exists() else n_journals_total

    data = {
        "journalData": journal_data,
        "fieldSummary": field_summ,
        "areaSummary": area_summ,
        "domainSummary": domain_summ,
        "kde": {
            "field": kde_field,
            "primary_area": kde_area,
            "mega_domain": kde_domain,
        },
        "hist": {
            "field": hist_field,
            "primary_area": hist_area,
            "mega_domain": hist_domain,
        },
        "heatmap": {
            "field": heatmap_field,
            "primary_area": heatmap_area,
            "mega_domain": heatmap_domain,
        },
        "coverage": {
            "field": cov_field,
            "primary_area": cov_area,
            "mega_domain": cov_domain,
        },
        "mappings": mappings,
        "meta": {
            "n_articles": n_articles,
            "n_journals": n_journals_total,
            "n_journals_with_data": n_journals_with_data,
            "n_journals_tracked": n_tracked,
            "n_fields": n_fields,
            "n_sources": n_sources,
            "sources": sorted(sources) if sources else ["Crossref", "PubMed"],
            "build_date": date.today().isoformat(),
        },
    }

    return data


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------
def render_site(data):
    """Render the Jinja2 template with pre-computed JSON data."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))
    template = env.get_template("index_v2.html.j2")

    data_json = json.dumps(data, separators=(",", ":"))

    html = template.render(data_json=data_json)
    return html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("Building v2 dashboard...\n")

    print("1. Checking summary data...")
    ensure_summaries()

    print("2. Loading data...")
    field_summary, journal_summary, articles = load_data()
    print(f"   {len(field_summary)} fields, {len(journal_summary)} journals, {len(articles)} articles")

    print("3. Pre-computing all aggregations...")
    data = build_data_json(field_summary, journal_summary, articles)

    print("4. Rendering site...")
    html = render_site(data)

    # Preserve old dashboard as v1
    DOCS_DIR.mkdir(exist_ok=True)
    old_index = DOCS_DIR / "index.html"
    if old_index.exists():
        v1_path = DOCS_DIR / "index_v1.html"
        if not v1_path.exists():
            shutil.copy2(old_index, v1_path)
            print(f"   Backed up old dashboard to {v1_path}")

    out_path = DOCS_DIR / "index.html"
    out_path.write_text(html, encoding="utf-8")

    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"\n   Output: {out_path} ({size_mb:.1f} MB)")
    print("   Open in browser to preview, or push to GitHub for Pages hosting.")
    print("\nDone!")


if __name__ == "__main__":
    main()
