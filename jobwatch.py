#!/usr/bin/env python3
"""
jobwatch: poll public ATS job boards (Ashby, Greenhouse, Lever, SmartRecruiters,
Workable, Recruitee, Personio) for roles whose titles match your keywords and
that look open to remote work from Europe.

Stdlib only, Python 3.11+.

  python jobwatch.py run                       # report matches new since last run
  python jobwatch.py run --all                 # report every current match
  python jobwatch.py probe "Owkin" "Cradle"    # detect which ATS + slug a company uses
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

UA = "Mozilla/5.0 (compatible; jobwatch/0.2; personal job-search script)"
TIMEOUT = 20
REPROBE_AFTER_DAYS = 7

MAX_PAGES = 10  # SmartRecruiters pages of 100


# --------------------------------------------------------------------------- model

@dataclass
class Job:
    company: str
    ats: str
    id: str
    title: str
    locations: list[str]
    remote_flag: bool | None  # what the ATS itself says (None = not stated)
    url: str
    text: str = ""
    posted: str = ""
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)
    bio: list[str] = field(default_factory=list)  # soft bio connection found (broader matches only)
    profile: list[str] = field(default_factory=list)  # profile topics found in the title (student roles only)

    @property
    def key(self) -> str:
        return f"{self.ats}:{self.company}:{self.id}"


# --------------------------------------------------------------------------- fetching

ENDPOINTS = {
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "greenhouse_eu": "https://boards-api.eu.greenhouse.io/v1/boards/{slug}/jobs?content=true",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "lever_eu": "https://api.eu.lever.co/v0/postings/{slug}?mode=json",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true",
    "recruitee": "https://{slug}.recruitee.com/api/offers/",
    "personio": "https://{slug}.jobs.personio.de/xml?language=en",
    "personio_com": "https://{slug}.jobs.personio.com/xml?language=en",
}
XML_ATS = {"personio", "personio_com"}
# These answer 200 with an empty list even for slugs that don't exist, so an
# empty board there proves nothing during probing.
EMPTY_IS_MEANINGLESS = {"smartrecruiters"}

COUNTRY = {
    "at": "Austria", "be": "Belgium", "bg": "Bulgaria", "ch": "Switzerland", "cy": "Cyprus",
    "cz": "Czechia", "de": "Germany", "dk": "Denmark", "ee": "Estonia", "es": "Spain",
    "fi": "Finland", "fr": "France", "gb": "United Kingdom", "uk": "United Kingdom",
    "gr": "Greece", "hr": "Croatia", "hu": "Hungary", "ie": "Ireland", "is": "Iceland",
    "it": "Italy", "lt": "Lithuania", "lu": "Luxembourg", "lv": "Latvia", "mt": "Malta",
    "nl": "Netherlands", "no": "Norway", "pl": "Poland", "pt": "Portugal", "ro": "Romania",
    "rs": "Serbia", "se": "Sweden", "si": "Slovenia", "sk": "Slovakia", "ua": "Ukraine",
    "us": "United States", "ca": "Canada",
}


def country_name(code_or_name: str | None) -> str:
    s = (code_or_name or "").strip()
    return COUNTRY.get(s.lower(), s)


def fetch_text(url: str, timeout: int = TIMEOUT) -> str | None:
    """GET a URL; returns None on 404 (board doesn't exist), raises on other errors."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "application/json, application/xml, text/html;q=0.9, */*;q=0.8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(5_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def decode(ats: str, body: str | None):
    """Turn a response body into JSON / an XML root; None if it isn't a board."""
    if body is None:
        return None
    try:
        if ats in XML_ATS:
            root = ET.fromstring(body)
            return root if root.tag == "workzag-jobs" else None
        return json.loads(body)
    except (json.JSONDecodeError, ET.ParseError):
        return None  # e.g. an HTML page served for an unknown subdomain


def load_board(ats: str, slug: str, fetch=fetch_text):
    """Fetch and decode one company's board (following SmartRecruiters paging)."""
    if ats != "smartrecruiters":
        return decode(ats, fetch(ENDPOINTS[ats].format(slug=slug)))
    content, offset = [], 0
    for _ in range(MAX_PAGES):
        page = decode(ats, fetch(ENDPOINTS[ats].format(slug=slug, offset=offset)))
        if not isinstance(page, dict):
            return None if offset == 0 else {"content": content}
        batch = page.get("content") or []
        content += batch
        offset += len(batch)
        if not batch or offset >= (page.get("totalFound") or 0):
            break
    return {"content": content}


_TAG = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    # Greenhouse double-escapes: unescape -> strip tags -> unescape entities.
    s = html.unescape(s or "")
    s = html.unescape(_TAG.sub(" ", s))
    return re.sub(r"\s+", " ", s).strip()


def _dedupe(items):
    seen, out = set(), []
    for x in items:
        x = (x or "").strip()
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


def _place(*parts) -> str:
    return ", ".join(p for p in (str(x).strip() for x in parts if x) if p)


def parse_ashby(company: str, data, ats: str, slug: str) -> list[Job]:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return []
    jobs = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        locs = [j.get("location")] + [s.get("location") for s in (j.get("secondaryLocations") or [])]
        wt = (j.get("workplaceType") or "").lower()
        remote = True if (j.get("isRemote") or wt == "remote") else None
        jobs.append(Job(
            company, ats, str(j.get("id", "")), j.get("title", ""), _dedupe(locs), remote,
            j.get("jobUrl") or j.get("applyUrl") or "",
            j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
            (j.get("publishedAt") or "")[:10],
        ))
    return jobs


def parse_greenhouse(company: str, data, ats: str, slug: str) -> list[Job]:
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return []
    jobs = []
    for j in data["jobs"]:
        locs = [(j.get("location") or {}).get("name")] + [o.get("name") for o in (j.get("offices") or [])]
        jobs.append(Job(
            company, ats, str(j.get("id", "")), j.get("title", ""), _dedupe(locs), None,
            j.get("absolute_url", ""), strip_html(j.get("content")), (j.get("updated_at") or "")[:10],
        ))
    return jobs


def parse_lever(company: str, data, ats: str, slug: str) -> list[Job]:
    if not isinstance(data, list):
        return []
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        locs = [cats.get("location")] + list(cats.get("allLocations") or [])
        remote = True if (j.get("workplaceType") or "").lower() == "remote" else None
        text = " ".join(
            [j.get("descriptionPlain") or "", j.get("additionalPlain") or ""]
            + [strip_html(l.get("content")) for l in (j.get("lists") or [])]
        )
        created = j.get("createdAt")
        posted = datetime.fromtimestamp(created / 1000, tz=timezone.utc).date().isoformat() if created else ""
        jobs.append(Job(company, ats, str(j.get("id", "")), j.get("text", ""), _dedupe(locs), remote,
                        j.get("hostedUrl", ""), text.strip(), posted))
    return jobs


def parse_smartrecruiters(company: str, data, ats: str, slug: str) -> list[Job]:
    # The list endpoint has no descriptions; location + remote flag carry the signal.
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        return []
    jobs = []
    for j in data["content"]:
        loc = j.get("location") or {}
        place = loc.get("fullLocation") or _place(loc.get("city"), loc.get("region"), country_name(loc.get("country")))
        remote = True if loc.get("remote") else None
        jid = str(j.get("id", ""))
        jobs.append(Job(company, ats, jid, j.get("name", ""), _dedupe([place, "Remote" if remote else ""]),
                        remote, f"https://jobs.smartrecruiters.com/{slug}/{jid}", "",
                        (j.get("releasedDate") or "")[:10]))
    return jobs


def parse_workable(company: str, data, ats: str, slug: str) -> list[Job]:
    # The widget lists multi-location jobs once per location: merge by shortcode.
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        return []
    merged: dict[str, Job] = {}
    for j in data["jobs"]:
        code = str(j.get("shortcode") or j.get("code") or j.get("url") or "")
        locs = [_place(j.get("city"), j.get("state"), country_name(j.get("country")))]
        locs += [_place(l.get("city"), l.get("region"), country_name(l.get("countryCode") or l.get("country")))
                 for l in (j.get("locations") or []) if not l.get("hidden")]
        remote = True if j.get("telecommuting") else None
        if code in merged:
            merged[code].locations = _dedupe(merged[code].locations + locs)
            continue
        merged[code] = Job(company, ats, code, j.get("title", ""), _dedupe(locs), remote,
                           j.get("url") or j.get("shortlink") or j.get("application_url") or "",
                           strip_html(j.get("description")),
                           (j.get("published_on") or j.get("created_at") or "")[:10])
    return list(merged.values())


def parse_recruitee(company: str, data, ats: str, slug: str) -> list[Job]:
    if not isinstance(data, dict) or not isinstance(data.get("offers"), list):
        return []
    jobs = []
    for j in data["offers"]:
        locs = [j.get("location"), _place(j.get("city"), country_name(j.get("country_code") or j.get("country")))]
        locs += [_place(l.get("city"), country_name(l.get("country_code") or l.get("country")))
                 for l in (j.get("locations") or [])]
        remote = True if j.get("remote") else None
        jobs.append(Job(company, ats, str(j.get("id", "")), j.get("title", ""), _dedupe(locs), remote,
                        j.get("careers_url") or j.get("careersUrl") or j.get("url") or "",
                        strip_html((j.get("description") or "") + " " + (j.get("requirements") or "")),
                        (j.get("published_at") or j.get("created_at") or "")[:10]))
    return jobs


def parse_personio(company: str, root, ats: str, slug: str) -> list[Job]:
    if root is None or not hasattr(root, "findall"):
        return []
    domain = "jobs.personio.com" if ats == "personio_com" else "jobs.personio.de"
    jobs = []
    for p in root.findall("position"):
        jid = (p.findtext("id") or "").strip()
        locs = [p.findtext("office")] + [o.text for o in p.findall("additionalOffices/office")]
        text = " ".join(strip_html(d.findtext("value")) for d in p.findall("jobDescriptions/jobDescription"))
        jobs.append(Job(company, ats, jid, (p.findtext("name") or "").strip(), _dedupe(locs), None,
                        f"https://{slug}.{domain}/job/{jid}", text, (p.findtext("createdAt") or "")[:10]))
    return jobs


PARSERS = {
    "ashby": parse_ashby, "greenhouse": parse_greenhouse, "greenhouse_eu": parse_greenhouse, "lever": parse_lever, "lever_eu": parse_lever,
    "smartrecruiters": parse_smartrecruiters, "workable": parse_workable, "recruitee": parse_recruitee,
    "personio": parse_personio, "personio_com": parse_personio,
}


def fetch_board(company: str, ats: str, slug: str, fetch=fetch_text) -> list[Job]:
    data = load_board(ats, slug, fetch)
    if data is None:
        raise LookupError(f"{ats} board '{slug}' not found")
    return PARSERS[ats](company, data, ats, slug)


# --------------------------------------------------------------------------- probing

GENERIC_SUFFIXES = {"bio", "biosciences", "bioscience", "biotechnology", "therapeutics", "labs", "lab",
                    "health", "technologies", "technology", "inc", "ai", "institute", "medicine", "discovery",
                    "genomics", "biomedicines", "research", "sciences", "bioworks", "biotools"}


def slug_candidates(name: str) -> list[tuple[str, bool]]:
    """Slug guesses for a company name, as (slug, exact).

    exact=False marks guesses built from a shortened name ("Character Biosciences"
    -> "character"), which can collide with a different company's board.
    """
    words = re.findall(r"[a-z0-9]+", name.lower())
    if not words:
        return []
    joined = "".join(words)
    cands = [(joined, True), ("-".join(words), True)]
    cands += [(joined + suf, True) for suf in ("bio", "ai", "hq", "inc")]
    stem = list(words)
    while len(stem) > 1 and stem[-1] in GENERIC_SUFFIXES:
        stem.pop()
    if stem != words:
        short = "".join(stem)
        # "characterbio" is still distinctive; a bare "character" is what collides.
        cands += [(short + "bio", True), (short + "-bio", True), ("-".join(stem), False), (short, False)]
    seen, out = set(), []
    for slug, exact in cands:
        if slug not in seen:
            seen.add(slug)
            out.append((slug, exact))
    return out


BOARD_KEY = {"ashby": "jobs", "greenhouse": "jobs", "greenhouse_eu": "jobs", "workable": "jobs",
             "recruitee": "offers", "smartrecruiters": "content"}


def is_board(ats: str, data) -> bool:
    """Does a decoded response have the shape of that ATS's job board?"""
    if ats in ("lever", "lever_eu"):
        return isinstance(data, list)
    if ats in XML_ATS:
        return data is not None
    return isinstance(data, dict) and isinstance(data.get(BOARD_KEY[ats]), list)


def probe(name: str, fetch=fetch_text, known=frozenset()) -> dict | None:
    """Try every ATS x slug candidate. Prefer boards that actually list jobs.

    Candidates already in `known` (boards found by `discover`) are tried first;
    if one of them lists jobs, the other ~60 guesses are skipped.
    """
    cands = slug_candidates(name)
    exact = {slug for slug, ex in cands if ex}
    all_attempts = [(ats, slug) for slug, _ in cands for ats in ENDPOINTS]
    first = [(a, s) for a, s in all_attempts if (a, s.lower()) in known]
    found = None
    if first:
        found = _probe(name, first, fetch)
    if not (found and found["confident"]):
        found = _probe(name, all_attempts, fetch)
    if found:
        found["exact"] = found["slug"] in exact
    return found


def _probe(name: str, attempts: list, fetch) -> dict | None:
    def try_one(pair):
        ats, slug = pair
        try:
            data = load_board(ats, slug, fetch)
        except Exception as e:
            return e
        if not is_board(ats, data):
            return None
        jobs = PARSERS[ats](name, data, ats, slug)
        if not jobs and ats in EMPTY_IS_MEANINGLESS:
            return None
        return {"ats": ats, "slug": slug, "n_jobs": len(jobs), "sample": [j.title for j in jobs[:3]]}

    with ThreadPoolExecutor(max_workers=8) as ex:
        outcomes = list(ex.map(try_one, attempts))
    results = [r for r in outcomes if isinstance(r, dict)]
    errors = [r for r in outcomes if isinstance(r, Exception)]
    if not results and errors and len(errors) == len(attempts):
        # Every request failed (network down?): don't mistake that for "no board".
        raise ConnectionError(f"probe for {name} failed: {errors[0]}")
    if not results:
        return None
    with_jobs = [r for r in results if r["n_jobs"] > 0]
    best = (with_jobs or results)[0]  # map() preserves candidate priority order
    best["confident"] = bool(with_jobs)
    return best


# --------------------------------------------------------------------------- classification

REMOTE_LOC = re.compile(r"remote|anywhere|distributed|work from home|\bwfh\b")
REMOTE_TXT = re.compile(r"fully remote|100% remote|remote[- ]first|work remotely|remote (position|role|opportunity)")
EUROPE_LOC = re.compile(
    r"europe|european|emea|\beu\b|\buk\b|united kingdom|england|scotland|ireland|dublin|london|oxford|"
    r"germany|berlin|munich|hamburg|france|paris|lyon|switzerland|z[uü]rich|basel|geneva|lausanne|"
    r"netherlands|amsterdam|belgium|brussels|denmark|copenhagen|sweden|stockholm|norway|oslo|finland|"
    r"helsinki|spain|madrid|barcelona|portugal|lisbon|italy|milan|austria|vienna|poland|warsaw|krak[oó]w|"
    r"czech|prague|praha|brno|slovakia|bratislava|slovenia|croatia|hungary|budapest|romania|bucharest|bulgaria|"
    r"serbia|ukraine|greece|athens|cyprus|malta|iceland|estonia|tallinn|latvia|riga|lithuania|vilnius|luxembourg"
)
US_LOC = re.compile(r"\bus\b|\busa\b|u\.s\.|united states|north america|canada|americas")
EUROPE_TXT = re.compile(
    r"(remote(ly)?|work) (from|in|within|across) (anywhere in )?(europe|the eu|emea)|"
    r"(europe|eu)[- ]based|based in europe|european time ?zones?|\bcet\b|\bcest\b|\bemea\b"
)
US_ONLY_TXT = re.compile(
    r"(must|need to|required to) (be|reside|live)[^.]{0,40}(united states|\bu\.?s\.?\b)|"
    r"\bus[- ]based (candidates|applicants) only|"
    r"authori[sz]ed to work in the (united states|u\.?s\.?)|"
    r"within the (continental )?united states"
)

VERDICTS = {
    "europe-remote": "Remote, with a Europe signal",
    "remote-unclear": "Remote, region unclear (ask the recruiter)",
    "europe-onsite": "European office, not flagged remote",
    "us-remote": "Remote, US-only signal",
    "other": "Other",
}


def _hit(rx: re.Pattern, s: str) -> str | None:
    m = rx.search(s)
    return m.group(0) if m else None


def classify(job: Job) -> Job:
    loc = " | ".join(job.locations).lower()
    text = job.text.lower()
    r = []
    remote = bool(job.remote_flag)
    if remote:
        r.append("ATS flags it remote")
    if (h := _hit(REMOTE_LOC, loc)):
        remote = True; r.append(f"location says '{h}'")
    if not remote and (h := _hit(REMOTE_TXT, text)):
        remote = True; r.append(f"description says '{h}'")

    eu_loc, eu_txt = _hit(EUROPE_LOC, loc), _hit(EUROPE_TXT, text)
    us_loc, us_txt = _hit(US_LOC, loc), _hit(US_ONLY_TXT, text)
    if eu_loc: r.append(f"location mentions '{eu_loc}'")
    if eu_txt: r.append(f"description mentions '{eu_txt}'")
    if us_loc: r.append(f"location mentions '{us_loc}'")
    if us_txt: r.append(f"description has US-only wording '{us_txt}'")

    if remote and (eu_loc or eu_txt):
        job.verdict = "europe-remote"
    elif remote and (us_loc or us_txt):
        job.verdict = "us-remote"
    elif remote:
        job.verdict = "remote-unclear"
    elif eu_loc:
        job.verdict = "europe-onsite"
    else:
        job.verdict = "other"
    job.reasons = r
    return job


def bio_connection(job: Job, patterns: list[re.Pattern]) -> list[str]:
    """Terms that tie a role to biology: any hit in the title or company name, or two different ones in the description."""
    def hits(s: str) -> list[str]:
        return list(dict.fromkeys(h for rx in patterns if (h := _hit(rx, s))))
    strong = hits(f"{job.title} {job.company}".lower())
    if strong:
        return strong
    weak = hits(job.text.lower())
    return weak[:3] if len(weak) >= 2 else []


# Language check. Stop words that are not also common English words, per language; the posting is taken
# to be in the language with the most hits (at least 4, and more than English has).
STOPWORDS = {
    "en": "the and of to with you we our will is are for as be your this that have".split(),
    "es": "los las del para con que una por nuestro nuestros somos su al como más experiencia trabajo".split(),
    "de": "der die das und mit für ist wir sie ein eine nicht auf den von werden bei auch oder uns".split(),
    "fr": "les des et une pour avec vous nous est dans du que sur nos vos sont être".split(),
    "cs": "pro že nebo jsme jsou naše který která kteří pozici pozice hledáme budete jako také ale nabízí týmu práci práce firma studenty".split(),
    "nl": "het een van voor met wij ons zijn wordt bij ook naar jij".split(),
    "it": "il che per della nel siamo sono delle degli come alla anche".split(),
    "pt": "os uma não são também você seu nós".split(),
    "pl": "jest dla nasz oraz się lub będzie przez naszym".split(),
}
LANGUAGE_NAMES = {
    "en": ["english"], "es": ["spanish", "español", "castellano"], "de": ["german", "deutsch"],
    "fr": ["french", "français", "francais"], "cs": ["czech", "čeština", "česky"], "nl": ["dutch", "nederlands"],
    "it": ["italian", "italiano"], "pt": ["portuguese", "português"], "pl": ["polish", "polski"],
    "sv": ["swedish"], "da": ["danish"], "no": ["norwegian"], "fi": ["finnish"], "ru": ["russian"],
    "uk": ["ukrainian"], "hu": ["hungarian"], "ro": ["romanian"], "el": ["greek"], "tr": ["turkish"],
    "ar": ["arabic"], "zh": ["chinese", "mandarin"], "ja": ["japanese"],
}
LANG_REQUIRED = re.compile(r"required|must|mandatory|fluent|fluency|native|proficien|speaker|speaking|"
                           r"business[- ]level|\bc1\b|\bc2\b|fließend|excellent command")
LANG_OPTIONAL = re.compile(r"\bplus\b|nice to have|advantage|bonus|preferred|\basset\b|desirable|beneficial")
WORD = re.compile(r"[a-zà-ÿčďěňřšťůýž]+")


def language_ok(job: Job, allowed: set[str]) -> bool:
    """False if the posting is written in a language outside `allowed`, or asks for one as a requirement."""
    text = f"{job.title}. {job.text}".lower()
    words = WORD.findall(text)
    scores = {lang: sum(1 for w in words if w in set(stop)) for lang, stop in STOPWORDS.items()}
    best = max(scores, key=scores.get)
    if best not in allowed and scores[best] >= 4 and scores[best] > scores["en"]:
        return False
    banned = [name for lang, names in LANGUAGE_NAMES.items() if lang not in allowed for name in names]
    named = re.compile(r"\b(" + "|".join(re.escape(n) for n in banned) + r")\b")
    for clause in re.split(r"[.;\n•]+", text):
        if named.search(clause) and LANG_REQUIRED.search(clause) and not LANG_OPTIONAL.search(clause):
            return False
    return True


def student_roles(jobs, cfg: dict, bio_hints: list[re.Pattern]) -> list[Job]:
    """Student, internship and junior roles that fit the profile in [students] and are open across Europe.

    A role needs a student- or junior-type title (and none of the excluded words), a topic from the profile in the
    title or a bio connection, and an accepted verdict; on-site roles also count when the location matches
    `local`."""
    def rx(key: str) -> list[re.Pattern]:
        return [re.compile(p, re.I) for p in cfg.get(key, [])]
    titles, excluded, topics = rx("titles"), rx("exclude"), rx("topics")
    verdicts = set(cfg.get("verdicts", ["europe-remote", "remote-unclear"]))
    local = re.compile(cfg["local"], re.I) if cfg.get("local") else None
    languages = set(cfg.get("languages", []))
    region = re.compile(cfg["region_ok"], re.I) if cfg.get("region_ok") else None
    blocked = [re.compile(p, re.I) for p in cfg.get("exclude_text", [])]
    out = []
    for j in jobs:
        if not any(r.search(j.title) for r in titles) or any(r.search(j.title) for r in excluded):
            continue
        hits = list(dict.fromkeys(h for r in topics if (h := _hit(r, j.title.lower()))))
        bio = bio_connection(j, bio_hints) if bio_hints else []
        if not hits and not bio:
            continue
        if languages and not language_ok(j, languages):
            continue
        if any(r.search(f"{j.title} {j.text}") for r in blocked):
            continue
        classify(j)
        loc = " | ".join(j.locations)
        near = j.verdict == "europe-onsite" and local and local.search(loc)
        if not (j.verdict in verdicts or near):
            continue
        # Remote across Europe: the posting must name Europe/EU/EMEA (or Czechia, or anywhere), not just
        # some other single country such as "Remote, Germany".
        if region and not near and not (region.search(loc) or EUROPE_TXT.search(j.text.lower())):
            continue
        j.profile, j.bio = hits, bio
        out.append(j)
    return out


def title_matches(title: str, include: list[str], exclude: list[str]) -> bool:
    t = title.lower()
    return any(re.search(p, t) for p in include) and not any(re.search(p, t) for p in exclude)


# --------------------------------------------------------------------------- state + report

def load_state(path: Path) -> dict:
    if path.exists():
        s = json.loads(path.read_text())
    else:
        s = {}
    s.setdefault("seen", {})
    s.setdefault("resolved", {})
    return s


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True))


# --------------------------------------------------------------------------- website scanning

_SLUG = r"([A-Za-z0-9][A-Za-z0-9._-]*)"
# Order matters: EU hosts before their US counterparts.
HTML_BOARD_PATTERNS = [
    ("ashby", r"jobs\.ashbyhq\.com/" + _SLUG),
    ("ashby", r"api\.ashbyhq\.com/posting-api/job-board/" + _SLUG),
    ("greenhouse_eu", r"(?:boards|job-boards)\.eu\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?" + _SLUG),
    ("greenhouse_eu", r"boards-api\.eu\.greenhouse\.io/v1/boards/" + _SLUG),
    ("greenhouse", r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?" + _SLUG),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/" + _SLUG),
    ("lever_eu", r"jobs\.eu\.lever\.co/" + _SLUG),
    ("lever", r"jobs\.lever\.co/" + _SLUG),
    ("lever", r"api\.lever\.co/v0/postings/" + _SLUG),
    ("workable", r"apply\.workable\.com/" + _SLUG),
    ("workable", r"//([a-z0-9-]+)\.workable\.com"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/" + _SLUG),
    ("recruitee", r"//([a-z0-9-]+)\.recruitee\.com"),
    ("personio_com", r"//([a-z0-9-]+)\.jobs\.personio\.com"),
    ("personio", r"//([a-z0-9-]+)\.jobs\.personio\.de"),
]
CAREERS_WORDS = re.compile(r"career|jobs?\b|join|work-with-us|work-at|vacanc|open-?positions|opportunit|hiring|stellen",
                           re.I)


def scan_html(html: str) -> list[tuple[str, str]]:
    """Job-board links in a page, most frequent first, as (ats, slug)."""
    html = html.replace("\\/", "/")  # links inside escaped JSON
    counts: dict[tuple[str, str], int] = {}
    for ats, rx in HTML_BOARD_PATTERNS:
        for m in re.finditer(rx, html, re.I):
            slug = m.group(1).rstrip(".")
            if slug.lower() in NOT_SLUGS or slug.lower() in ("eu", "boards") or not SLUG_OK.match(slug):
                continue
            key = (ats, slug)
            counts[key] = counts.get(key, 0) + 1
    return sorted(counts, key=lambda k: -counts[k])


def careers_links(html: str, page_url: str) -> list[str]:
    """Links on a page that look like a careers page on the same site."""
    host = urllib.parse.urlparse(page_url).netloc.lower().removeprefix("www.")
    out = []
    for href in re.findall(r"""href=["']([^"'#]+)["']""", html, re.I):
        url = urllib.parse.urljoin(page_url, html_unescape(href))
        u = urllib.parse.urlparse(url)
        if u.scheme in ("http", "https") and u.netloc.lower().removeprefix("www.").endswith(host) \
                and CAREERS_WORDS.search(u.path):
            out.append(url.split("?")[0])
    return list(dict.fromkeys(out))


def html_unescape(s: str) -> str:
    return html.unescape(s)


def find_boards_on_website(website: str, fetch=fetch_text, max_pages: int = 5) -> list[tuple[str, str]]:
    """Look for job-board links on a company's homepage, then its careers pages."""
    base = website if re.match(r"https?://", website) else "https://" + website
    queue, seen = [base], set()
    while queue and len(seen) < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        try:
            page = fetch(url)
        except Exception:
            page = None
        if not page:
            continue
        boards = scan_html(page)
        if boards:
            return boards
        if url == base:  # from the homepage, follow careers links, then common paths
            queue += careers_links(page, url)[:3]
            queue += [urllib.parse.urljoin(base, p) for p in ("/careers", "/jobs")]
    return []


def resolve_from_website(name: str, website: str, fetch=fetch_text) -> dict | None:
    """First job-board link on the website that is a real board."""
    for ats, slug in find_boards_on_website(website, fetch):
        try:
            data = load_board(ats, slug, fetch)
        except Exception:
            continue
        if is_board(ats, data):
            jobs = PARSERS[ats](name, data, ats, slug)
            return {"ats": ats, "slug": slug, "n_jobs": len(jobs), "sample": [j.title for j in jobs[:3]],
                    "confident": True, "exact": True, "via": "website"}
    return None


def _detect(c: dict, fetch, known) -> tuple[dict | None, Exception | None]:
    """Find one company's board: website first, then name guessing."""
    found = resolve_from_website(c["name"], c["website"], fetch) if c.get("website") else None
    try:
        return found or probe(c["name"], fetch, known), None
    except ConnectionError as e:
        return None, e


def _n(count: int, word: str) -> str:
    plural = {"company": "companies", "board": "boards"}.get(word, word + "s")
    return f"{count} {word if count == 1 else plural}"


def _names(names: list[str], limit: int = 25) -> str:
    shown = ", ".join(names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def resolve_boards(companies: list[dict], state: dict, today: date, fetch=fetch_text, known=frozenset(),
                   workers: int = 8):
    """Return [(name, ats, slug)] plus a few summary notes about board detection."""
    boards, to_detect = [], []
    for c in companies:
        name = c["name"]
        if c.get("ats") and c.get("slug"):
            boards.append((name, c["ats"], c["slug"]))
            continue
        cached = state["resolved"].get(name)
        stale = cached and cached.get("ats") is None and (
            today - date.fromisoformat(cached["checked"]) >= timedelta(days=REPROBE_AFTER_DAYS))
        if not cached or stale:
            to_detect.append(c)
        elif cached.get("ats"):
            boards.append((name, cached["ats"], cached["slug"]))

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        results = list(ex.map(lambda c: _detect(c, fetch, known), to_detect))

    via_site, guessed, unreachable, missing = [], [], [], []
    for c, (found, err) in zip(to_detect, results):
        name = c["name"]
        if err is not None:
            unreachable.append(name)
            continue
        state["resolved"][name] = ({**found, "checked": today.isoformat()} if found
                                   else {"ats": None, "checked": today.isoformat()})
        if not found:
            missing.append(name)
            continue
        boards.append((name, found["ats"], found["slug"]))
        if found.get("via") == "website":
            via_site.append(f"{name} → {found['ats']}/{found['slug']}")
        else:  # guesses can hit a different company with the same name: show evidence
            flag = "" if found["confident"] else " (empty board)"
            if not found.get("exact", True):
                flag += f" ⚠ shortened name '{found['slug']}', may be a different company"
            site = " (its website had no job-board link)" if c.get("website") else ""
            guessed.append(f"**{name}** → {found['ats']}/{found['slug']}{site}, "
                           f"{found['n_jobs']} jobs, e.g. {found['sample']}{flag}")

    notes = []
    if via_site:
        notes.append(f"Found {_n(len(via_site), 'job board')} via company websites: {_names(via_site)}.")
    if guessed:
        notes.append(f"Found {_n(len(guessed), 'job board')} by guessing from the name; check the sample titles "
                     f"belong to the right company, then pin ats + slug:")
        notes += guessed
    if missing:
        notes.append(f"No public job board found for {_n(len(missing), 'company')} (retried weekly; often Workday "
                     f"or a custom careers page): {_names(missing)}.")
    if unreachable:
        notes.append(f"Could not reach job boards for {_n(len(unreachable), 'company')}; will retry next run: "
                     f"{_names(unreachable)}.")
    return boards, notes


# --------------------------------------------------------------------------- discovery (Common Crawl)

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_TIMEOUT = 90

# (ats, Common Crawl URL pattern, regex capturing the board slug)
DISCOVERY_SOURCES = [
    ("ashby", "jobs.ashbyhq.com/*", r"^https?://jobs\.ashbyhq\.com/([^/?#]+)"),
    ("greenhouse", "boards.greenhouse.io/*",
     r"^https?://boards\.greenhouse\.io/(?:embed/job_board\?for=)?([^/?#&]+)"),
    ("greenhouse", "job-boards.greenhouse.io/*",
     r"^https?://job-boards\.greenhouse\.io/(?:embed/job_board\?for=)?([^/?#&]+)"),
    ("greenhouse_eu", "job-boards.eu.greenhouse.io/*",
     r"^https?://job-boards\.eu\.greenhouse\.io/(?:embed/job_board\?for=)?([^/?#&]+)"),
    ("lever", "jobs.lever.co/*", r"^https?://jobs\.lever\.co/([^/?#]+)"),
    ("lever_eu", "jobs.eu.lever.co/*", r"^https?://jobs\.eu\.lever\.co/([^/?#]+)"),
    ("workable", "apply.workable.com/*", r"^https?://apply\.workable\.com/([^/?#]+)"),
    ("recruitee", "*.recruitee.com", r"^https?://([a-z0-9-]+)\.recruitee\.com"),
    ("personio", "*.jobs.personio.de", r"^https?://([a-z0-9-]+)\.jobs\.personio\.de"),
    ("personio_com", "*.jobs.personio.com", r"^https?://([a-z0-9-]+)\.jobs\.personio\.com"),
]
# SmartRecruiters is left out: it answers for any slug, so junk slugs could never be pruned.
NOT_SLUGS = {"embed", "api", "j", "www", "app", "apply", "careers", "jobs", "static", "assets", "cdn",
             "favicon.ico", "robots.txt", "sitemap.xml", "posting-api", "login", "signup", "help", "support"}
SLUG_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def extract_slug(regex: str, url: str) -> str | None:
    m = re.match(regex, url, re.I)
    if not m:
        return None
    slug = urllib.parse.unquote(m.group(1)).strip()
    if slug.lower() in NOT_SLUGS or not SLUG_OK.match(slug):
        return None
    return slug


def fetch_patient(url: str, tries: int = 4, sleep=time.sleep) -> str | None:
    """Common Crawl's index is slow and sometimes answers 503: retry with backoff."""
    for i in range(tries):
        try:
            return fetch_text(url, timeout=CC_TIMEOUT)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if i == tries - 1:
                raise
            sleep(5 * 2 ** i)


def cc_urls(cdx_api: str, pattern: str, fetch, max_pages: int):
    q = f"{cdx_api}?url={urllib.parse.quote(pattern, safe='*/.')}&output=json&fl=url"
    meta = fetch(q + "&showNumPages=true")
    if not meta:
        return
    try:
        pages = int(json.loads(meta).get("pages", 0))
    except (json.JSONDecodeError, AttributeError, ValueError):
        return
    for p in range(min(pages, max_pages)):
        body = fetch(f"{q}&page={p}")
        for line in (body or "").splitlines():
            try:
                yield json.loads(line)["url"]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue


def load_discovered(path: Path) -> dict:
    d = json.loads(path.read_text()) if path.exists() else {}
    d.setdefault("updated", None)
    d.setdefault("boards", {})
    return d


def discover(path: Path, crawls: int = 2, max_pages: int = 100, fetch=fetch_patient,
             today: date | None = None, log=print) -> dict:
    """Find job boards in the newest Common Crawl indexes and merge them into `path`."""
    today = today or date.today()
    store = load_discovered(path)
    collinfo = json.loads(fetch(CC_COLLINFO) or "[]")
    apis = [c["cdx-api"] for c in collinfo[:crawls]]
    log(f"Using crawls: {', '.join(c['id'] for c in collinfo[:crawls])}")
    for ats, pattern, rx in DISCOVERY_SOURCES:
        slugs: dict[str, str] = {}
        for api in apis:
            try:
                for url in cc_urls(api, pattern, fetch, max_pages):
                    if (s := extract_slug(rx, url)):
                        slugs.setdefault(s.lower(), s)
            except Exception as e:  # one failing pattern shouldn't lose the rest
                log(f"  {pattern} in {api}: {e}")
        new = 0
        for low, s in slugs.items():
            key = f"{ats}:{low}"
            if key not in store["boards"]:
                store["boards"][key] = {"ats": ats, "slug": s, "first_seen": today.isoformat()}
                new += 1
        log(f"{ats:<15} {pattern:<28} {len(slugs):>6} slugs, {new:>6} new")
    store["updated"] = today.isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=1, sort_keys=True))
    live = sum(1 for b in store["boards"].values() if not b.get("dead"))
    log(f"{live} live boards in {path}")
    return store


# --------------------------------------------------------------------------- VC portfolio boards (Getro)

NEXT_DATA = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
GETRO_PAGE_SIZE = 20


def canon_url(url: str) -> str:
    """Comparable form of a job URL: no query string, fragment or trailing slash."""
    u = urllib.parse.urlparse(url.strip())
    return f"{u.scheme.lower()}://{u.netloc.lower()}{u.path.rstrip('/')}"


def _find_job_list(obj, depth: int = 0):
    """Fallback when the page layout changes: first list of dicts that look like jobs."""
    if depth > 10:
        return None
    if isinstance(obj, list) and obj and isinstance(obj[0], dict) and "title" in obj[0] \
            and ("organization" in obj[0] or "url" in obj[0]):
        return obj
    children = obj.values() if isinstance(obj, dict) else obj if isinstance(obj, list) else []
    for v in children:
        if (found := _find_job_list(v, depth + 1)) is not None:
            return found
    return None


def _getro_date(v) -> str:
    if isinstance(v, (int, float)) and v > 0:
        return datetime.fromtimestamp(v / 1000 if v > 1e12 else v, tz=timezone.utc).date().isoformat()
    return str(v or "")[:10]


def parse_getro_page(page: str | None, board_id: str, base: str) -> list[Job] | None:
    """Jobs on one Getro board page; None if the page isn't a Getro (Next.js) board."""
    m = NEXT_DATA.search(page or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    try:
        found = data["props"]["pageProps"]["initialState"]["jobs"]["found"]
    except (KeyError, TypeError):
        found = _find_job_list(data) or []
    jobs = []
    for it in found:
        if not isinstance(it, dict):
            continue
        org = it.get("organization") or {}
        title, url = (it.get("title") or "").strip(), (it.get("url") or "").strip()
        if not title or not url:
            continue
        url = urllib.parse.urljoin(base + "/", url)
        locs = [l if isinstance(l, str) else (l.get("name") or l.get("label") or "")
                for l in (it.get("locations") or []) if l]
        remote = True if (it.get("workMode") or it.get("work_mode") or "").lower() == "remote" else None
        jobs.append(Job(org.get("name") or "unknown company", f"getro:{board_id}", canon_url(url), title,
                        _dedupe(locs), remote, url, "", _getro_date(it.get("createdAt") or it.get("created_at"))))
    return jobs


def fetch_getro_board(board: dict, keywords: list[str], fetch=fetch_text, max_pages: int = 3) -> list[Job]:
    """Keyword searches on a Getro portfolio board (the board does the searching)."""
    base = board["url"].rstrip("/")
    jobs: dict[str, Job] = {}
    for i, kw in enumerate(keywords):
        for page in range(1, max_pages + 1):
            url = f"{base}/jobs?q={urllib.parse.quote_plus(kw)}" + (f"&page={page}" if page > 1 else "")
            page_html = fetch(url)
            got = parse_getro_page(page_html, board["id"], base)
            if got is None:
                if i == 0 and page == 1:
                    raise LookupError("page not found" if page_html is None
                                      else "doesn't look like a Getro board (no embedded job data)")
                break
            new = [j for j in got if j.id not in jobs]
            jobs.update((j.id, j) for j in got)
            if len(got) < GETRO_PAGE_SIZE or not new:
                break
    return list(jobs.values())


def board_from_job_url(url: str) -> tuple[str, str] | None:
    """The job board a job link points to, e.g. jobs.ashbyhq.com/acme/123 -> ("ashby", "acme")."""
    found = scan_html(url)
    return found[0] if found else None


# --------------------------------------------------------------------------- YC company import

YC_ALL = "https://yc-oss.github.io/api/companies/all.json"
YC_TAGS = {"biotech", "biotechnology", "therapeutics", "ai-powered drug discovery", "drug discovery",
           "synthetic biology", "diagnostics", "genomics", "gene therapy", "cell therapy", "crispr",
           "genetic engineering", "drug delivery", "computational biology", "bioinformatics", "proteomics",
           "precision medicine", "single-cell", "protein design"}
YC_SUBINDUSTRIES = {"healthcare -> therapeutics", "healthcare -> drug discovery and delivery",
                    "healthcare -> diagnostics", "healthcare -> industrial bio"}


def _domain(url: str | None) -> str:
    host = urllib.parse.urlparse(url if url and "//" in url else f"https://{url or ''}").netloc.lower()
    return host.removeprefix("www.")


def yc_relevant(c: dict) -> bool:
    if (c.get("status") or "").lower() != "active" or not c.get("website"):
        return False
    tags = {t.lower() for t in (c.get("tags") or [])}
    return bool(tags & YC_TAGS) or (c.get("subindustry") or "").lower() in YC_SUBINDUSTRIES


def write_yc_companies(companies: list[dict], out: Path, exclude_names=(), exclude_domains=(),
                       today: date | None = None) -> int:
    """Write relevant YC companies as [[company]] entries; returns how many."""
    today = today or date.today()
    names, domains = {n.lower() for n in exclude_names}, set(exclude_domains)
    picked = []
    for c in sorted(companies, key=lambda c: c.get("name", "").lower()):
        if yc_relevant(c) and c["name"].lower() not in names and _domain(c["website"]) not in domains:
            names.add(c["name"].lower())
            domains.add(_domain(c["website"]))
            picked.append(c)
    lines = [f"# generated: {today.isoformat()} by `python jobwatch.py import-yc` from the open yc-oss",
             "# dataset of Y Combinator's directory. Regenerated monthly; edit companies.toml instead.", ""]
    for c in picked:
        note = (c.get("one_liner") or "").replace("\n", " ")[:90]
        lines += [f"# YC {c.get('batch', '')}: {note}", "[[company]]",
                  f"name = {json.dumps(c['name'], ensure_ascii=False)}",
                  f"website = {json.dumps(c['website'], ensure_ascii=False)}", ""]
    out.write_text("\n".join(lines))
    return len(picked)


def generated_date(path: Path) -> date | None:
    if not path.exists():
        return None
    m = re.match(r"# generated: (\d{4}-\d{2}-\d{2})", path.read_text()[:200])
    return date.fromisoformat(m.group(1)) if m else None


def load_companies(cfg: dict, config_dir: Path) -> list[dict]:
    """companies.toml entries plus any extra company files, deduplicated (companies.toml wins)."""
    companies = list(cfg.get("company", []))
    names = {c["name"].lower() for c in companies}
    domains = {_domain(c["website"]) for c in companies if c.get("website")}
    for f in cfg.get("sources", {}).get("company_files", []):
        p = Path(f) if Path(f).is_absolute() else config_dir / f
        if not p.exists():
            continue
        for c in tomllib.loads(p.read_text()).get("company", []):
            d = _domain(c.get("website"))
            if c["name"].lower() in names or (d and d in domains):
                continue
            names.add(c["name"].lower())
            if d:
                domains.add(d)
            companies.append(c)
    return companies


# --------------------------------------------------------------------------- run

SUBDOMAIN_ATS = {"recruitee", "personio", "personio_com"}


def board_is_gone(ats: str, e: Exception) -> bool:
    """404 / not-a-board, or (for subdomain-based boards) a name that no longer resolves."""
    if isinstance(e, LookupError):
        return True
    return (ats in SUBDOMAIN_ATS and isinstance(e, urllib.error.URLError)
            and isinstance(e.reason, socket.gaierror))


def collect(boards, fetch=fetch_text, workers: int = 16):
    """Fetch boards in parallel. Returns jobs and [(board, exception)] failures."""
    jobs, failures = [], []

    def one(b):
        name, ats, slug = b
        try:
            return fetch_board(name, ats, slug, fetch), None
        except Exception as e:  # keep going if one board is down
            return [], e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for b, (js, err) in zip(boards, ex.map(one, boards)):
            jobs += js
            if err is not None:
                failures.append((b, err))
    return jobs, failures


def _verdict_groups(jobs: list[Job], level: str) -> list[str]:
    out = []
    for v, label in VERDICTS.items():
        group = sorted((j for j in jobs if j.verdict == v), key=lambda j: (j.company.lower(), j.title))
        if not group:
            continue
        out += [f"{level} {label}", ""]
        for j in group:
            posted = f", posted {j.posted}" if j.posted else ""
            out.append(f"- **[{j.title}]({j.url})**, {j.company} · {'; '.join(j.locations) or 'no location'}{posted}")
            if j.reasons:
                out.append(f"  - why: {'; '.join(j.reasons)}")
            if j.bio:
                out.append(f"  - bio connection: {', '.join(j.bio)}")
            if j.profile:
                out.append(f"  - profile match: {', '.join(j.profile)}")
        out.append("")
    return out


def render(jobs: list[Job], total: int, n_boards: int, notes, errors, show_all: bool, today: date,
           discovery_line: str = "", broad: list[Job] | None = None, broad_left: int = 0,
           students: list[Job] | None = None, students_left: int = 0) -> str:
    head = "all current matches" if show_all else "new since last run"
    extra = ""
    if broad:
        n_bio = sum(1 for j in broad if j.bio)
        extra = f" + {len(broad)} broader matches" + (f" ({n_bio} with a possible bio connection)" if n_bio else "")
    if students:
        extra += f" + {len(students)} student/junior roles"
    out = [f"# jobwatch, {today.isoformat()}",
           f"{len(jobs)} roles ({head}){extra}; {total} matching right now across {n_boards} boards.", ""]
    if discovery_line:
        out += [discovery_line, ""]
    out += _verdict_groups(jobs, "##")
    if students:
        out += ["## Student, internship and junior roles (Europe-friendly)", ""] + _verdict_groups(students, "###")
        if students_left:
            out += [f"{students_left} more student/junior roles are waiting; they appear in the next runs.", ""]
    if broad:
        out += ["## Broader matches (looser title filter, lower relevance)", ""]
        flagged, rest = [j for j in broad if j.bio], [j for j in broad if not j.bio]
        if flagged:
            out += ["### Possible bio connection", ""] + _verdict_groups(flagged, "####")
            if rest:
                out += ["### Other broader matches", ""] + _verdict_groups(rest, "####")
        else:
            out += _verdict_groups(broad, "###")
        if broad_left:
            out += [f"{broad_left} more broader matches are waiting; they appear in the next runs.", ""]
    if notes:
        out += ["## Board detection", ""] + [f"- {n}" for n in notes] + [""]
    if errors:
        out += ["## Errors", ""] + [f"- {e}" for e in errors] + [""]
    return "\n".join(out)


def run(config: Path, state_path: Path, out: Path | None, show_all: bool, fetch=fetch_text,
        today: date | None = None) -> tuple[str, int]:
    today = today or date.today()
    cfg = tomllib.loads(config.read_text())
    f = cfg.get("filters", {})
    include, exclude = f.get("include_titles", []), f.get("exclude_titles", [])
    broad_include, broad_max = f.get("broad_titles", []), int(f.get("broad_max", 100))
    bio_hints = [re.compile(p, re.I) for p in f.get("bio_hints", [])]
    stu = cfg.get("students", {})
    languages = set(f.get("languages", []))
    wanted = set(f.get("verdicts", ["europe-remote", "remote-unclear"]))
    disc_cfg = cfg.get("discovery", {})
    vc_cfg = cfg.get("vc_boards", {})
    workers = int(cfg.get("settings", {}).get("max_workers", 16))
    companies = load_companies(cfg, config.parent)

    # Boards found by `discover` (Common Crawl), if enabled.
    disc_path = Path(disc_cfg.get("file", ".jobwatch/discovered.json"))
    dstore = load_discovered(disc_path) if disc_cfg.get("enabled") else {"boards": {}}
    known = frozenset((b["ats"], b["slug"].lower()) for b in dstore["boards"].values() if not b.get("dead"))

    state = load_state(state_path)
    state.setdefault("learned", {})
    boards, notes = resolve_boards(companies, state, today, fetch, known, workers=min(workers, 8))
    named = {(ats, slug.lower()) for _, ats, slug in boards}
    learned = [(b["company"], b["ats"], b["slug"]) for b in state["learned"].values()
               if (b["ats"], b["slug"].lower()) not in named]
    named |= {(ats, slug.lower()) for _, ats, slug in learned}
    discovered = [(b["slug"], b["ats"], b["slug"]) for b in dstore["boards"].values()
                  if not b.get("dead") and (b["ats"], b["slug"].lower()) not in named]

    jobs, failures = collect(boards + learned + discovered, fetch, workers)

    # Investor portfolio boards: keyword searches, run alongside the job boards.
    vc_boards = cfg.get("vc_board", []) if vc_cfg.get("enabled", True) else []
    keywords = vc_cfg.get("keywords", ["computational biology", "bioinformatics"])
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as ex:
        vc_results = list(ex.map(lambda b: _safe(fetch_getro_board, b, keywords, fetch), vc_boards))
    vc_errors = []
    for b, (got, err) in zip(vc_boards, vc_results):
        jobs += got
        if err is not None:
            vc_errors.append(f"Investor board {b.get('name', b['id'])} ({b['url']}): {err}")

    # Failures: list named ones, forget vanished learned/cached boards, prune vanished discovered ones.
    disc_set = {(a, s) for _, a, s in discovered}
    learned_set = {(a, s) for _, a, s in learned}
    disc_failures = [(b, e) for b, e in failures if (b[1], b[2]) in disc_set]
    pinned = {c["name"] for c in companies if c.get("ats") and c.get("slug")}
    errors = []
    for (n, a, s_), e in failures:
        if (a, s_) in disc_set:
            continue
        if (a, s_) in learned_set and isinstance(e, LookupError):
            state["learned"].pop(f"{a}:{s_.lower()}", None)
            notes.append(f"Learned board for **{n}** ({a}/{s_}) no longer exists; dropped it.")
            continue
        msg = f"{n} ({a}/{s_}): {e}"
        if isinstance(e, LookupError) and n not in pinned and n in state["resolved"]:
            del state["resolved"][n]
            msg += " — will re-detect next run"
        errors.append(msg)
    errors += vc_errors
    outage = len(discovered) >= 20 and len(disc_failures) > len(discovered) / 2
    gone = 0
    if not outage:
        for (_, ats, slug), e in disc_failures:
            if board_is_gone(ats, e):
                dstore["boards"][f"{ats}:{slug.lower()}"]["dead"] = today.isoformat()
                gone += 1
    if discovered:
        disc_path.write_text(json.dumps(dstore, indent=1, sort_keys=True))
    discovery_line = ""
    if discovered:
        discovery_line = (f"Also checked {len(discovered)} discovered boards: {gone} no longer exist (pruned), "
                          f"{len(disc_failures) - gone} failed temporarily.")
        if outage:
            discovery_line += " Most failed, so this looks like a network problem; nothing was pruned."

    # The same job can arrive from a company's board and an investor board: keep one, preferring the former.
    by_url: dict[str, Job] = {}
    for j in jobs:
        c = canon_url(j.url) if j.url.startswith("http") else j.key
        if c not in by_url or (by_url[c].ats.startswith("getro:") and not j.ats.startswith("getro:")):
            by_url[c] = j
    title_hits = [j for j in by_url.values() if title_matches(j.title, include, exclude)]

    # Learn companies: relevant roles found on investor boards or discovered boards.
    learned_now = []
    for j in title_hits:
        if j.ats.startswith("getro:"):
            board, via = board_from_job_url(j.url), f"investor board {j.ats.split(':', 1)[1]}"
        elif (j.ats, j.company) in disc_set:
            board, via = (j.ats, j.company), "discovered boards"
        else:
            continue
        if not board or (board[0], board[1].lower()) in named:
            continue
        key = f"{board[0]}:{board[1].lower()}"
        if key not in state["learned"]:
            name = j.company if j.ats.startswith("getro:") else board[1]
            state["learned"][key] = {"ats": board[0], "slug": board[1], "company": name,
                                     "via": via, "first_seen": today.isoformat(), "example": j.title}
            named.add((board[0], board[1].lower()))
            learned_now.append(f"**{name}** → {board[0]}/{board[1]} (from {via}; e.g. “{j.title}”)")
    if learned_now:
        notes = [f"Learned {len(learned_now)} new relevant compan{'y' if len(learned_now) == 1 else 'ies'}; "
                 f"they're checked every run from now on (`python jobwatch.py learned` lists them as "
                 f"companies.toml entries):"] + learned_now + notes

    # Student, internship and junior roles that fit the configured profile. Handled first, so a role shows up in
    # one section only.
    students_all = student_roles(by_url.values(), stu, bio_hints) if stu.get("enabled") else []
    student_ids = {id(j) for j in students_all}

    matches = [classify(j) for j in title_hits if id(j) not in student_ids]
    matches = [j for j in matches if j.verdict in wanted and (not languages or language_ok(j, languages))]
    def url_key(j):
        return f"url:{canon_url(j.url)}" if j.url.startswith("http") else None

    def is_new(j):
        return j.key not in state["seen"] and url_key(j) not in state["seen"]

    def mark_seen(js):
        for j in js:
            state["seen"].setdefault(j.key, today.isoformat())
            if url_key(j):
                state["seen"].setdefault(url_key(j), today.isoformat())

    new = [j for j in matches if is_new(j)]
    mark_seen(matches)

    # Broader matches: titles that only the looser `broad_titles` list accepts. Reported in their own
    # section; they don't teach new companies. Newest first, at most broad_max new ones per run (the
    # rest stay unseen and show up in later runs).
    strict = {id(j) for j in title_hits} | student_ids
    broad_matches = [classify(j) for j in by_url.values()
                     if broad_include and id(j) not in strict and title_matches(j.title, broad_include, exclude)]
    broad_matches = [j for j in broad_matches if j.verdict in wanted and (not languages or language_ok(j, languages))]
    for j in broad_matches:
        j.bio = bio_connection(j, bio_hints) if bio_hints else []
    # Bio-connected roles first (so the cap never delays them), then newest first.
    broad_new = sorted((j for j in broad_matches if is_new(j)), key=lambda j: j.posted or "", reverse=True)
    broad_new.sort(key=lambda j: not j.bio)
    if show_all:
        broad_shown, broad_left = broad_matches, 0
        broad_count = len(broad_new)
    else:
        broad_shown, broad_left = broad_new[:broad_max], max(0, len(broad_new) - broad_max)
        broad_count = len(broad_shown)
    mark_seen(broad_shown)

    students_new = sorted((j for j in students_all if is_new(j)), key=lambda j: j.posted or "", reverse=True)
    stu_max = int(stu.get("max", 50))
    if show_all:
        students_shown, students_left = students_all, 0
        students_count = len(students_new)
    else:
        students_shown, students_left = students_new[:stu_max], max(0, len(students_new) - stu_max)
        students_count = len(students_shown)
    mark_seen(students_shown)
    save_state(state_path, state)

    shown = matches if show_all else new
    n_boards = len(boards) + len(learned) + len(discovered)
    if vc_boards:
        discovery_line = (discovery_line + " " if discovery_line else "") + \
            f"Searched {len(vc_boards)} investor portfolio boards for: {', '.join(keywords)}."
    report = render(shown, len(matches), n_boards, notes, errors, show_all, today, discovery_line,
                    broad_shown, broad_left, students_shown, students_left)
    if out:
        out.write_text(report)
    return report, len(new) + broad_count + students_count


def _safe(fn, *args):
    try:
        return fn(*args), None
    except Exception as e:
        return [], e


def learned_as_toml(state: dict) -> str:
    out = []
    for b in sorted(state.get("learned", {}).values(), key=lambda b: b["company"].lower()):
        out += [f"# learned {b['first_seen']} from {b['via']}; e.g. {b.get('example', '')}",
                "[[company]]", f"name = {json.dumps(b['company'], ensure_ascii=False)}",
                f"ats = \"{b['ats']}\"", f"slug = \"{b['slug']}\"", ""]
    return "\n".join(out) or "# nothing learned yet"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fetch boards and report matches")
    r.add_argument("--config", type=Path, default=Path("companies.toml"))
    r.add_argument("--state", type=Path, default=Path(".jobwatch/state.json"))
    r.add_argument("--out", type=Path, help="also write the Markdown report here")
    r.add_argument("--all", action="store_true", help="show all current matches, not only new ones")
    p = sub.add_parser("probe", help="detect which ATS/slug companies use")
    p.add_argument("names", nargs="+")
    d = sub.add_parser("discover", help="find job boards in Common Crawl and save them for `run`")
    d.add_argument("--file", type=Path, default=Path(".jobwatch/discovered.json"))
    d.add_argument("--crawls", type=int, default=2, help="how many of the newest crawls to search")
    d.add_argument("--max-pages", type=int, default=100, help="index pages per URL pattern per crawl")
    d.add_argument("--if-older-than", type=int, metavar="DAYS",
                   help="skip unless the last discovery is at least this many days old")
    y = sub.add_parser("import-yc", help="write relevant Y Combinator biotech companies to companies_yc.toml")
    y.add_argument("--config", type=Path, default=Path("companies.toml"))
    y.add_argument("--out", type=Path, default=Path("companies_yc.toml"))
    y.add_argument("--if-older-than", type=int, metavar="DAYS")
    l = sub.add_parser("learned", help="print learned companies as companies.toml entries")
    l.add_argument("--state", type=Path, default=Path(".jobwatch/state.json"))
    a = ap.parse_args(argv)

    if a.cmd == "learned":
        print(learned_as_toml(load_state(a.state)))
        return 0

    if a.cmd == "import-yc":
        last = generated_date(a.out)
        if a.if_older_than is not None and last and (date.today() - last).days < a.if_older_than:
            print(f"{a.out} is from {last}; skipping (--if-older-than {a.if_older_than}).")
            return 0
        try:
            data = json.loads(fetch_patient(YC_ALL) or "[]")
        except Exception as e:
            print(f"Could not download the YC dataset ({e}); keeping {a.out} as it is.")
            return 1
        cfg = tomllib.loads(a.config.read_text())
        own = cfg.get("company", [])
        n = write_yc_companies(data, a.out, [c["name"] for c in own],
                               [_domain(c["website"]) for c in own if c.get("website")])
        print(f"Wrote {n} relevant YC companies to {a.out}.")
        return 0

    if a.cmd == "probe":
        for name in a.names:
            print(f"{name}: {probe(name) or 'no public board found'}")
        return 0

    if a.cmd == "discover":
        last = load_discovered(a.file)["updated"]
        if a.if_older_than is not None and last and \
                (date.today() - date.fromisoformat(last)).days < a.if_older_than:
            print(f"Discovery is from {last}; skipping (--if-older-than {a.if_older_than}).")
            return 0
        try:
            discover(a.file, a.crawls, a.max_pages)
        except Exception as e:
            print(f"Could not reach Common Crawl ({e}); existing discovered boards are kept.")
            return 1
        return 0

    report, n_new = run(a.config, a.state, a.out, a.all)
    print(report)
    if gh_out := os.environ.get("GITHUB_OUTPUT"):
        with open(gh_out, "a") as fh:
            fh.write(f"new_count={n_new}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
