"""Offline tests: no network. Payloads mirror the public ATS response shapes."""
import json
import tomllib
from datetime import date
from pathlib import Path

import jobwatch as jw

ASHBY = {"jobs": [
    {"id": "a1", "title": "Computational Biologist", "location": "Zurich ; Boston; New York; Remote",
     "secondaryLocations": [], "isRemote": False, "isListed": True,
     "jobUrl": "https://jobs.ashbyhq.com/proxima/a1", "descriptionPlain": "Build pipelines.",
     "publishedAt": "2026-10-01T00:00:00Z"},
    {"id": "a2", "title": "Office Manager", "location": "Zurich", "isListed": True, "jobUrl": "u"},
    {"id": "a3", "title": "Hidden Bioinformatician", "location": "Remote", "isListed": False, "jobUrl": "u"},
]}
GREENHOUSE = {"jobs": [
    {"id": 11, "title": "Senior Scientist, Computational Biology", "location": {"name": "Remote"},
     "absolute_url": "https://job-boards.greenhouse.io/x/jobs/11",
     "content": "&lt;p&gt;You must be authorized to work in the United States.&lt;/p&gt;",
     "updated_at": "2026-10-10T00:00:00-04:00"},
    {"id": 12, "title": "Bioinformatics Scientist", "location": {"name": "Remote"},
     "absolute_url": "u", "content": "&lt;p&gt;Remote from anywhere in Europe (CET hours).&lt;/p&gt;"},
]}
LEVER = [
    {"id": "l1", "text": "ML Scientist, Single-Cell", "workplaceType": "remote",
     "categories": {"location": "Remote", "allLocations": ["Remote"]},
     "hostedUrl": "https://jobs.lever.co/y/l1", "descriptionPlain": "Join our team.",
     "createdAt": 1760000000000},
    {"id": "l2", "text": "Computational Biologist", "workplaceType": "on-site",
     "categories": {"location": "Paris, France"}, "hostedUrl": "u", "descriptionPlain": ""},
]


def by_id(jobs):
    return {j.id: jw.classify(j) for j in jobs}


def test_parse_ashby_skips_unlisted_and_reads_locations():
    jobs = jw.parse_ashby("Proxima", ASHBY, "ashby", "proxima")
    assert [j.id for j in jobs] == ["a1", "a2"]
    assert jobs[0].locations == ["Zurich ; Boston; New York; Remote"]
    assert jobs[0].posted == "2026-10-01"


def test_greenhouse_html_is_unescaped_and_stripped():
    j = jw.parse_greenhouse("X", GREENHOUSE, "greenhouse", "x")[0]
    assert "<" not in j.text and "authorized to work in the United States" in j.text


def test_lever_epoch_and_remote_flag():
    j = jw.parse_lever("Y", LEVER, "lever", "y")[0]
    assert j.remote_flag is True and j.posted == "2025-10-09"


def test_verdicts():
    v = {**by_id(jw.parse_ashby("P", ASHBY, "ashby", "p")), **by_id(jw.parse_greenhouse("X", GREENHOUSE, "greenhouse", "x")),
         **by_id(jw.parse_lever("Y", LEVER, "lever", "y"))}
    assert v["a1"].verdict == "europe-remote"      # Zurich + Remote (the Proxima case)
    assert v["11"].verdict == "us-remote"          # US work-authorization wording
    assert v["12"].verdict == "europe-remote"      # Europe in description
    assert v["l1"].verdict == "remote-unclear"     # remote, no region
    assert v["l2"].verdict == "europe-onsite"
    assert any("zurich" in r for r in v["a1"].reasons)


def test_us_token_in_description_is_not_misread():
    # "us" as a pronoun in prose must not mark a role US-only
    j = jw.Job("C", "ashby", "1", "t", ["Remote"], None, "u", text="Join us and help us build.")
    assert jw.classify(j).verdict == "remote-unclear"


def test_title_filter():
    inc, exc = ["computational biolog", "bioinformatic"], [r"\bintern\b"]
    assert jw.title_matches("Senior Computational Biologist", inc, exc)
    assert not jw.title_matches("Computational Biology Intern", inc, exc)
    assert not jw.title_matches("Office Manager", inc, exc)


def fake_fetch(boards: dict):
    """boards: {(ats, slug): payload}; payloads are dicts/lists (served as JSON) or XML strings."""
    def fetch(url):
        for (ats, slug), payload in boards.items():
            if url.split("&offset=")[0] == jw.ENDPOINTS[ats].format(slug=slug, offset=0).split("&offset=")[0]:
                if ats == "smartrecruiters":  # serve one page of 2 at the requested offset
                    off = int(url.split("offset=")[1])
                    items = payload["content"]
                    payload = {"totalFound": len(items), "content": items[off:off + 2]}
                return payload if isinstance(payload, str) else json.dumps(payload)
        return None
    return fetch


def test_probe_prefers_board_with_jobs():
    fetch = fake_fetch({("lever", "acmebio"): [], ("greenhouse", "acme-bio"): GREENHOUSE})
    found = jw.probe("Acme Bio", fetch)
    assert (found["ats"], found["slug"], found["confident"]) == ("greenhouse", "acme-bio", True)


def test_probe_none_when_nothing_found():
    assert jw.probe("Nobody", fake_fetch({})) is None


CONFIG = """
[filters]
include_titles = ["computational biolog", "bioinformatic", "scientist"]
verdicts = ["europe-remote", "remote-unclear"]
[[company]]
name = "Proxima"
ats = "ashby"
slug = "proxima"
[[company]]
name = "Acme"
[[company]]
name = "Ghost"
"""


def test_run_reports_only_new_on_second_pass(tmp_path: Path):
    cfg = tmp_path / "companies.toml"; cfg.write_text(CONFIG)
    state = tmp_path / "state.json"
    fetch = fake_fetch({("ashby", "proxima"): ASHBY, ("greenhouse", "acme"): GREENHOUSE})

    report, n_new = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 19))
    assert n_new == 2                                  # a1 (Proxima) + 12 (Acme Europe); 11 is US-only
    assert "**Acme** → greenhouse/acme" in report and "No public job board found for 1 company" in report and "Ghost" in report

    report2, n_new2 = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 20))
    assert n_new2 == 0
    _, n_all = jw.run(cfg, state, None, True, fetch, today=date(2026, 10, 20))
    assert n_all == 0 and "2 matching right now" in jw.run(cfg, state, None, True, fetch)[0]


def test_one_broken_board_does_not_stop_the_run(tmp_path: Path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[filters]\ninclude_titles=["computational"]\n'
                   '[[company]]\nname="P"\nats="ashby"\nslug="proxima"\n'
                   '[[company]]\nname="Gone"\nats="lever"\nslug="gone"\n')
    report, n = jw.run(cfg, tmp_path / "s.json", None, False,
                       fake_fetch({("ashby", "proxima"): ASHBY}), today=date(2026, 10, 19))
    assert n == 1 and "## Errors" in report and "Gone" in report


def test_network_failure_is_not_cached_as_no_board(tmp_path: Path):
    def down(url):
        raise OSError("Name or service not known")
    cfg = tmp_path / "c.toml"
    cfg.write_text('[filters]\ninclude_titles=["x"]\n[[company]]\nname="Acme"\n')
    state = tmp_path / "s.json"
    report, _ = jw.run(cfg, state, None, False, down, today=date(2026, 10, 19))
    assert "will retry next run" in report
    assert "Acme" not in jw.load_state(state)["resolved"]


# ----------------------------------------------------------------- newer ATSes

SMARTRECRUITERS = {"content": [
    {"id": "s1", "name": "Senior Computational Biologist", "releasedDate": "2026-10-02T10:00:00.000Z",
     "location": {"city": "Basel", "country": "ch", "remote": True}},
    {"id": "s2", "name": "Bioinformatician", "location": {"city": "Boston", "region": "MA", "country": "us"}},
    {"id": "s3", "name": "Data Scientist", "location": {"fullLocation": "Remote, Poland", "remote": True}},
]}
WORKABLE = {"name": "Acme", "jobs": [
    {"title": "Computational Biologist", "shortcode": "W1", "telecommuting": True,
     "city": "Amsterdam", "country": "Netherlands", "url": "https://apply.workable.com/j/W1",
     "description": "<p>Work with us.</p>", "published_on": "2026-10-05"},
    {"title": "Computational Biologist", "shortcode": "W1", "telecommuting": True,
     "city": "Berlin", "country": "Germany", "url": "https://apply.workable.com/j/W1"},
]}
RECRUITEE = {"offers": [
    {"id": 7, "title": "Single-cell Scientist", "location": "Prague, Czechia", "city": "Prague",
     "country_code": "CZ", "remote": True, "careers_url": "https://acme.recruitee.com/o/sc",
     "description": "<p>Hybrid or remote.</p>", "requirements": "", "published_at": "2026-10-07 09:00:00 UTC"},
]}
PERSONIO = """<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>42</id><office>Munich</office>
    <additionalOffices><office>Remote</office></additionalOffices>
    <name>Computational Biologist (m/f/d)</name>
    <jobDescriptions><jobDescription><name>About</name>
      <value><![CDATA[<p>Remote within Germany or the EU is possible.</p>]]></value>
    </jobDescription></jobDescriptions>
    <createdAt>2026-10-08T08:00:00+00:00</createdAt>
  </position>
</workzag-jobs>"""


def test_smartrecruiters_pages_and_country_codes():
    data = jw.load_board("smartrecruiters", "acme", fake_fetch({("smartrecruiters", "acme"): SMARTRECRUITERS}))
    jobs = jw.parse_smartrecruiters("Acme", data, "smartrecruiters", "acme")
    assert [j.id for j in jobs] == ["s1", "s2", "s3"]          # 2 pages fetched
    v = by_id(jobs)
    assert jobs[0].url == "https://jobs.smartrecruiters.com/acme/s1"
    assert v["s1"].verdict == "europe-remote"                 # 'ch' -> Switzerland
    assert v["s3"].verdict == "europe-remote"
    assert v["s2"].verdict == "other"


def test_workable_merges_duplicate_locations():
    jobs = jw.parse_workable("Acme", WORKABLE, "workable", "acme")
    assert len(jobs) == 1 and jobs[0].locations == ["Amsterdam, Netherlands", "Berlin, Germany"]
    assert jw.classify(jobs[0]).verdict == "europe-remote"


def test_recruitee():
    j = jw.parse_recruitee("Acme", RECRUITEE, "recruitee", "acme")[0]
    assert j.remote_flag and j.url.endswith("/o/sc") and "Prague, Czechia" in j.locations
    assert jw.classify(j).verdict == "europe-remote"


def test_personio_xml():
    root = jw.decode("personio", PERSONIO)
    j = jw.parse_personio("Acme", root, "personio", "acme")[0]
    assert (j.id, j.title, j.url) == ("42", "Computational Biologist (m/f/d)", "https://acme.jobs.personio.de/job/42")
    assert j.locations == ["Munich", "Remote"] and "Remote within Germany" in j.text
    assert jw.classify(j).verdict == "europe-remote"


def test_html_page_for_unknown_subdomain_is_not_a_board():
    assert jw.decode("personio", "<!doctype html><html><body>Not found</body></html>") is None
    assert jw.decode("recruitee", "<html>oops</html>") is None


def test_probe_ignores_empty_smartrecruiters_and_finds_personio():
    fetch = fake_fetch({("smartrecruiters", "acme"): {"content": []}, ("personio", "acme"): PERSONIO})
    found = jw.probe("Acme", fetch)
    assert (found["ats"], found["slug"], found["n_jobs"]) == ("personio", "acme", 1)


def test_probe_empty_smartrecruiters_alone_is_not_a_match():
    assert jw.probe("Nobody", fake_fetch({("smartrecruiters", "nobody"): {"content": []}})) is None


# ----------------------------------------------------------------- discovery

def test_extract_slug():
    rx = dict((a + p, r) for a, p, r in jw.DISCOVERY_SOURCES)
    gh = rx["greenhouseboards.greenhouse.io/*"]
    assert jw.extract_slug(rx["ashbyjobs.ashbyhq.com/*"], "https://jobs.ashbyhq.com/proxima/2443af21") == "proxima"
    assert jw.extract_slug(gh, "https://boards.greenhouse.io/embed/job_board?for=acmebio&b=x") == "acmebio"
    assert jw.extract_slug(gh, "https://boards.greenhouse.io/acme/jobs/123") == "acme"
    assert jw.extract_slug(gh, "https://boards.greenhouse.io/embed/job_app?token=1") is None
    assert jw.extract_slug(rx["recruitee*.recruitee.com"], "https://acme.recruitee.com/o/sc") == "acme"
    assert jw.extract_slug(rx["recruitee*.recruitee.com"], "https://www.recruitee.com/pricing") is None
    assert jw.extract_slug(rx["workableapply.workable.com/*"], "https://apply.workable.com/j/ABC123") is None


def cc_fetch(captures: dict):
    """Fake Common Crawl: captures = {pattern: [urls]} served as one page in one crawl."""
    import urllib.parse as up
    api = "https://index.commoncrawl.org/CC-MAIN-2026-40-index"

    def fetch(url):
        if url == jw.CC_COLLINFO:
            return json.dumps([{"id": "CC-MAIN-2026-40", "cdx-api": api}])
        q = up.parse_qs(up.urlparse(url).query)
        urls = captures.get(q["url"][0])
        if not urls:
            return None                                # CDX answers 404 when nothing matches
        if "showNumPages" in q:
            return json.dumps({"pages": 1, "pageSize": 5, "blocks": 1})
        return "\n".join(json.dumps({"url": u}) for u in urls)
    return fetch


def test_discover_merges_and_dedupes(tmp_path: Path):
    path = tmp_path / "discovered.json"
    fetch = cc_fetch({
        "jobs.ashbyhq.com/*": ["https://jobs.ashbyhq.com/proxima/1", "https://jobs.ashbyhq.com/Proxima/2",
                               "https://jobs.ashbyhq.com/other"],
        "*.jobs.personio.de": ["https://acme.jobs.personio.de/job/42"],
    })
    store = jw.discover(path, fetch=fetch, today=date(2026, 10, 1), log=lambda *_: None)
    assert set(store["boards"]) == {"ashby:proxima", "ashby:other", "personio:acme"}
    # A second discovery keeps first_seen and existing entries
    store = jw.discover(path, fetch=fetch, today=date(2026, 11, 1), log=lambda *_: None)
    assert store["boards"]["ashby:other"]["first_seen"] == "2026-10-01" and store["updated"] == "2026-11-01"


DISC_CONFIG = """
[filters]
include_titles = ["computational biolog", "bioinformatic", "scientist"]
[discovery]
enabled = true
file = "{file}"
[[company]]
name = "Proxima"
ats = "ashby"
slug = "proxima"
"""


def test_run_uses_discovered_boards_and_prunes_dead_ones(tmp_path: Path):
    dfile = tmp_path / "discovered.json"
    dfile.write_text(json.dumps({"updated": "2026-10-01", "boards": {
        "ashby:proxima": {"ats": "ashby", "slug": "proxima"},          # duplicate of named company
        "greenhouse:acme": {"ats": "greenhouse", "slug": "acme"},
        "lever:gone": {"ats": "lever", "slug": "gone"},
    }}))
    cfg = tmp_path / "c.toml"
    cfg.write_text(DISC_CONFIG.format(file=dfile.as_posix()))
    fetch = fake_fetch({("ashby", "proxima"): ASHBY, ("greenhouse", "acme"): GREENHOUSE})

    report, n_new = jw.run(cfg, tmp_path / "s.json", None, False, fetch, today=date(2026, 10, 19))
    assert n_new == 2                                   # Proxima a1 once (not twice) + acme's Europe role
    assert "2 discovered boards" in report and "1 no longer exist" in report
    assert "## Errors" not in report                    # discovered failures are summarised, not listed
    boards = json.loads(dfile.read_text())["boards"]
    assert boards["lever:gone"]["dead"] == "2026-10-19" and "dead" not in boards["greenhouse:acme"]


def test_no_pruning_during_an_outage(tmp_path: Path):
    dfile = tmp_path / "discovered.json"
    dfile.write_text(json.dumps({"boards": {f"lever:b{i}": {"ats": "lever", "slug": f"b{i}"} for i in range(25)}}))
    cfg = tmp_path / "c.toml"
    cfg.write_text(DISC_CONFIG.format(file=dfile.as_posix()).split("[[company]]")[0])
    report, _ = jw.run(cfg, tmp_path / "s.json", None, False, fake_fetch({}), today=date(2026, 10, 19))
    assert "nothing was pruned" in report
    assert not any("dead" in b for b in json.loads(dfile.read_text())["boards"].values())


def test_probe_tries_discovered_boards_first():
    calls = []
    inner = fake_fetch({("workable", "acme-bio"): WORKABLE})

    def fetch(url):
        calls.append(url)
        return inner(url)
    found = jw.probe("Acme Bio", fetch, known=frozenset({("workable", "acme-bio")}))
    assert (found["ats"], found["slug"]) == ("workable", "acme-bio") and len(calls) == 1


def test_slug_candidates_flag_shortened_names():
    c = dict(jw.slug_candidates("Character Biosciences"))
    assert c["characterbiosciences"] is True and c["character"] is False
    assert "new" not in dict(jw.slug_candidates("New Limit"))      # no bare first-word guesses
    assert dict(jw.slug_candidates("Monte Rosa Therapeutics"))["monterosa"] is False


def test_shortened_name_match_is_flagged_in_report(tmp_path: Path):
    """The Character.AI trap: 'Character Biosciences' must not silently match ashby/character."""
    cfg = tmp_path / "c.toml"
    cfg.write_text('[filters]\ninclude_titles=["x"]\n[[company]]\nname="Character Biosciences"\n')
    report, _ = jw.run(cfg, tmp_path / "s.json", None, False,
                       fake_fetch({("ashby", "character"): ASHBY}), today=date(2026, 10, 19))
    assert "shortened name 'character'" in report


def test_full_name_beats_shortened_name():
    fetch = fake_fetch({("ashby", "character"): ASHBY, ("greenhouse", "characterbio"): GREENHOUSE})
    found = jw.probe("Character Biosciences", fetch)
    assert (found["ats"], found["slug"], found["exact"]) == ("greenhouse", "characterbio", True)


# ----------------------------------------------------------------- website scanning

def test_scan_html_finds_board_links():
    page = """
      <a href="https://jobs.ashbyhq.com/acmebio">Jobs</a>
      <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme-gh"></script>
      <div data-x='{"url":"https:\\/\\/job-boards.eu.greenhouse.io\\/acmeeu\\/jobs\\/1"}'></div>
      <a href="https://acme.recruitee.com/o/sc">x</a> <a href="https://apply.workable.com/acme-w/">y</a>
      <a href="https://jobs.ashbyhq.com/acmebio/123">again</a>
    """
    found = jw.scan_html(page)
    assert found[0] == ("ashby", "acmebio")                       # most frequent first
    assert {("greenhouse", "acme-gh"), ("greenhouse_eu", "acmeeu"), ("recruitee", "acme"),
            ("workable", "acme-w")} <= set(found)
    assert not any(slug in ("embed", "eu", "www") for _, slug in found)


def site_fetch(pages: dict, boards: dict):
    """Serve website HTML from `pages` and board JSON/XML via fake_fetch(boards)."""
    inner = fake_fetch(boards)

    def fetch(url):
        return pages[url] if url in pages else inner(url)
    return fetch


def test_website_scan_follows_careers_link():
    pages = {
        "https://acme.bio": '<a href="/about">About</a> <a href="/company/careers">Careers</a>',
        "https://acme.bio/company/careers": '<iframe src="https://jobs.lever.co/acme-therapeutics"></iframe>',
    }
    assert jw.find_boards_on_website("acme.bio", site_fetch(pages, {})) == [("lever", "acme-therapeutics")]


def test_website_beats_guessing_for_character_biosciences(tmp_path: Path):
    """With a website, the right board is found directly; Character.AI's board is never considered."""
    pages = {"https://characterbio.com": '<a href="https://job-boards.greenhouse.io/characterbio">Careers</a>'}
    fetch = site_fetch(pages, {("greenhouse", "characterbio"): GREENHOUSE, ("ashby", "character"): ASHBY})
    cfg = tmp_path / "c.toml"
    cfg.write_text('[filters]\ninclude_titles=["x"]\n'
                   '[[company]]\nname="Character Biosciences"\nwebsite="https://characterbio.com"\n')
    report, _ = jw.run(cfg, tmp_path / "s.json", None, False, fetch, today=date(2026, 10, 19))
    assert "via company websites: Character Biosciences → greenhouse/characterbio" in report
    assert jw.load_state(tmp_path / "s.json")["resolved"]["Character Biosciences"]["slug"] == "characterbio"


def test_website_without_board_link_falls_back_to_guessing(tmp_path: Path):
    pages = {"https://acme.example": "<p>We are hiring! Email us.</p>"}
    fetch = site_fetch(pages, {("ashby", "acme"): ASHBY})
    cfg = tmp_path / "c.toml"
    cfg.write_text('[filters]\ninclude_titles=["x"]\n[[company]]\nname="Acme"\nwebsite="acme.example"\n')
    report, _ = jw.run(cfg, tmp_path / "s.json", None, False, fetch, today=date(2026, 10, 19))
    assert "**Acme** → ashby/acme (its website had no job-board link)" in report


def test_vanished_board_is_redetected(tmp_path: Path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[filters]\ninclude_titles=["x"]\n[[company]]\nname="Acme"\n')
    state = tmp_path / "s.json"
    jw.run(cfg, state, None, False, fake_fetch({("ashby", "acme"): ASHBY}), today=date(2026, 10, 19))
    report, _ = jw.run(cfg, state, None, False, fake_fetch({}), today=date(2026, 10, 20))
    assert "will re-detect next run" in report and "Acme" not in jw.load_state(state)["resolved"]


def test_eu_greenhouse_board_parses():
    jobs = jw.fetch_board("Acme", "greenhouse_eu", "acmeeu", fake_fetch({("greenhouse_eu", "acmeeu"): GREENHOUSE}))
    assert len(jobs) == 2 and jobs[0].ats == "greenhouse_eu"


# ----------------------------------------------------------------- investor (Getro) boards

def getro_html(items, layout="standard"):
    if layout == "standard":
        data = {"props": {"pageProps": {"network": {"id": 1}, "initialState": {"jobs": {"found": items, "total": len(items)}}}}}
    else:  # a changed layout: jobs nested somewhere else
        data = {"props": {"pageProps": {"somethingNew": {"results": {"list": items}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


GETRO_ITEMS = [
    {"title": "Senior Computational Biologist", "url": "https://jobs.ashbyhq.com/novacell/abc-123?utm_source=getro.com",
     "organization": {"name": "NovaCell", "slug": "novacell"}, "locations": ["Remote - Europe"],
     "workMode": "remote", "createdAt": 1760000000},
    {"title": "Office Manager", "url": "/companies/novacell/jobs/1", "organization": {"name": "NovaCell"},
     "locations": ["Boston"]},
]


def test_parse_getro_page_standard_and_changed_layout():
    for layout in ("standard", "changed"):
        jobs = jw.parse_getro_page(getro_html(GETRO_ITEMS, layout), "lux", "https://jobs.luxcapital.com")
        assert [j.title for j in jobs] == ["Senior Computational Biologist", "Office Manager"]
        assert jobs[0].company == "NovaCell" and jobs[0].remote_flag is True and jobs[0].posted == "2025-10-09"
        assert jobs[1].url == "https://jobs.luxcapital.com/companies/novacell/jobs/1"   # made absolute
    assert jw.parse_getro_page("<html>not getro</html>", "x", "https://x") is None


def test_fetch_getro_board_searches_keywords_and_pages():
    page1 = [dict(GETRO_ITEMS[0], url=f"https://jobs.ashbyhq.com/novacell/{i}") for i in range(20)]
    page2 = [dict(GETRO_ITEMS[0], url="https://jobs.ashbyhq.com/novacell/last")]
    pages = {
        "https://jobs.luxcapital.com/jobs?q=computational+biology": getro_html(page1),
        "https://jobs.luxcapital.com/jobs?q=computational+biology&page=2": getro_html(page2),
        "https://jobs.luxcapital.com/jobs?q=bioinformatics": getro_html(page2),
    }
    jobs = jw.fetch_getro_board({"id": "lux", "url": "https://jobs.luxcapital.com"},
                                ["computational biology", "bioinformatics"], pages.get)
    assert len(jobs) == 21                                     # 20 + 1, duplicates merged


def test_non_getro_investor_board_is_an_error():
    import pytest
    with pytest.raises(LookupError):
        jw.fetch_getro_board({"id": "x", "url": "https://jobs.example.vc"}, ["x"], lambda u: "<html></html>")


def test_board_from_job_url():
    assert jw.board_from_job_url("https://jobs.ashbyhq.com/novacell/abc?utm=x") == ("ashby", "novacell")
    assert jw.board_from_job_url("https://job-boards.greenhouse.io/acme/jobs/9") == ("greenhouse", "acme")
    assert jw.board_from_job_url("https://jobs.luxcapital.com/companies/x/jobs/1") is None


VC_CONFIG = """
[filters]
include_titles = ["computational biolog"]
[vc_boards]
keywords = ["computational biology"]
[[vc_board]]
id = "lux"
name = "Lux Capital"
url = "https://jobs.luxcapital.com"
"""

NOVACELL_ASHBY = {"jobs": [{"id": "abc-123", "title": "Senior Computational Biologist", "location": "Remote - Europe",
                            "isRemote": True, "jobUrl": "https://jobs.ashbyhq.com/novacell/abc-123"}]}


def test_investor_board_job_is_reported_and_its_company_learned(tmp_path: Path):
    cfg = tmp_path / "c.toml"
    cfg.write_text(VC_CONFIG)
    state = tmp_path / "s.json"
    pages = {"https://jobs.luxcapital.com/jobs?q=computational+biology": getro_html(GETRO_ITEMS)}
    fetch = site_fetch(pages, {("ashby", "novacell"): NOVACELL_ASHBY})

    report, n_new = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 19))
    assert n_new == 1 and "Senior Computational Biologist" in report and "Remote, with a Europe signal" in report
    assert "Learned 1 new relevant company" in report and "**NovaCell** → ashby/novacell" in report
    assert jw.load_state(state)["learned"]["ashby:novacell"]["company"] == "NovaCell"

    # Next run: the learned board is checked directly and the same role is not reported twice.
    report2, n_new2 = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 20))
    assert n_new2 == 0 and "Learned" not in report2
    assert "2 matching" not in report2                       # one role, even though two sources show it


def test_learned_board_that_vanishes_is_dropped(tmp_path: Path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('[filters]\ninclude_titles=["x"]\n[vc_boards]\nenabled = false\n')
    state = tmp_path / "s.json"
    jw.save_state(state, {"seen": {}, "resolved": {}, "learned": {
        "ashby:gone": {"ats": "ashby", "slug": "gone", "company": "Gone Bio", "via": "x", "first_seen": "2026-10-01"}}})
    report, _ = jw.run(cfg, state, None, False, fake_fetch({}), today=date(2026, 10, 19))
    assert "no longer exists; dropped it" in report and jw.load_state(state)["learned"] == {}


def test_discovered_board_with_relevant_role_is_learned(tmp_path: Path):
    dfile = tmp_path / "d.json"
    dfile.write_text(json.dumps({"boards": {"greenhouse:acme": {"ats": "greenhouse", "slug": "acme"}}}))
    cfg = tmp_path / "c.toml"
    cfg.write_text(f'[filters]\ninclude_titles=["bioinformatic"]\n[discovery]\nenabled=true\nfile="{dfile.as_posix()}"\n')
    state = tmp_path / "s.json"
    report, _ = jw.run(cfg, state, None, False, fake_fetch({("greenhouse", "acme"): GREENHOUSE}), today=date(2026, 10, 19))
    assert "greenhouse:acme" in jw.load_state(state)["learned"]
    learned = tomllib.loads(jw.learned_as_toml(jw.load_state(state)))["company"]
    assert learned == [{"name": "acme", "ats": "greenhouse", "slug": "acme"}]


# ----------------------------------------------------------------- YC import and extra company files

YC_SAMPLE = [
    {"name": "Helix Fold", "website": "https://helixfold.bio", "status": "Active", "tags": ["AI-powered Drug Discovery"],
     "batch": "W25", "one_liner": "Protein design"},
    {"name": "Payroll Co", "website": "https://payroll.co", "status": "Active", "tags": ["Fintech"]},
    {"name": "Old Bio", "website": "https://oldbio.com", "status": "Inactive", "tags": ["Biotech"]},
    {"name": "Dx Labs", "website": "https://www.dxlabs.com", "status": "Active", "tags": [],
     "subindustry": "Healthcare -> Diagnostics", "batch": "S24", "one_liner": 'Blood "tests"'},
    {"name": "Benchling", "website": "https://benchling.com", "status": "Active", "tags": ["Biotech"]},
]


def test_yc_import_filters_and_skips_companies_already_listed(tmp_path: Path):
    out = tmp_path / "companies_yc.toml"
    n = jw.write_yc_companies(YC_SAMPLE, out, exclude_domains={"benchling.com"}, today=date(2026, 10, 20))
    parsed = tomllib.loads(out.read_text())["company"]
    assert n == 2 and [c["name"] for c in parsed] == ["Dx Labs", "Helix Fold"]
    assert jw.generated_date(out) == date(2026, 10, 20)


def test_load_companies_merges_extra_files_without_duplicates(tmp_path: Path):
    (tmp_path / "extra.toml").write_text('[[company]]\nname = "Seqera"\nwebsite = "https://seqera.io"\n'
                                         '[[company]]\nname = "Seqera Labs"\nwebsite = "https://www.seqera.io"\n'
                                         '[[company]]\nname = "Helix Fold"\nwebsite = "https://helixfold.bio"\n')
    cfg = {"company": [{"name": "Seqera", "website": "https://seqera.io"}],
           "sources": {"company_files": ["extra.toml", "missing.toml"]}}
    assert [c["name"] for c in jw.load_companies(cfg, tmp_path)] == ["Seqera", "Helix Fold"]


BROAD_JOBS = {"jobs": [
    {"id": 20 + i, "title": f"Data Scientist {n}", "location": {"name": "Remote"},
     "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{20 + i}",
     "content": "&lt;p&gt;Remote from anywhere in Europe (CET hours).&lt;/p&gt;",
     "updated_at": f"2026-10-1{i}T00:00:00-04:00"}
    for i, n in enumerate("ABC", start=1)
] + [GREENHOUSE["jobs"][1]]}


def broad_config(extra: str = "") -> str:
    return f"""
[filters]
include_titles = ["computational biolog"]
broad_titles = ["scientist"]
{extra}
verdicts = ["europe-remote", "remote-unclear"]
[[company]]
name = "Acme"
ats = "greenhouse"
slug = "acme"
"""


def test_broad_titles_get_their_own_section_and_are_reported_once(tmp_path: Path):
    cfg = tmp_path / "companies.toml"; cfg.write_text(broad_config())
    state = tmp_path / "state.json"
    fetch = fake_fetch({("greenhouse", "acme"): GREENHOUSE})

    report, n_new = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 19))
    head, _, broad = report.partition("## Broader matches")
    assert n_new == 1 and "Bioinformatics Scientist" in broad and "Bioinformatics Scientist" not in head
    assert "### Remote, with a Europe signal" in broad
    assert "+ 1 broader matches" in report
    assert "acme" not in json.loads(state.read_text())["learned"]    # broad matches teach nothing
    assert jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 20))[1] == 0


def test_without_broad_titles_nothing_changes(tmp_path: Path):
    cfg = tmp_path / "companies.toml"; cfg.write_text(broad_config().replace('broad_titles = ["scientist"]', ""))
    report, n_new = jw.run(cfg, tmp_path / "s.json", None, False, fake_fetch({("greenhouse", "acme"): GREENHOUSE}),
                           today=date(2026, 10, 19))
    assert n_new == 0 and "Broader matches" not in report


def test_broad_matches_are_capped_newest_first_and_the_rest_wait(tmp_path: Path):
    cfg = tmp_path / "companies.toml"; cfg.write_text(broad_config("broad_max = 2"))
    state = tmp_path / "state.json"
    fetch = fake_fetch({("greenhouse", "acme"): BROAD_JOBS})

    report, n1 = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 19))
    assert n1 == 2 and "Data Scientist C" in report and "Data Scientist B" in report
    assert "Data Scientist A" not in report and "2 more broader matches" in report
    report2, n2 = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 20))
    assert n2 == 2 and "Data Scientist A" in report2 and "Bioinformatics Scientist" in report2 and "more broader" not in report2
    assert jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 21))[1] == 0


def test_broad_matches_with_a_bio_connection_are_listed_first(tmp_path: Path):
    jobs = {"jobs": [
        {"id": 31, "title": "Data Scientist", "location": {"name": "Remote"},
         "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/31",
         "content": "&lt;p&gt;Remote in Europe. We analyse genomics data for drug discovery.&lt;/p&gt;",
         "updated_at": "2026-10-10T00:00:00-04:00"},
        {"id": 32, "title": "Data Scientist, Pricing", "location": {"name": "Remote"},
         "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/32",
         "content": "&lt;p&gt;Remote in Europe. Pricing for a payments app; one genomics mention.&lt;/p&gt;",
         "updated_at": "2026-10-12T00:00:00-04:00"},
        {"id": 33, "title": "Biostatistics Scientist", "location": {"name": "Remote"},
         "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/33",
         "content": "&lt;p&gt;Remote in Europe.&lt;/p&gt;", "updated_at": "2026-10-11T00:00:00-04:00"},
    ]}
    fetch = fake_fetch({("greenhouse", "acme"): jobs})
    hints = 'bio_hints = ["biostat", "genom", "drug discovery"]\n'
    state = tmp_path / "state.json"

    # Cap of 1: the slot goes to a bio-connected role, although the pricing job is the newest.
    cfg = tmp_path / "companies.toml"; cfg.write_text(broad_config(hints + "broad_max = 1"))
    report, n = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 19))
    assert n == 1 and "Pricing" not in report and "(1 with a possible bio connection)" in report
    assert "### Possible bio connection" in report and "Other broader matches" not in report

    # No cap: the rest follow. A single description hit ("genomics" once) is not a bio connection.
    cfg.write_text(broad_config(hints + "broad_max = 10"))
    report2, n2 = jw.run(cfg, state, None, False, fetch, today=date(2026, 10, 20))
    assert n2 == 2
    bio_part, _, other_part = report2.partition("### Other broader matches")
    assert "### Possible bio connection" in bio_part and "Pricing" not in bio_part and "Pricing" in other_part
    assert "bio connection:" not in other_part
