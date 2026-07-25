#!/usr/bin/env python3
"""One-shot scrape + score run that writes strong matches to an artifact file."""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

from anthropic import Anthropic
from playwright.sync_api import sync_playwright

from job_monitor import (
    CAREER_SOURCES,
    PAGE_TIMEOUT_MS,
    SCORE_THRESHOLD,
    extract_job_links,
    fetch_job_description,
    job_key,
    load_seen_jobs,
    matches_keywords,
    save_seen_jobs,
    score_job_with_claude,
    send_digest_email,
)

OUT = Path("/opt/cursor/artifacts/jr_copywriter_matches.json")
OUT.parent.mkdir(parents=True, exist_ok=True)


def main() -> int:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    can_email = bool(os.getenv("GMAIL_ADDRESS") and os.getenv("GMAIL_APP_PASSWORD"))
    print(f"Sources: {len(CAREER_SOURCES)}")
    print(f"Anthropic key present: {bool(api_key)}")
    print(f"Gmail present: {can_email}")

    all_jobs: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

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

        for i, source in enumerate(CAREER_SOURCES, start=1):
            url = source["url"]
            company = source["company"]
            print(f"[{i}/{len(CAREER_SOURCES)}] {company}: {url}", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
                page.wait_for_timeout(1_500)
                links = extract_job_links(page, url)
                matched = [link for link in links if matches_keywords(link["title"])]
                print(f"  links={len(links)} matched={len(matched)}", flush=True)
                for link in matched:
                    all_jobs.append(
                        {
                            "title": link["title"],
                            "url": link["url"],
                            "company": company,
                            "source_url": url,
                        }
                    )
            except Exception as exc:
                print(f"  ERROR: {exc}", flush=True)
                errors.append({"company": company, "url": url, "error": str(exc)})

        deduped = {job_key(job["title"], job["url"]): job for job in all_jobs}
        jobs = list(deduped.values())
        print(f"\nUnique jr. copywriter matches: {len(jobs)}", flush=True)

        strong: list[dict] = []
        scored: list[dict] = []

        if api_key and jobs:
            client = Anthropic(api_key=api_key)
            seen = load_seen_jobs()
            for i, job in enumerate(jobs, start=1):
                print(
                    f"Scoring [{i}/{len(jobs)}] {job['title']} @ {job['company']}",
                    flush=True,
                )
                try:
                    description = fetch_job_description(page, job["url"])
                    result = score_job_with_claude(client, job["title"], description)
                    entry = {
                        **job,
                        "score": int(result["score"]),
                        "reason": str(result["reason"]),
                    }
                    scored.append(entry)
                    print(f"  -> {entry['score']}/10 — {entry['reason']}", flush=True)
                    key = job_key(job["title"], job["url"])
                    seen.setdefault("jobs", {})[key] = {
                        **entry,
                        "first_seen": datetime.now().isoformat(timespec="seconds"),
                    }
                    save_seen_jobs(seen)
                    if entry["score"] >= SCORE_THRESHOLD:
                        strong.append(entry)
                except Exception as exc:
                    print(f"  ERROR scoring: {exc}", flush=True)
                    traceback.print_exc()
        else:
            print("Skipping Claude scoring — ANTHROPIC_API_KEY not set.", flush=True)
            scored = jobs

        context.close()
        browser.close()

    if strong and can_email:
        try:
            send_digest_email(strong)
            print("Digest email sent.", flush=True)
        except Exception as exc:
            print(f"ERROR sending email: {exc}", flush=True)
            traceback.print_exc()

    payload = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "scored_with_claude": bool(api_key),
        "emailed": bool(strong and can_email),
        "threshold": SCORE_THRESHOLD,
        "match_count": len(jobs),
        "strong_matches": strong,
        "all_keyword_matches": scored,
        "errors": errors,
    }
    OUT.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {OUT}")
    print(f"Strong matches (>= {SCORE_THRESHOLD}): {len(strong)}")
    for match in strong:
        print(
            f"- {match['score']}/10 | {match['company']} | "
            f"{match['title']} | {match['url']}"
        )
    if not api_key:
        print("\nKeyword matches (unscored):")
        for match in jobs:
            print(f"- {match['company']} | {match['title']} | {match['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
