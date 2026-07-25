# Jr. Copywriter Job Monitor (Ad Agencies)

Python script that checks **advertising holding-company and agency career pages** every 24 hours, finds **new** junior / associate copywriter postings, scores them against your resume with Anthropic Claude, and emails a digest only when jobs score **7 or higher**.

## What it does

1. **Scrape** — Playwright loads each career URL (handles JavaScript-rendered pages) and extracts job titles + links
2. **Filter** — Keeps junior copywriter-style titles (`copywriter`, `copywriting`, `junior creative`, etc.) and drops senior / leadership titles (`senior`, `director`, `ECD`, etc.)
3. **Deduplicate** — Compares against `seen_jobs.json` so each posting is only processed once
4. **Score** — Sends job + resume to Claude (`claude-sonnet-4-6`) for a 1–10 match score focused on agency copywriting fit
5. **Email** — If any jobs score ≥ 7, sends one Gmail digest to `wclober1@gmail.com`
6. **Schedule** — APScheduler re-runs the pipeline every 24 hours

## Sources

Career pages are hardcoded in `CAREER_SOURCES` inside `job_monitor.py`, covering:

- **Holding companies:** WPP, Publicis Groupe, Dentsu, Havas, Stagwell
- **Network agencies:** Ogilvy, VML, Grey, Leo, Digitas, McCann, BBDO, TBWA, Omnicom Media, and related PR/media brands
- **Independents / creatives:** Wieden+Kennedy, Droga5, R/GA, Huge, Mother, Deutsch, Fallon, The Martin Agency, Mischief, and others

## Setup

```bash
# 1. Create a virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt
playwright install chromium

# 3. Configure secrets
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
| --- | --- |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_ADDRESS` | Sender Gmail address (default: `wclober1@gmail.com`) |
| `GMAIL_APP_PASSWORD` | [Gmail App Password](https://support.google.com/accounts/answer/185833) (16 characters) |

> **Gmail tip:** Regular account passwords usually fail with SMTP. Enable 2-Step Verification, then create an App Password and put that in `GMAIL_APP_PASSWORD`.

## Usage

Run the pipeline once (manual trigger):

```bash
python job_monitor.py --once
```

Run on a 24-hour schedule (runs immediately, then every 24 hours):

```bash
python job_monitor.py
```

Press `Ctrl+C` to stop the scheduler.

## Files

| File | Purpose |
| --- | --- |
| `job_monitor.py` | Main script (agency career URLs + resume are hardcoded here) |
| `.env` | Secrets (not committed to git) |
| `seen_jobs.json` | Created automatically; stores previously seen titles/URLs |
| `requirements.txt` | Python dependencies |

## Notes

- If one career page fails to load, the error is logged and the script continues to the next URL
- Console prints status updates throughout each run
- No email is sent on days when nothing scores 7 or higher
- Delete an entry from `seen_jobs.json` (or the whole file) to re-process a posting
- To add or remove agencies, edit the `CAREER_SOURCES` list in `job_monitor.py`
