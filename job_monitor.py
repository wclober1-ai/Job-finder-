#!/usr/bin/env python3
"""
Job board monitor: scrape career pages, score new roles with Claude, email strong matches.

Runs on a 24-hour schedule by default. Uses Playwright so dynamically rendered
career sites (Greenhouse, Lever, custom SPAs, etc.) work reliably.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import schedule
import yaml
from anthropic import Anthropic
from dotenv import load_dotenv
from playwright.sync_api import Browser, Page, sync_playwright

logger = logging.getLogger("job_monitor")

DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_THRESHOLD = 7
DEFAULT_INTERVAL_HOURS = 24
NAV_TIMEOUT_MS = 45_000
BODY_TEXT_LIMIT = 12_000

# Broad selector that matches common ATS / career-site job links.
DEFAULT_JOB_LINK_SELECTOR = ", ".join(
    [
        "a[href*='/job']",
        "a[href*='/jobs/']",
        "a[href*='/careers/']",
        "a[href*='/career/']",
        "a[href*='/position']",
        "a[href*='/opening']",
        "a[href*='myworkdayjobs.com']",
        "a[href*='greenhouse.io']",
        "a[href*='lever.co']",
        "a[href*='icims.com']",
        "a[href*='smartrecruiters.com']",
        "a[href*='jobvite.com']",
        "a[href*='taleo.net']",
        "a[href*='workable.com']",
        "a[href*='ashbyhq.com']",
        "a.posting-title",
        ".opening a",
        "[data-automation-id='jobTitle'] a",
        "a[data-testid*='job']",
    ]
)
DEFAULT_DESCRIPTION_SELECTOR = ", ".join(
    [
        "main",
        "article",
        "[role='main']",
        ".job-description",
        "#content",
        ".content",
        "[data-automation-id='jobPostingDescription']",
        ".section-wrapper",
        ".posting",
    ]
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CompanyConfig:
    name: str
    url: str
    job_link_selector: str = DEFAULT_JOB_LINK_SELECTOR
    wait_for_selector: str | None = None
    description_selector: str | None = DEFAULT_DESCRIPTION_SELECTOR


@dataclass
class AppConfig:
    resume_path: Path
    seen_jobs_path: Path
    score_threshold: int
    check_interval_hours: int
    claude_model: str
    companies: list[CompanyConfig]


@dataclass
class JobPosting:
    company: str
    title: str
    url: str
    description: str
    job_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.job_id = _stable_job_id(self.url)


@dataclass
class MatchResult:
    job: JobPosting
    score: int
    reason: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stable_job_id(url: str) -> str:
    """Normalize a job URL into a stable id for de-duplication."""
    parsed = urlparse(url.strip())
    # Drop fragment and common tracking query params while keeping path identity.
    path = parsed.path.rstrip("/") or "/"
    normalized = f"{parsed.scheme}://{parsed.netloc.lower()}{path}".lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _clean_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _title_from_link(text: str, href: str) -> str:
    title = _clean_text(text)
    if title:
        return title[:200]
    # Fall back to a readable path segment
    path = urlparse(href).path.rstrip("/").split("/")
    return path[-1].replace("-", " ").replace("_", " ").title() if path else href


# ---------------------------------------------------------------------------
# Config / storage
# ---------------------------------------------------------------------------


def load_config(path: Path) -> AppConfig:
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    companies_raw = raw.get("companies") or []
    if not companies_raw:
        raise ValueError(f"No companies configured in {path}")

    companies = [
        CompanyConfig(
            name=c["name"],
            url=c["url"],
            job_link_selector=c.get("job_link_selector") or DEFAULT_JOB_LINK_SELECTOR,
            wait_for_selector=c.get("wait_for_selector"),
            description_selector=(
                DEFAULT_DESCRIPTION_SELECTOR
                if "description_selector" not in c
                else c.get("description_selector")
            ),
        )
        for c in companies_raw
    ]

    base = path.parent
    resume = Path(raw.get("resume_path", "resume.txt"))
    seen = Path(raw.get("seen_jobs_path", "seen_jobs.json"))
    if not resume.is_absolute():
        resume = base / resume
    if not seen.is_absolute():
        seen = base / seen

    return AppConfig(
        resume_path=resume,
        seen_jobs_path=seen,
        score_threshold=int(
            os.getenv("SCORE_THRESHOLD", raw.get("score_threshold", DEFAULT_THRESHOLD))
        ),
        check_interval_hours=int(
            os.getenv(
                "CHECK_INTERVAL_HOURS",
                raw.get("check_interval_hours", DEFAULT_INTERVAL_HOURS),
            )
        ),
        claude_model=os.getenv(
            "CLAUDE_MODEL", raw.get("claude_model", DEFAULT_MODEL)
        ),
        companies=companies,
    )


def load_seen_jobs(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"jobs": {}}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if "jobs" not in data or not isinstance(data["jobs"], dict):
        data["jobs"] = {}
    return data


def save_seen_jobs(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def load_resume(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"Resume file not found: {path}. Copy resume.example.txt to resume.txt."
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"Resume file is empty: {path}")
    return text


# ---------------------------------------------------------------------------
# Scraping (Playwright)
# ---------------------------------------------------------------------------


def _collect_job_links(page: Page, company: CompanyConfig) -> list[tuple[str, str]]:
    """Return unique (title, absolute_url) pairs from a listing page."""
    wait_sel = company.wait_for_selector or company.job_link_selector
    try:
        page.wait_for_selector(wait_sel, timeout=NAV_TIMEOUT_MS)
    except Exception:
        logger.warning(
            "Timed out waiting for %s on %s — continuing with whatever loaded",
            wait_sel,
            company.url,
        )

    # Scroll to trigger lazy-loaded listings
    for _ in range(4):
        page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        page.wait_for_timeout(400)

    anchors = page.query_selector_all(company.job_link_selector)
    seen_urls: set[str] = set()
    results: list[tuple[str, str]] = []

    for anchor in anchors:
        href = anchor.get_attribute("href")
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        absolute = urljoin(company.url, href)
        # Skip pure listing roots that aren't individual postings when possible
        if absolute.rstrip("/") == company.url.rstrip("/"):
            continue
        if absolute in seen_urls:
            continue
        seen_urls.add(absolute)
        title = _title_from_link(anchor.inner_text() or "", absolute)
        results.append((title, absolute))

    return results


def _extract_description(page: Page, company: CompanyConfig) -> str:
    if company.description_selector:
        try:
            page.wait_for_selector(company.description_selector, timeout=15_000)
            parts = [
                el.inner_text()
                for el in page.query_selector_all(company.description_selector)
                if el
            ]
            text = _clean_text("\n\n".join(parts))
            if text:
                return text[:BODY_TEXT_LIMIT]
        except Exception:
            logger.debug(
                "description_selector %s failed on %s; falling back to body",
                company.description_selector,
                page.url,
            )

    body = page.inner_text("body")
    return _clean_text(body)[:BODY_TEXT_LIMIT]


def scrape_company(browser: Browser, company: CompanyConfig) -> list[JobPosting]:
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (compatible; JobMonitor/1.0; +https://github.com/local/job-monitor)"
        ),
        viewport={"width": 1280, "height": 900},
    )
    page = context.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)

    jobs: list[JobPosting] = []
    try:
        logger.info("Fetching listings for %s (%s)", company.name, company.url)
        page.goto(company.url, wait_until="domcontentloaded")
        links = _collect_job_links(page, company)
        logger.info("Found %d job link(s) on %s", len(links), company.name)

        for title, job_url in links:
            try:
                page.goto(job_url, wait_until="domcontentloaded")
                description = _extract_description(page, company)
                # Prefer the page <title> / h1 if the listing text was sparse
                if len(title) < 4:
                    h1 = page.query_selector("h1")
                    if h1 and h1.inner_text().strip():
                        title = _clean_text(h1.inner_text())[:200]
                jobs.append(
                    JobPosting(
                        company=company.name,
                        title=title,
                        url=job_url,
                        description=description or "(No description extracted)",
                    )
                )
            except Exception:
                logger.exception("Failed to scrape job page %s", job_url)
    except Exception:
        logger.exception("Failed to scrape company page %s", company.url)
    finally:
        context.close()

    return jobs


def scrape_all(companies: list[CompanyConfig]) -> list[JobPosting]:
    all_jobs: list[JobPosting] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for company in companies:
                all_jobs.extend(scrape_company(browser, company))
        finally:
            browser.close()
    return all_jobs


# ---------------------------------------------------------------------------
# Claude scoring
# ---------------------------------------------------------------------------


def score_job(
    client: Anthropic,
    model: str,
    resume: str,
    job: JobPosting,
) -> MatchResult:
    prompt = f"""You are helping a job seeker decide whether to apply.

Score how well the candidate's resume matches this job on a scale of 1-10.
Be practical: 10 = excellent fit, 7 = strong enough to apply, 4 = weak, 1 = irrelevant.

Return ONLY valid JSON with this exact shape (no markdown):
{{"score": <integer 1-10>, "reason": "<one or two short sentences>"}}

=== CANDIDATE RESUME ===
{resume[:BODY_TEXT_LIMIT]}

=== JOB ===
Company: {job.company}
Title: {job.title}
URL: {job.url}

Description:
{job.description[:BODY_TEXT_LIMIT]}
"""

    message = client.messages.create(
        model=model,
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    ).strip()

    score, reason = _parse_score_response(text)
    return MatchResult(job=job, score=score, reason=reason)


def _parse_score_response(text: str) -> tuple[int, str]:
    # Strip optional markdown fences
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
        score = int(data["score"])
        reason = str(data.get("reason", "")).strip() or "No reason provided."
        return max(1, min(10, score)), reason
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        match = re.search(r'"?score"?\s*[:=]\s*(\d{1,2})', text, re.IGNORECASE)
        score = int(match.group(1)) if match else 1
        score = max(1, min(10, score))
        return score, _clean_text(text)[:400] or "Could not parse model response."


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def send_match_email(match: MatchResult) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    # Gmail App Passwords are often shown with spaces; SMTP expects them removed.
    password = os.environ["SMTP_PASSWORD"].replace(" ", "")
    email_from = os.getenv("EMAIL_FROM", user)
    email_to = os.environ["EMAIL_TO"]

    job = match.job
    subject = f"[Job match {match.score}/10] {job.title} @ {job.company}"
    body = f"""New role that looks like a good fit for your resume.

Score: {match.score}/10
Reason: {match.reason}

Company: {job.company}
Title: {job.title}
URL: {job.url}

--- Job description (truncated) ---
{job.description[:3000]}
"""

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(body)

    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        if port != 25:
            smtp.starttls()
            smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(msg)

    logger.info("Emailed match: %s (%d/10)", job.title, match.score)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def require_env() -> None:
    missing = [
        key
        for key in (
            "ANTHROPIC_API_KEY",
            "SMTP_HOST",
            "SMTP_USER",
            "SMTP_PASSWORD",
            "EMAIL_TO",
        )
        if not os.getenv(key)
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in values."
        )


def mark_seen(
    seen: dict[str, Any],
    job: JobPosting,
    *,
    score: int | None = None,
    reason: str | None = None,
    notified: bool = False,
) -> None:
    entry: dict[str, Any] = {
        "company": job.company,
        "title": job.title,
        "url": job.url,
        "first_seen": datetime.now(timezone.utc).isoformat(),
        "notified": notified,
    }
    if score is not None:
        entry["score"] = score
    if reason is not None:
        entry["reason"] = reason
    # Preserve original first_seen if re-processing somehow
    existing = seen["jobs"].get(job.job_id)
    if existing and "first_seen" in existing:
        entry["first_seen"] = existing["first_seen"]
    seen["jobs"][job.job_id] = entry


def run_once(config: AppConfig) -> None:
    require_env()
    resume = load_resume(config.resume_path)
    seen = load_seen_jobs(config.seen_jobs_path)
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    logger.info("Starting scrape of %d company page(s)", len(config.companies))
    postings = scrape_all(config.companies)

    new_jobs = [j for j in postings if j.job_id not in seen["jobs"]]
    logger.info(
        "Scraped %d posting(s); %d new",
        len(postings),
        len(new_jobs),
    )

    for job in new_jobs:
        try:
            match = score_job(client, config.claude_model, resume, job)
            logger.info(
                "Scored %s @ %s → %d/10 (%s)",
                job.title,
                job.company,
                match.score,
                match.reason,
            )
            notified = False
            if match.score >= config.score_threshold:
                try:
                    send_match_email(match)
                    notified = True
                except Exception:
                    logger.exception("Failed to send email for %s", job.url)
            mark_seen(
                seen,
                job,
                score=match.score,
                reason=match.reason,
                notified=notified,
            )
            # Persist after each job so a crash mid-run does not re-notify
            save_seen_jobs(config.seen_jobs_path, seen)
        except Exception:
            logger.exception(
                "Failed to score job %s — leaving unseen so the next run can retry",
                job.url,
            )

    # Also record jobs that were already known so the file stays informative
    save_seen_jobs(config.seen_jobs_path, seen)
    logger.info("Run complete. Seen jobs file: %s", config.seen_jobs_path)


def run_scheduler(config: AppConfig) -> None:
    hours = max(1, config.check_interval_hours)
    logger.info("Scheduling checks every %d hour(s)", hours)

    def job() -> None:
        try:
            run_once(config)
        except Exception:
            logger.exception("Scheduled run failed")

    # Run immediately, then on the interval
    job()
    schedule.every(hours).hours.do(job)
    while True:
        schedule.run_pending()
        time.sleep(30)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor career pages, score new jobs with Claude, email strong matches."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to YAML config (default: config.yaml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check and exit (no 24h loop)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(args.config)
    if not config_path.exists():
        example = config_path.with_name("config.example.yaml")
        raise SystemExit(
            f"Config not found: {config_path}\n"
            f"Copy {example} to {config_path} and edit your company URLs."
        )

    config = load_config(config_path)

    if args.once:
        run_once(config)
    else:
        run_scheduler(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
