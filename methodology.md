# Methodology: Peer Review Time Database

## Overview

This document describes the data collection methodology for the Peer Review Time Database, a project that measures how long peer review takes across academic fields. The database collects submission-to-acceptance and acceptance-to-publication timelines for journals listed in the Scimago Journal Rankings (SJR), covering 84 research fields across all academic disciplines.

The database is part of the [rapidPeer](https://github.com/stevenworthington/review-time-database) project, which aims to identify fields with the worst peer review bottlenecks to guide outreach efforts.

**Dashboard:** https://stevenworthington.github.io/review-time-database/

## Target Population

The sampling frame is the full Scimago Journal Rankings (SJR) 2023 list: **29,553 journals** across 84 fields, 27 subject areas, and 5 mega-domains (Life Sciences, Physical Sciences, Social Sciences, Health Sciences, and Humanities & Arts). Each journal is classified by its primary SJR field and has associated metadata including publisher, ISSN, SJR rank, and SJR score.

## Data Collection Strategy

For each journal, the goal is to collect **100 articles with valid review dates** (received and accepted dates). Dates are validated by computing the submission-to-acceptance delta and filtering out implausible values (negative deltas or deltas exceeding 3 years). Acceptance-to-publication deltas exceeding 1 year are also excluded.

Data collection proceeds through multiple sources in a priority cascade. When the same article (identified by DOI) appears in multiple sources, the highest-priority source is retained:

1. **PubMed** (highest priority) — structured date fields in article history metadata
2. **Crossref** — assertion dates deposited by publishers via the Crossref API
3. **Frontiers PDFs** — dates extracted from open-access PDF full text
4. **J-STAGE** — dates scraped from Japanese journal article pages
5. **Inderscience** — dates scraped from publisher article pages
6. **Small publisher scrapers** (Copernicus, Thieme, Galenos, Minerva Medica) — dates scraped from article pages
7. **De Gruyter + Virtus Interpress** — dates scraped from article pages
8. **General HTML scrapers** (Elsevier, Springer, Wiley, Taylor & Francis, Oxford) — dates scraped from article pages via HTTP or Playwright

## Data Sources in Detail

### 1. PubMed (E-utilities API)

PubMed provides article history dates (received, revised, accepted, published) for biomedical and life science journals through the NCBI E-utilities API. Coverage is limited to journals indexed in MEDLINE/PubMed.

- **Script:** `03_pubmed_collect_v2.py`
- **Method:** Bulk ISSN queries via `esearch` + `efetch`, extracting `<History>` elements from PubMed XML
- **Journals queried:** 11,871
- **Articles collected:** 456,218
- **Status:** Complete

### 2. Crossref (Assertion Dates)

Crossref metadata includes an `assertion` field where publishers can deposit received/accepted/revised dates. Not all publishers use this field — deposit rates vary dramatically by publisher.

- **Script:** `02_crossref_collect_v2.py`
- **Method:** Per-DOI queries to the Crossref REST API (the bulk ISSN endpoint does not return assertion data). Articles are first discovered via the ISSN endpoint, then individual DOIs are queried for assertions.
- **Journals queried:** 28,789
- **Articles collected:** 817,361 (articles with valid assertion dates)
- **Status:** Complete
- **Key optimization:** A publisher skip list excludes ~2,500 journals from publishers known to never deposit assertion dates (e.g., IEEE, ACS, SAGE, Cambridge UP, MDPI), saving ~46,000 API calls. Additionally, unknown publishers are probed with 20 DOIs before committing to a full journal query — if zero have dates, the journal is skipped.

### 3. Frontiers PDF Extraction

Frontiers journals are fully open access and display received/accepted dates in a consistent format at the top of every article PDF. Dates are extracted using PyMuPDF (fitz) from PDFs downloaded via the Unpaywall API.

- **Script:** `07_frontiers_pdf_collect.py`
- **Method:** DOIs from Crossref → PDF URLs from Unpaywall → text extraction with PyMuPDF → regex parsing of `RECEIVED DD Month YYYY` / `ACCEPTED DD Month YYYY` patterns
- **Journals:** 129
- **Articles collected:** 11,303
- **Hit rate:** ~100%
- **Status:** Complete

### 4. J-STAGE (Japanese Journals)

J-STAGE is Japan's electronic journal platform. Article pages display review dates in a structured format.

- **Script:** `08_jstage_collect.py`
- **Method:** DOIs from Crossref → resolve to J-STAGE article URL → scrape `<span class="accodion_lic">` elements for received/accepted dates
- **Journals:** 264 Japanese-publisher journals
- **Articles collected:** 13,654
- **Status:** Complete

### 5. Inderscience

Inderscience article pages (`inderscience.com/info/inarticle.php`) consistently display received and accepted dates.

- **Script:** `10_inderscience_collect.py`
- **Method:** Journal codes mapped via Inderscience's site → issue TOC pages scraped for article IDs → individual article pages scraped for dates
- **Journals:** 237
- **Articles collected:** ~23,000 (in progress)
- **Hit rate:** ~100%
- **Status:** Running (~80% complete)

### 6. Small Publisher Scrapers (Copernicus, Thieme, Galenos, Minerva Medica)

Four smaller publishers whose article pages expose review dates, accessed via DOI resolution.

- **Script:** `11_small_publishers_collect.py`
- **Method:** DOIs from Crossref → resolve DOI to article page → publisher-specific regex extraction
- **Publishers and date formats:**
  - **Copernicus** (60 journals, earth/atmospheric science): `Received: DD Mon YYYY – Accepted: DD Mon YYYY`
  - **Georg Thieme Verlag** (77 journals, medical): `Received: DD Month YYYY` / `Accepted after revision: DD Month YYYY`
  - **Galenos Publishing** (31 journals, Turkish medical): `Received Date: DD.MM.YYYY` / `Accepted Date: DD.MM.YYYY`
  - **Edizioni Minerva Medica** (28 journals, Italian medical): `received: Month DD, YYYY` / `accepted: Month DD, YYYY`
- **Articles collected:** ~9,000 (in progress)
- **Status:** Running (~66% complete)

### 7. De Gruyter + Virtus Interpress

De Gruyter (387 journals across all imprints) and Virtus Interpress (7 business/accounting journals) display dates on their article pages.

- **Script:** `12_degruyter_collect.py`
- **Method:** DOIs from Crossref → resolve to article page → regex extraction
- **Date formats:**
  - **De Gruyter:** `Received: YYYY-MM-DD` / `Accepted: YYYY-MM-DD`
  - **Virtus Interpress:** `Received: DD.MM.YYYY` / `Accepted: DD.MM.YYYY`
- **Journals:** 394
- **Articles collected:** ~5,500 (in progress)
- **Hit rate:** ~73% (some De Gruyter journals, particularly older humanities titles, lack dates)
- **Status:** Running (~1% complete)

### 8. General HTML Scrapers (Tier 1 and Tier 2)

Two-tier scraping system for publisher article pages, using Crossref DOIs to find articles and then fetching the publisher's HTML page.

- **Script:** `05_scrape_publishers.py` with `publisher_parsers.py`
- **Tier 1 (HTTP/requests):** Simple HTTP requests for publishers that don't require JavaScript rendering. Includes publisher-specific parsers for Elsevier, Wiley, Taylor & Francis, Springer, and Oxford UP.
- **Tier 2 (Playwright headless):** Headless Chromium for publishers requiring JavaScript rendering.
- **Publisher-level skip:** After 3 journals from the same publisher yield 0 dates, remaining journals from that publisher are skipped.
- **Articles collected:** ~68,000 combined (in progress)
- **Status:** Running

## Sources Explored and Ruled Out

| Source | Journals | Outcome | Blocker |
|--------|----------|---------|---------|
| **SAGE Publications** | 1,104 | No dates obtained | Cloudflare blocks all automated access (HTTP, Playwright, playwright-stealth, PDF downloads). TDM policy exists but Cloudflare enforcement prevents use. IP whitelisting requested. |
| **Cambridge University Press** | 381 | 17 via PubMed only | No Crossref assertion dates. Article pages accessible but do not display received/accepted dates. |
| **Emerald Publishing** | 339 | 9 via PubMed only | No Crossref assertions. Article pages return 403 for automated requests. |
| **IEEE** | 232 | 4 via PubMed only | No Crossref assertions (0% deposit rate). IEEE Xplore CSV export does not include received/accepted dates. PDFs do not contain dates. |
| **Brill Academic Publishers** | 252 | 5 via PubMed only | Cannot find articles via Crossref (0 DOIs under their member ID). Article pages do not display dates. |
| **American Chemical Society** | 79 | 8 via PubMed only | No Crossref assertions. PDFs do not contain review dates. |
| **MDPI** | 314 | 80 via PubMed only | No Crossref assertions. PDFs do not contain dates. Article pages have dates but are not yet scraped. |
| **Annual Reviews** | 32 | 0 | No Crossref assertions. No dates on article pages. |
| **John Benjamins** | 69 | 0 | No Crossref assertions. Article pages do not display dates. |
| **Duke University Press** | 54 | 0 | 403 blocked. No dates visible when accessible. |
| **University of Chicago Press** | 75 | 0 | No Crossref assertions. No dates on article pages. |
| **Intellect Ltd.** | 81 | 0 | No Crossref assertions. No dates on article pages. |
| **IGI Global** | 72 | 0 | 429 rate limited. No dates visible. |
| **Common Ground Research Networks** | 47 | 0 | No dates on article pages. |
| **Open Library of Humanities** | 13 in SJR | No submission dates | Article pages show accepted and published dates only — no received/submitted date, so submission-to-acceptance time cannot be computed. |
| **Europe PMC** | — | Redundant | Only has dates for PMC articles, which are the same articles already captured via PubMed. No new coverage. |
| **DOAJ** | — | No review dates | API metadata does not include received/accepted dates. |
| **OpenAlex** | — | No review dates | Only tracks publication date, not received or accepted dates. |
| **JSTOR** | — | No review dates | Historical archive; no peer review timeline metadata. |
| **SciELO** | 279 attempted | 0 | Most "Latin American" journals in the SJR are hosted on OJS at university domains, not scielo.br. OJS pages do not expose review dates. |
| **Project MUSE** | — | No dates | Humanities journals do not display review dates on article pages. Cultural difference from STEM. |
| **ScienceDirect/Scopus CSV export** | — | No dates | Bulk CSV export does not include received/accepted dates (only shown on individual article pages, which are scraped via Tier 1). |
| **IEEE Xplore CSV export** | — | No dates | CSV export includes publication dates but not received/accepted dates. |

## Publisher Date Deposit Patterns

Empirical testing revealed stark differences in how publishers deposit review dates to Crossref:

| Category | Publishers | Crossref Hit Rate |
|----------|-----------|-------------------|
| **Always deposit** | BioMed Central, Springer (all imprints), BMJ, Wolters Kluwer, SpringerOpen, John Wiley & Sons | 89–100% |
| **Partial deposit** | Taylor & Francis (~78%), Nature Research (~63–71%), Oxford UP (~61%), Elsevier (~40–87% depending on imprint), Cell Press (~70%) |  |
| **Never deposit** | IEEE, Annual Reviews, ACS, AMA, Emerald, AEA, INFORMS, U Chicago Press, IMS, Copernicus, Now Publishers, SAGE, Cambridge, MDPI, APA, APS, IOP, Karger, De Gruyter, Royal Society of Chemistry, Mary Ann Liebert, JMIR, KeAi, IEEE Computer Society | 0% |

## Deduplication

When the same article appears in multiple sources (identified by DOI), only the highest-priority version is retained. Priority order: PubMed > Crossref > Frontiers > J-STAGE > Inderscience > Small Publishers > De Gruyter > Scraped HTML. This ensures that the most reliable date source is used for each article.

## Date Validation

All computed deltas are validated:

- **Submission-to-acceptance:** Must be ≥ 0 and ≤ 1,095 days (3 years). Values outside this range are excluded.
- **Acceptance-to-publication:** Must be ≥ 0 and ≤ 365 days (1 year). Values outside this range are excluded.
- **Total (submission-to-publication):** Must be ≥ 0 and ≤ 1,460 days (4 years).

## Temporal Coverage

Articles are sorted by publication date (most recent first) during collection, so the sample is weighted toward recent publications. The target of 100 articles per journal means that for high-volume journals, only the most recent 1–2 years are captured, while for low-volume journals, the sample may span a longer period.

| Year | Articles |
|------|----------|
| 2026 | 236,570 |
| 2025 | 399,605 |
| 2024 | 133,132 |
| 2023 | 63,950 |
| 2022 | 38,628 |
| 2021 | 25,103 |
| 2020 | 12,278 |
| 2019 | 7,418 |
| 2018 | 5,417 |
| 2017 | 3,448 |
| 2016 | 2,282 |
| 2015 | 1,556 |
| ≤2014 | 3,849 |

The bulk of the data (68%) comes from 2025–2026, reflecting both the recency-first sampling strategy and the larger volume of recent publications.

## Current Coverage Summary

| Metric | Value |
|--------|-------|
| Journals in SJR | 29,553 |
| Journals with review time data | 10,825 (36.6%) |
| Total articles (after dedup) | ~1,024,000 |
| Data sources | 9 pipelines |
| Fields covered | 84 / 84 |
| Median review time (across journal medians) | 128 days |
| Mean review time (across journal medians) | 148 days |

### Coverage by Major Publisher

| Publisher | Journals Covered | Total in SJR | Coverage |
|-----------|-----------------|--------------|----------|
| BioMed Central | 257 | 258 | 99.6% |
| John Wiley & Sons | 358 | 365 | 98.1% |
| Springer (all imprints) | ~1,500 | ~1,600 | ~94% |
| Wiley-Blackwell | 382 | 426 | 89.7% |
| Inderscience | 206 | 232 | 88.8% |
| BMJ Publishing | 54 | 63 | 85.7% |
| Elsevier (all imprints) | ~1,400 | ~2,100 | ~67% |
| Copernicus/Thieme/Galenos/Minerva | ~100 | 196 | ~51% |
| Taylor & Francis / Routledge | 1,410 | 2,116 | 66.7% |
| Oxford University Press | 217 | 451 | 48.1% |
| MDPI | 80 | 314 | 25.5% |
| De Gruyter (all imprints) | ~47 | 394 | ~12% |
| SAGE (all imprints) | 105 | 1,104 | 9.5% |
| Cambridge University Press | 17 | 381 | 4.5% |
| Brill | 5 | 252 | 2.0% |
| IEEE | 4 | 232 | 1.7% |
| Emerald | 9 | 339 | 2.7% |

### Coverage Gaps

The lowest-coverage fields are predominantly in the humanities and social sciences:

| Field | Journals Covered | Total | Coverage |
|-------|-----------------|-------|----------|
| Library & Information Science | 12 | 135 | 9% |
| Arts & Humanities | 245 | 2,513 | 10% |
| History | 109 | 1,102 | 10% |
| Linguistics | 71 | 605 | 12% |
| Law | 106 | 790 | 13% |
| Philosophy | 72 | 465 | 15% |

These gaps reflect a structural reality: humanities publishers generally do not track or expose peer review timelines. This is a cultural difference from STEM fields, where submission and acceptance dates are routinely recorded and displayed.

## Aggregation

Journal-level summaries are computed as the median of all article-level submission-to-acceptance times for that journal. Field-level summaries are computed as the median of journal-level medians within that field, weighted equally by journal (not by article count). This prevents high-volume journals from dominating field-level statistics.

## Reproducibility

All scripts are in the `scripts/` directory, numbered in execution order (01–12). Each script supports a `--resume` flag for checkpoint-based recovery after interruption. Checkpoints are stored as JSON files in `data/`. The full pipeline can be re-run from scratch or resumed at any point.

```
scripts/
├── 01_build_journal_list.py      # Build journal list from SJR CSV
├── 02_crossref_collect_v2.py     # Crossref assertion date collection
├── 03_pubmed_collect_v2.py       # PubMed article history collection
├── 04_merge_and_summarize.py     # Merge sources, compute summaries
├── 05_scrape_publishers.py       # HTML scraping (Tier 1 + Tier 2)
├── 06_build_dashboard.py         # Build interactive dashboard
├── 07_frontiers_pdf_collect.py   # Frontiers PDF date extraction
├── 08_jstage_collect.py          # J-STAGE article page scraping
├── 10_inderscience_collect.py    # Inderscience article page scraping
├── 11_small_publishers_collect.py # Copernicus/Thieme/Galenos/Minerva
├── 12_degruyter_collect.py       # De Gruyter + Virtus Interpress
├── publisher_parsers.py          # Publisher-specific HTML parsers
├── status.py                     # Pipeline status checker
└── run_pipelines.sh              # nohup wrapper with auto-restart
```
