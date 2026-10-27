# jobwatch

Polls the public job-board APIs of Ashby, Greenhouse (US and EU), Lever, SmartRecruiters, Workable, Recruitee and Personio (free, no keys), keeps roles whose titles match your keywords, and sorts them by how likely they are to be open to remote work from Europe. New roles since the last run land in a Markdown report, and on GitHub as an issue (which emails you).

## Where the companies come from

There's no "search all companies" endpoint on any of these systems; each company has its own board. jobwatch builds its list from five sources, and the last two keep it growing on their own:

1. **Named companies** in `companies.toml`: about 90 hand-picked AI-bio, single-cell, genomics and bioinformatics companies, each with its website. Always checked; any problem with them is listed in the report.
2. **Y Combinator companies** in `companies_yc.toml`: about 220 active YC biotech, drug discovery, genomics and diagnostics companies with websites, from the open [yc-oss](https://github.com/yc-oss/api) copy of YC's directory. `python jobwatch.py import-yc` regenerates it (the workflow does this monthly); companies already in `companies.toml` are skipped. Add more lists the same way under `[sources] company_files`.
3. **Investor portfolio boards** (`[[vc_board]]` in `companies.toml`): job boards that venture firms run for all their portfolio companies. jobwatch searches each one for your keywords (`[vc_boards] keywords`), so the board does the filtering. Only boards built on Getro work (the footer says "Powered by Getro"); Consider-based ones (a16z, Sequoia, Greylock and others) currently reject automated requests.
4. **Learned companies**: whenever a relevant role turns up on an investor board or a discovered board, jobwatch notes which job board it came from and checks that company directly on every later run. `python jobwatch.py learned` prints them as `companies.toml` entries if you want to keep them permanently.
5. **Discovered boards**: `python jobwatch.py discover` searches the two newest Common Crawl web archives for every captured job-board address and saves them to `.jobwatch/discovered.json` (thousands, mostly general tech; the title filter keeps only relevant roles). Vanished boards are pruned, but never during an apparent network outage. SmartRecruiters isn't discovered this way because it answers for any name.

To find a named or YC company's job board, jobwatch reads its website (homepage, then up to three careers pages, then `/careers` and `/jobs`) and picks out links to a job board; only if there are none does it guess from the name. Results are cached, and a cached board that later disappears is detected again. The same role found through several sources is reported once.

Coverage is broad but not complete: companies on Workday or their own careers pages aren't reachable, and Common Crawl doesn't capture every board.

## Run locally

```bash
python jobwatch.py discover             # once: find boards (can take 15-60 min)
python jobwatch.py run --all            # first run: everything matching now
python jobwatch.py run                  # later runs: only new roles
python jobwatch.py probe "Owkin" "Cradle"   # find a company's ATS + slug
python jobwatch.py import-yc            # refresh companies_yc.toml
python jobwatch.py learned              # learned companies, as companies.toml entries
python -m pytest                        # offline tests
```

The first `run` is slower: it finds boards for ~310 companies (cached afterwards, eight at a time) and checks every discovered board. To skip discovered boards, set `enabled = false` under `[discovery]`.

Python 3.11+, no dependencies (pytest only for tests).

## Run daily for free on GitHub

1. Push this folder to a **private** GitHub repo.
2. Actions tab → enable workflows → run "jobwatch" once by hand.
3. It then runs daily at 06:00 UTC: refreshes discovered boards and the YC list if they're over 30 days old, searches the investor boards, checks every board, opens an issue when new roles appear, and commits `.jobwatch/` so it remembers what you've seen. The first run includes discovery, so expect it to take up to an hour or two.

GitHub may pause scheduled workflows in repos without activity for a long stretch; the daily state commit normally keeps it alive, and the Actions tab lets you re-enable it.

## Adding companies

Edit `companies.toml`. The slug is in the careers URL:

| `ats` | Careers URL looks like |
|---|---|
| `ashby` | `jobs.ashbyhq.com/<slug>` |
| `greenhouse` | `job-boards.greenhouse.io/<slug>` |
| `greenhouse_eu` | `job-boards.eu.greenhouse.io/<slug>` |
| `lever` / `lever_eu` | `jobs.lever.co/<slug>` / `jobs.eu.lever.co/<slug>` |
| `smartrecruiters` | `jobs.smartrecruiters.com/<slug>` or `careers.smartrecruiters.com/<slug>` |
| `workable` | `apply.workable.com/<slug>` |
| `recruitee` | `<slug>.recruitee.com` |
| `personio` / `personio_com` | `<slug>.jobs.personio.de` / `<slug>.jobs.personio.com` |

Easiest is to add `website = "https://..."` and let the first run find the board; with only a name it has to guess. Either way it's reported under "Board detection" with sample titles: check they're really that company, then pin `ats` and `slug`. Careers pages that load jobs purely with JavaScript may not contain the link, in which case it falls back to guessing. Workday, iCIMS, Taleo, Teamtailor and custom careers pages aren't covered.

Quirks handled: SmartRecruiters' list API has no descriptions (location and remote flag only) and answers even for unknown companies, so the auto-detect never trusts an empty SmartRecruiters board; Workable's widget repeats multi-location jobs, which are merged; Personio serves XML.

## How roles are sorted

- **Remote, with a Europe signal**: remote in the ATS flag, location, or description, plus a European place or phrase such as "EMEA", "Europe-based" or "CET".
- **Remote, region unclear**: remote with nothing about region. Worth one email asking whether they can hire in Czechia.
- **European office**: not flagged remote (off by default; add `europe-onsite` to `verdicts`).
- **US-only remote**: wording like "authorized to work in the United States" (off by default).

Each role lists the matched phrases under "why", so you can see what drove the call. It's keyword matching, so expect the occasional misread.
