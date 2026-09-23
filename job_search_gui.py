"""
Job Search GUI - Indeed & LinkedIn
-----------------------------------
Searches Indeed and LinkedIn for up to 5 user-defined queries, restricted to a
date range (days back from today), an optional keyword-group filter verified
against each job's full posting text, and writes the found job-posting URLs
to .txt files inside a dated results folder. Indeed and LinkedIn are run
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

DEFAULT_QUERIES = [
    "biomedical python",
    "biomedical simulation",
    "biomedical algorithm",
    "neural engineering",
    "brain computer interface",
]

DEFAULT_GROUP1 = "engineer, scientist, software, simulation, python, C++, algorithm, algorithms"
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


def extract_main_text(soup: BeautifulSoup, engine: str):
    """Returns (text, used_fallback). Scopes text to the job title + the actual
    description container, so sidebar content (similar/related jobs, nav, JSON-LD,
    etc.) can't cause false-positive keyword matches. Falls back to the whole page
    only if the expected container isn't found (e.g. the site changed its markup),
    in which case used_fallback is True so callers can flag it."""
    title_tag = soup.find("h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    container = None
    if engine == "indeed":
        container = soup.find(id="jobDescriptionText")
        if not container:
            container = soup.find("div", class_=lambda c: c and "jobsearch-jobDescriptionText" in c)
    elif engine == "linkedin":
        container = soup.find("div", class_=lambda c: c and "show-more-less-html__markup" in c)
        if not container:
            container = soup.find("div", class_=lambda c: c and "description__text" in c)

    if container:
        return f"{title} {container.get_text(' ', strip=True)}", False
    return soup.get_text(" ", strip=True), True


def get_page_text_playwright(page, url: str, engine: str, log):
    """Returns (text, used_fallback)."""
    try:
        page.goto(url, timeout=30000)
        page.wait_for_timeout(1000)
        soup = BeautifulSoup(page.content(), "html.parser")
        return extract_main_text(soup, engine)
    except Exception as e:
        log(f"    Could not load job page for verification: {e}")
        return "", False


def get_page_text_requests(url: str, engine: str, log):
    """Returns (text, used_fallback)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return extract_main_text(soup, engine)
    except Exception as e:
        log(f"    Could not load job page for verification: {e}")
        return "", False


def search_indeed(query: str, days: int, domain: str, context, log) -> list:
    """Uses a real headless browser (Playwright) since Indeed blocks plain HTTP requests (403)."""
    urls = []
    seen = set()
    start = 0
    page = context.new_page()
    try:
        while True:
            search_url = f"https://{domain}/jobs?q={quote_plus(query)}&fromage={days}&start={start}"
            page.goto(search_url, timeout=30000)
            page.wait_for_timeout(1500)
            soup = BeautifulSoup(page.content(), "html.parser")

            new_links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/rc/clk" in href or "/viewjob" in href or "jk=" in href:
                    if href.startswith("/"):
                        href = f"https://{domain}" + href
                    href = href.split("&")[0]
                    if href not in seen:
                        seen.add(href)
                        new_links.append(href)

            if not new_links:
                break
            urls.extend(new_links)

            start += RESULTS_PER_PAGE
            time.sleep(random.uniform(2.0, 4.0))
            if start > PAGE_SAFETY_CAP:
                break
    except Exception as e:
        log(f"  [Indeed] Error for '{query}': {e}")
    finally:
        page.close()
    return sorted(urls)


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
            frm, text="Broad match (substring, e.g. 'neuro' also matches 'neurotech') — "
                      "default is whole-word only",
            variable=self.broad_match_var
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
        """Returns (queries, days, region, apply_filter, group1, group2, broad_match) or None if invalid."""
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
        group1 = parse_keywords(self.group1_entry.get())
        group2 = parse_keywords(self.group2_entry.get())
        if apply_filter and (not group1 or not group2):
            messagebox.showwarning(
                "Keyword filter", "Keyword filter is enabled but one of the groups is empty; "
                "filtering will be skipped."
            )
            apply_filter = False

        return queries, days, region, apply_filter, group1, group2, broad_match

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

    def run_indeed_search(self, queries, days, region, apply_filter, group1, group2, broad_match):
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
                detail_page = context.new_page()

                for query in queries:
                    safe_name = sanitize(query)
                    self.log_msg(f"  Searching '{query}'...")
                    urls = search_indeed(query, days, domain, context, self.log_msg)

                    if apply_filter:
                        self.log_msg(f"    Verifying keyword match on {len(urls)} listing(s)...")
                        kept = []
                        fallback_count = 0
                        for u in urls:
                            text, used_fallback = get_page_text_playwright(detail_page, u, "indeed", self.log_msg)
                            if used_fallback:
                                fallback_count += 1
                            if keyword_groups_match(text, group1, group2, broad_match):
                                kept.append(u)
                            time.sleep(random.uniform(1.0, 2.0))
                        self.log_msg(f"    Keyword filter: kept {len(kept)}/{len(urls)}.")
                        if fallback_count:
                            self.log_msg(
                                f"    Note: {fallback_count} page(s) used whole-page text "
                                "(description container not found) — may be less precise."
                            )
                        urls = kept

                    path = os.path.join(folder, f"results_{safe_name}.txt")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write("\n".join(urls))
                    self.log_msg(f"    {len(urls)} URL(s) -> {path}")

                detail_page.close()
                context.close()
                browser.close()
            self.log_msg("\nDone.")
        except Exception as e:
            self.log_msg(f"  [Indeed] Could not start browser: {e}")
            self.log_msg("  See README.md 'Fixing a Windows .exe build' if this happened from the .exe.")
        finally:
            self.root.after(0, lambda: self.set_buttons_enabled(True))

    def run_linkedin_search(self, queries, days, region, apply_filter, group1, group2, broad_match):
        geo_id = COUNTRIES[region]["linkedin_geo"]
        date_str = datetime.now().strftime("%y%m%d")

        folder = get_output_folder(date_str, "LinkedIn", region)
        self.log_msg(f"LinkedIn folder: {folder}")

        for query in queries:
            safe_name = sanitize(query)
            self.log_msg(f"  Searching '{query}'...")
            urls = search_linkedin(query, days, geo_id, self.log_msg)

            if apply_filter:
                self.log_msg(f"    Verifying keyword match on {len(urls)} listing(s)...")
                kept = []
                fallback_count = 0
                for u in urls:
                    text, used_fallback = get_page_text_requests(u, "linkedin", self.log_msg)
                    if used_fallback:
                        fallback_count += 1
                    if keyword_groups_match(text, group1, group2, broad_match):
                        kept.append(u)
                    time.sleep(random.uniform(1.0, 2.0))
                self.log_msg(f"    Keyword filter: kept {len(kept)}/{len(urls)}.")
                if fallback_count:
                    self.log_msg(
                        f"    Note: {fallback_count} page(s) used whole-page text "
                        "(description container not found) — may be less precise."
                    )
                urls = kept

            path = os.path.join(folder, f"results_{safe_name}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(urls))
            self.log_msg(f"    {len(urls)} URL(s) -> {path}")

        self.log_msg("\nDone.")
        self.root.after(0, lambda: self.set_buttons_enabled(True))


if __name__ == "__main__":
    root = tk.Tk()
    app = JobSearchApp(root)
    root.mainloop()
