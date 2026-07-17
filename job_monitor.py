#!/usr/bin/env python3
"""
Job monitoring script.

Every 24 hours (or when run manually), this script:
  1. Scrapes a list of company career pages with Playwright
  2. Pulls broader keyword matches from Adzuna (optional) + RemoteOK/Arbeitnow
  3. Keeps only desk/office jobs whose titles match target keywords
  4. Skips jobs already stored in seen_jobs.json
  5. Scores new jobs against a hardcoded resume via Claude
  6. Emails a daily digest of jobs scoring 7+ to Gmail

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

import requests
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
    "coordinator",
    "associate",
    "account",
    "marketing",
    "partnerships",
    "events",
    "creative",
    "communications",
    "brand",
]

# Broader keyword API searches (desk/office focused phrases)
SEARCH_QUERIES = [
    "marketing coordinator",
    "brand coordinator",
    "account coordinator",
    "partnerships coordinator",
    "events coordinator",
    "communications coordinator",
    "creative coordinator",
    "marketing associate",
    "brand associate",
    "account associate",
    "partnerships associate",
    "brand marketing",
    "influencer coordinator",
    "campaign coordinator",
]

# Terms to push Adzuna away from store-floor noise
ADZUNA_WHAT_EXCLUDE = (
    "retail sales associate,sales associate,store associate,"
    "cashier,barista,stock associate,key holder"
)

# Keywords that also appear in website nav/footer chrome. Titles that only
# match these must also have a job-like URL before we treat them as postings.
WEAK_KEYWORDS = {
    "account",
    "brand",
    "events",
    "creative",
    "communications",
    "marketing",
}

# Exact / near-exact titles that are almost never real job postings
TITLE_BLOCKLIST = {
    "my account",
    "create account",
    "account",
    "sign in",
    "sign up",
    "log in",
    "login",
    "register",
    "join",
    "rewards",
    "top brands",
    "brands",
    "events",
    "news & events",
    "news and events",
    "events and presentations",
    "privacy policy",
    "cookie policy",
    "terms of use",
    "terms of service",
    "cart",
    "checkout",
    "wishlist",
    "search",
    "home",
    "careers",
    "jobs",
    "about us",
    "contact us",
    "press",
    "media",
    "investors",
}

# Substrings in titles that signal non-job pages
TITLE_BLOCKLIST_SUBSTRINGS = (
    "privacy policy",
    "cookie policy",
    "terms of",
    "data processor",
    "my account",
    "create account",
    "sign in",
    "log in",
    "join ",
    "rewards",
    "brand design",
    "top brands",
    "news & events",
    "news and events",
    "events and presentations",
    "investor",
    "press and media",
    "mailing list",
    "newsletter",
    "add to cart",
)

# Store-floor / retail titles to exclude (we want desk/office roles)
RETAIL_TITLE_PATTERNS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bsales associate\b",
        r"\bstore associate\b",
        r"\bstock associate\b",
        r"\bstock coordinator\b",
        r"\binventory associate\b",
        r"\bcashier\b",
        r"\bkeyholder\b",
        r"\bkey holder\b",
        r"\bsales stylist\b",
        r"\bstore manager\b",
        r"\bassistant store manager\b",
        r"\bshift lead\b",
        r"\bshift leader\b",
        r"^retail\b",
        r"\bretail:\b",
        r"\bpart[-\s]?time stock\b",
        r"\bfull[-\s]?time stock\b",
        r"\boperations coordinator\b.*\b(men's|women's|store|shop)\b",
        r"\b(men's|women's|store|shop)\b.*\boperations coordinator\b",
    )
]

# Text on career landing pages that usually leads to a real job board
VIEW_JOBS_LINK_HINTS = (
    "view open",
    "see open",
    "search job",
    "view job",
    "open position",
    "current opening",
    "browse job",
    "explore career",
    "explore job",
    "see all job",
    "view all job",
    "join our team",
    "search openings",
    "find a job",
    "apply now",
)

ATS_HOST_HINTS = (
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "icims.com",
    "smartrecruiters.com",
    "jobvite.com",
    "ashbyhq.com",
    "workable.com",
    "taleo.net",
    "ultipro.com",
    "dayforcehcm.com",
    "paylocity.com",
    "bamboohr.com",
    "recruitee.com",
    "job-boards.",
    "boards.greenhouse",
    "jobs.lever",
)

# URL fragments that are almost never individual job postings
URL_BLOCKLIST_FRAGMENTS = (
    "/login",
    "/log-in",
    "/signin",
    "/sign-in",
    "/sign_in",
    "/signup",
    "/sign-up",
    "/register",
    "/account",
    "/accounts/",
    "/userhome",
    "/user-home",
    "/userHome",
    "/privacy",
    "/cookie",
    "/cookies",
    "/terms",
    "/legal",
    "/investors",
    "/investor",
    "/press-",
    "/press/",
    "/media/",
    "/news-events",
    "/news-and-events",
    "/rewards",
    "/cart",
    "/checkout",
    "/wishlist",
    "/bag",
    "investors.",
    "policies.google.com",
    "account.",
)

# Positive signals that a URL is likely a real job posting
JOB_URL_TOKENS = (
    "/job",
    "/jobs/",
    "/jobs?",
    "/career",
    "/careers/",
    "/position",
    "/opening",
    "/vacancy",
    "/requisition",
    "/role/",
    "/apply",
    "greenhouse.io",
    "boards.greenhouse",
    "lever.co",
    "myworkdayjobs.com",
    "icims.com",
    "smartrecruiters.com",
    "jobvite.com",
    "ashbyhq.com",
    "workable.com",
    "taleo.net",
    "ultipro.com",
    "dayforcehcm.com",
    "paylocity.com",
    "bamboohr.com",
    "recruitee.com",
)

# Phrases that suggest scraped page text is a real job description
JOB_DESCRIPTION_SIGNALS = (
    "responsibilities",
    "requirements",
    "qualifications",
    "about the role",
    "about this role",
    "job description",
    "what you'll do",
    "what you will do",
    "you will",
    "we're looking for",
    "we are looking for",
    "equal opportunity",
    "apply now",
    "apply for",
    "full-time",
    "part-time",
    "benefits",
    "salary",
    "experience required",
    "preferred qualifications",
    "job type",
    "location",
)

# Phrases that suggest the page is login/shop/nav noise, not a posting
NON_JOB_DESCRIPTION_SIGNALS = (
    "local_rate_limited",
    "sign in to continue",
    "please sign in",
    "create an account",
    "forgot password",
    "reset your password",
    "add to cart",
    "add to bag",
    "shopping cart",
    "newsletter signup",
    "subscribe to our newsletter",
    "investor relations",
    "upcoming events",
    "financial calendar",
)

# ---------------------------------------------------------------------------
# Career pages to monitor (duplicates removed; spaced domains fixed)
# ---------------------------------------------------------------------------

CAREER_URLS = [
    "https://careers.gtb.com",
    "https://www.woolpert.com/careers",
    "https://www.livenation.com/careers",
    "https://www.altrarunning.com/careers",
    "https://www.rothys.com/pages/careers",
    "https://www.waremalcomb.com/careers",
    "https://warriors.com/careers",
    "https://leoburnett.com/careers",
    "https://www.ddb.com/careers",
    "https://www.goodbysilverstein.com/careers",
    "https://careers.kraftheinzcompany.com",
    "https://careers.mondelezinternational.com",
    "https://careers.conagrabrands.com",
    "https://www.abbott.com/careers",
    "https://www.ingredion.com/careers",
    "https://careers.mars.com",
    "https://www.fairlife.com/careers",
    "https://www.reynoldsconsumerproducts.com/careers",
    "https://www.fbin.com/careers",
    "https://www.world.kitchen/careers",
    "https://www.azekco.com/careers",
    "https://www.treehousefoods.com/careers",
    "https://www.lifeway.net/careers",
    "https://www.usfoods.com/careers",
    "https://www.blistex.com/careers",
    "https://www.unilever.com/careers",
    "https://www.crateandbarrel.com/careers",
    "https://www.wilson.com/en-us/careers",
    "https://www.shopakira.com/careers",
    "https://www.threadless.com/careers",
    "https://www.fossilgroup.com/careers",
    "https://careers.levistrauss.com",
    "https://www.pvh.com/careers",
    "https://www.hanesbrands.com/careers",
    "https://www.cintas.com/careers",
    "https://www.wolverineworldwide.com/careers",
    "https://www.randabrands.com/careers",
    "https://www.bradfordexchange.com/careers",
    "https://www.weathertech.com/careers",
    "https://www.dainese.com/us/en/careers",
    "https://www.americanapparel.com/careers",
    "https://www.medline.com/careers",
    "https://www.baxter.com/careers",
    "https://www.pfizer.com/careers",
    "https://www.zebra.com/careers",
    "https://www.accobrands.com/careers/",
    "https://jobs.groupon.com",
    "https://www.morningstar.com/careers",
    "https://www.usg.com/careers",
    "https://www.brunswick.com/careers",
    "https://www.itw.com/careers",
    "https://www.stepan.com/careers",
    "https://www.ryerson.com/careers",
    "https://www.kemper.com/careers",
    "https://www.cliffbar.com/our-company/careers",
    "https://www.harmlessharvest.com/pages/careers",
    "https://www.fairytalebrownies.com/careers",
    "https://www.dreyers.com/careers",
    "https://careers.nestle.com",
    "https://www.clorox.com/careers",
    "https://www.del-monte.com/en/careers",
    "https://www.readyrefresh.com/careers",
    "https://www.guldensmustard.com/careers",
    "https://www.philzcoffee.com/careers",
    "https://www.ghirardelli.com/careers",
    "https://www.tasteofnature.com/careers",
    "https://www.miyokos.com/pages/careers",
    "https://www.ripplefoods.com/careers",
    "https://www.impossiblefoods.com/careers",
    "https://www.notmilk.com/careers",
    "https://www.sossupreme.com/careers",
    "https://www.evolvausa.com/careers",
    "https://www.rebbl.co/pages/careers",
    "https://www.kiva.com/careers",
    "https://www.allbirds.com/pages/careers",
    "https://www.everlane.com/careers",
    "https://www.gap.com/careers",
    "https://www.gapinc.com/careers",
    "https://www.levi.com/US/en_US/careers",
    "https://www.cuyana.com/careers",
    "https://www.thirdlove.com/pages/careers",
    "https://www.marinelayer.com/pages/careers",
    "https://www.dollskill.com/pages/careers",
    "https://www.outerknown.com/pages/careers",
    "https://www.buckmason.com/pages/careers",
    "https://www.taylorstitch.com/pages/careers",
    "https://www.madewell.com/careers",
    "https://www.betabrand.com/careers",
    "https://www.aviatornation.com/pages/careers",
    "https://www.quayaustralia.com/pages/careers",
    "https://www.stance.com/pages/careers",
    "https://www.tracksmith.com/pages/careers",
    "https://www.vuori.com/pages/careers",
    "https://www.henkel.com/careers",
    "https://www.pg.com/careers",
    "https://www.bayer.com/careers",
    "https://www.abbvie.com/careers",
    "https://www.genentech.com/careers",
    "https://www.gilead.com/careers",
    "https://www.rigel.com/careers",
    "https://www.natus.com/careers",
    "https://www.coopersurgical.com/careers",
    "https://www.genomichealth.com/careers",
    "https://www.invitae.com/careers",
    "https://www.formatherapeutics.com/careers",
    "https://www.veeva.com/careers",
    "https://www.natera.com/careers",
    "https://www.guardanthealth.com/careers",
    "https://www.10xgenomics.com/careers",
    "https://www.fluidigm.com/careers",
    "https://www.pacificbiosciences.com/careers",
    "https://www.walgreens.com/topic/careers/main.jsp",
    "https://www.pgcareers.com",
    "https://careers.sherwin-williams.com",
    "https://www.bosch.us/our-company/careers",
    "https://www.wba.com/careers",
    "https://www.greenthumbindustries.com/careers",
    "https://www.champrosports.com/careers",
    "https://www.storck.com/en/careers",
    "https://www.everspring.com/careers",
    "https://www.popmart.com/us/pages/careers",
    "https://www.medela.com/en/careers",
    "https://www.idexcorp.com/careers",
    "https://www.wms.com/careers",
    "https://www.clearwaterpaper.com/careers",
    "https://www.hollister.com/en/careers",
    "https://www.akorn.com/careers",
    "https://www.nice.com/careers",
    "https://www.pactiv.com/careers",
    "https://www.anixter.com/careers",
    "https://www.lululemon.com/en-us/careers",
    "https://www.dolcegabbana.com/en/careers",
    "https://www.outdoorvoices.com/pages/careers",
    "https://www.reformation.com/pages/careers",
    "https://www.frankandoak.com/pages/careers",
    "https://www.revieve.com/careers",
    "https://www.poshmark.com/careers",
    "https://www.stitchfix.com/careers",
    "https://www.fanatics.com/careers",
    "https://www.thewarehousegroup.co.nz/careers",
    "https://www.aptos.com/careers",
    "https://www.centricsoftware.com/careers",
    "https://www.apparelmagic.com/careers",
    "https://www.deltagalil.com/careers",
    "https://www.fossilgroup.com/en/careers",
    "https://www.movadogroup.com/careers",
    "https://www.marmot.com/careers",
    "https://www.thenorthface.com/en-us/careers",
    "https://www.patagonia.com/careers",
    "https://www.prana.com/careers",
    "https://www.smartwool.com/careers",
    "https://www.icebreaker.com/en-us/careers",
    "https://www.bluediamondgrowers.com/careers",
    "https://www.sunmaid.com/careers",
    "https://www.driscolls.com/pages/careers",
    "https://www.lagunitas.com/jobs",
    "https://www.jellybelly.com/careers",
    "https://www.shiseidogroup.com/careers",
    "https://www.loreal.com/en/careers",
    "https://www.sephora.com/careers",
    "https://www.glossier.com/pages/careers",
    "https://www.tatcha.com/pages/careers",
    "https://www.olaplex.com/pages/careers",
    "https://www.iliabeauty.com/pages/careers",
    "https://www.saie.com/pages/careers",
    "https://www.tower28beauty.com/pages/careers",
    "https://www.versedskyn.com/pages/careers",
    "https://www.cotopaxi.com/pages/careers",
    "https://www.backcountry.com/careers",
    "https://www.hydroflask.com/pages/careers",
    "https://www.yeti.com/careers",
    "https://www.stanley1913.com/pages/careers",
    "https://www.away.com/careers",
    "https://www.samsonite.com/careers",
    "https://www.tumi.com/s/careers",
    "https://www.skullcandy.com/careers",
    "https://www.warriors.com/careers",
    "https://www.sfgiants.com/careers",
    "https://careers.nike.com",
    "https://www.hoka.com/en/us/careers.html",
    "https://www.on.com/en-us/careers",
    "https://www.rei.com/careers",
    "https://www.nestlejobs.com",
    "https://www.dyson.com/en/careers",
    "https://www.panasonic.com/us/careers",
    "https://www.pepsico.com/careers",
    "https://www.mortonssalt.com/careers",
    "https://www.newlywedfoods.com/careers",
    "https://www.achfood.com/careers",
    "https://www.kikconsumerproducts.com/careers",
    "https://www.dawnfoods.com/careers",
    "https://www.galderma.com/careers",
    "https://www.eaton.com/us/en-us/company/careers",
    "https://www.schneider-electric.com/en/careers",
    "https://www.caterpillar.com/en/careers",
    "https://www.stanleyblackanddecker.com/careers",
    "https://www.heicocompanies.com/careers",
    "https://www.innophos.com/careers",
    "https://www.johncrane.com/careers",
    "https://www.evraznorthamerica.com/careers",
    "https://www.abhmfg.com/careers",
    "https://www.amphenol.com/careers",
    "https://www.beautycounter.com/pages/careers",
    "https://www.babylist.com/careers",
    "https://www.warbyparker.com/careers",
    "https://www.fentybeauty.com/pages/careers",
    "https://www.kosas.com/pages/careers",
    "https://www.nudestix.com/pages/careers",
    "https://www.briogeo.com/pages/careers",
    "https://www.innbeauty.com/pages/careers",
    "https://www.supergoop.com/pages/careers",
    "https://www.ursamajorvt.com/pages/careers",
    "https://www.necessaire.com/pages/careers",
    "https://www.huronconsultinggroup.com/careers",
    "https://www.wholesomesweet.com/careers",
    "https://www.enjoylifefoods.com/careers",
    "https://www.mikesharder.com/careers",
    "https://www.whitewave.com/careers",
    "https://www.seedsofchange.com/careers",
    "https://www.vans.com/en-us/careers",
    "https://www.timberland.com/en-us/careers",
    "https://www.kipling-usa.com/careers",
    "https://www.jansport.com/careers",
    "https://www.eastpak.com/careers",
    "https://www.dickies.com/careers",
    "https://www.wrangler.com/careers",
    "https://www.lee.com/careers",
    "https://www.nautica.com/careers",
    "https://www.speedousa.com/careers",
    "https://www.redbull.com/us-en/energydrink/careers",
    "https://www.constellationbrands.com/careers",
    "https://www.jmsmucker.com/careers",
    "https://www.nationalbeverage.com/careers",
    "https://www.shastabeverages.com/careers",
    "https://www.blommer.com/careers",
    "https://www.paxvapor.com/careers",
    "https://www.kindersbbq.com/careers",
    "https://www.equitypackaging.com/careers",
    "https://www.zara.com/us/en/z-work-with-us-aw-careers.html",
    "https://www.lego.com/en-us/careers",
    "https://www.ashleyfurniture.com/careers",
    "https://www.hallmark.com/careers",
    "https://www.renewalbyandersen.com/careers",
    "https://www.loreal.com/en/usa/careers",
    "https://www.paxlabs.com/careers",
    "https://www.kinders.com/careers",
    "https://www.davidsontea.com/pages/careers",
    "https://www.bluebottlecoffee.com/careers",
    "https://www.ritual.com/careers",
    "https://www.sightglass.com/pages/careers",
    "https://www.fourbarrel.com/careers",
    "https://www.equatorchocolates.com/careers",
    "https://www.dandelionchocolate.com/pages/careers",
    "https://www.recchiuti.com/pages/careers",
    "https://www.premiumchocolatiers.com/careers",
    "https://www.oliveandsinclaire.com/careers",
    "https://www.kitehill.com/pages/careers",
    "https://www.foragerproject.com/pages/careers",
    "https://www.califiafarms.com/pages/careers",
    "https://www.sujajuice.com/pages/careers",
    "https://www.pressed.com/pages/careers",
    "https://www.daily-harvest.com/pages/careers",
    "https://www.gotts.com/careers",
    "https://www.melsdrive-in.com/careers",
    "https://www.taylorfarms.com/careers",
    "https://www.driscollsberry.com/pages/careers",
    "https://www.sunworksfarm.com/careers",
    "https://www.cloversonoma.com/careers",
    "https://www.strausfamilycreamery.com/careers",
    "https://www.bellwetherfarms.com/careers",
    "https://www.cowgirlcreamery.com/pages/careers",
    "https://www.achadinha.com/careers",
    "https://www.pointreyescheese.com/careers",
    "https://www.fiscalinicheese.com/careers",
    "https://www.homechef.com/careers",
    "https://www.pamperedchef.com/page/careers",
    "https://www.doublegood.com/careers",
    "https://www.kimberlycsark.com/careers",
    "https://www.whirlpoolcorp.com/careers",
    "https://www.mcdonalds.com/us/en-us/careers.html",
    "https://www.meadjohnson.com/careers",
    "https://www.surgecreates.com/careers",
    "https://www.kaleidoscopebranding.com/careers",
    "https://www.iacollaborative.com/careers",
    "https://www.mnml.com/careers",
    "https://www.choidesigngroup.com/careers",
    "https://www.dscout.com/careers",
    "https://www.zerocater.com/careers",
    "https://www.goodfoodinstitute.org/careers",
    "https://www.acefitness.org/careers",
    "https://www.rotary.org/en/careers",
    "https://www.kimberly-clark.com/en-us/careers",
    "https://www.corellehome.com/careers",
    "https://www.lsccom.com/careers",
    "https://www.masterbrand.com/careers",
    "https://www.sgws.com/careers",
    "https://www.breakthrubev.com/careers",
    "https://www.importia.com/careers",
    "https://www.prestigebrands.com/careers",
    "https://www.wintrust.com/careers",
    "https://www.midway.com/careers",
    "https://www.candid.co/careers",
    "https://www.auraframes.com/careers",
    "https://www.sundialbrands.com/careers",
    "https://www.sheamoisture.com/pages/careers",
    "https://www.carolsdaughter.com/pages/careers",
    "https://www.softsheen-carson.com/careers",
    "https://www.kenraprofessional.com/careers",
    "https://www.colorproof.com/careers",
    "https://www.renpure.com/careers",
    "https://www.hain.com/careers",
    "https://www.earthboundorganic.com/careers",
    "https://www.arrowheadmills.com/careers",
    "https://www.spectrumbrands.com/careers",
    "https://www.energizer.com/careers",
    "https://www.rayovac.com/careers",
    "https://www.remington.com/careers",
    "https://www.hottools.com/pages/careers",
    "https://www.helenoftroy.com/careers",
    "https://www.madisonreed.com/pages/careers",
    "https://www.chowbus.com/careers",
    "https://www.armstrongflooring.com/en-us/careers",
    "https://www.millerknoll.com/careers",
    "https://www.agati.com/careers",
    "https://www.imbibe.com/careers",
    "https://www.3m.com/3M/en_US/careers-us",
    "https://www.agentofchange.com/careers",
    "https://www.revolutionrugs.com/careers",
    "https://www.ruggable.com/pages/careers",
    "https://www.aholddelhaize.com/careers",
    "https://www.wurawe.com/careers",
    "https://www.forcebrands.com/careers",
    "https://www.oxo.com/pages/careers",
    "https://www.nuk-usa.com/careers",
    "https://www.chiccousa.com/careers",
    "https://www.gracobaby.com/careers",
    "https://www.britaxusa.com/pages/careers",
    "https://www.uppababy.com/pages/careers",
    "https://www.bugaboo.com/en-us/careers",
    "https://www.4moms.com/careers",
    "https://www.ergobaby.com/pages/careers",
    "https://www.boppy.com/pages/careers",
    "https://www.naturemade.com/pages/careers",
    "https://www.gnc.com/careers",
    "https://www.vitaminshoppe.com/careers",
    "https://www.gardenoflife.com/pages/careers",
    "https://www.draxe.com/careers",
    "https://www.ancientnaturals.com/careers",
    "https://www.nowfoods.com/careers",
    "https://www.countrylifevitamins.com/careers",
    "https://www.solgar.com/careers",
    "https://www.rainbowlight.com/careers",
    "https://www.megafood.com/pages/careers",
    "https://www.newchapter.com/pages/careers",
    "https://www.florasana.com/careers",
    "https://www.jarrow.com/pages/careers",
    "https://www.sourcenaturals.com/careers",
    "https://www.nordicnaturals.com/pages/careers",
    "https://www.carlsonlabs.com/careers",
    "https://www.naturelo.com/pages/careers",
    "https://www.thorne.com/careers",
    "https://www.designforhealth.com/careers",
    "https://www.klaire.com/careers",
    "https://www.orthomolecularproducts.com/careers",
    "https://www.pureencapsulations.com/careers",
    "https://www.integrativepro.com/careers",
    "https://www.standardprocess.com/careers",
]

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
    """Derive a readable company name from a career page URL."""
    host = urlparse(url).netloc.lower()
    # Strip leading www. / careers.
    host = re.sub(r"^(www\.|careers\.|jobs\.)", "", host)
    # Use the first label (e.g. kraftheinzcompany from kraftheinzcompany.com)
    label = host.split(".")[0] if host else url
    return label.replace("-", " ").title()


def job_key(title: str, url: str) -> str:
    """Stable key for de-duplication in seen_jobs.json."""
    return f"{title.strip().lower()}|{url.strip().lower()}"


def matches_keywords(title: str) -> bool:
    """Return True if the job title contains any of the target keywords."""
    lower = title.lower()
    return any(keyword in lower for keyword in KEYWORDS)


def matched_keywords(title: str) -> set[str]:
    """Return which configured keywords appear in the title."""
    lower = title.lower()
    return {keyword for keyword in KEYWORDS if keyword in lower}


def looks_like_job_url(url: str) -> bool:
    """Return True if the URL path/host looks like an ATS or job posting link."""
    parsed = urlparse(url.lower())
    host = parsed.netloc or ""
    path_q = f"{parsed.path}?{parsed.query}"

    # ATS hosts / known job boards are always job-related
    if any(hint in host for hint in ATS_HOST_HINTS):
        return True
    if any(
        hint in host
        for hint in (
            "remotive.com",
            "remoteok.com",
            "remoteok.io",
            "arbeitnow.com",
            "adzuna.",
            "indeed.com",
        )
    ):
        return True

    # Path tokens only (avoid matching hostnames like careers.example.com)
    path_tokens = (
        "/job/",
        "/jobs/",
        "/jobs?",
        "/job?",
        "/careers/",
        "/career/",
        "/position",
        "/opening",
        "/vacancy",
        "/requisition",
        "/role/",
        "/apply",
        "/search-results",
        "/remote-jobs/",
    )
    if any(token in path_q for token in path_tokens):
        return True
    # Individual posting style: .../job/R-12345
    if re.search(r"/job/[a-z0-9_-]+", path_q):
        return True
    return False


def is_blocked_title(title: str) -> bool:
    """Return True for nav/account/investor titles that are not job postings."""
    cleaned = re.sub(r"\s+", " ", title).strip().lower()
    # Drop trailing UI noise like "(opens in a new tab)"
    cleaned = re.sub(r"\(.*?\)", "", cleaned).strip(" -–—|:")
    if cleaned in TITLE_BLOCKLIST:
        return True
    return any(fragment in cleaned for fragment in TITLE_BLOCKLIST_SUBSTRINGS)


def is_blocked_url(url: str) -> bool:
    """Return True for login, account, investor, shop, and policy URLs."""
    lower = url.lower()
    parsed = urlparse(lower)
    path = parsed.path or "/"
    netloc = parsed.netloc or ""
    haystack = f"{netloc}{path}?{parsed.query}"

    # Path-segment checks avoid false hits like "/accounting" matching "/account"
    segments = [seg for seg in path.split("/") if seg]
    blocked_segments = {
        "login",
        "log-in",
        "signin",
        "sign-in",
        "sign_in",
        "signup",
        "sign-up",
        "register",
        "account",
        "accounts",
        "userhome",
        "user-home",
        "privacy",
        "cookie",
        "cookies",
        "terms",
        "legal",
        "investors",
        "investor",
        "rewards",
        "cart",
        "checkout",
        "wishlist",
        "bag",
    }
    if any(seg in blocked_segments for seg in segments):
        return True

    # Host / full-URL fragment checks
    for fragment in (
        "investors.",
        "policies.google.com",
        "account.",
        "/press-",
        "/press/",
        "/media/",
        "/news-events",
        "/news-and-events",
        "/investor-relations",
    ):
        if fragment in haystack:
            return True
    return False


def is_retail_store_role(title: str) -> bool:
    """Return True for store-floor retail roles we do not want to score."""
    cleaned = re.sub(r"\s+", " ", title).strip()
    return any(pattern.search(cleaned) for pattern in RETAIL_TITLE_PATTERNS)


def is_plausible_job_listing(title: str, url: str) -> bool:
    """
    Keep only links that look like real desk/office job postings.

    Rules:
      - title must match at least one keyword
      - title/URL must not be on the blocklists
      - exclude retail/sales-floor roles
      - if the title only matches "weak" keywords (account/brand/events/...),
        the URL must also look job-related
    """
    if not matches_keywords(title):
        return False
    if is_blocked_title(title) or is_blocked_url(url):
        return False
    if is_retail_store_role(title):
        return False

    hits = matched_keywords(title)
    strong_hits = hits - WEAK_KEYWORDS
    if strong_hits:
        return True
    # Weak keywords alone are too noisy without a job-like URL
    return looks_like_job_url(url)


def looks_like_job_description(description: str) -> bool:
    """
    Heuristic check that scraped page text is a job posting, not a login/shop page.

    Used after fetching the detail page, before spending Claude tokens.
    """
    text = (description or "").strip()
    if len(text) < 180:
        return False

    lower = text.lower()
    if any(signal in lower for signal in NON_JOB_DESCRIPTION_SIGNALS):
        # Allow if the page also has strong job signals (some ATS pages mention sign-in)
        job_hits = sum(1 for signal in JOB_DESCRIPTION_SIGNALS if signal in lower)
        if job_hits < 2:
            return False

    job_hits = sum(1 for signal in JOB_DESCRIPTION_SIGNALS if signal in lower)
    return job_hits >= 1 or len(text) >= 1_200


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
    for _ in range(4):
        try:
            page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        except Exception:
            break
        page.wait_for_timeout(400)

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

        title = _title_from_generic_link(text, absolute)
        # Drop obvious non-jobs early (login/account/nav/investor/retail links)
        if not is_plausible_job_listing(title, absolute):
            continue

        seen_hrefs.add(absolute)
        results.append({"title": title, "url": absolute})

    return results


def _title_from_generic_link(text: str, url: str) -> str:
    """
    Recover a usable title when the anchor text is generic (e.g. "View Job").

    Many modern boards use CTA text for the link and put the role name in the URL.
    """
    cleaned = re.sub(r"\s+", " ", text).strip()
    generic = {
        "view job",
        "view jobs",
        "apply",
        "apply now",
        "see job",
        "see role",
        "job details",
        "learn more",
        "read more",
    }
    if cleaned.lower() not in generic and len(cleaned) >= 4:
        return cleaned.split("\n")[0].strip()

    path = urlparse(url).path
    parts = [p for p in path.split("/") if p]
    # Pattern: /some-job-slug/job/R-12345
    if "job" in parts:
        idx = parts.index("job")
        if idx > 0:
            return parts[idx - 1].replace("-", " ").replace("_", " ").strip().title()
    # Pattern: /jobs/123-some-slug or /job/some-slug
    for i, part in enumerate(parts):
        if part in {"jobs", "job", "opening", "position"} and i + 1 < len(parts):
            slug = parts[i + 1]
            slug = re.sub(r"^\d+[-_]?", "", slug)
            if len(slug) > 3 and not slug.isdigit():
                return slug.replace("-", " ").replace("_", " ").strip().title()
    return cleaned.split("\n")[0].strip()


def discover_job_board_urls(page, page_url: str) -> list[str]:
    """
    Find secondary ATS / job-search URLs linked from a career marketing page.

    Many employers only show a "View jobs" button that points at Greenhouse,
    Lever, Workday, etc. Following those boards is required to see real titles.
    """
    raw_links = page.eval_on_selector_all(
        "a",
        """elements => elements.map(el => ({
            text: (el.innerText || el.textContent || '').trim().toLowerCase(),
            href: el.getAttribute('href') || ''
        }))""",
    )

    boards: list[str] = []
    seen: set[str] = set()
    page_host = urlparse(page_url).netloc.lower()

    # iframe embeds of ATS boards
    try:
        for frame in page.frames:
            frame_url = (frame.url or "").strip()
            if not frame_url or frame_url.startswith(("about:", "chrome:", "data:")):
                continue
            lower = frame_url.lower()
            if any(hint in lower for hint in ATS_HOST_HINTS):
                key = frame_url.rstrip("/").lower()
                if key not in seen and key != page_url.rstrip("/").lower():
                    seen.add(key)
                    boards.append(frame_url)
    except Exception:
        pass

    locale_re = re.compile(
        r"^/[a-z]{2}(?:-[a-z]{2})?/?$",
        re.IGNORECASE,
    )

    for item in raw_links:
        href = (item.get("href") or "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        key = absolute.rstrip("/").lower()
        if key in seen or key == page_url.rstrip("/").lower():
            continue
        if is_blocked_url(absolute):
            continue
        # Skip language-switcher paths like /de/ or /es-es/
        if locale_re.match(parsed.path or ""):
            continue

        lower_url = absolute.lower()
        text = (item.get("text") or "").strip().lower()
        text_compact = re.sub(r"\s+", " ", text)
        is_ats = any(hint in lower_url for hint in ATS_HOST_HINTS)
        is_view_jobs = any(hint in text_compact for hint in VIEW_JOBS_LINK_HINTS)
        looks_search = any(
            token in lower_url
            for token in (
                "/job",
                "/jobs",
                "/search",
                "/opening",
                "/position",
                "myworkdayjobs",
                "search-results",
            )
        )
        same_host_category = (
            parsed.netloc.lower() == page_host
            and any(
                k in text_compact
                for k in (
                    "marketing",
                    "brand",
                    "creative",
                    "communications",
                    "corporate",
                    "coordinator",
                    "partnership",
                )
            )
            and ("career" in lower_url or "/us/" in lower_url or "job" in lower_url)
        )

        if is_ats or (is_view_jobs and looks_search) or same_host_category:
            seen.add(key)
            boards.append(absolute)

    # Prefer ATS hosts / job search paths first
    boards.sort(
        key=lambda u: (
            0 if any(h in u.lower() for h in ATS_HOST_HINTS) else 1,
            0 if "/jobs" in u.lower() or "search-results" in u.lower() else 1,
            len(u),
        )
    )
    return boards[:5]


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


def _scrape_one_url(context, url: str) -> list[dict[str, str]]:
    """
    Scrape one career URL in an isolated page, then follow linked job boards.

    Using a fresh page per employer avoids Playwright "navigation interrupted"
    failures caused by prior redirects still in flight.
    """
    found: list[dict[str, str]] = []
    seen_job_urls: set[str] = set()
    pages_to_scan = [url]
    visited_pages: set[str] = set()

    while pages_to_scan and len(visited_pages) < 5:
        target = pages_to_scan.pop(0)
        target_key = target.rstrip("/").lower()
        if target_key in visited_pages:
            continue
        visited_pages.add(target_key)

        page = context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            page.wait_for_timeout(1_200)

            for link in extract_job_links(page, page.url or target):
                if link["url"] in seen_job_urls:
                    continue
                seen_job_urls.add(link["url"])
                found.append(link)

            # Only discover secondary boards from the original career landing page
            # and the first ATS hop — avoids wandering the whole internet.
            if len(visited_pages) <= 2:
                for board in discover_job_board_urls(page, page.url or target):
                    board_key = board.rstrip("/").lower()
                    if board_key not in visited_pages:
                        pages_to_scan.append(board)
        finally:
            page.close()

    return found


def scrape_career_pages() -> list[dict[str, str]]:
    """
    Visit every career URL with Playwright and collect job title + link pairs.

    If one page fails, log the error and continue to the next URL.
    """
    all_jobs: list[dict[str, str]] = []
    ok_count = 0
    err_count = 0
    with_links = 0
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

        for i, url in enumerate(CAREER_URLS, start=1):
            company = company_name_from_url(url)
            log(f"[{i}/{len(CAREER_URLS)}] Scraping {company}: {url}")
            try:
                links = _scrape_one_url(context, url)
                ok_count += 1
                if links:
                    with_links += 1
                log(f"  Found {len(links)} candidate link(s)")
                for link in links:
                    all_jobs.append(
                        {
                            "title": link["title"],
                            "url": link["url"],
                            "company": company,
                            "source_url": url,
                        }
                    )
            except Exception as exc:
                err_count += 1
                # Keep logs readable — full tracebacks made failures hard to scan
                log(f"  ERROR scraping {url}: {exc}")

        context.close()
        browser.close()

    log(
        f"Scrape complete. Total candidate links: {len(all_jobs)} "
        f"(pages ok={ok_count}, with_links={with_links}, errors={err_count})"
    )
    return all_jobs


# ---------------------------------------------------------------------------
# Step 2 — Keyword filter
# ---------------------------------------------------------------------------


def filter_by_keywords(jobs: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keep only plausible desk/office listings whose titles contain a keyword."""
    filtered = [j for j in jobs if is_plausible_job_listing(j["title"], j["url"])]
    dropped = len(jobs) - len(filtered)
    retail_dropped = sum(1 for j in jobs if is_retail_store_role(j["title"]))
    log(
        f"Listing filter: kept {len(filtered)} of {len(jobs)} "
        f"(dropped {dropped} false/non-desk links, including {retail_dropped} retail; "
        f"keywords: {', '.join(KEYWORDS)})"
    )
    return filtered


# ---------------------------------------------------------------------------
# Broader keyword job search (Adzuna + Remotive)
# ---------------------------------------------------------------------------


def _normalize_api_job(
    *,
    title: str,
    url: str,
    company: str,
    description: str = "",
    source: str,
) -> dict[str, str] | None:
    """Convert an API hit into the same shape used by career-page scraping."""
    title = re.sub(r"\s+", " ", (title or "")).strip()
    url = (url or "").strip()
    company = re.sub(r"\s+", " ", (company or "")).strip() or "Unknown"
    if not title or not url:
        return None
    if not url.startswith("http"):
        return None
    if not is_plausible_job_listing(title, url):
        return None
    desc = re.sub(r"<[^>]+>", " ", description or "")
    desc = re.sub(r"\s+", " ", desc).strip()[:DESCRIPTION_CHAR_LIMIT]
    return {
        "title": title,
        "url": url,
        "company": company,
        "source_url": f"api:{source}",
        "description": desc,
    }


def fetch_adzuna_jobs() -> list[dict[str, str]]:
    """
    Search Adzuna for desk-job phrases.

    Requires ADZUNA_APP_ID and ADZUNA_APP_KEY (free at https://developer.adzuna.com/).
    If credentials are missing, this source is skipped.
    """
    app_id = os.getenv("ADZUNA_APP_ID", "").strip()
    app_key = os.getenv("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        log("Adzuna: skipped (set ADZUNA_APP_ID and ADZUNA_APP_KEY in .env to enable)")
        return []

    country = os.getenv("ADZUNA_COUNTRY", "us").strip().lower() or "us"
    where = os.getenv("ADZUNA_WHERE", "").strip()
    results_per_page = int(os.getenv("ADZUNA_RESULTS_PER_QUERY", "20"))
    max_pages = int(os.getenv("ADZUNA_MAX_PAGES", "1"))

    jobs: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    log(
        f"Adzuna: searching {len(SEARCH_QUERIES)} queries "
        f"(country={country}, where={where or 'anywhere'})"
    )

    for query in SEARCH_QUERIES:
        for page_num in range(1, max_pages + 1):
            url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page_num}"
            params = {
                "app_id": app_id,
                "app_key": app_key,
                "results_per_page": results_per_page,
                "what": query,
                "what_exclude": ADZUNA_WHAT_EXCLUDE,
                "content-type": "application/json",
            }
            if where:
                params["where"] = where
            try:
                resp = requests.get(url, params=params, timeout=30)
                if resp.status_code != 200:
                    log(f"  Adzuna error for '{query}' page {page_num}: HTTP {resp.status_code}")
                    break
                payload = resp.json()
            except Exception as exc:
                log(f"  Adzuna request failed for '{query}': {exc}")
                break

            results = payload.get("results") or []
            if not results:
                break
            kept = 0
            for item in results:
                normalized = _normalize_api_job(
                    title=str(item.get("title") or ""),
                    url=str(item.get("redirect_url") or item.get("adref") or ""),
                    company=str((item.get("company") or {}).get("display_name") or ""),
                    description=str(item.get("description") or ""),
                    source="adzuna",
                )
                if not normalized:
                    continue
                key = normalized["url"].rstrip("/").lower()
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                jobs.append(normalized)
                kept += 1
            log(f"  Adzuna '{query}' p{page_num}: {len(results)} hits, kept {kept}")

    log(f"Adzuna: {len(jobs)} unique desk-job listing(s)")
    return jobs


def fetch_remotive_jobs() -> list[dict[str, str]]:
    """Deprecated alias — Remotive's free endpoint ignores search filters."""
    return []


def fetch_remoteok_jobs() -> list[dict[str, str]]:
    """
    Pull recent remote roles from RemoteOK's free public API (no key required).
    """
    enabled = os.getenv("REMOTEOK_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        log("RemoteOK: skipped (REMOTEOK_ENABLED=0)")
        return []

    log("RemoteOK: fetching public feed")
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "JobMonitor/1.0 (+local)"},
            timeout=30,
        )
        if resp.status_code != 200:
            log(f"  RemoteOK error: HTTP {resp.status_code}")
            return []
        payload = resp.json()
    except Exception as exc:
        log(f"  RemoteOK request failed: {exc}")
        return []

    jobs: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        title = str(item.get("position") or item.get("title") or "")
        url = str(item.get("url") or item.get("apply_url") or "")
        company = str(item.get("company") or "")
        description = str(item.get("description") or "")
        normalized = _normalize_api_job(
            title=title,
            url=url,
            company=company,
            description=description,
            source="remoteok",
        )
        if not normalized:
            continue
        key = normalized["url"].rstrip("/").lower()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        jobs.append(normalized)

    log(f"RemoteOK: {len(jobs)} unique desk-job listing(s)")
    return jobs


def fetch_arbeitnow_jobs() -> list[dict[str, str]]:
    """
    Pull roles from Arbeitnow's free job-board API (no key required).
    """
    enabled = os.getenv("ARBEITNOW_ENABLED", "1").strip().lower()
    if enabled in {"0", "false", "no", "off"}:
        log("Arbeitnow: skipped (ARBEITNOW_ENABLED=0)")
        return []

    log("Arbeitnow: fetching public feed")
    try:
        resp = requests.get(
            "https://www.arbeitnow.com/api/job-board-api",
            headers={"User-Agent": "JobMonitor/1.0 (+local)"},
            timeout=30,
        )
        if resp.status_code != 200:
            log(f"  Arbeitnow error: HTTP {resp.status_code}")
            return []
        payload = resp.json()
    except Exception as exc:
        log(f"  Arbeitnow request failed: {exc}")
        return []

    results = payload.get("data") or []
    jobs: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_api_job(
            title=str(item.get("title") or ""),
            url=str(item.get("url") or ""),
            company=str(item.get("company_name") or item.get("company") or ""),
            description=str(item.get("description") or ""),
            source="arbeitnow",
        )
        if not normalized:
            continue
        key = normalized["url"].rstrip("/").lower()
        if key in seen_urls:
            continue
        seen_urls.add(key)
        jobs.append(normalized)

    log(f"Arbeitnow: {len(jobs)} unique desk-job listing(s)")
    return jobs


def fetch_keyword_api_jobs() -> list[dict[str, str]]:
    """Combine optional Adzuna + free RemoteOK/Arbeitnow keyword feeds."""
    combined: list[dict[str, str]] = []
    seen: set[str] = set()
    for job in fetch_adzuna_jobs() + fetch_remoteok_jobs() + fetch_arbeitnow_jobs():
        key = job["url"].rstrip("/").lower()
        if key in seen:
            continue
        seen.add(key)
        combined.append(job)
    log(f"Keyword APIs: {len(combined)} unique listing(s) after merge")
    return combined


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
        "On a scale of 1-10, how well does this resume match this role? "
        "Consider the candidate's coordination experience, agency background, "
        "client management skills, and any relevant industry overlap. "
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
        f"Daily job digest — {len(matches)} match(es) scoring "
        f"{SCORE_THRESHOLD}+ / 10\n\n"
    )
    return header + divider.join(sections) + "\n"


def send_digest_email(matches: list[dict[str, Any]]) -> None:
    """
    Send one digest email listing all jobs that scored 7+.

    Uses Gmail SMTP with credentials from the .env file.
    """
    gmail_address = os.environ["GMAIL_ADDRESS"]
    # Google shows App Passwords with spaces; SMTP expects them removed.
    gmail_password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    recipient = os.getenv("EMAIL_TO", gmail_address)

    subject = f"Job digest: {len(matches)} strong match(es) — {datetime.now():%Y-%m-%d}"
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

      scrape companies → keyword APIs → filter → unseen check → Claude → email
    """
    log("=" * 60)
    log("Job monitor pipeline starting")
    log("=" * 60)
    require_env()

    seen = load_seen_jobs()
    seen_keys: set[str] = set(seen.get("jobs", {}).keys())
    log(f"Loaded {len(seen_keys)} previously seen job(s) from {SEEN_JOBS_PATH.name}")

    # Step 1a: scrape configured company career pages
    scraped = scrape_career_pages()

    # Step 1b: broader keyword search via job APIs (Adzuna + RemoteOK/Arbeitnow)
    api_jobs = fetch_keyword_api_jobs()

    # Step 2: keyword / desk-job filter
    keyword_jobs = filter_by_keywords(scraped + api_jobs)

    # Step 3: only process jobs not already in seen_jobs.json
    new_jobs: list[dict[str, str]] = []
    for job in keyword_jobs:
        key = job_key(job["title"], job["url"])
        if key in seen_keys:
            continue
        new_jobs.append(job)

    log(f"New (unseen) keyword-matching jobs to score: {len(new_jobs)}")

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

        for i, job in enumerate(new_jobs, start=1):
            key = job_key(job["title"], job["url"])
            log(
                f"Scoring [{i}/{len(new_jobs)}] {job['title']} @ {job['company']}"
            )
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            try:
                # Prefer API-provided description when present; else scrape detail page
                description = (job.get("description") or "").strip()
                if len(description) < 180:
                    description = fetch_job_description(page, job["url"])
                if not looks_like_job_description(description):
                    log(
                        "  Skipping — page does not look like a real job posting "
                        "(login/nav/shop/investor text)."
                    )
                    # Remember it so we do not keep re-fetching noise links
                    seen["jobs"][key] = {
                        "title": job["title"],
                        "url": job["url"],
                        "company": job["company"],
                        "source_url": job.get("source_url", ""),
                        "score": None,
                        "reason": "filtered_non_job_page",
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
            finally:
                page.close()

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
    """
    Run the pipeline every day at midnight (local time).

    Keep this process running (or use system cron / launchd with --once)
    so the midnight job can fire.
    """
    hour = int(os.getenv("SCHEDULE_HOUR", "0"))
    minute = int(os.getenv("SCHEDULE_MINUTE", "0"))
    run_on_start = os.getenv("RUN_ON_START", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    log(
        f"Scheduler mode: daily at {hour:02d}:{minute:02d} local time "
        f"(set SCHEDULE_HOUR / SCHEDULE_MINUTE to change)."
    )

    if run_on_start:
        log("RUN_ON_START=1 — running pipeline once before waiting for midnight.")
        try:
            run_pipeline()
        except Exception:
            log("Initial scheduled run failed:")
            traceback.print_exc()

    scheduler = BlockingScheduler()
    scheduler.add_job(
        run_pipeline,
        "cron",
        hour=hour,
        minute=minute,
        id="daily_job_monitor",
    )
    jobs = scheduler.get_jobs()
    if jobs and jobs[0].next_run_time:
        log(f"Next run scheduled for: {jobs[0].next_run_time}")
    log("APScheduler started. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log("Scheduler stopped.")


def main(argv: list[str] | None = None) -> int:
    # Load ANTHROPIC_API_KEY, GMAIL_ADDRESS, GMAIL_APP_PASSWORD from .env
    load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "Monitor career pages + keyword job APIs, score with Claude, "
            "email strong matches."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the full pipeline once and exit (manual trigger).",
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="In scheduler mode, also run once immediately before waiting for midnight.",
    )
    args = parser.parse_args(argv)

    if args.once:
        run_pipeline()
    else:
        if args.now:
            os.environ["RUN_ON_START"] = "1"
        run_scheduler()
    return 0


if __name__ == "__main__":
    sys.exit(main())
