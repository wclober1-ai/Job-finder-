# Jr. Copywriter Job Monitor (Ad Agencies)

Python script that checks **advertising holding-company and agency career pages** every morning, finds **new** US junior / associate copywriter postings, scores them against your resume with Anthropic Claude, and emails a digest to `wclober1@gmail.com`.

## What it does

1. **Scrape** — Playwright loads each career URL (handles JavaScript-rendered pages) and extracts job titles + links
2. **Filter** — Keeps junior copywriter-style titles (`copywriter`, `copywriting`, `junior creative`, etc.) and drops senior / leadership titles (`senior`, `director`, `ECD`, etc.)
3. **US only** — Keeps United States locations only (title/URL country codes, then job-description check) to avoid visa issues
4. **Deduplicate** — Compares against `seen_jobs.json` so each posting is only processed once
5. **Score** — Sends job + resume to Claude (`claude-sonnet-4-6`) for a 1–10 match score focused on agency copywriting fit
6. **Email** — Sends a morning Gmail digest to `wclober1@gmail.com` (strong matches scoring ≥ 7, or a short “no hits” summary)
7. **Schedule** — Runs every morning at **8:00 AM Pacific** via GitHub Actions (recommended) or local APScheduler

## Morning email (recommended setup)

Use GitHub Actions so the search runs in the cloud every morning without leaving your laptop on.

1. Merge this branch / open the repo on GitHub
2. Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GMAIL_ADDRESS` | `wclober1@gmail.com` |
| `GMAIL_APP_PASSWORD` | [Gmail App Password](https://support.google.com/accounts/answer/185833) (16 characters) |
| `EMAIL_TO` | Optional; defaults to `GMAIL_ADDRESS` if omitted |

3. Confirm the workflow exists at `.github/workflows/daily-jr-copywriter.yml`
4. Optionally open **Actions → Daily jr. copywriter monitor → Run workflow** once to test

The schedule is `0 15 * * *` UTC (~8:00 AM Pacific during PDT).

> **Gmail tip:** Regular account passwords usually fail with SMTP. Enable 2-Step Verification, then create an App Password and put that in `GMAIL_APP_PASSWORD`.

## Local setup

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

Edit `.env` and fill in `ANTHROPIC_API_KEY`, `GMAIL_ADDRESS`, and `GMAIL_APP_PASSWORD`.

### Usage

Run once (manual trigger):

```bash
python job_monitor.py --once
```

Run on a morning schedule (runs immediately, then every day at 08:00 America/Los_Angeles):

```bash
python job_monitor.py
```

Press `Ctrl+C` to stop the scheduler.

## Sources

Career pages are hardcoded in `CAREER_SOURCES` inside `job_monitor.py`, covering:

- **Holding companies:** WPP, Publicis Groupe, Dentsu, Havas, Stagwell
- **Network agencies:** Ogilvy, VML, Grey, Leo, Digitas, McCann, DDB/TBWA, FCB, Saatchi, Omnicom Media, and related PR/media brands
- **Independents / creatives:** Wieden+Kennedy, 72andSunny, Droga5, Anomaly, R/GA, Huge, Mother, Deutsch, Cramer-Krasselt, VaynerMedia, IDEO, Pentagram, Frog, Preacher, Zambezi, GS&F, and others

## Files

| File | Purpose |
| --- | --- |
| `job_monitor.py` | Main script (agency career URLs + resume are hardcoded here) |
| `.github/workflows/daily-jr-copywriter.yml` | Morning GitHub Actions schedule + email run |
| `.env` | Secrets (not committed to git) |
| `seen_jobs.json` | Created automatically; stores previously seen titles/URLs |
| `requirements.txt` | Python dependencies |

## Notes

- If one career page fails to load, the error is logged and the script continues to the next URL
- Console prints status updates throughout each run
- By default a morning email is always sent (`ALWAYS_EMAIL_DIGEST=1`), even when nothing scores 7+
- Delete an entry from `seen_jobs.json` (or the whole file) to re-process a posting
- To add or remove agencies, edit the `CAREER_SOURCES` list in `job_monitor.py`
