#!/usr/bin/env python3
"""
Junior copywriter job monitoring script (advertising agencies).

Every 24 hours (or when run manually), this script:
  1. Scrapes advertising holding-company and agency career pages with Playwright
  2. Keeps only junior / associate copywriter-style titles
  3. Skips jobs already stored in seen_jobs.json
  4. Scores new jobs against a hardcoded resume via Claude
  5. Emails a daily digest of jobs scoring 7+ to Gmail

Run once:       python job_monitor.py --once
Run on schedule: python job_monitor.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import sys
import traceback
from datetime import datetime
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from anthropic import Anthropic
from apscheduler.schedulers.blocking import BlockingScheduler
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

# Directory this script lives in (used for seen_jobs.json)
BASE_DIR = Path(__file__).resolve().parent
SEEN_JOBS_PATH = BASE_DIR / "seen_jobs.json"

# Claude settings (as specified)
CLAUDE_MODEL = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS = 200
SCORE_THRESHOLD = 7

# How long Playwright waits for pages (milliseconds)
PAGE_TIMEOUT_MS = 45_000

# Cap description length sent to Claude so prompts stay reasonable
DESCRIPTION_CHAR_LIMIT = 8_000

# Case-insensitive title keywords — a job is kept if ANY match
KEYWORDS = [
    "copywriter",
    "copy writer",
    "copywriting",
    "junior creative",
    "jr. creative",
    "jr creative",
    "associate creative",
    "copy intern",
    "copywriting intern",
]

# Drop senior / leadership creative titles even if they contain a keyword.
# Short tokens (sr, vp, ecd) are matched as whole words via regex below.
EXCLUDE_PHRASES = [
    "senior",
    "director",
    "vice president",
    "head of",
    "chief",
    "group creative",
    "executive creative",
    "creative director",
    "lead copywriter",
    "principal copywriter",
]
EXCLUDE_WORD_RE = re.compile(r"\b(sr|vp|ecd)\b", re.IGNORECASE)

# United States-only location filter (visa / work-authorization safe)
US_LOCATION_PHRASES = [
    "united states",
    "u.s.",
    "u.s.a.",
    "usa",
    "us only",
    "remote - us",
    "remote us",
    "remote, us",
    "remote (us)",
    "based in the us",
    "based in the u.s",
    "new york",
    "nyc",
    "los angeles",
    "chicago",
    "san francisco",
    "sf bay",
    "portland",
    "seattle",
    "austin",
    "dallas",
    "miami",
    "atlanta",
    "boston",
    "denver",
    "detroit",
    "minneapolis",
    "richmond",
    "nashville",
    "philadelphia",
    "washington, dc",
    "washington dc",
    "brooklyn",
    "manhattan",
]
# Non-US country / region cues in title, URL, or description
NON_US_LOCATION_PHRASES = [
    "united kingdom",
    "uk only",
    "london",
    "manchester",
    "canada",
    "toronto",
    "vancouver",
    "montreal",
    "mexico",
    "mexico city",
    "ciudad de méxico",
    "germany",
    "deutschland",
    "berlin",
    "munich",
    "münchen",
    "hamburg",
    "frankfurt",
    "düsseldorf",
    "france",
    "paris",
    "spain",
    "madrid",
    "barcelona",
    "italy",
    "milan",
    "rome",
    "netherlands",
    "amsterdam",
    "belgium",
    "brussels",
    "ireland",
    "dublin",
    "australia",
    "sydney",
    "melbourne",
    "new zealand",
    "auckland",
    "singapore",
    "hong kong",
    "tokyo",
    "japan",
    "china",
    "shanghai",
    "beijing",
    "india",
    "mumbai",
    "bangalore",
    "bengaluru",
    "delhi",
    "brazil",
    "são paulo",
    "sao paulo",
    "argentina",
    "colombia",
    "chile",
    "peru",
    "uae",
    "dubai",
    "saudi",
    "riyadh",
    "south africa",
    "cape town",
    "poland",
    "warsaw",
    "czech",
    "prague",
    "turkey",
    "istanbul",
    "indonesia",
    "jakarta",
    "philippines",
    "manila",
    "malaysia",
    "kuala lumpur",
    "lebanon",
    "beirut",
    "puerto rico",
    "remote - uk",
    "remote uk",
    "remote, uk",
    "emea",
    "apac",
    "latam",
]
# ISO-ish country tokens often embedded in ATS slugs (e.g. VML ...-gb-copywriter)
NON_US_COUNTRY_CODES = {
    "ae",
    "ar",
    "at",
    "au",
    "be",
    "bg",
    "br",
    "ca",
    "ch",
    "cl",
    "cn",
    "co",
    "cz",
    "de",
    "dk",
    "es",
    "fi",
    "fr",
    "gb",
    "gr",
    "hk",
    "hu",
    "id",
    "ie",
    "il",
    "in",
    "it",
    "jp",
    "kr",
    "lb",
    "mx",
    "my",
    "nl",
    "no",
    "nz",
    "pe",
    "ph",
    "pl",
    "pt",
    "ro",
    "ru",
    "sa",
    "se",
    "sg",
    "th",
    "tr",
    "tw",
    "ua",
    "uk",
    "vn",
    "za",
}
US_COUNTRY_CODES = {"us", "usa"}
# ATS slug patterns like /job/123-gb-copywriter or /jobs/123/gb/
COUNTRY_CODE_IN_PATH_RE = re.compile(
    r"(?:/job(?:s)?/|/careers/job/)\d+[-_/]([a-z]{2})(?:[-_/]|$)",
    re.IGNORECASE,
)
# Match lang/locale/country query params, but not unrelated keys like "language="
LOCALE_QUERY_RE = re.compile(
    r"(?:^|[?&])(?:lang|locale|country)=([a-z]{2})(?:[-_]([a-z]{2}))?",
    re.IGNORECASE,
)
NON_US_HOST_HINTS = (
    ".de",
    ".fr",
    ".co.uk",
    ".uk",
    ".nl",
    ".ie",
    ".com.au",
    ".co.nz",
    ".com.br",
    ".co.jp",
    ".com.mx",
    "personio.de",
)

# ---------------------------------------------------------------------------
# Advertising holding companies + agency career pages to monitor
# (company label is used in digests; URLs verified where possible)
# ---------------------------------------------------------------------------

CAREER_SOURCES = [
    # Holding companies
    {"company": "WPP", "url": "https://www.wpp.com/en/careers"},
    {"company": "Publicis Groupe", "url": "https://careers.publicisgroupe.com/jobs"},
    {"company": "Dentsu", "url": "https://www.dentsu.com/careers"},
    {"company": "Dentsu US", "url": "https://www.dentsu.com/us/en/careers"},
    {"company": "Havas", "url": "https://www.havas.com/who-we-are/our-careers/"},
    {"company": "Stagwell", "url": "https://www.stagwellglobal.com/careers/"},
    # Holding-network agencies
    {"company": "Ogilvy", "url": "https://www.ogilvy.com/careers"},
    {"company": "VML", "url": "https://www.vml.com/careers"},
    {"company": "Grey", "url": "https://job-boards.greenhouse.io/grey"},
    {"company": "WPP Media", "url": "https://welcome.wppmedia.com/"},
    {"company": "Mindshare", "url": "https://www.mindshareworld.com/careers"},
    {"company": "AKQA", "url": "https://www.akqa.com/careers/"},
    {"company": "DDB", "url": "https://www.ddb.com/careers"},
    {"company": "TBWA", "url": "https://tbwa.com/"},
    {"company": "TBWA Chiat Day", "url": "https://tbwachiatday.com/"},
    {"company": "Omnicom Media", "url": "https://omnicommedia.com/careers/"},
    {"company": "McCann", "url": "https://careers.mccann.com/en_US/careersmccann"},
    {"company": "FCB", "url": "https://www.fcb.com/careers"},
    {"company": "MullenLowe", "url": "https://www.mullenlowe.com/careers"},
    {"company": "Octagon", "url": "https://www.octagon.com/careers/"},
    {"company": "Weber Shandwick", "url": "https://webershandwick.com/careers"},
    {"company": "Golin / Ketchum", "url": "https://golinketchum.com/careers-jobs/"},
    {"company": "FleishmanHillard", "url": "https://fleishmanhillard.com/join-us/"},
    {"company": "Porter Novelli", "url": "https://porternovelli.com/careers/"},
    {"company": "Leo", "url": "https://careers.publicisgroupe.com/leoburnett/jobs"},
    {"company": "Leo Constellation", "url": "https://www.leoconstellation.com/careers"},
    {"company": "Saatchi & Saatchi", "url": "https://careers.publicisgroupe.com/jobs?brand=Saatchi%20%26%20Saatchi"},
    {"company": "Digitas", "url": "https://careers.publicisgroupe.com/digitas/jobs"},
    {"company": "Digitas Site", "url": "https://www.digitas.com/en-us/careers"},
    {"company": "Starcom", "url": "https://careers.publicisgroupe.com/starcom/jobs"},
    {"company": "Zenith", "url": "https://careers.publicisgroupe.com/zenith/jobs"},
    {"company": "Publicis Sapient", "url": "https://careers.publicissapient.com/"},
    {"company": "BBH", "url": "https://www.bbh.com/careers"},
    {"company": "Sid Lee", "url": "https://www.sidlee.com/careers"},
    {"company": "Razorfish", "url": "https://www.razorfish.com/careers/"},
    # Independents & creative shops (US-focused where possible)
    {"company": "Cramer-Krasselt", "url": "https://c-k.com/careers"},
    {"company": "VSA Partners", "url": "https://www.vsapartners.com/careers"},
    {"company": "Slingshot", "url": "https://www.slingshotagency.com/careers"},
    {"company": "YouTech Agency", "url": "https://www.youtechagency.com/careers"},
    {"company": "Wieden+Kennedy", "url": "https://www.wk.com/jobs/"},
    {"company": "72andSunny", "url": "https://www.72andsunny.com/careers"},
    {"company": "Droga5", "url": "https://droga5.com/careers/"},
    {"company": "Anomaly", "url": "https://jobs.lever.co/anomaly"},
    {"company": "Deutsch", "url": "https://www.deutsch.com/careers"},
    {"company": "Mother", "url": "https://www.mother.xyz/careers"},
    {"company": "Mother New York", "url": "https://www.mothernewyork.com/careers"},
    {"company": "R/GA", "url": "https://www.rga.com/careers"},
    {"company": "Crispin", "url": "https://www.crispin.com/"},
    {"company": "GS&F", "url": "https://www.gsandf.com/careers"},
    {"company": "VaynerMedia", "url": "https://www.vaynermedia.com/careers"},
    {"company": "Huge", "url": "https://www.hugeinc.com/careers/"},
    {"company": "IDEO", "url": "https://www.ideo.com/careers"},
    {"company": "Pentagram", "url": "https://www.pentagram.com/careers"},
    {"company": "Frog", "url": "https://www.frogdesign.com/careers"},
    {"company": "Wolff Olins", "url": "https://www.wolffolins.com/"},
    {"company": "Instrument", "url": "https://www.instrument.com/careers"},
    {"company": "EP+Co / Erwin Penland", "url": "https://www.erwinpenland.com/careers"},
    {"company": "Preacher", "url": "https://job-boards.greenhouse.io/preacher"},
    {"company": "Zambezi", "url": "https://www.zambezi.com/careers"},
    {"company": "Laundry Service / THE·TEAM", "url": "https://247laundryservice.com/careers"},
    {"company": "Mekanism", "url": "https://www.mekanism.com/"},
    {"company": "Fallon", "url": "https://www.fallon.com/careers"},
    {"company": "The Martin Agency", "url": "https://job-boards.greenhouse.io/themartinagency"},
    {"company": "Mischief", "url": "https://mischiefusa.com/careers"},
    {"company": "Joan", "url": "https://www.joan.co/careers"},
    {"company": "Code and Theory", "url": "https://www.codeandtheory.com/careers"},
    {"company": "Buck", "url": "https://www.buck.co/careers"},
    {"company": "Lippincott", "url": "https://www.lippincott.com/careers/"},
    {"company": "Barton Fink", "url": "https://www.bartonfink.com/careers"},
]

# Flat URL list kept for scrape loop convenience
CAREER_URLS = [source["url"] for source in CAREER_SOURCES]
COMPANY_BY_URL = {source["url"]: source["company"] for source in CAREER_SOURCES}

# ---------------------------------------------------------------------------
# Hardcoded resume text used for Claude match scoring
# ---------------------------------------------------------------------------

RESUME_TEXT = """
I keep things moving and people aligned. From coordinating multi-channel campaigns for major brands to managing vendors, timelines, and tenant communications on the ground, I've built a reputation for being the person who holds things together when the pace picks up. Detail-oriented by nature, proactive by habit.

GS&F — Freelance Marketing Copywriter / Marketing Copywriter Intern, June 2025–September 2025

Pitched and sold a digital campaign to LP Solutions, then led execution through to completion
Functioned as a central point of contact for clients including LP Solutions, Bridgestone, and Guthrie's Fried Chicken, managing confidential client information and deliverables across accounts under NDA
Contributed to digital campaign development for Guthrie's Fried Chicken's sponsorship partnership with University of Alabama Athletics

H/L Agency — Marketing Copywriter Intern, June 2024–August 2024

Coordinated production timelines and deliverables across social, radio, and video assets for McDonald's and Toyota
Supported social media campaigns for Toyota's sponsorship partnership with the San Francisco Giants
Participated in creative and client reviews, tracking action items and communicating project status

Allen Hall Advertising — Copywriter, May 2023–June 2024

Planned and executed an experiential brand activation for PeaceHealth Rides, organizing a 10+ business partner network and managing logistics to drive bikeshare users to partner locations
Tracked deliverables, deadlines, and budgets for the Oregon Innovation Challenge, coordinating speaker events, digital content, and an annual winners publication

Trinity College Dublin — Digital Marketing Intern, June 2023–August 2023

Analyzed and presented quarterly room rental profit reports, surfacing insights to improve scheduling and utilization
Revamped event coordination process and managed 50+ bookings, improving scheduling accuracy and resource utilization

Property Management Assistant, Linden Laurel LLC, November 2025–Present

Coordinated renovation timelines for vacant units, managing contractors and vendors to ensure projects were completed on schedule
Fielded confidential tenant, vendor, and ownership communications as primary point of contact
Managed renovation timelines for vacant units, coordinating action items with contractors and vendors

Skills: Account Coordination, Cross-Functional Collaboration, Sponsorship Marketing, Project Coordination, Vendor Management, Digital Campaign Support, Asset Management, Presentation Development, Invoice Tracking, Microsoft Office Suite, Adobe Creative Suite, Google Workspace, AI Tools
University of Oregon — B.S. in Advertising, Minor in Business Administration, 2020–2024
""".strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    """Print a timestamped status update to the console."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def company_name_from_url(url: str) -> str:
    """Return the configured company label, or derive one from the URL host."""
    if url in COMPANY_BY_URL:
        return COMPANY_BY_URL[url]
    host = urlparse(url).netloc.lower()
    # Strip leading www. / careers. / jobs. / job-boards.
    host = re.sub(r"^(www\.|careers\.|jobs\.|job-boards\.)", "", host)
    label = host.split(".")[0] if host else url
    return label.replace("-", " ").title()


def job_key(title: str, url: str) -> str:
    """Stable key for de-duplication in seen_jobs.json."""
    return f"{title.strip().lower()}|{url.strip().lower()}"


def matches_keywords(title: str) -> bool:
    """
    Return True for junior / associate copywriter-style titles.

    Keeps titles that match KEYWORDS and do not look like senior/leadership roles.
    """
    lower = title.lower()
    if not any(keyword in lower for keyword in KEYWORDS):
        return False
    if any(exclude in lower for exclude in EXCLUDE_PHRASES):
        return False
    if EXCLUDE_WORD_RE.search(title):
        return False
    return True


def _host_looks_non_us(url: str) -> bool:
    """Return True when the job URL host itself suggests a non-US board."""
    host = urlparse(url).netloc.lower()
    return any(hint in host for hint in NON_US_HOST_HINTS)


def _country_codes_from_url(url: str) -> set[str]:
    """Extract likely country codes from ATS URL path / locale query params."""
    codes: set[str] = set()
    parsed = urlparse(url)
    path_match = COUNTRY_CODE_IN_PATH_RE.search(parsed.path)
    if path_match:
        codes.add(path_match.group(1).lower())
    # Search the full URL query string with delimiters so lang= matches, not language=
    query = parsed.query or ""
    for match in LOCALE_QUERY_RE.finditer("?" + query):
        lang = (match.group(1) or "").lower()
        region = (match.group(2) or "").lower()
        # lang=es-mx / locale=en-US → prefer the region token when present
        if region:
            codes.add(region)
        elif lang in NON_US_COUNTRY_CODES or lang in US_COUNTRY_CODES:
            codes.add(lang)
    return codes


def _phrase_in_text(phrase: str, text: str) -> bool:
    """Substring match for multi-word phrases; word-boundary for single tokens."""
    if " " in phrase or "." in phrase or "-" in phrase or "(" in phrase:
        return phrase in text
    return re.search(rf"\b{re.escape(phrase)}\b", text) is not None


def location_is_us(
    title: str,
    url: str,
    description: str = "",
    link_text: str = "",
) -> bool:
    """
    Return True only when the posting looks US-based.

    Strict for visa safety:
      - reject clear non-US signals in title/URL/description
      - accept clear US signals
      - if still ambiguous after a description check, reject
    """
    blob = " ".join(
        part for part in (title, link_text, url, description[:4_000]) if part
    ).lower()

    if _host_looks_non_us(url):
        return False
    codes = _country_codes_from_url(url)
    if codes & NON_US_COUNTRY_CODES and not (codes & US_COUNTRY_CODES):
        return False
    if any(_phrase_in_text(phrase, blob) for phrase in NON_US_LOCATION_PHRASES):
        return False
    if codes & US_COUNTRY_CODES:
        return True
    if any(_phrase_in_text(phrase, blob) for phrase in US_LOCATION_PHRASES):
        return True

    # No positive US signal. With a description we can be decisive; without one,
    # keep the job for a later description check.
    if description.strip():
        return False
    return True


def is_clearly_non_us(title: str, url: str, link_text: str = "") -> bool:
    """Fast reject for listings that are obviously outside the United States."""
    if _host_looks_non_us(url):
        return True
    blob = " ".join(part for part in (title, link_text, url) if part).lower()
    codes = _country_codes_from_url(url)
    if codes & NON_US_COUNTRY_CODES and not (codes & US_COUNTRY_CODES):
        return True
    return any(_phrase_in_text(phrase, blob) for phrase in NON_US_LOCATION_PHRASES)


# ---------------------------------------------------------------------------
# Step 3 — seen_jobs.json persistence
# ---------------------------------------------------------------------------


def load_seen_jobs() -> dict[str, Any]:
    """Load previously seen job titles/URLs from seen_jobs.json."""
    if not SEEN_JOBS_PATH.exists():
        return {"jobs": {}}
    try:
        with SEEN_JOBS_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "jobs" not in data:
            return {"jobs": {}}
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log(f"Warning: could not read {SEEN_JOBS_PATH} ({exc}); starting fresh.")
        return {"jobs": {}}


def save_seen_jobs(data: dict[str, Any]) -> None:
    """Write seen jobs back to disk (atomic replace)."""
    tmp = SEEN_JOBS_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(SEEN_JOBS_PATH)


# ---------------------------------------------------------------------------
# Step 1 — Scrape career pages with Playwright
# ---------------------------------------------------------------------------


def extract_job_links(page, page_url: str) -> list[dict[str, str]]:
    """
    Pull job title text and hrefs from the loaded page.

    Many career sites use different markup, so we collect all anchors with
    visible text and keep ones that look like individual postings.
    """
    # Scroll a few times to trigger lazy-loaded listings
    for _ in range(3):
        page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)

    raw_links = page.eval_on_selector_all(
        "a",
        """elements => elements.map(el => ({
            text: (el.innerText || el.textContent || '').trim(),
            href: el.getAttribute('href') || ''
        }))""",
    )

    results: list[dict[str, str]] = []
    seen_hrefs: set[str] = set()

    for item in raw_links:
        text = (item.get("text") or "").strip()
        href = (item.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        if not text or len(text) < 3:
            continue
        # Skip very long nav blobs
        if len(text) > 200:
            continue

        absolute = urljoin(page_url, href)
        if absolute.rstrip("/") == page_url.rstrip("/"):
            continue
        if absolute in seen_hrefs:
            continue

        # Prefer links that look job-related; still keep keyword matches later
        path = urlparse(absolute).path.lower()
        jobish = any(
            token in path
            for token in (
                "/job",
                "/jobs",
                "/career",
                "/careers",
                "/position",
                "/opening",
                "/vacancy",
                "/role",
                "/apply",
                "greenhouse",
                "lever.co",
                "workday",
                "icims",
                "smartrecruiters",
                "jobvite",
                "ashbyhq",
            )
        )
        # Keep if path looks job-related OR title will pass keyword filter later
        if not jobish and not matches_keywords(text):
            continue

        seen_hrefs.add(absolute)
        results.append(
            {
                "title": text.split("\n")[0].strip(),
                "url": absolute,
                # Full anchor text often includes location on later lines
                "link_text": text,
            }
        )

    return results


def fetch_job_description(page, job_url: str) -> str:
    """Load a job detail page and return cleaned body text (best-effort)."""
    try:
        page.goto(job_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(1_000)
        text = page.inner_text("body")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:DESCRIPTION_CHAR_LIMIT]
    except Exception as exc:
        log(f"  Could not load description for {job_url}: {exc}")
        return ""


def scrape_career_pages() -> list[dict[str, str]]:
    """
    Visit every career URL with Playwright and collect job title + link pairs.

    If one page fails, log the error and continue to the next URL.
    """
    all_jobs: list[dict[str, str]] = []
    log(f"Starting scrape of {len(CAREER_URLS)} career pages...")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        for i, url in enumerate(CAREER_URLS, start=1):
            company = company_name_from_url(url)
            log(f"[{i}/{len(CAREER_URLS)}] Scraping {company}: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                page.wait_for_timeout(1_500)
                links = extract_job_links(page, url)
                log(f"  Found {len(links)} candidate link(s)")
                for link in links:
                    all_jobs.append(
                        {
                            "title": link["title"],
                            "url": link["url"],
                            "company": company,
                            "source_url": url,
                            "link_text": link.get("link_text", link["title"]),
                        }
                    )
            except Exception as exc:
                # Graceful failure: one bad page must not crash the whole run
                log(f"  ERROR scraping {url}: {exc}")
                traceback.print_exc()

        context.close()
        browser.close()

    log(f"Scrape complete. Total candidate links: {len(all_jobs)}")
    return all_jobs


# ---------------------------------------------------------------------------
# Step 2 — Keyword filter
# ---------------------------------------------------------------------------


def filter_by_keywords(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep only junior / associate copywriter-style titles."""
    filtered = [j for j in jobs if matches_keywords(j["title"])]
    log(
        f"Keyword filter: {len(filtered)} of {len(jobs)} jobs match "
        f"jr. copywriter keywords ({', '.join(KEYWORDS)}); "
        f"excluded senior titles containing "
        f"({', '.join(EXCLUDE_PHRASES[:6])}…)"
    )
    return filtered


def filter_by_us_location(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Drop listings that are clearly outside the United States (title/URL)."""
    kept: list[dict[str, str]] = []
    rejected = 0
    for job in jobs:
        if is_clearly_non_us(
            job.get("title", ""),
            job.get("url", ""),
            job.get("link_text", ""),
        ):
            rejected += 1
            continue
        kept.append(job)
    log(
        f"US location filter: kept {len(kept)} of {len(jobs)} jobs "
        f"({rejected} clearly non-US rejected from title/URL)"
    )
    return kept


# ---------------------------------------------------------------------------
# Step 4 — Score against resume with Claude
# ---------------------------------------------------------------------------


def score_job_with_claude(
    client: Anthropic,
    title: str,
    description: str,
) -> dict[str, Any]:
    """
    Ask Claude to score the resume against this role.

    Returns {"score": int, "reason": str}. On parse failure, score defaults to 0.
    """
    job_description = description.strip() or f"(No description available. Title only: {title})"
    prompt = (
        f"Here is a job description: {job_description}. "
        f"Here is my resume: {RESUME_TEXT}. "
        "On a scale of 1-10, how well does this resume match this junior / "
        "associate copywriter role at an advertising agency in the United States? "
        "Prioritize copywriting craft, agency internship/freelance experience, "
        "campaign concepting, client brand work, and fit for entry-to-junior "
        "creative roles. Penalize senior-level, non-copywriting, or non-US roles. "
        'Reply with only a JSON object in this format: '
        '{"score": 8, "reason": "one sentence explanation"}'
    )

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=CLAUDE_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    text = "".join(
        getattr(block, "text", "")
        for block in message.content
        if getattr(block, "type", None) == "text"
    ).strip()

    # Strip optional markdown code fences if the model wraps JSON
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(cleaned)
        score = int(data["score"])
        reason = str(data.get("reason", "")).strip() or "No reason provided."
        return {"score": max(1, min(10, score)), "reason": reason}
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        match = re.search(r'"?score"?\s*[:=]\s*(\d{1,2})', text, re.IGNORECASE)
        score = int(match.group(1)) if match else 0
        return {
            "score": max(0, min(10, score)),
            "reason": cleaned[:300] or "Could not parse Claude response.",
        }


# ---------------------------------------------------------------------------
# Step 5 — Email digest via Gmail SMTP
# ---------------------------------------------------------------------------


def build_digest_body(matches: list[dict[str, Any]]) -> str:
    """Format the daily digest email body with dividers between jobs."""
    sections: list[str] = []
    for m in matches:
        sections.append(
            "\n".join(
                [
                    f"Job title: {m['title']}",
                    f"Company: {m['company']}",
                    f"Link: {m['url']}",
                    f"Score: {m['score']}/10",
                    f"Reason: {m['reason']}",
                ]
            )
        )
    divider = "\n\n" + ("-" * 40) + "\n\n"
    header = (
        f"Jr. copywriter digest — {len(matches)} agency match(es) scoring "
        f"{SCORE_THRESHOLD}+ / 10\n\n"
    )
    return header + divider.join(sections) + "\n"


def send_digest_email(matches: list[dict[str, Any]]) -> None:
    """
    Send one digest email listing all jobs that scored 7+.

    Uses Gmail SMTP with credentials from the .env file.
    """
    gmail_address = os.environ["GMAIL_ADDRESS"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.getenv("EMAIL_TO", gmail_address)

    subject = (
        f"Jr. copywriter digest: {len(matches)} strong match(es) — "
        f"{datetime.now():%Y-%m-%d}"
    )
    body = build_digest_body(matches)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = recipient

    log(f"Sending digest email to {recipient} ({len(matches)} job(s))...")
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(gmail_address, gmail_password)
        smtp.send_message(msg)
    log("Digest email sent successfully.")


# ---------------------------------------------------------------------------
# Full pipeline orchestration
# ---------------------------------------------------------------------------


def require_env() -> None:
    """Ensure required secrets are present before calling external APIs."""
    missing = [
        key
        for key in ("ANTHROPIC_API_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")
        if not os.getenv(key)
    ]
    if missing:
        raise SystemExit(
            "Missing required environment variables: "
            + ", ".join(missing)
            + "\nCopy .env.example to .env and fill in your values."
        )


def run_pipeline() -> None:
    """
    Execute the full monitoring pipeline once:

      scrape → keyword filter → US location filter → unseen check →
      Claude score → email digest
    """
    log("=" * 60)
    log("Jr. copywriter agency job monitor starting (US locations only)")
    log("=" * 60)
    require_env()

    seen = load_seen_jobs()
    seen_keys: set[str] = set(seen.get("jobs", {}).keys())
    log(f"Loaded {len(seen_keys)} previously seen job(s) from {SEEN_JOBS_PATH.name}")

    # Step 1: scrape
    scraped = scrape_career_pages()

    # Step 2: keyword filter
    keyword_jobs = filter_by_keywords(scraped)

    # Step 2b: drop clearly non-US listings before scoring
    us_jobs = filter_by_us_location(keyword_jobs)

    # Step 3: only process jobs not already in seen_jobs.json
    new_jobs: list[dict[str, str]] = []
    for job in us_jobs:
        key = job_key(job["title"], job["url"])
        if key in seen_keys:
            continue
        new_jobs.append(job)

    log(f"New (unseen) US jr. copywriter jobs to score: {len(new_jobs)}")

    if not new_jobs:
        log("Nothing new to score. Pipeline complete.")
        return

    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    strong_matches: list[dict[str, Any]] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)

        for i, job in enumerate(new_jobs, start=1):
            key = job_key(job["title"], job["url"])
            log(
                f"Scoring [{i}/{len(new_jobs)}] {job['title']} @ {job['company']}"
            )
            try:
                # Fetch description for richer Claude context + US location check
                description = fetch_job_description(page, job["url"])
                if not location_is_us(
                    job.get("title", ""),
                    job.get("url", ""),
                    description=description,
                    link_text=job.get("link_text", ""),
                ):
                    log("  → skipped (non-US location in job details)")
                    seen["jobs"][key] = {
                        "title": job["title"],
                        "url": job["url"],
                        "company": job["company"],
                        "source_url": job.get("source_url", ""),
                        "score": 0,
                        "reason": "Skipped: location outside the United States.",
                        "skipped_non_us": True,
                        "first_seen": datetime.now().isoformat(timespec="seconds"),
                    }
                    seen_keys.add(key)
                    save_seen_jobs(seen)
                    continue

                result = score_job_with_claude(client, job["title"], description)
                score = int(result["score"])
                reason = str(result["reason"])
                log(f"  → {score}/10 — {reason}")

                entry = {
                    "title": job["title"],
                    "url": job["url"],
                    "company": job["company"],
                    "source_url": job.get("source_url", ""),
                    "score": score,
                    "reason": reason,
                    "first_seen": datetime.now().isoformat(timespec="seconds"),
                }
                seen["jobs"][key] = entry
                seen_keys.add(key)

                # Persist after each job so a crash mid-run does not re-score forever
                save_seen_jobs(seen)

                if score >= SCORE_THRESHOLD:
                    strong_matches.append(
                        {
                            "title": job["title"],
                            "company": job["company"],
                            "url": job["url"],
                            "score": score,
                            "reason": reason,
                        }
                    )
            except Exception as exc:
                # Leave unseen so the next run can retry scoring
                log(f"  ERROR scoring {job['url']}: {exc}")
                traceback.print_exc()

        context.close()
        browser.close()

    # Step 5: email only if we have strong matches
    if strong_matches:
        log(f"{len(strong_matches)} job(s) scored {SCORE_THRESHOLD}+ — sending digest.")
        try:
            send_digest_email(strong_matches)
        except Exception as exc:
            log(f"ERROR sending email: {exc}")
            traceback.print_exc()
    else:
        log(f"No jobs scored {SCORE_THRESHOLD}+ today — skipping email.")

    log("Pipeline complete.")


# ---------------------------------------------------------------------------
# Step 6 — Scheduler / CLI entrypoint
# ---------------------------------------------------------------------------


def run_scheduler() -> None:
    """Run the pipeline immediately, then every 24 hours with APScheduler."""
    log("Scheduler mode: running pipeline now, then every 24 hours.")
    # First run right away so starting the script is useful immediately
    try:
        run_pipeline()
    except Exception:
        log("Initial scheduled run failed:")
        traceback.print_exc()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_pipeline, "interval", hours=24, id="daily_job_monitor")
    log("APScheduler started — next run in 24 hours. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log("Scheduler stopped.")


def main(argv: list[str] | None = None) -> int:
    # Load ANTHROPIC_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD from .env
    load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "Monitor advertising agency career pages for jr. copywriter roles, "
            "score new jobs with Claude, and email a digest of strong matches."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the full pipeline once and exit (manual trigger).",
    )
    args = parser.parse_args(argv)

    if args.once:
        # Manual one-shot run
        run_pipeline()
    else:
        # Default: keep process alive and check every 24 hours
        run_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
