# Job Search GUI

## Run on Windows
1. Install Python 3.9+ from python.org (check "Add to PATH" during install).
2. Open Command Prompt in this folder and run:
   ```
   pip install -r requirements.txt
   playwright install chromium
   python job_search_gui.py
   ```
3. Fill in / edit the five search fields, the date range, the region, and the
   two keyword groups. Click **Run Indeed Search**, **Run LinkedIn Search**, or **Run Stepstone Search** — they run independently.
4. Results will be saved in a `results_YYMMDD_<Engine>_<Region>.md` file.
5. Running log can be saved in a `log_YYMMDD.txt` file by clicking **Save Log**.

## Turning it into a standalone .exe (optional)
```
pip install pyinstaller
pyinstaller --onefile --windowed --collect-all playwright job_search_gui.py
```
The .exe appears in `dist/`. `--collect-all playwright` ensures Playwright's
internal driver files are bundled correctly.

Note: the .exe still needs Playwright's Chromium on the machine it runs on
(used by Indeed and Stepstone). If it isn't installed yet, open Command Prompt

## AI Disclosure

This project was developed with the assistance of an AI agent (Claude). While AI was utilized to accelerate the development process, autocomplete boilerplate code, and assist with initial drafting, core architectures and integrations have been reviewed and tested by human maintainers.
and run `pip install playwright` followed by `playwright install chromium`.