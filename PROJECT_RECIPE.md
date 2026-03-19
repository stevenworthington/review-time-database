# Journal Review Time Database — Project Recipe

## Goal

Build a database of peer review timelines (submission → acceptance → publication) across ~50 academic fields and the top 10–20 journals in each. The purpose is to identify which fields experience the most peer review pain, to guide outreach for the rapidPeer project.

## Strategy

Exhaust the two free, journal-agnostic APIs first (Crossref + PubMed), then assess coverage gaps and decide whether to fill them with publisher-specific scraping.

---

## Phase 1: Build the Journal List

**Objective:** Create a master list of ~500–1,000 journals (10–20 per field, ~50 fields).

**Approach:**
1. Use Scimago Journal Rankings (SJR) — freely downloadable CSV at https://www.scimagojr.com/journalrank.php
2. SJR provides ISSN, field classification, and ranking. Download the full CSV and filter by:
   - Top 10–20 journals per SJR subject category
   - Across all four broad areas: Social Sciences, Physical Sciences, Life Sciences, Health Sciences, plus Arts & Humanities
3. Store as `data/journal_list.csv` with columns: `field`, `field_category`, `journal_name`, `issn`, `publisher`, `sjr_rank`

**Notes:**
- SJR uses Scopus subject categories (~330 categories). You'll want to group these into ~50 broader fields. A reasonable mapping might collapse, e.g., "Cardiology and Cardiovascular Medicine" + "Critical Care and Intensive Care Medicine" into "Medicine (Clinical)".
- Some journals span multiple fields. Assign each journal to its primary SJR category.

---

## Phase 2: Crossref Data Collection

**Objective:** Pull accepted/received dates from Crossref for all journals on the list.

### What We Learned in Prototyping

- The standard `accepted` field in Crossref is **almost universally empty** — no major publisher deposits dates there.
- **The dates live in the `assertion` field** — an underused part of Crossref metadata. Springer Nature and BioMed Central deposit `received` and `accepted` dates here with 100% coverage. Most other publishers do not.
- You must query individual articles by DOI (not batch by ISSN) to get the assertions, since the `select` parameter in the ISSN-filtered `/works` endpoint doesn't return assertions.

### Implementation

```
For each journal (by ISSN):
  1. Query Crossref: GET /works?filter=issn:{issn},type:journal-article&sort=published&order=desc&rows=50&select=DOI
  2. For each DOI returned:
     - GET /works/{doi} (full metadata)
     - Check msg["assertion"] for entries with name="received" and name="accepted"
     - Parse date values (format: "DD Month YYYY", e.g., "29 August 2025")
     - Also check msg["accepted"] field (rare but possible)
     - Extract published-online or issued date
  3. Compute: days_submission_to_acceptance, days_acceptance_to_publication
  4. Save article-level data to data/crossref_articles.csv
  5. Checkpoint after each journal (save progress to resume if interrupted)
```

### Technical Details

- **Library:** `habanero` (Python) or direct HTTP with `requests`
- **Auth:** No API key needed. Include `mailto=your@email.com` for polite pool (faster rate limit)
- **Rate limit:** ~50 req/sec in polite pool. But since we need individual DOI lookups, expect ~2–3 articles/sec with network latency.
- **Scale:** ~1,000 journals × 50 articles × 2 requests each = ~100,000 requests. At 2–3/sec = ~10–14 hours.
- **Date parsing:** Assertion dates use formats like "29 August 2025" or "August 29, 2025". Use `datetime.strptime` with fallback patterns.

### Expected Coverage

Based on prototype testing:

| Publisher | Deposits assertions with dates? | Share of top journals |
|-----------|-------------------------------|----------------------|
| Springer Nature | Yes (100% for BMC, partial for Nature-branded) | ~15–20% |
| BioMed Central | Yes (100%) | ~5% |
| All others (Elsevier, Wiley, ACS, IEEE, SAGE, Oxford UP, T&F, AEA, etc.) | No | ~75–80% |

**Expect usable Crossref data for roughly 20–25% of journals.**

---

## Phase 3: PubMed Enrichment

**Objective:** For biomedical/life science journals not covered by Crossref assertions, pull dates from PubMed.

### What PubMed Has

PubMed stores received/accepted dates in its XML metadata under `<PubMedPubDate PubStatus="received">` and `<PubMedPubDate PubStatus="accepted">`. Coverage is ~55% of PubMed-indexed journals.

### Implementation

```
For each journal with ISSN AND missing Crossref data:
  1. Search PubMed: esearch.fcgi?db=pubmed&term={issn}[journal]&retmax=50&sort=pub_date
  2. Fetch full XML: efetch.fcgi?db=pubmed&id={pmid_list}&rettype=xml
  3. Parse XML for:
     - <PubMedPubDate PubStatus="received"> → submission date
     - <PubMedPubDate PubStatus="accepted"> → acceptance date
     - <PubMedPubDate PubStatus="epublish"> → online publication date
  4. Compute deltas, save to data/pubmed_articles.csv
```

### Technical Details

- **Library:** `biopython` (Entrez module) or direct HTTP
- **Auth:** Free. Requires email in requests. API key available from NCBI for 10 req/sec (vs 3 without).
- **Rate limit:** 3 req/sec without key, 10/sec with key
- **Scale:** Maybe 300–500 journals × 50 articles. Much smaller than Crossref since PubMed only covers biomedical.
- **Coverage:** ~55% of PubMed-indexed journals have these dates. PubMed itself only covers biomedical/life science — so this adds nothing for economics, CS, philosophy, etc.

---

## Phase 4: Merge and Assess Coverage

**Objective:** Combine Crossref + PubMed data, compute journal-level summaries, and identify gaps.

### Implementation

```python
# Merge article-level data
# Priority: PubMed received/accepted dates > Crossref assertion dates > Crossref accepted field

# Compute per-journal summary:
#   - median_days_submission_to_acceptance
#   - median_days_acceptance_to_publication
#   - median_days_total
#   - p25, p75
#   - n_articles_with_data
#   - data_source (crossref_assertion / pubmed / both)

# Compute coverage report:
#   - By field: what % of top journals have data?
#   - By publisher: which publishers are missing?
#   - Overall: how many of ~1,000 journals have usable timelines?
```

### Output Files

- `data/journal_summary.csv` — one row per journal with median timelines
- `data/field_summary.csv` — one row per field with aggregated stats
- `data/coverage_report.csv` — gap analysis by field and publisher
- `outputs/review_times.xlsx` — formatted spreadsheet with all sheets

---

## Phase 5 (Optional): Publisher HTML Scraping

Only pursue this if Phase 4 shows unacceptable coverage gaps. This is the most labor-intensive step.

### Target Publishers (in priority order)

1. **Elsevier / ScienceDirect** — Largest publisher. Dates appear in article HTML in a `<dt>Received</dt><dd>date</dd>` pattern. ~3,000+ journals.
2. **Wiley** — Second largest. Similar HTML pattern on article pages.
3. **Taylor & Francis** — Third largest. Dates in article header.
4. **SAGE** — Social sciences heavy. Important for your use case.
5. **IEEE** — Computer science / engineering.
6. **Oxford University Press** — Humanities and social sciences.
7. **ACS** — Chemistry.

### Approach

For each publisher:
1. Identify the HTML pattern for article dates (inspect 2–3 article pages)
2. Write a parser specific to that publisher's HTML structure
3. For each journal, get 50 recent article URLs (via Crossref DOI → publisher URL redirect)
4. Fetch each article page, extract dates
5. Respect robots.txt and rate limit (1–2 sec delay between requests)

### Risks

- Publisher ToS may prohibit scraping (though publicly displayed metadata is generally fair game)
- HTML structures change without notice — parsers break
- Rate limiting / IP blocking if too aggressive
- Much slower than API access: ~50,000 page fetches at 1/sec = ~14 hours per publisher

---

## File Structure

```
review_time_database/
├── PROJECT_RECIPE.md          ← this file
├── prototype_results.xlsx     ← initial prototype from Cowork
├── scripts/
│   ├── 01_build_journal_list.py
│   ├── 02_crossref_collect.py
│   ├── 03_pubmed_collect.py
│   ├── 04_merge_and_summarize.py
│   └── 05_scrape_publishers.py   (optional)
├── data/
│   ├── journal_list.csv
│   ├── crossref_articles.csv
│   ├── pubmed_articles.csv
│   ├── journal_summary.csv
│   ├── field_summary.csv
│   └── coverage_report.csv
└── outputs/
    └── review_times.xlsx
```

---

## Key Lessons from Prototyping

1. **Crossref's `accepted` field is virtually unused.** The dates live in the `assertion` field instead — and even then, only Springer/BMC deposit them.
2. **You must query individual DOIs** to get assertions. The bulk `/works?filter=issn:xxx` endpoint with `select=` doesn't return assertion data.
3. **Date formats in assertions vary:** "29 August 2025" vs "August 29, 2025". Build a parser with fallbacks.
4. **Publication dates with year 2035/2036 are bogus** — some small publishers deposit bad data. Filter out any accept-to-publish delta > 365 days or < 0 days.
5. **Nature-branded journals** (Nature, Nature Medicine, etc.) only have dates for ~17% of articles via Crossref, vs 100% for BMC journals — even though both are Springer Nature.
6. **PubMed is biomedical only** — it won't help for economics, CS, philosophy, humanities, or most social sciences.
7. **For ~75% of journals, scraping article HTML is the only automated path** to get review timeline dates. This is a significant engineering commitment.

---

## Recommended Claude Code Workflow

```bash
# Step 0: Create a virtual environment
# The user has micromamba 2.5.0 and python 3.14.2 installed via Homebrew.
# Create a project-specific conda env to keep dependencies isolated.
micromamba create -n review_time_db python=3.12 -y   # 3.12 recommended for library compat
micromamba activate review_time_db
pip install habanero biopython pandas openpyxl requests beautifulsoup4 lxml

# Step 1: Set up the project directory structure
mkdir -p scripts data outputs

# Step 2: Build journal list
python scripts/01_build_journal_list.py

# Step 3: Collect Crossref data (long-running — ~10-14 hours)
python scripts/02_crossref_collect.py  # has checkpointing, can resume

# Step 4: Collect PubMed data (~1-2 hours)
python scripts/03_pubmed_collect.py

# Step 5: Merge and create outputs
python scripts/04_merge_and_summarize.py

# Step 6 (assess first): If coverage is too low, scrape publishers
python scripts/05_scrape_publishers.py
```

Each script should:
- Accept `--resume` flag to continue from last checkpoint
- Log progress to stdout
- Save intermediate results to `data/` after each journal
- Handle errors gracefully (skip and log failed journals/articles)

---

*Created: March 18, 2026 — from Cowork prototype session*
