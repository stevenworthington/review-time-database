# Review Time Database

## Project Overview

Database of peer review timelines (submission → acceptance → publication) across ~50 academic fields and top 10–20 journals per field. Purpose: identify which fields have the worst peer review bottlenecks, to guide outreach for the rapidPeer project.

## Key Files

- `PROJECT_RECIPE.md` — Full project plan, API quirks, coverage analysis, and workflow
- `prototype_results.xlsx` — Initial prototype results from Cowork session
- `scripts/` — Numbered pipeline scripts (01–05)
- `data/` — Raw and processed CSVs
- `outputs/` — Final formatted spreadsheets

## Environment

- Python 3.14.3 in a local venv (`.venv/`) — micromamba had codesigning issues on this machine
- Activate: `source .venv/bin/activate`
- Key packages: `habanero`, `biopython`, `pandas`, `openpyxl`, `requests`, `beautifulsoup4`, `lxml`

## Pipeline

1. `01_build_journal_list.py` — Build journal list from SJR CSV data
2. `02_crossref_collect.py` — Collect review dates from Crossref assertions (individual DOI lookups required)
3. `03_pubmed_collect.py` — Enrich with PubMed dates for biomedical journals
4. `04_merge_and_summarize.py` — Merge sources, compute journal/field summaries
5. `05_scrape_publishers.py` — (Optional) Scrape publisher HTML for missing data

## Critical API Quirks

- Crossref `accepted` field is almost universally empty — dates live in the `assertion` field
- Must query individual DOIs for assertions; bulk ISSN endpoint doesn't return them
- Only Springer Nature / BMC deposit assertion dates (~20-25% of journals)
- PubMed only covers biomedical/life science journals
- Date formats vary: "29 August 2025" vs "August 29, 2025" — parser needs fallbacks
- Filter out accept-to-publish deltas > 365 days or < 0 days (bogus data)

## Script Conventions

- Each script should accept `--resume` flag for checkpoint recovery
- Log progress to stdout
- Save intermediate results to `data/` after each journal
- Handle errors gracefully (skip and log failed journals/articles)
- Include `mailto` parameter in Crossref requests for polite pool access
