# Review Time Database

## Project Overview

Database of peer review timelines (submission → acceptance → publication) across academic fields and ~29,500 journals from the SJR rankings. Purpose: identify which fields have the worst peer review bottlenecks, to guide outreach for the rapidPeer project.

- Interactive dashboard: https://stevenworthington.github.io/review-time-database/
- GitHub repo: https://github.com/stevenworthington/review-time-database

## Key Files

- `PROJECT_RECIPE.md` — Full project plan, API quirks, coverage analysis, and workflow
- `scripts/` — Numbered pipeline scripts (01–09) + status.py
- `data/` — Raw CSVs, checkpoints, SJR source data
- `outputs/` — Final formatted spreadsheets
- `docs/` — Static dashboard HTML (GitHub Pages)
- `templates/` — Jinja2 template for dashboard
- `logs/` — Pipeline log files (from nohup runs)

## Environment

- Python 3.14.3 in a local venv (`.venv/`)
- Activate: `source .venv/bin/activate`
- Key packages: `habanero`, `biopython`, `pandas`, `openpyxl`, `requests`, `beautifulsoup4`, `lxml`, `plotly`, `scipy`, `pymupdf`, `playwright`, `playwright-stealth`

## Pipeline Scripts

1. `01_build_journal_list.py` — Build journal list from SJR CSV (29,553 journals, all fields)
2. `02_crossref_collect.py` — Phase 1 Crossref (50 articles/journal, 1,260 journals)
3. `02_crossref_collect_v2.py` — Phase 2 Crossref (100 articles with dates, 28,789 journals, publisher skip list)
4. `03_pubmed_collect.py` — Phase 1 PubMed
5. `03_pubmed_collect_v2.py` — Phase 2 PubMed (100 articles, 11,871 journals) ✅ Complete
6. `04_merge_and_summarize.py` — Merge all sources, compute journal/field summaries, output Excel
7. `05_scrape_publishers.py` — Scrape publisher HTML (Tier 1: HTTP, Tier 2: Playwright headless)
8. `06_build_dashboard.py` — Build Plotly dashboard → docs/index.html
9. `07_frontiers_pdf_collect.py` — Extract dates from Frontiers OA PDFs via Unpaywall
10. `08_jstage_collect.py` — Scrape J-STAGE article pages (Japanese journals)
11. `09_scielo_collect.py` — ❌ Killed — most Latin American journals aren't on SciELO, OJS pages don't expose dates
12. `status.py` — Quick status check for all running pipelines
13. `run_pipelines.sh` — nohup wrapper to run all pipelines with auto-restart

## Running Pipelines

Background tasks in Claude Code have a **10-minute timeout**. Use `nohup` for long-running scripts:
```bash
nohup ./scripts/run_pipelines.sh &> logs/pipelines.log &
```

Check status:
```bash
source .venv/bin/activate && python scripts/status.py
```

All scripts support `--resume` for checkpoint recovery after interruption.

## Critical API Quirks

- Crossref `accepted` field is almost universally empty — dates live in the `assertion` field
- Must query individual DOIs for assertions; bulk ISSN endpoint doesn't return them
- Crossref v2 has a publisher skip list: known 0% publishers are skipped entirely (~2,500 journals, ~46K API calls saved)
- Crossref v2 also probes unknown publishers: 20 DOIs checked, if 0 have dates, skip that journal
- Scraper has publisher-level skip: after 3 journals with 0 dates from same publisher, skip rest of that publisher
- PubMed only covers biomedical/life science journals
- Date formats vary wildly — parser needs fallbacks for EN/PT/ES/JP formats
- Filter out accept-to-publish deltas > 365 days or < 0 days (bogus data)

## Publisher Date Deposit Patterns (from empirical testing)

### Crossref assertions work (89%+ hit rate)
BioMed Central, Springer (all imprints), BMJ, Wolters Kluwer, SpringerOpen, John Wiley & Sons

### Crossref never has dates (0%)
IEEE, Annual Reviews, ACS, AMA, Emerald, American Economic Association, INFORMS, U Chicago Press, Inst Math Stats, Copernicus, Now Publishers, SAGE, Cambridge, MDPI, Frontiers, APA, American Physical Society, IOP, Karger, De Gruyter, S. Karger, Royal Society of Chemistry, Mary Ann Liebert, JMIR, KeAi, IEEE Computer Society

### Partial coverage
- Elsevier: ~40-87% depending on imprint — PubMed covers biomedical subset
- Oxford UP: ~61% — PubMed for biomedical
- Nature Research: ~63-71%
- Taylor & Francis: ~78%
- Cell Press: ~70%

### Alternative sources
- Frontiers: dates extracted from OA PDFs (100% success via Unpaywall + PyMuPDF)
- J-STAGE: dates on article HTML pages (Japanese journals, HTTP scraping)

### Tested and ruled out
- SAGE: Cloudflare blocks all automated access (HTTP, Playwright, playwright-stealth, PDF download)
- Europe PMC: only has dates for PMC articles (same as PubMed, no new coverage)
- DOAJ: no review dates in API metadata
- OpenAlex: no received/accepted dates (only publication date)
- Project MUSE: no review dates on article pages (humanities journals don't track/expose review timelines)
- Brill: can't find articles via Crossref (0 DOIs under their member ID)
- Duke University Press: 403 blocked + no dates when accessible
- IEEE Xplore export: CSV export has publication dates but no received/accepted dates
- ScienceDirect/Scopus export: bulk export doesn't include received/accepted dates (only shown on individual article pages)
- IEEE/ACS/MDPI PDFs: no dates in PDFs
- PDF parsing for SAGE: Cloudflare blocks PDF downloads too
- SciELO scraper: most "Latin American" journals aren't actually on SciELO (they're on OJS at university domains), and OJS pages don't expose review dates
- Humanities publishers generally don't track or expose peer review timelines (cultural difference from STEM)

## Dashboard Design

- Tabbed layout: By Field → Journal Spread → Distributions → Scatter → Histograms
- Color scale: teal → sage green → amber → burnt orange → deep red
- Bar chart & boxplot: per-bar discrete colors sampled by position (not value) — ensures distinct colors across all bars
- Ridge plot: overlapping densities with transparency, bottom outline removed (only top outline visible), labels vertically centered at peak of each density
- Ridge plot uses article-level data; bar chart/boxplot use field_summary — captions explain the difference
- Summary cards show median with "of tracked journals" qualifier (not 100% coverage claim)
- Built with Plotly (Python) + Jinja2 template → static HTML on GitHub Pages

## Script Conventions

- Each script accepts `--resume` flag for checkpoint recovery
- Checkpoints saved as JSON in `data/` after each journal
- Log progress to stdout
- Handle errors gracefully (skip and log failed journals/articles)
- Include `mailto` parameter in Crossref requests for polite pool access
- Large CSVs (>50MB) are in `.gitignore` — not tracked by git
- Playwright runs in headless mode to avoid focus-stealing from user's active applications
