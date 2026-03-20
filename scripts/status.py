#!/usr/bin/env python3
"""Quick status check for all pipelines."""
import json, subprocess, os
from pathlib import Path

os.chdir(Path(__file__).parent.parent)

pipelines = [
    ("Crossref v2",    "02_crossref_collect_v2", "data/crossref_checkpoint_v2.json", "data/crossref_articles_v2.csv", 28789),
    ("PubMed v2",      "03_pubmed_collect_v2",   "data/pubmed_checkpoint_v2.json",   "data/pubmed_articles_v2.csv",   11871),
    ("Scraper Tier 1", "05_scrape.*tier.1",       "data/scrape_checkpoint_t1.json",   "data/scraped_articles_t1.csv",  None),
    ("Scraper Tier 2", "05_scrape.*tier.2",       "data/scrape_checkpoint_t2.json",   "data/scraped_articles_t2.csv",  None),
    ("Frontiers PDF",  "07_frontiers_pdf",        "data/frontiers_checkpoint.json",   "data/frontiers_articles.csv",   129),
    ("J-STAGE",        "08_jstage",               "data/jstage_checkpoint.json",      "data/jstage_articles.csv",      249),
    ("SciELO",         "09_scielo",               "data/scielo_checkpoint.json",      "data/scielo_articles.csv",      279),
]

for name, grep_pat, cp_file, csv_file, total in pipelines:
    # Check if running
    result = subprocess.run(f"ps aux | grep '{grep_pat}' | grep -v grep | wc -l",
                          shell=True, capture_output=True, text=True)
    running = int(result.stdout.strip()) > 0
    status = "🟢 Running" if running else "🔴 Stopped"

    # Checkpoint
    cp_path = Path(cp_file)
    if cp_path.exists():
        cp = json.load(open(cp_path))
        done = len(cp)
    else:
        done = 0

    # Articles
    csv_path = Path(csv_file)
    articles = 0
    if csv_path.exists():
        with open(csv_path) as f:
            articles = max(0, sum(1 for _ in f) - 1)

    # Print
    if total:
        pct = done / total * 100
        print(f"  {name:<15} {status}  {done:>6,} / {total:>6,} journals ({pct:>5.1f}%)  {articles:>8,} articles")
    else:
        print(f"  {name:<15} {status}  {done:>6,} journals{' ' * 20}  {articles:>8,} articles")
