# Job Monitor

Python script that checks company career pages on a schedule, finds **new** job postings, scores how well your resume matches each role with Anthropic Claude (1–10), and emails you only when the score is **7 or higher**.

Previously seen jobs are stored in `seen_jobs.json` so you are only notified about new postings. Playwright is used so dynamically rendered career pages work.

## Features

- Configurable list of company career URLs + CSS selectors
- Playwright (Chromium) scraping for JS-heavy boards (Greenhouse, Lever, custom SPAs)
- Claude match scoring with a short reason
- Email alerts only for scores ≥ threshold (default 7)
- JSON persistence of seen jobs (id, title, URL, score, notified flag)
- Runs once (`--once`) or every 24 hours by default

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env
cp config.example.yaml config.yaml
cp resume.example.txt resume.txt
```

Edit:

1. **`.env`** — `ANTHROPIC_API_KEY`, SMTP settings, and `EMAIL_TO`
2. **`config.yaml`** — company career URLs and CSS selectors for job links
3. **`resume.txt`** — your resume as plain text

### Gmail note

Use an [App Password](https://support.google.com/accounts/answer/185833) with `SMTP_HOST=smtp.gmail.com` and `SMTP_PORT=587`.

## Usage

Single check:

```bash
python job_monitor.py --once
```

Continuous monitor (default every 24 hours; runs once immediately, then on the interval):

```bash
python job_monitor.py
```

Options:

```text
--config PATH   Config file (default: config.yaml)
--once          Run one pass and exit
-v / --verbose  Debug logging
```

## Config

Each company entry needs:

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Display name in emails / logs |
| `url` | yes | Careers listing page |
| `job_link_selector` | yes | CSS selector for links to individual jobs |
| `wait_for_selector` | no | Wait until this appears (SPA listings) |
| `description_selector` | no | On the detail page, extract this element; else use full body text |

Environment overrides: `SCORE_THRESHOLD`, `CHECK_INTERVAL_HOURS`, `CLAUDE_MODEL`.

## Seen jobs file

`seen_jobs.json` looks like:

```json
{
  "jobs": {
    "a1b2c3...": {
      "company": "Example Corp",
      "title": "Senior Engineer",
      "url": "https://example.com/jobs/123",
      "first_seen": "2026-07-13T02:00:00+00:00",
      "score": 8,
      "reason": "Strong overlap with Python backend experience.",
      "notified": true
    }
  }
}
```

Job IDs are a hash of the normalized job URL. Delete an entry (or the whole file) to re-process a posting.

## How scoring works

For each new job, the script sends your resume and the job description to Claude and expects JSON:

```json
{"score": 8, "reason": "Strong backend Python overlap; less cloud infra depth."}
```

Only scores ≥ `score_threshold` (default 7) trigger an email.

## Tips for selectors

Inspect a careers page and pick a selector that matches **individual** job links, for example:

- Greenhouse: `a[href*="/jobs/"]`
- Lever: `a.posting-title`
- Custom: `.careers-list a.job-title`

Prefer a `description_selector` that targets the posting body so Claude is not flooded with nav/footer noise.
