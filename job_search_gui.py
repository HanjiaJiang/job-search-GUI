"""
Job Search GUI - Indeed & LinkedIn
-----------------------------------
Searches Indeed and LinkedIn for up to 5 user-defined queries, restricted to a
date range (days back from today), an optional keyword-group filter verified
against each job's full posting text, and writes the found job-posting URLs
to .txt files inside a dated results folder. Optionally also saves each
posting's text (title, company, location, description) to a .md file per query,
since AI tools generally cannot open Indeed/LinkedIn URLs themselves. Indeed and LinkedIn are run
separately (separate buttons) so one engine's issues don't block the other.

NOTE ON RELIABILITY:
- Indeed and LinkedIn both employ anti-scraping measures (rate limiting,
  bot detection, changing page structure, CAPTCHAs). Treat this as a
  best-effort tool, not a guaranteed scraper. If a site blocks requests
  outright, that engine's .txt files will simply be empty (with a message
  logged in the GUI).
- The keyword-group filter opens each candidate job's own posting page to
  check its full text, which is slower and adds load on top of the search
  itself -- expect longer runs, and a higher chance of temporary blocks,
  when it's enabled with many results.

Requirements: pip install -r requirements.txt
Run:          python job_search_gui.py
"""

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import requests
from bs4 import BeautifulSoup
import sys
import os
import json

# Playwright normally stores its downloaded browser in a stable per-user
# cache. When this script is frozen into a PyInstaller --onefile executable
# (.exe on Windows, .app on macOS), Playwright's path lookup can instead
# resolve to the app's temporary extraction folder, which is deleted after
# every run -- so the browser it finds there is missing on the next launch.
# Pinning PLAYWRIGHT_BROWSERS_PATH to a fixed, persistent, OS-appropriate
# folder avoids that. This MUST be set before `playwright` is imported, and
# the same folder must be used when running `playwright install chromium`
# (see README).
def _default_playwright_browsers_dir() -> str:
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA", home)
    elif sys.platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME", os.path.join(home, ".local", "share"))
    return os.path.join(base, "JobSearchGUI", "pw-browsers")


os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _default_playwright_browsers_dir())

from playwright.sync_api import sync_playwright
import re
import time
import random
import threading
from datetime import datetime, timezone
from urllib.parse import quote_plus

RESULTS_PER_PAGE = 10   # Indeed's pagination step
PAGE_SAFETY_CAP = 300   # stop after ~20 pages even if more exist
FALLBACK_TEXT_LIMIT = 8000       # max chars saved when only whole-page text was found
BLOCKED_PAGE_MIN_CHARS = 300     # whole-page text shorter than this = probably a block page

DEFAULT_QUERIES = [
    "biomedical python",
    "biomedical simulation",
    "biomedical algorithm",
    "neural engineering",
    "brain computer interface",
]

DEFAULT_GROUP1 = "engineer, scientist, software, simulation, python, C++, algorithm"
DEFAULT_GROUP2 = "biomed, medic, clinical, biolog, physio, neural, neuro, biophysic"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# Indeed uses country-specific subdomains; LinkedIn uses a numeric geoId.
# LinkedIn geoIds are unofficial/community-sourced and may drift over time.
COUNTRIES = {
    "Germany":        {"indeed_domain": "de.indeed.com",  "linkedin_geo": "101282230", "locale": "de-DE", "timezone": "Europe/Berlin"},
    "Netherlands":    {"indeed_domain": "nl.indeed.com",  "linkedin_geo": "102890719", "locale": "nl-NL", "timezone": "Europe/Amsterdam"},
    "Switzerland":    {"indeed_domain": "ch.indeed.com",  "linkedin_geo": "106693272", "locale": "de-CH", "timezone": "Europe/Zurich"},
    "United States":  {"indeed_domain": "www.indeed.com", "linkedin_geo": "103644278", "locale": "en-US", "timezone": "America/New_York"},
    "United Kingdom": {"indeed_domain": "uk.indeed.com",  "linkedin_geo": "101165590", "locale": "en-GB", "timezone": "Europe/London"},
    "Australia":      {"indeed_domain": "au.indeed.com",  "linkedin_geo": "101452733", "locale": "en-AU", "timezone": "Australia/Sydney"},
    "Japan":          {"indeed_domain": "jp.indeed.com",  "linkedin_geo": "101355337", "locale": "ja-JP", "timezone": "Asia/Tokyo"},
    "Taiwan":         {"indeed_domain": "tw.indeed.com",  "linkedin_geo": "104187078", "locale": "zh-TW", "timezone": "Asia/Taipei"},
}


def sanitize(query: str) -> str:
    """Turn a query string into a filename-safe token, e.g. 'biomedical python' -> 'biomedical+python'."""
    return "+".join(query.strip().split())


def parse_keywords(text: str) -> list:
    """Comma-separated keyword field -> list of non-empty, stripped keywords."""
    return [k.strip() for k in text.split(",") if k.strip()]


def get_output_folder(date_str: str, engine: str, region: str) -> str:
    """Create (and return) results_YYMMDD_Engine_Region, appending _NN if it already exists."""
    region_label = region.replace(" ", "")
    base_name = f"results_{date_str}_{engine}_{region_label}"
    if not os.path.exists(base_name):
        os.makedirs(base_name)
        return base_name
    n = 2
    while True:
        candidate = f"{base_name}_{n:02d}"
        if not os.path.exists(candidate):
            os.makedirs(candidate)
            return candidate
        n += 1


def contains_keyword(text_lower: str, keyword: str, broad_match: bool = False) -> bool:
    """Case-insensitive match. By default, whole-word/whole-token (e.g. 'neuro' will
    NOT match 'neurotech'). With broad_match=True, any substring occurrence counts
    (e.g. 'neuro' WILL match 'neurotech', 'neurological', etc.)."""
    kw = keyword.strip().lower()
    if not kw:
        return False
    if broad_match:
        return kw in text_lower
    pattern = r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])"
    return re.search(pattern, text_lower) is not None


def keyword_groups_match(text: str, group1: list, group2: list, broad_match: bool = False) -> bool:
    """True if the text contains at least one keyword from EACH group."""
    if not text:
        return False
    text_lower = text.lower()
    return (
        any(contains_keyword(text_lower, k, broad_match) for k in group1)
        and any(contains_keyword(text_lower, k, broad_match) for k in group2)
    )


def _extract_json_string_field(html: str, key: str) -> str | None:
    """Indeed's search-results page embeds the full HTML description of the
    currently 'opened' job as an escaped JSON string inside its bundled JS
    (not a clean top-level JSON blob), keyed e.g. "sanitizedJobDescription".
    This scans for `"<key>":"` and parses the string literal by hand,
    respecting backslash escapes, then JSON-decodes it. Returns None if the
    key isn't present on the page."""
    needle = f'"{key}":"'
    pos = html.find(needle)
    if pos == -1:
        return None
    i = pos + len(needle)
    while i < len(html):
        c = html[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            break
        i += 1
    try:
        return json.loads('"' + html[pos + len(needle):i] + '"')
    except Exception:
        return None


def jk_from_url(url: str) -> str:
    m = re.search(r"jk=([0-9a-fA-F]+)", url)
    return m.group(1) if m else ""


DESCRIPTION_JSON_KEYS = ["sanitizedJobDescription", "descriptionContent"]


def fetch_indeed_description_via_search(page, domain: str, query: str, days: int, jk: str,
                                         log, debug_dir=None, dump_count=None) -> tuple[str, bool]:
    """Loads the search-results page with &vjk=<jk>, which is how Indeed's desktop
    split-view embeds that job's full HTML description as JSON for instant render.
    This stays on the /jobs search endpoint - unlike /viewjob, which redirects this
    script's traffic to a login wall - so it's the workaround for reading full
    descriptions without ever hitting the blocked endpoint. Returns (description, ok).
    """
    url = f"https://{domain}/jobs?q={quote_plus(query)}&fromage={days}&vjk={jk}"
    try:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(1500)
        html = page.content()
        for key in DESCRIPTION_JSON_KEYS:
            desc_html = _extract_json_string_field(html, key)
            if desc_html:
                return _block_text(BeautifulSoup(desc_html, "html.parser")), True
        # No embedded description found - dump for inspection, capped per query.
        if debug_dir is not None and dump_count is not None and dump_count[0] < DEBUG_DUMP_LIMIT:
            os.makedirs(debug_dir, exist_ok=True)
            with open(os.path.join(debug_dir, f"debug_{dump_count[0] + 1}.html"), "w", encoding="utf-8") as f:
                f.write(html)
            dump_count[0] += 1
        return "", False
    except Exception as e:
        log(f"    Could not load full description for job {jk}: {e}")
        return "", False


def _first_text(soup, selectors):
    """Text of the first CSS selector that matches a non-empty element, else ''."""
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(" ", strip=True)
            if t:
                return t
    return ""


def _block_text(el) -> str:
    """Text of an element with paragraph/line/bullet structure kept, without splitting
    inline tags (<b>, <a>, ...) onto their own lines."""
    for br in el.find_all("br"):
        br.replace_with("\n")
    for li in el.find_all("li"):
        li.insert(0, "- ")
    for blk in el.find_all(["p", "li", "div", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6"]):
        blk.append("\n")
    text = el.get_text("")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_job_details(soup: BeautifulSoup, engine: str) -> dict:
    """Extracts title, company, location and description from a job page.
    The description is scoped to the actual description container, so sidebar
    content (similar/related jobs, nav, JSON-LD, etc.) can't cause false-positive
    keyword matches or end up in the saved text. Falls back to the whole page
    only if the container isn't found (e.g. the site changed its markup); in
    that case used_fallback is True so callers can flag it."""
    title = _first_text(soup, ["h1"])
    company = location = ""
    container = None

    if engine == "indeed":
        company = _first_text(soup, [
            '[data-testid="inlineHeader-companyName"]', '[data-company-name="true"]'])
        location = _first_text(soup, [
            '[data-testid="inlineHeader-companyLocation"]', '[data-testid="job-location"]'])
        container = soup.find(id="jobDescriptionText")
        if not container:
            container = soup.find(attrs={"data-testid": "jobDescriptionText"})
        if not container:
            container = soup.find("div", class_=lambda c: c and "jobsearch-jobDescriptionText" in c)
        if not container:
            container = soup.find("div", class_=lambda c: c and "jobsearch-JobComponent-description" in c)
    elif engine == "linkedin":
        company = _first_text(soup, ["a.topcard__org-name-link", "span.topcard__flavor"])
        location = _first_text(soup, ["span.topcard__flavor--bullet"])
        container = soup.find("div", class_=lambda c: c and "show-more-less-html__markup" in c)
        if not container:
            container = soup.find("div", class_=lambda c: c and "description__text" in c)

    if container:
        description, used_fallback = _block_text(container), False
    else:
        description, used_fallback = soup.get_text("\n", strip=True), True

    return {
        "title": title, "company": company, "location": location,
        "description": description, "used_fallback": used_fallback,
        "blocked": _looks_blocked(soup),
    }


BLOCK_PAGE_MARKERS = [
    "verifying you are human", "additional verification required",
    "just a moment", "checking your browser",
    "bestätigen sie, dass sie ein mensch sind", "zusätzliche überprüfung erforderlich",
    "please verify you are a human", "access denied",
    # Indeed's login wall, seen redirecting /viewjob?jk=... to a sign-in page
    "ready to take the next step", "create an account or sign in",
    "erstellen sie ein indeed-konto", "sign in | indeed",
]


def _looks_blocked(soup) -> bool:
    """True if the page text matches known bot-check / consent-wall phrasing,
    so a missing description container can be reported as 'likely blocked'
    rather than a silent parsing miss."""
    head = soup.get_text(" ", strip=True)[:2000].lower()
    return any(marker in head for marker in BLOCK_PAGE_MARKERS)


def _empty_record(url: str) -> dict:
    return {"url": url, "title": "", "company": "", "location": "",
            "description": "", "used_fallback": False, "blocked": False, "ok": False}


def _fill_record(record: dict, html: str, engine: str) -> dict:
    record.update(extract_job_details(BeautifulSoup(html, "html.parser"), engine))
    # Flag as not-ok if the page matched a known block/consent phrase, or if the
    # only text we got was a short whole-page fallback (likely the same thing
    # without matching phrasing).
    record["ok"] = not (
        record["blocked"]
        or (record["used_fallback"] and len(record["description"]) < BLOCKED_PAGE_MIN_CHARS)
    )
    return record


DEBUG_DUMP_LIMIT = 3  # save at most this many raw HTML pages per query, for inspection


def _maybe_dump_debug_html(record: dict, html: str, debug_dir, dump_count: list):
    """Saves the raw HTML of a page that came back blocked/unreadable, up to
    DEBUG_DUMP_LIMIT per query, so the actual markup can be inspected by hand
    when extraction silently fails."""
    if debug_dir is None or record["ok"] or dump_count[0] >= DEBUG_DUMP_LIMIT:
        return
    os.makedirs(debug_dir, exist_ok=True)
    fname = f"debug_{dump_count[0] + 1}.html"
    with open(os.path.join(debug_dir, fname), "w", encoding="utf-8") as f:
        f.write(html)
    dump_count[0] += 1


def fetch_details_playwright(page, url: str, engine: str, log, debug_dir=None, dump_count=None,
                              referer=None) -> dict:
    """Loads a job page in the browser and returns a record dict (see _empty_record).
    referer, if given, is sent as the HTTP Referer header - some sites (Indeed
    included) treat a direct goto() with no referer as more bot-like than a
    "click" arriving from their own search results page."""
    record = _empty_record(url)
    try:
        if referer:
            page.goto(url, timeout=30000, referer=referer)
        else:
            page.goto(url, timeout=30000)
        _accept_cookie_consent(page)
        try:
            page.wait_for_selector(
                "#jobDescriptionText, [data-testid=\"jobDescriptionText\"]", timeout=4000)
        except Exception:
            pass  # container never showed up - fall through, extraction will flag it
        page.wait_for_timeout(500)
        html = page.content()
        _fill_record(record, html, engine)
        # Strongest signal of all: we got redirected clean off the job page (e.g.
        # to a login wall or an /auth error), whatever that landing page's text says.
        jk_match = re.search(r"jk=([0-9a-fA-F]+)", url)
        if jk_match and jk_match.group(1) not in page.url:
            record["blocked"], record["ok"] = True, False
        _maybe_dump_debug_html(record, html, debug_dir, dump_count or [0])
        return record
    except Exception as e:
        log(f"    Could not load job page: {e}")
        return record


def _accept_cookie_consent(page):
    """Best-effort click of a cookie/consent accept button, since some sites
    withhold real content behind the banner until it's dismissed."""
    for sel in ["#onetrust-accept-btn-handler", 'button[aria-label*="Accept" i]',
                'button[aria-label*="Akzeptieren" i]', 'button:has-text("Accept all")',
                'button:has-text("Alle akzeptieren")']:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=1000)
                page.wait_for_timeout(300)
                return
        except Exception:
            pass


def fetch_details_requests(url: str, engine: str, log) -> dict:
    """Loads a job page with plain HTTP and returns a record dict (see _empty_record)."""
    record = _empty_record(url)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return _fill_record(record, resp.text, engine)
    except Exception as e:
        log(f"    Could not load job page: {e}")
        return record


def write_markdown(path: str, engine: str, query: str, region: str, records: list):
    """Writes the saved posting text as one Markdown file, so it can be uploaded to an AI
    directly instead of asking it to open URLs it isn't allowed to read."""
    lines = [f"# {engine} results for '{query}' ({region}) - {len(records)} posting(s)", ""]
    for i, r in enumerate(records, 1):
        lines.append(f"## {i}. {r['title'] or '(no title found)'}")
        lines.append(f"- Company: {r['company'] or 'n/a'}")
        lines.append(f"- Location: {r['location'] or 'n/a'}")
        lines.append(f"- URL: {r['url']}")
        desc = r["description"]
        if r.get("preview_only"):
            lines.append("- NOTE: preview only - Indeed blocks full descriptions for automated "
                          "tools; open the URL in your browser for the full text")
        elif not r["ok"]:
            lines.append("- NOTE: page could not be read (likely blocked); text below may be missing or junk")
        elif r["used_fallback"]:
            lines.append("- NOTE: description container not found; text below is whole-page text and may be noisy")
            desc = desc[:FALLBACK_TEXT_LIMIT]
        lines += ["", desc or "(no description retrieved)", "", "---", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


INDEED_CARD_SELECTORS = [
    "div.job_seen_beacon", "div[data-testid=\"slider_item\"]",
    "td.resultContent", "li > div.cardOutline",
]


def _parse_indeed_card(card, domain: str) -> dict | None:
    """Extracts url/title/company/location/snippet from one Indeed search-result
    card, so keyword filtering and the saved .md can use the preview text that's
    already on the search page - the detail pages (/viewjob) are behind a login
    wall for automated traffic and can't be read this way."""
    a = card.find("a", href=True)
    if not a:
        return None
    href = a["href"]
    if href.startswith("/"):
        href = f"https://{domain}" + href
    m = re.search(r"jk=([0-9a-fA-F]+)", href)
    if not m:
        return None
    url = f"https://{domain}/viewjob?jk={m.group(1)}"

    title = _first_text(card, ["h2.jobTitle span[title]", "h2.jobTitle", "a.jcs-JobTitle span",
                                "[data-testid=\"jobTitle\"]"])
    company = _first_text(card, ["span.companyName", "[data-testid=\"company-name\"]"])
    location = _first_text(card, ["div.companyLocation", "[data-testid=\"text-location\"]"])

    snippet_items = card.select("div.job-snippet ul li, [data-testid=\"jobsnippet_footer\"] ul li, ul.underShelfFooter li")
    snippet = " ".join(li.get_text(" ", strip=True) for li in snippet_items) if snippet_items else \
        _first_text(card, ["div.job-snippet", "[data-testid=\"jobsnippet_footer\"]"])

    return {"url": url, "title": title, "company": company, "location": location, "snippet": snippet}


def search_indeed(query: str, days: int, domain: str, context, log, debug_dir=None) -> list:
    """Uses a real headless browser (Playwright) since Indeed blocks plain HTTP requests (403).
    Returns a list of card dicts (see _parse_indeed_card) rather than bare URLs, since Indeed's
    job detail pages (/viewjob) are behind a login wall for this kind of automated traffic - the
    snippet on the search results page is the only description text we can reliably get."""
    cards_out = []
    seen = set()
    start = 0
    page = context.new_page()
    dumped_search_page = False
    try:
        while True:
            search_url = f"https://{domain}/jobs?q={quote_plus(query)}&fromage={days}&start={start}"
            page.goto(search_url, timeout=30000)
            page.wait_for_timeout(1500)
            html = page.content()
            if debug_dir and not dumped_search_page:
                os.makedirs(debug_dir, exist_ok=True)
                with open(os.path.join(debug_dir, "search_page.html"), "w", encoding="utf-8") as f:
                    f.write(html)
                dumped_search_page = True
            soup = BeautifulSoup(html, "html.parser")

            found_any_card = False
            new_cards = []
            for sel in INDEED_CARD_SELECTORS:
                card_els = soup.select(sel)
                if card_els:
                    found_any_card = True
                    for card in card_els:
                        parsed = _parse_indeed_card(card, domain)
                        if parsed and parsed["url"] not in seen:
                            seen.add(parsed["url"])
                            new_cards.append(parsed)
                    break

            if not found_any_card:
                # Selectors didn't match this layout - fall back to raw link scanning
                # so we at least still collect URLs (with no title/company/snippet).
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/rc/clk" in href or "/viewjob" in href or "jk=" in href:
                        if href.startswith("/"):
                            href = f"https://{domain}" + href
                        m = re.search(r"jk=([0-9a-fA-F]+)", href)
                        url = f"https://{domain}/viewjob?jk={m.group(1)}" if m else href.split("&")[0]
                        if url not in seen:
                            seen.add(url)
                            new_cards.append({"url": url, "title": "", "company": "", "location": "", "snippet": ""})

            if not new_cards:
                break
            cards_out.extend(new_cards)

            start += RESULTS_PER_PAGE
            time.sleep(random.uniform(2.0, 4.0))
            if start > PAGE_SAFETY_CAP:
                break
    except Exception as e:
        log(f"  [Indeed] Error for '{query}': {e}")
    finally:
        page.close()
    return sorted(cards_out, key=lambda c: c["url"])


def _posted_days_ago(card):
    """Returns how many days ago a LinkedIn card was posted, or None if undeterminable.
    Tries the machine-readable `datetime` attribute first; LinkedIn often suffixes it
    with 'Z', which datetime.fromisoformat() cannot parse on Python < 3.11, so that
    attempt is wrapped and falls back to the human-readable '3 days ago' text, which
    is present on every card regardless of Python version or attribute format.
    """
    time_tag = card.find("time")
    if not time_tag:
        return None

    dt_attr = time_tag.get("datetime", "")
    if dt_attr:
        try:
            posted = datetime.fromisoformat(dt_attr.replace("Z", "+00:00"))
            if posted.tzinfo is None:
                posted = posted.replace(tzinfo=timezone.utc)
            return max((datetime.now(timezone.utc) - posted).days, 0)
        except ValueError:
            pass

    text = time_tag.get_text(strip=True).lower()
    if not text or "new" in text or "just now" in text or text == "today":
        return 0
    if "yesterday" in text:
        return 1
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month|year)", text)
    if not m:
        return None
    value, unit = int(m.group(1)), m.group(2)
    return {"minute": 0, "hour": 0, "day": value, "week": value * 7,
            "month": value * 30, "year": value * 365}.get(unit)


def search_linkedin(query: str, days: int, geo_id: str, log) -> list:
    """Paginates LinkedIn's public 'load more' endpoint (the single search page
    only server-renders the first batch), and filters by each listing's own
    posted date, since the f_TPR date-range parameter is not reliably honored
    for anonymous/guest requests."""
    seconds = int(days) * 86400

    urls = []
    seen = set()
    start = 0
    skipped_old = 0
    undated = 0

    while True:
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
            f"keywords={quote_plus(query)}&f_TPR=r{seconds}&geoId={geo_id}&start={start}"
        )
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception as e:
            log(f"  [LinkedIn] Error for '{query}' at start={start}: {e}")
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        cards = soup.find_all("div", class_="base-card")
        if not cards:
            break

        new_count = 0
        for card in cards:
            a = card.find("a", href=True)
            if not a:
                continue
            href = a["href"].split("?")[0]
            if href in seen:
                continue
            seen.add(href)

            days_ago = _posted_days_ago(card)
            if days_ago is None:
                undated += 1
            elif days_ago > days:
                skipped_old += 1
                continue

            urls.append(href)
            new_count += 1

        if new_count == 0:
            break
        start += 25
        time.sleep(random.uniform(1.5, 3.0))
        if start > PAGE_SAFETY_CAP:
            break

    if skipped_old:
        log(f"  [LinkedIn] Filtered out {skipped_old} listing(s) older than {days} day(s).")
    if undated:
        log(f"  [LinkedIn] {undated} listing(s) had no readable post date and were kept.")
    return sorted(urls)


class JobSearchApp:
    def __init__(self, root):
        self.root = root
        root.title("Job Search Tool - Indeed & LinkedIn")
        root.geometry("680x700")

        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(
            frm, text="Search strings (leave blank to skip):", font=("Segoe UI", 10, "bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        self.query_entries = []
        for i, default in enumerate(DEFAULT_QUERIES):
            ttk.Label(frm, text=f"Search {i + 1}:").grid(row=i + 1, column=0, sticky="w", pady=3)
            e = ttk.Entry(frm, width=50)
            e.insert(0, default)
            e.grid(row=i + 1, column=1, sticky="we", pady=3)
            self.query_entries.append(e)

        row = len(DEFAULT_QUERIES) + 1
        ttk.Label(frm, text="Date range (days back):").grid(row=row, column=0, sticky="w", pady=(12, 3))
        self.days_entry = ttk.Entry(frm, width=10)
        self.days_entry.insert(0, "7")
        self.days_entry.grid(row=row, column=1, sticky="w", pady=(12, 3))

        row += 1
        ttk.Label(frm, text="Region:").grid(row=row, column=0, sticky="w", pady=3)
        self.region_combo = ttk.Combobox(
            frm, values=list(COUNTRIES.keys()), state="readonly", width=20
        )
        self.region_combo.set("Germany")
        self.region_combo.grid(row=row, column=1, sticky="w", pady=3)

        row += 1
        self.filter_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm, text="Apply keyword-group filter (job must match >=1 keyword from EACH group below)",
            variable=self.filter_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 3))

        row += 1
        self.broad_match_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm, text="Broad match (substring, e.g. 'neuro' also matches 'neurotech')",
            variable=self.broad_match_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=3)

        row += 1
        self.save_text_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frm, text="Save posting text to .md file (so an AI can read it without opening URLs)",
            variable=self.save_text_var
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=3)

        row += 1
        ttk.Label(frm, text="Group 1 (comma-separated):").grid(row=row, column=0, sticky="w", pady=3)
        self.group1_entry = ttk.Entry(frm, width=50)
        self.group1_entry.insert(0, DEFAULT_GROUP1)
        self.group1_entry.grid(row=row, column=1, sticky="we", pady=3)

        row += 1
        ttk.Label(frm, text="Group 2 (comma-separated):").grid(row=row, column=0, sticky="w", pady=3)
        self.group2_entry = ttk.Entry(frm, width=50)
        self.group2_entry.insert(0, DEFAULT_GROUP2)
        self.group2_entry.grid(row=row, column=1, sticky="we", pady=3)

        row += 1
        btn_frame = ttk.Frame(frm)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=14)
        self.indeed_btn = ttk.Button(btn_frame, text="Run Indeed Search", command=self.on_run_indeed)
        self.indeed_btn.pack(side="left", padx=6)
        self.linkedin_btn = ttk.Button(btn_frame, text="Run LinkedIn Search", command=self.on_run_linkedin)
        self.linkedin_btn.pack(side="left", padx=6)

        row += 1
        self.log = scrolledtext.ScrolledText(frm, width=76, height=18, state="disabled")
        self.log.grid(row=row, column=0, columnspan=2, sticky="nsew")

        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(row, weight=1)

    def log_msg(self, msg: str):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")
        self.root.update_idletasks()

    def set_buttons_enabled(self, enabled: bool):
        state = "normal" if enabled else "disabled"
        self.indeed_btn.configure(state=state)
        self.linkedin_btn.configure(state=state)

    def _gather_common_inputs(self):
        """Returns (queries, days, region, apply_filter, group1, group2, broad_match, save_text) or None if invalid."""
        queries = [e.get().strip() for e in self.query_entries]
        queries = [q for q in queries if q]
        if not queries:
            messagebox.showwarning("No input", "Please enter at least one search string.")
            return None

        days_str = self.days_entry.get().strip() or "7"
        try:
            days = int(days_str)
            if days <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid input", "Date range must be a positive whole number of days.")
            return None

        region = self.region_combo.get() or "Germany"
        apply_filter = self.filter_var.get()
        broad_match = self.broad_match_var.get()
        save_text = self.save_text_var.get()
        group1 = parse_keywords(self.group1_entry.get())
        group2 = parse_keywords(self.group2_entry.get())
        if apply_filter and (not group1 or not group2):
            messagebox.showwarning(
                "Keyword filter", "Keyword filter is enabled but one of the groups is empty; "
                "filtering will be skipped."
            )
            apply_filter = False

        return queries, days, region, apply_filter, group1, group2, broad_match, save_text

    def on_run_indeed(self):
        inputs = self._gather_common_inputs()
        if inputs is None:
            return
        self.set_buttons_enabled(False)
        threading.Thread(target=self.run_indeed_search, args=inputs, daemon=True).start()

    def on_run_linkedin(self):
        inputs = self._gather_common_inputs()
        if inputs is None:
            return
        self.set_buttons_enabled(False)
        threading.Thread(target=self.run_linkedin_search, args=inputs, daemon=True).start()

    def _process_listings(self, urls, fetch, apply_filter, save_text, group1, group2, broad_match):
        """Fetches each posting (only if filtering or saving text) and returns
        (kept_urls, records, all_records). `fetch` is a callable url -> record dict.
        all_records is every fetched record regardless of keyword filter, so blocked/
        fallback pages can still be written to .md for inspection even when 0 pass
        the filter."""
        if not (apply_filter or save_text):
            return urls, [], []
        self.log_msg(f"    Fetching {len(urls)} posting(s)...")
        kept, records, all_records = [], [], []
        fallback_count = blocked_count = failed_count = 0
        for u in urls:
            rec = fetch(u)
            all_records.append(rec)
            if rec["blocked"]:
                blocked_count += 1
            elif not rec["ok"]:
                failed_count += 1
            elif rec["used_fallback"]:
                fallback_count += 1
            text = " ".join(f"{rec['title']} {rec['description']}".split())
            if not apply_filter or keyword_groups_match(text, group1, group2, broad_match):
                kept.append(u)
                records.append(rec)
            time.sleep(random.uniform(1.0, 2.0))
        if apply_filter:
            self.log_msg(f"    Keyword filter: kept {len(kept)}/{len(urls)}.")
        if blocked_count:
            self.log_msg(
                f"    Note: {blocked_count} page(s) hit a bot-check/consent wall instead of "
                "the real posting - see debug_*.html in the results folder."
            )
        if fallback_count:
            self.log_msg(
                f"    Note: {fallback_count} page(s) used whole-page text "
                "(description container not found) - may be less precise."
            )
        if failed_count:
            self.log_msg(f"    Note: {failed_count} page(s) could not be read (failed to load).")
        return kept, records, all_records

    def _process_indeed_cards(self, cards, apply_filter, save_text, group1, group2, broad_match,
                               page=None, domain=None, query=None, days=None, debug_dir=None):
        """Builds a record per Indeed card (title/company/location from the search page) and,
        when page/domain/query/days are given, tries to fetch each job's FULL description via
        the &vjk= search-page embed (see fetch_indeed_description_via_search) rather than the
        blocked /viewjob endpoint. Falls back to the card's snippet, or just the title, if that
        fetch comes back empty. Returns (kept_urls, records)."""
        kept, records = [], []
        full_fetch = page is not None and (apply_filter or save_text)
        full_ok_count = full_fail_count = 0
        dump_count = [0]
        for c in cards:
            description, used_full = c["snippet"], False
            if full_fetch:
                desc, ok = fetch_indeed_description_via_search(
                    page, domain, query, days, jk_from_url(c["url"]), self.log_msg, debug_dir, dump_count)
                time.sleep(random.uniform(1.0, 2.0))
                if ok:
                    description, used_full = desc, True
                    full_ok_count += 1
                else:
                    full_fail_count += 1

            text = " ".join(f"{c['title']} {description}".split())
            if not apply_filter or keyword_groups_match(text, group1, group2, broad_match):
                kept.append(c["url"])
                records.append({
                    "url": c["url"], "title": c["title"], "company": c["company"],
                    "location": c["location"],
                    "description": description or "(no description text found)",
                    "used_fallback": False, "blocked": False,
                    "ok": bool(c["title"] or description),
                    "preview_only": not used_full,
                })
        if apply_filter:
            self.log_msg(f"    Keyword filter: kept {len(kept)}/{len(cards)}.")
        if full_fetch:
            self.log_msg(f"    Full descriptions: got {full_ok_count}/{len(cards)} via search-page embed.")
            if full_fail_count:
                self.log_msg(
                    f"    Note: {full_fail_count} job(s) had no embedded description "
                    "(falling back to preview snippet/title) - see debug_*.html."
                )
        empty_count = sum(1 for c in cards if not c["title"] and not c["snippet"])
        if empty_count:
            self.log_msg(
                f"    Note: {empty_count}/{len(cards)} card(s) had no title/snippet text - "
                "Indeed's search page layout may have changed; see debug/search_page.html."
            )
        return kept, records

    def _save_results(self, folder, engine, query, region, urls, records, all_records):
        safe_name = sanitize(query)
        path = os.path.join(folder, f"results_{safe_name}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(urls))
        self.log_msg(f"    {len(urls)} URL(s) -> {path}")
        # Prefer the kept (filtered) records; if the filter kept nothing but pages
        # were still fetched, write those instead so blocked/fallback pages are
        # visible rather than silently producing no .md at all.
        to_write = records or all_records
        if to_write:
            md_path = os.path.join(folder, f"results_{safe_name}.md")
            write_markdown(md_path, engine, query, region, to_write)
            self.log_msg(f"    Posting text -> {md_path}")

    def run_indeed_search(self, queries, days, region, apply_filter, group1, group2, broad_match, save_text):
        domain = COUNTRIES[region]["indeed_domain"]
        locale = COUNTRIES[region]["locale"]
        timezone_id = COUNTRIES[region]["timezone"]
        date_str = datetime.now().strftime("%y%m%d")

        folder = get_output_folder(date_str, "Indeed", region)
        self.log_msg(f"Indeed folder: {folder}")
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale=locale,
                    timezone_id=timezone_id,
                    viewport={"width": 1366, "height": 768},
                )
                for query in queries:
                    self.log_msg(f"  Searching '{query}'...")
                    debug_dir = os.path.join(folder, f"debug_{sanitize(query)}")
                    cards = search_indeed(query, days, domain, context, self.log_msg, debug_dir)
                    self.log_msg(f"    Found {len(cards)} posting(s).")
                    # Fetching full descriptions per job (via /viewjob or the &vjk= search embed)
                    # reliably triggers a Cloudflare Turnstile CAPTCHA after a handful of requests -
                    # not a fixable-by-this-script problem. Sticking to what the search page itself
                    # gives us: title/company/location/URL, filtered on title text only.
                    urls, records = self._process_indeed_cards(
                        cards, apply_filter, save_text, group1, group2, broad_match)
                    self._save_results(folder, "Indeed", query, region, urls, records, [])

                context.close()
                browser.close()
            self.log_msg("\nDone.")
        except Exception as e:
            self.log_msg(f"  [Indeed] Could not start browser: {e}")
            self.log_msg("  See README.md 'Fixing a Windows .exe build' if this happened from the .exe.")
        finally:
            self.root.after(0, lambda: self.set_buttons_enabled(True))

    def run_linkedin_search(self, queries, days, region, apply_filter, group1, group2, broad_match, save_text):
        geo_id = COUNTRIES[region]["linkedin_geo"]
        date_str = datetime.now().strftime("%y%m%d")

        folder = get_output_folder(date_str, "LinkedIn", region)
        self.log_msg(f"LinkedIn folder: {folder}")

        try:
            for query in queries:
                self.log_msg(f"  Searching '{query}'...")
                urls = search_linkedin(query, days, geo_id, self.log_msg)
                urls, records, all_records = self._process_listings(
                    urls,
                    lambda u: fetch_details_requests(u, "linkedin", self.log_msg),
                    apply_filter, save_text, group1, group2, broad_match,
                )
                self._save_results(folder, "LinkedIn", query, region, urls, records, all_records)
            self.log_msg("\nDone.")
        finally:
            self.root.after(0, lambda: self.set_buttons_enabled(True))


if __name__ == "__main__":
    root = tk.Tk()
    app = JobSearchApp(root)
    root.mainloop()
