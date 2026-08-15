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
| **Flatfox** | public JSON API | **Works.** Verified end-to-end, 3 700+ listings per sync. Enabled by default. |
| **Any portal, via alert email** | IMAP (`mailbox` source) | **Works, and is the way in to the blocked four.** Needs one-time manual setup. |
| **ImmoScout24** | headed Chromium + page hydration state | Adapter verified against real payloads, but the site now serves a DataDome captcha. Off by default. |
| **Homegate** | headed Chromium | Blocked. Largely redundant — see below. Off by default. |
| **Newhome** | headed Chromium + generic parser | Cloudflare challenge. Off by default. |
| **Comparis** | headed Chromium + generic parser | DataDome captcha. Off by default. |

**Scraping the last four is a dead end, and going "direct to the API" makes it
worse, not better.** Their JSON endpoints are real and their frontends do call
them, but every one of them is behind DataDome or Cloudflare and refuses a plain
HTTP client outright — harder than it refuses a headed Chromium, because there is
no browser fingerprint or cookie jar to go with the request. Measured directly:

```
flatfox.ch/api/v1/public-listing/     200   35 500 listings
api.homegate.ch/search/listings       403   geo.captcha-delivery.com
www.immoscout24.ch/…                  403   geo.captcha-delivery.com
www.comparis.ch/immobilien/result     403   geo.captcha-delivery.com
www.newhome.ch/api/search/list        403   Cloudflare "Just a moment..."
```

So the default is Flatfox plus the mailbox. Three more things worth knowing:

- **Flatfox alone is a real service.** It covers Zürich and Aargau well, and it
  is the only portal that needs no browser and no setup at all.
- **The blocked portals will send you the same listings by email.** Every one of
  them offers saved-search alerts. The `mailbox` source reads those over IMAP —
  no anti-bot layer, nothing to keep working around, and the mail arrives when
  the listing is posted rather than up to six hours later. See
  [Alert emails](#alert-emails-the-mailbox-source).
- **Homegate matters less than it looks.** Homegate and ImmoScout24 are both SMG
  properties; ImmoScout24 listings carry
  `platforms: ["homegate", "immoscout24", …]`, i.e. the same inventory is
  syndicated across both, and `dedup.py` would merge them anyway.

The browser adapters are still in the tree and still enableable from the settings
page — blocking is per-IP and reputation-based, and your Pi on a Swiss
residential connection is a different proposition from a development machine that
hammered the site for an afternoon. Run `scout probe immoscout` there to find
out; it dumps exactly what the site returned. But every run with one enabled
launches a Chromium, so leave them off unless a probe says otherwise.

Nothing here fakes data. A source that cannot fetch records an error against
that run and the others carry on; the Runs page shows you which is which.

---

## Install

```bash
git clone <your-repo> appart-scout && cd appart-scout
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"          # add ",browser" only for the blocked portals
cp .env.example .env                       # then fill it in
.venv/bin/scout init
```

Playwright is an optional extra now that the default sources need no browser.
Install `".[browser,dev]"` and `.venv/bin/playwright install chromium` only if
you intend to enable ImmoScout24 / Homegate / Newhome / Comparis.

### Credentials

Every credential — the OpenAI key, the `SMTP_*` block for sending, the `IMAP_*`
block for reading alert mail — can be set in **two** places:

- the **Settings** page, which is the point: changing a password on a headless
  Pi should not mean SSH, an editor and `systemctl --user restart`;
- `.env`, which survives a database reset and is the better home for a secret.

**The Settings page wins wherever it is filled in; a blank there falls back to
`.env`.** So you can use either, or both. Fill in nothing at install time and do
it all from the UI if you prefer.

The page never displays a stored secret back — only its last four characters, so
you can tell which key is loaded. Submitting a blank password box keeps the
stored one rather than erasing it; erasing takes the explicit **Clear** button
next to the field. Ports and the TLS toggles are three-way: a value, or "from
.env".

Secrets set in the UI are stored **in plain text** in the SQLite file. Encrypting
them would need a key, and that key would have to live in `.env` — the file this
feature exists to avoid touching. So the protection is file permissions instead:
`scout init` (and every settings save) chmods the database to `0600`. Keep it
that way, and don't put the database on a shared volume.

For Gmail you need an [App Password](https://myaccount.google.com/apppasswords),
not your normal password — for SMTP and IMAP alike.

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

## Alert emails: the `mailbox` source

This is how the blocked portals get in. They will not let you read their
listings, but they will happily mail them to you.

**One-time setup, by hand:**

1. Pick a mailbox for the alerts. A dedicated address is cleanest; a dedicated
   folder plus a filter works too. Everything in the configured folder from a
   recognised portal sender gets parsed, so don't point it at a busy inbox.
2. On each portal — ImmoScout24, Homegate, Newhome, Comparis — create an account,
   run the search you want (the corridor, your budget, your room count) and save
   it as an alert. Set the frequency to immediate/daily. Send it to that mailbox.
3. Fill in the IMAP host, user, password and folder on the **Settings** page
   (Gmail: an App Password), or the `IMAP_*` block in `.env`.
4. Enable **mailbox** in the source list on the same page.

```bash
.venv/bin/scout run --source mailbox     # check it before trusting the schedule
```

Mail is only ever read — never deleted, moved, or marked seen. The adapter keeps
an IMAP UID cursor so each message is parsed once, and re-scans from scratch if
the server ever renumbers the folder.

**What you get and what you don't.** An alert email carries the link, price,
rooms, surface and town — enough for every hard filter, and enough for the
commute lookup, which resolves a town name when there are no coordinates. It
does not carry structured amenity flags, the year built, or more than one photo,
so those stay empty and score as unknown rather than absent. Listings that also
appear on Flatfox get merged by `dedup.py` as usual.

Parsing is generic rather than four hand-written templates: the portals restyle
their mail regularly, but all of them write `CHF 2'450`, `3.5 Zimmer` and
`85 m²`. Fields are read out of the text block around each listing link. If a
portal changes its layout badly enough to break that, the run records it as
fewer listings, not as wrong ones.

## On the Raspberry Pi

```bash
mkdir -p ~/.config/systemd/user
cp deploy/appart-scout.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now appart-scout
systemctl --user status appart-scout
loginctl enable-linger $USER      # keep it running after you log out
```

`enable-linger` is not optional: without it the user unit dies when you log out
of SSH and never starts at boot.

Day to day — note there is no `sudo`, these are **user** units:

```bash
systemctl --user restart appart-scout      # after editing .env; it is read at start only
systemctl --user stop appart-scout
journalctl --user -u appart-scout -f       # logs
systemctl --user daemon-reload             # after editing the unit file itself
```

The unit has `Restart=on-failure`, so a crash comes back by itself after 30 s but
an explicit `stop` stays stopped.

**Only if you enable the browser sources** do you also need:

```bash
sudo apt install xvfb chromium
echo 'SCOUT_CHROMIUM_PATH=/usr/bin/chromium' >> .env   # Playwright's own arm64 build is unreliable
```

The unit wraps the process in `xvfb-run` on purpose: **headless Chromium is
detected and blocked; the same browser on a virtual display is not.** With the
default sources nothing ever launches a browser, and `xvfb-run` is a harmless
no-op wrapper.

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
                              photos (only listings already scoring well) ───┤
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
- **Photos** are the only paid step, so they are spent last and narrowly. A
  listing is photographed only once it has **earned it on the free metrics**:
  a score at or above `vision_min_score` (70 by default) *and* both commutes
  resolved. That second condition matters more than it sounds — until the
  timetable lookup runs, a flat is scored on price and size alone, so cheap
  roomy places an hour outside the corridor sit at the very top of the ranking.
  Photographing those was the main way the budget got spent on flats that then
  dropped out. At most 4 images each, downscaled to 768 px, cached forever, so
  cost tracks newly-qualifying listings rather than catalogue size.

  Any listing can still be evaluated on demand from its **details page**,
  whatever it scores.

- **The web UI opens on "Best fits"**: only listings that passed every hard
  filter *and* both commute ceilings — the same eligibility rule the digest
  uses — so the default grid can be skimmed without skepticism. ⭐ saves a
  listing to the **Shortlist** (its own view, linked in the nav with a count);
  ✕ dismisses it; a "new" pill marks anything first seen in the last 48 h so a
  returning visit only reads the pills. "Everything" shows the provisional
  rest. Commute lookups are spent on the highest-provisionally-scored listings
  first, so the best-fits view fills in best-first rather than randomly.

- **The listing details page** — clicking any card — shows
  every photo, the full commute breakdown per person, the photo evaluation, the
  score bars, the description, and a map. The map is an OpenStreetMap embed —
  no API key, no JS library — and is the one part of the UI that needs
  internet. Listings that arrived by alert email carry a town but no
  coordinates, so those get an address search link instead of a pin, rather
  than a map claiming a precision the source never gave.

## Cost

- `transport.opendata.ch` — free, no key, rate-limited (scout backs off and
  resumes next run).
- Flatfox — free.
- OpenAI — the only thing you pay for, and only for listings that already score
  70+ with both commutes resolved. At the defaults (at most 10 listings × 4
  low-detail images per run, cached forever) it is a few cents per run, and most
  runs qualify nobody at all. Turn it off in **Settings** and everything else
  still works.

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
