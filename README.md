# Job Monitor

Python script that finds **new** desk/office job postings, scores them against your resume with Anthropic Claude, and emails a digest only when jobs score **7 or higher**.

## What it does

1. **Scrape companies** — Playwright loads each career URL and extracts job titles + links
2. **Broader keyword search** — Also queries **RemoteOK** + **Arbeitnow** (free) and optionally **Adzuna** for roles like “marketing coordinator”
3. **Filter** — Keeps desk titles containing: coordinator, associate, account, marketing, partnerships, events, creative, communications, brand (retail/store roles excluded)
4. **Deduplicate** — Compares against `seen_jobs.json` so each posting is only processed once
5. **Score** — Sends job + resume to Claude (`claude-sonnet-4-6`) for a 1–10 match score
6. **Email** — If any jobs score ≥ 7, sends one Gmail digest to `wclober1@gmail.com`
7. **Schedule** — Runs every day at **midnight** (local time) when you leave the scheduler running

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

| Variable | Required? | Description |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | **Yes** | Your Anthropic API key |
| `GMAIL_ADDRESS` | **Yes** | Sender Gmail address |
| `GMAIL_APP_PASSWORD` | **Yes** | [Gmail App Password](https://support.google.com/accounts/answer/185833) |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | No | Free keys from [developer.adzuna.com](https://developer.adzuna.com/) for broader US job search |
| `ADZUNA_WHERE` | No | Optional location, e.g. `California` |
| `REMOTEOK_ENABLED` | No | `1` (default) to include RemoteOK remote jobs; `0` to disable |
| `ARBEITNOW_ENABLED` | No | `1` (default) to include Arbeitnow feed; `0` to disable |

> **Gmail tip:** Regular account passwords usually fail with SMTP. Enable 2-Step Verification, then create an App Password and put that in `GMAIL_APP_PASSWORD`.

## Usage

Run the pipeline once (manual trigger):

```bash
python job_monitor.py --once
```

Run on a **daily midnight** schedule (keeps running until you stop it):

```bash
python job_monitor.py
```

Also run once immediately, then wait for midnight:

```bash
python job_monitor.py --now
```

Press `Ctrl+C` to stop the scheduler.

Optional `.env` schedule controls:

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCHEDULE_HOUR` | `0` | Hour of day (0 = midnight) |
| `SCHEDULE_MINUTE` | `0` | Minute |
| `RUN_ON_START` | `0` | `1` to run once when the scheduler starts |

### Alternative: system cron (Mac/Linux)

If you don’t want to leave the Python process open, schedule a one-shot run:

```bash
0 0 * * * cd /path/to/Job-finder- && .venv/bin/python job_monitor.py --once >> job_monitor.log 2>&1
```

## Files

| File | Purpose |
| --- | --- |
| `job_monitor.py` | Main script (career URLs + resume are hardcoded here) |
| `.env` | Secrets (not committed to git) |
| `seen_jobs.json` | Created automatically; stores previously seen titles/URLs |
| `requirements.txt` | Python dependencies |

## Notes

- False listings (login pages, "My Account", investor events, shop/nav links) are filtered out before Claude scoring
- Retail/store-floor roles (sales associate, stock coordinator, etc.) are excluded so desk jobs surface
- Each employer is scraped in a fresh browser page, and "View jobs" links into Greenhouse/Lever/Workday are followed
- Keyword APIs expand beyond your pasted company list; Adzuna is optional, RemoteOK/Arbeitnow work with no key
- If one career page fails to load, the error is logged and the script continues to the next URL
- Console prints status updates throughout each run
- No email is sent on days when nothing scores 7 or higher
- Delete an entry from `seen_jobs.json` (or the whole file) to re-process a posting
