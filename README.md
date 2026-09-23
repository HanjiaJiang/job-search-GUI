# Job Search GUI (Indeed & LinkedIn)

## Run on Windows
1. Install Python 3.9+ from python.org (check "Add to PATH" during install).
2. Open Command Prompt in this folder and run:
   ```
   pip install -r requirements.txt
   playwright install chromium
   python job_search_gui.py
   ```
3. Fill in / edit the five search fields, the date range, the region, and the
   two keyword groups (or leave the defaults). Click **Run Indeed Search**
   and/or **Run LinkedIn Search** — they run independently.
4. Results land in `results_YYMMDD_<Engine>_<Region>` folders next to the script.

## Turning it into a standalone .exe (optional)
```
pip install pyinstaller
pyinstaller --onefile --windowed --collect-all playwright job_search_gui.py
```
The .exe appears in `dist/`. `--collect-all playwright` ensures Playwright's
internal driver files are bundled correctly.

### One-time browser setup (required before the .exe can use Indeed)
A PyInstaller `--onefile` .exe re-extracts itself to a temporary folder on
every run, so a browser installed to Playwright's normal cache location can
end up "invisible" to the .exe. The script pins Playwright's browser folder
to a fixed, persistent location instead
(`%LOCALAPPDATA%\JobSearchGUI\pw-browsers`). On each Windows machine that
will run the .exe, install the browser into that same folder once, from a
regular Python prompt (not the .exe):
```
set PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\JobSearchGUI\pw-browsers
playwright install chromium
```
After that, the .exe will find it automatically on every run — no need to
repeat this unless the folder is deleted or you move to a new machine.

### Fixing a Windows .exe build
If Indeed still reports "Executable doesn't exist" from the .exe:
1. Confirm you ran the one-time browser setup above, in that exact folder.
2. Rebuild with `--collect-all playwright` if you haven't already.
3. Check `%LOCALAPPDATA%\JobSearchGUI\pw-browsers` actually contains a
   `chromium_headless_shell-*` (or `chromium-*`) folder — if not, the
   install step above didn't complete successfully.

## Known limitations
- Indeed blocks plain HTTP requests (403), so Indeed searches launch a real
  headless Chromium browser via Playwright — slower than LinkedIn's requests-
  based search, and it paginates through result pages (capped at ~20 pages)
  with short delays to avoid tripping rate limits.
- LinkedIn now paginates through its public "load more" endpoint (capped at
  the same ~20-page safety limit as Indeed) and filters results locally by
  each listing's own posted date, since LinkedIn's date-range parameter is
  not reliably enforced for anonymous requests. The filter reads LinkedIn's
  machine-readable date first and falls back to its "3 days ago"-style text
  if that isn't parseable. A listing with no readable date at all is kept
  rather than guessed at, and the GUI log reports how many were filtered or
  kept undated.
- The optional keyword-group filter (on by default) requires a job to match
  at least one keyword from Group 1 AND at least one from Group 2. By
  default matching is whole-word (e.g. "neuro" won't match "neurotech");
  checking "Broad match" switches to plain substring matching instead (e.g.
  "neuro" then matches "neurotech", "neurological", etc.) — off by default,
  since it trades precision for recall. It reads only the job title and
  the actual description container (Indeed's `jobDescriptionText`,
  LinkedIn's `show-more-less-html__markup`/`description__text`), not the
  whole page, so sidebar content like "similar jobs" or "people also
  viewed" can't cause false-positive matches. If a page's structure doesn't
  match those selectors (site redesign), it falls back to whole-page text
  and the GUI log flags how many listings used that fallback, so you know
  when results may be less precise. This means opening every candidate
  job's own page individually, so it noticeably slows a run and adds extra
  load on top of the search itself; with hundreds of results, expect a much
  longer run and a higher chance of temporary blocks. Uncheck it for a
  faster, looser run.
- Both sites can still block, rate-limit, or change their page structure at
  any time; treat this as best-effort, not a guaranteed scraper.
