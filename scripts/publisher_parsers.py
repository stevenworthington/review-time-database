"""
publisher_parsers.py

Publisher-specific parsers to extract peer review dates from article HTML.
Each parser returns a dict: {received_date, accepted_date, revised_date, published_date}
where dates are datetime objects or None.

Tested publishers:
  - Elsevier/ScienceDirect (HTTP/curl — JSON embedded in HTML)
  - MDPI (HTTP/curl — visible text)
  - Taylor & Francis (Playwright — visible text)
  - Oxford University Press (Playwright — click "Article history" dropdown)
  - Frontiers (Playwright — visible text)

Not working:
  - SAGE: Cloudflare blocks all automated access
  - Wiley: Cloudflare blocks all automated access
  - Cambridge UP: No received/accepted dates in HTML
"""

import re
import json
import subprocess
from datetime import datetime

# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------
DATE_FORMATS = [
    "%d %B %Y",       # "29 August 2025"
    "%B %d, %Y",      # "August 29, 2025"
    "%d %b %Y",       # "29 Aug 2025"
    "%b %d, %Y",      # "Aug 29, 2025"
    "%Y-%m-%d",        # "2025-08-29"
    "%d/%m/%Y",        # "29/08/2025"
    "%m/%d/%Y",        # "08/29/2025"
]


def parse_date(date_str):
    """Try multiple date formats to parse a date string."""
    if not date_str:
        return None
    date_str = str(date_str).strip()
    # Remove ordinal suffixes (1st, 2nd, 3rd, 4th)
    date_str = re.sub(r'(\d+)(?:st|nd|rd|th)\b', r'\1', date_str)
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    return None


def _curl_fetch(url, timeout=15):
    """Fetch a URL using curl (bypasses some bot detection that blocks requests)."""
    try:
        result = subprocess.run([
            'curl', '-s', '-L', '--max-time', str(timeout),
            '-H', 'User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
            '-H', 'Accept: text/html,application/xhtml+xml',
            url
        ], capture_output=True, text=True, timeout=timeout + 5)
        if result.returncode == 0 and len(result.stdout) > 1000:
            return result.stdout
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Tier 1: HTTP-accessible publishers (curl/subprocess)
# ---------------------------------------------------------------------------

def parse_elsevier(url, html=None):
    """
    Extract dates from Elsevier/ScienceDirect article pages.

    Elsevier embeds a JSON object in the page HTML containing:
      "dates":{"Available online":"2 January 2025","Received":"10 May 2024",
               "Revised":["20 November 2024"],"Accepted":"2 December 2024",...}

    Note: linkinghub.elsevier.com URLs don't have dates — convert to sciencedirect.com
    """
    if html is None:
        # Convert linkinghub URL to ScienceDirect URL
        if "linkinghub.elsevier.com" in url:
            pii_match = re.search(r'pii/(\S+)', url)
            if pii_match:
                url = f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}"
        html = _curl_fetch(url)
    if not html:
        return None

    result = {"received_date": None, "accepted_date": None,
              "revised_date": None, "published_date": None}

    # Extract the dates JSON block
    match = re.search(r'"dates"\s*:\s*\{([^}]+)\}', html)
    if not match:
        return None

    try:
        dates_json = json.loads('{' + match.group(1) + '}')
    except json.JSONDecodeError:
        # Try fixing common issues (lists as values)
        raw = '{' + match.group(1) + '}'
        # Replace arrays with first element
        raw = re.sub(r'\[([^\]]*)\]', lambda m: m.group(1).split(',')[0].strip(), raw)
        try:
            dates_json = json.loads(raw)
        except json.JSONDecodeError:
            return None

    # Map JSON keys to our fields
    for key, value in dates_json.items():
        if isinstance(value, list):
            value = value[-1] if value else None  # Use last revision
        if not value:
            continue

        key_lower = key.lower()
        if "received" in key_lower and "revision" not in key_lower:
            result["received_date"] = parse_date(value)
        elif "accepted" in key_lower:
            result["accepted_date"] = parse_date(value)
        elif "revised" in key_lower or "revision" in key_lower:
            result["revised_date"] = parse_date(value)
        elif "available online" in key_lower or "publication" in key_lower:
            if result["published_date"] is None:
                result["published_date"] = parse_date(value)

    # Only return if we got at least received + accepted
    if result["received_date"] and result["accepted_date"]:
        return result
    return None


def parse_mdpi(url, html=None):
    """
    Extract dates from MDPI article pages.

    MDPI displays dates in two formats:
    1. Plain text: "Received: 3 April 2018 / Accepted: 11 May 2018"
    2. HTML spans: "Received: <span class="font-semibold">29 November 2024</span>"

    Strips HTML tags before parsing to handle both.
    """
    if html is None:
        html = _curl_fetch(url)
    if not html:
        return None

    # Strip HTML tags to get clean text
    clean = re.sub(r'<[^>]+>', ' ', html)
    clean = re.sub(r'\s+', ' ', clean)

    result = {"received_date": None, "accepted_date": None,
              "revised_date": None, "published_date": None}

    patterns = {
        "received_date": r'(?:Received|Submission received)\s*:\s*(\d{1,2}\s+\w+\s+\d{4})',
        "accepted_date": r'Accepted\s*:\s*(\d{1,2}\s+\w+\s+\d{4})',
        "revised_date": r'Revised\s*:\s*(\d{1,2}\s+\w+\s+\d{4})',
        "published_date": r'Published\s*:\s*(\d{1,2}\s+\w+\s+\d{4})',
    }

    for field, pattern in patterns.items():
        match = re.search(pattern, clean, re.IGNORECASE)
        if match:
            result[field] = parse_date(match.group(1))

    if result["received_date"] and result["accepted_date"]:
        return result
    return None


# ---------------------------------------------------------------------------
# Tier 2: Playwright-based publishers
# ---------------------------------------------------------------------------

def parse_mdpi_playwright(page):
    """
    Extract dates from MDPI article pages using Playwright (MDPI now uses Cloudflare).

    Same text pattern as parse_mdpi() but reads from Playwright page content.
    """
    html = page.content()
    return parse_mdpi(None, html=html)


def parse_taf(page):
    """
    Extract dates from Taylor & Francis article pages (tandfonline.com).

    Uses Playwright page object. Dates appear as visible text:
      "Received 03 Jul 2025, Accepted 27 Feb 2026, Published online ..."
    """
    html = page.content()
    result = {"received_date": None, "accepted_date": None,
              "revised_date": None, "published_date": None}

    patterns = {
        "received_date": r'Received\s+(\d{1,2}\s+\w+\s+\d{4})',
        "accepted_date": r'Accepted\s+(\d{1,2}\s+\w+\s+\d{4})',
        "revised_date": r'Revised\s+(\d{1,2}\s+\w+\s+\d{4})',
        "published_date": r'Published\s+(?:online\s+)?(\d{1,2}\s+\w+\s+\d{4})',
    }

    for field, pattern in patterns.items():
        match = re.search(pattern, html, re.IGNORECASE)
        if match:
            result[field] = parse_date(match.group(1))

    if result["received_date"] and result["accepted_date"]:
        return result
    return None


def _dismiss_cookie_banner(page):
    """Dismiss cookie consent banners that block interactions."""
    # Remove the overlay via JS (more reliable than clicking)
    try:
        page.evaluate('''() => {
            const overlay = document.querySelector('#onetrust-consent-sdk');
            if (overlay) overlay.remove();
            const backdrop = document.querySelector('.onetrust-pc-dark-filter');
            if (backdrop) backdrop.remove();
            // Also remove any generic cookie overlays
            document.querySelectorAll('[class*="cookie-overlay"], [class*="consent-overlay"]')
                .forEach(el => el.remove());
        }''')
        page.wait_for_timeout(500)
    except Exception:
        pass


def parse_oup(page):
    """
    Extract dates from Oxford University Press article pages (academic.oup.com).

    Uses Playwright page object. Requires clicking "Article history" to expand
    the dropdown before dates are visible. Dates appear as:
      "Received: 11 August 2023 Revision received: 20 September 2023 Accepted: 10 October 2023"
    """
    result = {"received_date": None, "accepted_date": None,
              "revised_date": None, "published_date": None}

    # Dismiss cookie banner first
    _dismiss_cookie_banner(page)

    # Click "Article history" to expand the dropdown
    try:
        history_btn = page.locator('text=Article history')
        if history_btn.count() > 0:
            history_btn.first.click(timeout=5000)
            page.wait_for_timeout(2000)
    except Exception:
        pass

    # Get the history section text
    try:
        history_text = page.evaluate('''() => {
            const wrap = document.querySelector('.pub-history-wrap');
            if (wrap) return wrap.innerText;
            return '';
        }''')
    except Exception:
        history_text = ""

    if not history_text:
        return None

    patterns = {
        "received_date": r'Received:\s*(\d{1,2}\s+\w+\s+\d{4})',
        "accepted_date": r'Accepted:\s*(\d{1,2}\s+\w+\s+\d{4})',
        "revised_date": r'Revision received:\s*(\d{1,2}\s+\w+\s+\d{4})',
        "published_date": r'Published:\s*(\d{1,2}\s+\w+\s+\d{4})',
    }

    for field, pattern in patterns.items():
        match = re.search(pattern, history_text, re.IGNORECASE)
        if match:
            result[field] = parse_date(match.group(1))

    if result["received_date"] and result["accepted_date"]:
        return result
    return None


def parse_frontiers(page):
    """
    Extract dates from Frontiers article pages (frontiersin.org).

    Uses Playwright page object. Dates appear in HTML as:
      Received</p><p>30 January 2026</p>
      Accepted</p><p>09 March 2026</p>
    """
    html = page.content()
    # Strip HTML tags to get clean text, then match
    clean = re.sub(r'<[^>]+>', ' ', html)
    clean = re.sub(r'<!--.*?-->', '', clean, flags=re.DOTALL)

    result = {"received_date": None, "accepted_date": None,
              "revised_date": None, "published_date": None}

    patterns = {
        "received_date": r'Received\s+(\d{1,2}\s+\w+\s+\d{4})',
        "accepted_date": r'Accepted\s+(\d{1,2}\s+\w+\s+\d{4})',
        "revised_date": r'Revised\s+(\d{1,2}\s+\w+\s+\d{4})',
        "published_date": r'Published\s+(\d{1,2}\s+\w+\s+\d{4})',
    }

    for field, pattern in patterns.items():
        match = re.search(pattern, clean, re.IGNORECASE)
        if match:
            result[field] = parse_date(match.group(1))

    if result["received_date"] and result["accepted_date"]:
        return result
    return None


# ---------------------------------------------------------------------------
# Publisher routing
# ---------------------------------------------------------------------------

# Map URL domains to parser type
PUBLISHER_DOMAINS = {
    # Tier 1: HTTP (curl)
    "sciencedirect.com": ("http", "elsevier"),
    "linkinghub.elsevier.com": ("http", "elsevier"),
    # Tier 2: Playwright
    "mdpi.com": ("playwright", "mdpi"),
    "tandfonline.com": ("playwright", "taf"),
    "academic.oup.com": ("playwright", "oup"),
    "frontiersin.org": ("playwright", "frontiers"),
}

# Map publisher names (from journal_list) to scrapeable domains
# Used to determine which journals to try scraping
SCRAPEABLE_PUBLISHERS = {
    # Elsevier group
    "Elsevier B.V.", "Elsevier Ltd", "Elsevier Inc.", "Elsevier Ireland Ltd",
    "W.B. Saunders", "W.B. Saunders Ltd", "Academic Press", "Academic Press Inc.",
    "Cell Press",
    # MDPI
    "Multidisciplinary Digital Publishing Institute (MDPI)",
    # Taylor & Francis group
    "Taylor and Francis Ltd.", "Routledge", "Informa Healthcare",
    "Informa UK Limited",
    # Oxford UP
    "Oxford University Press",
    # Frontiers
    "Frontiers Media SA",
}

HTTP_PARSERS = {
    "elsevier": parse_elsevier,
}

PLAYWRIGHT_PARSERS = {
    "mdpi": parse_mdpi_playwright,
    "taf": parse_taf,
    "oup": parse_oup,
    "frontiers": parse_frontiers,
}


def get_parser_for_url(url):
    """
    Determine the parser type and name for a given URL.
    Returns (tier, parser_name) or (None, None) if no parser matches.
    tier is "http" or "playwright".
    """
    url_lower = url.lower()
    for domain, (tier, parser_name) in PUBLISHER_DOMAINS.items():
        if domain in url_lower:
            return tier, parser_name
    return None, None


def parse_http(url, parser_name):
    """Parse an article using an HTTP-based parser (no browser needed)."""
    parser_fn = HTTP_PARSERS.get(parser_name)
    if not parser_fn:
        return None
    return parser_fn(url)


def parse_playwright_page(page, parser_name):
    """Parse an already-loaded Playwright page using the appropriate parser."""
    parser_fn = PLAYWRIGHT_PARSERS.get(parser_name)
    if not parser_fn:
        return None
    return parser_fn(page)
