# Appart-Scout

Finds flats between Zürich and Basel for two people who commute in opposite
directions, ranks them against criteria you set in a web UI (including an LLM
look at the listing photos), and emails a digest every few days. Runs
unattended on a Raspberry Pi.

The thing it does that no portal does: it scores the **pair** of commutes. A
flat that is 15 minutes for one of you and 70 for the other loses to a 40/40
flat, because `commute_fairness` is a weighted term in the score.

---

## Portal status — read this first

Only some Swiss portals are reachable by software. Measured while building this:

| Portal | How it's fetched | Status |
|---|---|---|
| **Flatfox** | public JSON API | **Works.** Verified end-to-end, 3 700+ listings per sync. |
| **ImmoScout24** | headed Chromium + page hydration state | **Adapter verified against real payloads**, but the site began serving captchas to the development IP. See below. |
| **Homegate** | headed Chromium | **Blocked** every attempt. Largely redundant — see below. |
| **Newhome** | headed Chromium + generic parser | Loads in a headed browser; result URL not pinned down. |
| **Comparis** | headed Chromium + generic parser | Passes the anti-bot layer; result URL not pinned down. |

Three things worth knowing:

- **Flatfox alone is a real service.** It covers Zürich and Aargau well, and it
  is the only source that needs no browser at all.
- **Homegate matters less than it looks.** Homegate and ImmoScout24 are both SMG
  properties; ImmoScout24 listings carry
  `platforms: ["homegate", "immoscout24", …]`, i.e. the same inventory is
  syndicated across both, and `dedup.py` would merge them anyway.
- **Blocking is per-IP and reputation-based.** ImmoScout24 loaded fine for hours
  (the test fixtures are real captures from it) and only started refusing after
  sustained automated traffic from one address. Your Pi on a Swiss residential
  connection is a different proposition. Run `scout probe immoscout` there to
  find out — it dumps exactly what the site returned.

Nothing here fakes data. A source that cannot fetch records an error against
that run and the others carry on; the Runs page shows you which is which.

---

## Install

```bash
git clone <your-repo> appart-scout && cd appart-scout
python3 -m venv .venv
.venv/bin/pip install -e ".[browser,dev]"
.venv/bin/playwright install chromium     # skip if using system chromium
cp .env.example .env                       # then fill it in
.venv/bin/scout init
```

`.env` holds the only secrets: `OPENAI_API_KEY`, the `SMTP_*` credentials and
`DIGEST_TO`. Everything else is editable in the web UI.

For Gmail you need an [App Password](https://myaccount.google.com/apppasswords),
not your normal password.

## Run it

```bash
.venv/bin/scout serve       # web UI on http://<host>:8080 + background scheduler
```

Open the UI, set your two workplaces and your budget on **Criteria**, then press
**Run now**.

> The first run resolves commute times from scratch and deliberately does not
> finish: `transport.opendata.ch` is free and volunteer-run, so scout stops after
> 150 lookups and picks the rest up on later runs against a 30-day cache. Expect
> the commute columns to fill in over the first day, not the first run.

## On the Raspberry Pi

```bash
sudo apt install xvfb chromium
echo 'SCOUT_CHROMIUM_PATH=/usr/bin/chromium' >> .env   # Playwright's own arm64 build is unreliable

mkdir -p ~/.config/systemd/user
cp deploy/appart-scout.service ~/.config/systemd/user/
systemctl --user enable --now appart-scout
systemctl --user status appart-scout
loginctl enable-linger $USER      # keep it running after you log out
```

The unit wraps the process in `xvfb-run` on purpose: **headless Chromium is
detected and blocked; the same browser on a virtual display is not.**

The UI binds `0.0.0.0:8080` with **no authentication** — it is meant for your
LAN. Set `SCOUT_AUTH_USER` and `SCOUT_AUTH_PASSWORD` to turn on HTTP basic auth,
and don't port-forward it to the internet as it stands.

---

## The CLI

Every stage runs on its own, so you never have to wait for a scheduled digest to
check a change:

```bash
scout fetch -s flatfox -n 10       # one adapter, printed as a table
scout commute "5200 Brugg"         # door-to-door minutes to both workplaces
scout run --no-email --no-vision   # full pipeline, no side effects
scout top -n 15                    # current ranking
scout digest --dry-run -o d.html   # render the email, open it in a browser
scout digest --send                # really send it
scout probe comparis               # dump what a portal actually returned
scout run                          # the real thing
pytest                             # 77 tests, no network
```

## How it works

```
sources/*  ──►  normalize  ──►  dedup  ──►  hard filters  ──►  commute  ──►  score
                                                                             │
                                            photos (top N only) ─────────────┤
                                                                             ▼
                                                            SQLite ──► digest email
                                                                  └──► web UI
```

- **Dedup** buckets on postcode and room count only — never on price or size,
  because the same flat is routinely listed at 2199 on one portal and 2201 on
  another, and bucketing those separately means they can never be compared.
  Within a bucket, streets are matched fuzzily and rent/size compared with
  tolerance. The merged record keeps every portal's URL.
- **Commute** resolves the nearest station per ~100 m grid cell, then routes
  station→workplace, caching both. Times are door-to-door: walking minutes plus
  the median of the three fastest trains arriving by your target time, on a
  weekday.
- **Scoring** normalises each dimension to 0–1 and takes a weighted mean.
  Sub-scores that can't be computed yet are *dropped from the mean*, not counted
  as zero — a flat is never punished for data we haven't fetched.
- **Photos** are evaluated only for the top N previously-unseen listings, at most
  4 images each, downscaled to 768 px, and the result is cached forever. Cost
  tracks new listings, not catalogue size — cents per run.

## Cost

- `transport.opendata.ch` — free, no key, rate-limited (scout backs off and
  resumes next run).
- Flatfox — free.
- OpenAI — the only thing you pay for. At the defaults (10 listings × 4 low-detail
  images per run) it is a few cents per run. Turn it off in **Settings** and
  everything else still works.

## Layout

```
scout/
  sources/       one adapter per portal + the shared browser/generic bases
  browser.py     Playwright session, anti-bot detection, hydration-state extraction
  geo.py         commute times + caching
  scoring.py     hard filters and the weighted score
  dedup.py       cross-portal merging
  vision.py      OpenAI photo evaluation
  pipeline.py    one full run
  web/           FastAPI + Jinja2 UI (no build step)
tests/           77 tests against recorded real payloads, no network
```

## When a portal breaks

They will — these are unofficial integrations against sites that change.

1. The Runs page names the source and the error.
2. `scout probe <source>` dumps the rendered HTML and any hydration JSON it finds
   (including for blocked responses — seeing the interstitial is the diagnosis).
3. Fix the one file under `scout/sources/`. Nothing else needs to change.
