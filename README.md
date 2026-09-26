# scholar-api

A self-hosted Google Scholar scraper with a **SerpAPI-compatible JSON API**. It returns
the same response shape as SerpAPI's Google Scholar engines, plus extra structured fields
and diagnostics. Client code written for SerpAPI mostly just needs a new base URL.

| Engine | What it returns |
|---|---|
| `google_scholar` (default) | Search results, papers citing a paper (`cites`), all versions of a paper (`cluster`) |
| `google_scholar_cite` | MLA/APA/Chicago/Harvard/Vancouver citations, export links, and optionally the parsed BibTeX |
| `google_scholar_author` | Profile, citation metrics, citations per year, public-access stats, co-authors, articles (optionally all of them), or one article's full record |
| `google_scholar_profiles` | Find author profiles by name (runs a regular search, since Scholar's own author search now requires sign-in) |

---

## 1. Install

Requires **Python 3.10+** (check with `python --version`).

```bash
git clone https://github.com/saurav717/google-scholar-api.git
cd google-scholar-api
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip   # pip older than 21.3 can't do editable installs
pip install -e ".[dev]"               # dev includes the optional browser-cookie reader
```

**Using Anaconda?** Its `base` Python may be too old, and `base`'s own `pytest` can
crash on unrelated plugins. Use a dedicated environment instead of the venv above:

```bash
conda create -n scholar-api python=3.12 -y
conda activate scholar-api            # do this in every new terminal
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

Always run tests with `python -m pytest` so they use this environment's Python.

## 2. Run the offline test suite (no internet needed)

```bash
python -m pytest -v
```

This runs 75 tests against saved Scholar pages in `tests/fixtures/`. They cover every
parser, every endpoint, caching, retries, CAPTCHA handling, proxy rotation and a full
end-to-end run over real HTTP. They should all pass on a fresh clone.

## 3. Test against live Google Scholar

```bash
scholar-api check
```

This makes about 6 real requests, one per engine, spaced a few seconds apart. It prints a
field-by-field report:

```
[1/5] google_scholar  {'q': 'attention is all you need'}
    ok                 total results          2340000
    ok                 title                  "Attention is all you need"
    ok                 cited by               167432
    ...
32 required fields ok, 0 missing/failed.
```

It also saves each page's raw HTML and resulting JSON in `./scholar-check/`. Options:
`--query "..."`, `--author-id JicYPdAAAAAJ`, `--profiles "name"`, `--save-dir DIR`.

* **All `ok`**: the parsers match Scholar's current HTML.
* **Some `MISSING`**: Google changed its markup. Copy the saved `.html` from `scholar-check/`
  into `tests/fixtures/`, fix the selector in `scholar_api/parsers.py`, and rerun `pytest`.
* **"Google is blocking this IP"**: see [Blocking](#blocking-read-this) below.

## 4. Run the server

```bash
scholar-api serve                        # http://127.0.0.1:8000
scholar-api serve --host 0.0.0.0 --port 8000
# or with Docker:
docker build -t scholar-api . && docker run -p 8000:8000 scholar-api
```

Then open:

| URL | What it is |
|---|---|
| http://127.0.0.1:8000/docs | Interactive docs. Every parameter is documented; click **Try it out** to run queries from the browser |
| http://127.0.0.1:8000/engines | Every engine, its parameters and a ready-to-click example URL |
| http://127.0.0.1:8000/status | Per-proxy request, success, block and error counts, CAPTCHA-solver stats, cache stats, pacing config |
| http://127.0.0.1:8000/ | Service info and quick links |

## 5. Try it

```bash
# Search
curl "localhost:8000/search.json?q=attention+is+all+you+need"
curl "localhost:8000/search.json?q=graph+neural+networks&as_ylo=2020&num=20"
curl "localhost:8000/search.json?q=author:%22y+lecun%22+convolutional"

# Papers citing a paper / all versions (ids come from inline_links in a search result)
curl "localhost:8000/search.json?cites=2960712678066186980"
curl "localhost:8000/search.json?cluster=2960712678066186980"

# Citations + parsed BibTeX for a result (q = organic_results[].result_id)
curl "localhost:8000/search.json?engine=google_scholar_cite&q=5Gohgn6QFikJ&include_bibtex=true"

# Author profile; every article; a single article's record
curl "localhost:8000/search.json?engine=google_scholar_author&author_id=JicYPdAAAAAJ"
curl "localhost:8000/search.json?engine=google_scholar_author&author_id=JicYPdAAAAAJ&all_articles=true"
curl "localhost:8000/search.json?engine=google_scholar_author&view_op=view_citation&citation_id=JicYPdAAAAAJ:u5HHmVD_uO8C"

# Author search
curl "localhost:8000/search.json?engine=google_scholar_profiles&mauthors=geoffrey+hinton"
```

Add `| python -m json.tool` to pretty-print the output. Every `serpapi_*` link in a
response points back at your server, so you can follow pagination, "cited by", versions
and author links directly.

---

## What's in a response

Shared by every engine:

```jsonc
"search_metadata": {
  "status": "Success",
  "google_scholar_url": "https://scholar.google.com/scholar?q=...",   // the page that was scraped
  "total_time_taken": 3.21,
  "cached": false,            // true = served from cache, no request to Google
  "scholar_requests": 1,      // HTTP requests made to Google (including retries)
  "blocked_attempts": 0,      // how many of those hit a CAPTCHA
  "captchas_solved": 0,       // CAPTCHAs cleared by the solving service (see below)
  "pages_fetched": 1,
  "warnings": ["Ignored unknown parameter 'foo' ..."]   // only when there is something to say
}
```

Extra fields beyond what SerpAPI returns:

* **Search results:**
  * `publication_info` is split into `author_names`, `authors_truncated`, `venue`, `year`
    and `source`.
  * `inline_links.web_of_science` gives the Web of Science citation count and link.
  * `search_information.did_you_mean` and `showing_results_for` carry spelling
    suggestions, and `results_on_page` gives the result count.
* **Cite:** with `include_bibtex=true`, `bibtex.raw` holds the file and `bibtex.parsed`
  holds `{type, key, fields, authors}`.
* **Author:**
  * `public_access` gives the counts of available and not-available articles.
  * `articles_summary` gives the number returned, `has_more`, and total citations.
  * `all_articles=true` pages through the whole list, 100 articles per request.
* **Article view:** `total_citations.graph` gives citations per year.
* **Profiles:** Scholar's dedicated author search (`citations?view_op=search_authors`) now
  redirects anonymous users to a Google sign-in page, so this engine searches Scholar for
  the name. It returns the "User profiles for …" cards Scholar shows above the results
  (`source: "profile_box"`, with affiliation, email and `cited_by`), plus linked authors on
  the results whose name matches (`source: "search_results"`). Both include
  `papers_in_results`. `label:` searches are no longer possible.

Errors always look like this:

```json
{"error": "Missing 'author_id'", "error_type": "invalid_parameter", "hint": "The id is the user= value of a Scholar profile URL, e.g. JicYPdAAAAAJ."}
```

| HTTP | `error_type` | Meaning |
|---|---|---|
| 400 | `invalid_parameter` | Bad or missing parameter |
| 401 | `unauthorized` | `SCHOLAR_API_KEY` is set and `api_key` is wrong or missing |
| 403 | `sign_in_required` | Google redirected to a sign-in page; that Scholar page can't be scraped anonymously |
| 404 | `not_found` | Scholar has no page for that id |
| 502 | `upstream_error` | Google unreachable or returned 5xx |
| 503 | `blocked` | Google returned a CAPTCHA or rate-limit page on every attempt |

If a page parses to nothing, the response still succeeds, but a warning explains that
Scholar's HTML may have changed.

### Use as a Python library

```python
import asyncio
from scholar_api import ScholarClient, run

async def main():
    client = ScholarClient(min_interval=5)
    data = await run("google_scholar", client, {"q": "graph neural networks", "num": 20})
    for r in data["organic_results"]:
        info = r["publication_info"]
        print(info.get("year"), r["title"], r["inline_links"].get("cited_by", {}).get("total"))
    await client.aclose()

asyncio.run(main())
```

## Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `SCHOLAR_PROXIES` | *(direct)* | Comma-separated proxy URLs, e.g. `http://user:pass@host:port,...`. Requests rotate across them |
| `SCHOLAR_MIN_INTERVAL` | `3` | Minimum seconds between requests on one proxy |
| `SCHOLAR_JITTER` | `2` | Random extra delay (0–N s) on each request |
| `SCHOLAR_MAX_RETRIES` | `3` | Retries after a CAPTCHA, 429, 5xx or network error (each retry uses the next proxy) |
| `SCHOLAR_BLOCK_COOLDOWN` | `600` | Seconds a blocked proxy is skipped |
| `SCHOLAR_CACHE_TTL` | `3600` | In-memory cache TTL in seconds (`0` disables it) |
| `SCHOLAR_TIMEOUT` | `20` | HTTP timeout in seconds |
| `SCHOLAR_API_KEY` | *(none)* | If set, requests must include `api_key=<value>` |
| `SCHOLAR_HTTP_BACKEND` | `curl_cffi` | `curl_cffi` makes requests with a real Chrome TLS/HTTP2 fingerprint. `httpx` is a plain Python client that Google blocks quickly, so use it only for debugging |
| `SCHOLAR_IMPERSONATE` | `chrome` | Browser profile for `curl_cffi`: `chrome`, `edge`, `safari`, … |
| `SCHOLAR_COOKIE_FILE` | `~/.scholar-api/cookies.json` | Where Google's cookies are kept between runs (file mode 0600). `none` disables it |
| `SCHOLAR_WARMUP` | `1` | Visit the Scholar homepage once per connection before the first query. `0` turns it off |
| `SCHOLAR_COOKIES` | *(none)* | Cookies copied from your browser. See [Using your browser's cookies](#using-your-browsers-cookies) |
| `SCHOLAR_BROWSER` | *(none)* | Read Scholar's cookies from this browser each time the scraper starts: `chrome`, `firefox`, `safari`, `edge`, `brave`, … or `auto` |
| `SCHOLAR_CAPTCHA_PROVIDER` | *(none)* | Solve CAPTCHAs yourself in a browser window (`browser`), or with a paid service: `2captcha`, `capsolver`, `anticaptcha` or `capmonster`. See [CAPTCHA solving](#captcha-solving) |
| `SCHOLAR_CAPTCHA_API_KEY` | *(none)* | Your key for the paid service (not needed for `browser`) |
| `SCHOLAR_CAPTCHA_MAX_SOLVES` | `1` | CAPTCHAs to solve per API request before giving up |
| `SCHOLAR_CAPTCHA_TIMEOUT` | `180` (`300` for `browser`) | Seconds to wait for one CAPTCHA to be solved |
| `SCHOLAR_CAPTCHA_BROWSER_CHANNEL` | `chrome` | `browser` only: which browser to open. `chrome` uses your installed Google Chrome, falling back to Playwright's Chromium; also `msedge`, or empty for Chromium |
| `SCHOLAR_CAPTCHA_BROWSER_PATH` | *(none)* | `browser` only: path to a specific Chrome/Chromium binary |
| `SCHOLAR_CAPTCHA_POLL_INTERVAL` | `5` | Seconds between result polls |
| `SCHOLAR_CAPTCHA_USE_PROXY` | `1` | When the blocked request went through a proxy, have the service solve through that proxy too. `0` = solve from the service's own IPs |
| `SCHOLAR_CAPTCHA_API_URL` | *(provider's)* | Override the service's API address |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Server bind address |

Run **one worker process**, because the rate limiter and cache live in memory.

## Blocking (read this)

Requests go out through [`curl_cffi`](https://github.com/lexiforest/curl_cffi), which
connects exactly like Chrome. This matters: in testing from a home connection, a plain
Python client (`httpx`) got an immediate HTTP 429 and was redirected to Google's `/sorry/`
page, while the same request through `curl_cffi` returned normal results. VPNs make
blocks far more likely, because their IPs are shared and flagged.

Google also trusts a **returning visitor** more than a brand-new one. So the scraper keeps
the cookies Google gives it in `~/.scholar-api/cookies.json` and reuses them on the next
run. It also opens the Scholar homepage once before its first query, as a person would.

What you pay SerpAPI for is mostly not the parsing. It's the **proxy pool and CAPTCHA
solving** that keep requests from getting blocked. Google Scholar has no official API and
rate-limits aggressively. Expect this:

* **One home or office IP, slow pace** (≥3–5 s between requests): fine for personal
  research, maybe a few hundred requests a day before you see a CAPTCHA.
* **Cloud/datacenter IPs** (AWS, GCP, …): often blocked almost immediately.
* **Volume**: you need rotating **residential** proxies (`SCHOLAR_PROXIES`). That costs
  money too, but usually far less than SerpAPI per request.

When blocked, a proxy is benched for `SCHOLAR_BLOCK_COOLDOWN` seconds, and its cookies
are cleared. With no proxies configured, the API does **not** retry a CAPTCHA right away,
because retrying the same IP only extends the block. `scholar-api check` saves the block
page to `scholar-check/blocked.html`. `GET /status` shows which proxies are getting blocked.
`all_articles` and `include_bibtex` make extra requests, so use them sparingly.

### CAPTCHA solving

Instead of giving up when Google shows a CAPTCHA, the scraper can get it solved: by you, in
a browser window, for free, or by a paid solving service, the way SerpAPI does.

#### Solve it yourself in a browser (free)

```bash
pip install -e ".[solver]"        # Playwright
playwright install chromium       # only needed if you don't have Google Chrome
export SCHOLAR_CAPTCHA_PROVIDER=browser
scholar-api serve                 # or: scholar-api check
```

When Google blocks a request:

1. A browser window opens on the block page, going through the same proxy as the blocked
   request. The terminal beeps and prints `Google is showing a CAPTCHA. Solve it in the
   browser window...`.
2. You tick the box or solve the image grid. The window is seeded with the scraper's own
   Google cookies, so Google sees the same visitor.
3. When Google accepts it, the window closes and the scraper takes the
   `GOOGLE_ABUSE_EXEMPTION` cookie Google set (plus `NID`/`GSP`, never sign-in cookies),
   retries the request and carries on. The cookie is saved to the cookie file and usually
   lasts for hours.

The API request waits while you solve, up to `SCHOLAR_CAPTCHA_TIMEOUT` (5 minutes), so
give your HTTP client a long timeout. Only one window opens at a time. Other requests on
the same connection that were blocked at the same moment reuse the solve instead of
opening their own window. While a CAPTCHA is waiting, `GET /status` shows
`captcha.waiting_for_person: true` and the page URL. If you close the window or don't solve
it in time, the request fails with the usual `blocked` error.

This needs a desktop to show the window. It won't work inside Docker or on a headless
server, where the error says so; use a paid provider there.

#### Paid solving services

Supported: [2Captcha](https://2captcha.com/),
[CapSolver](https://www.capsolver.com/), [Anti-Captcha](https://anti-captcha.com/) and
[CapMonster Cloud](https://capmonster.cloud/). All four use the same task API, so switching
is just a setting:

```bash
export SCHOLAR_CAPTCHA_PROVIDER=2captcha      # or capsolver / anticaptcha / capmonster
export SCHOLAR_CAPTCHA_API_KEY=<your key>
scholar-api serve
```

What happens on a block:

1. **Create task.** The reCAPTCHA (or hCaptcha) site key, the page URL and Google's `data-s`
   value are read from the block page and sent to the service. If the request used a
   proxy, the service solves through that proxy as well, so the token matches the IP.
2. **Poll for the result** every `SCHOLAR_CAPTCHA_POLL_INTERVAL` seconds, for up to
   `SCHOLAR_CAPTCHA_TIMEOUT`. Solving usually takes 10–60 s.
3. **Inject the token.** The scraper submits the token in the page's own form on the same
   connection. Google answers with a `GOOGLE_ABUSE_EXEMPTION` cookie, which is kept for
   later requests, and the original request goes through. The proxy is not benched.

If the page has nothing to solve (a bare HTTP 429), the service fails (for example, zero
balance), or Google rejects the token, the request falls back to the usual block
handling, and the error message says why. Each solve costs money (roughly $1–3 per
1,000 reCAPTCHAs), so at most `SCHOLAR_CAPTCHA_MAX_SOLVES` are paid for per API request.
`search_metadata.captchas_solved` shows solves for a request, and the `captcha` section of
`GET /status` shows totals, failures, tokens Google rejected, average solve time and the
last error.

A CAPTCHA is Google asking you to slow down. Solving one doesn't lift the rate limit, so
keep the pacing settings and proxies above in place as well.

### Using your browser's cookies

If Scholar opens fine in your browser but the scraper is blocked, you can let the scraper
reuse your browser's standing with Scholar.

**Automatically (recommended):**

```bash
scholar-api import-cookies                    # reads Chrome; or --browser firefox / safari / edge / brave / auto
```

This reads `NID`, `GSP` and `GOOGLE_ABUSE_EXEMPTION` for google.com from your browser and
saves them to `~/.scholar-api/cookies.json`. Every later run uses them, with no settings
needed. On macOS, Chrome, Edge and Brave ask once for your login (Keychain) password to
unlock their cookie store; click **Allow**. Safari needs Full Disk Access for your
terminal app. Re-run the command if blocks come back.

To re-read the browser's cookies every time the scraper starts, set
`SCHOLAR_BROWSER=chrome` (or pass `scholar-api check --browser chrome`). `GET /status`
shows the browser in use and any error reading it.

**By hand:**

1. Open https://scholar.google.com in Chrome, and solve a CAPTCHA if one appears.
2. Open DevTools (`Cmd+Option+I` on a Mac, `F12` on Windows) → **Application** →
   **Cookies** → `https://scholar.google.com`.
3. Copy the values of `NID` and `GSP`. If you just solved a CAPTCHA, also copy
   `GOOGLE_ABUSE_EXEMPTION`.
4. Start the scraper with them:

   ```bash
   export SCHOLAR_COOKIES="NID=<value>; GSP=<value>"
   scholar-api check        # or: scholar-api serve
   ```

Either way, only `NID`, `GSP` and `GOOGLE_ABUSE_EXEMPTION` are ever used. Anything else you paste,
including Google sign-in cookies such as `SID` or `SAPISID`, is dropped and listed under
"ignored", so your Google account is never involved. These cookies expire, so refresh
them if blocks come back. `GET /status` shows which cookies are in use.

Scraping Google Scholar is against Google's Terms of Service. Use it responsibly, at low
volume, and for your own research.

## Project layout

```
scholar_api/
  parsers.py   HTML -> dicts. All CSS selectors live here
  client.py    HTTP: pacing, proxy rotation, CAPTCHA detection, retries, cache, stats
  captcha.py   CAPTCHA-solving services (2Captcha, CapSolver, Anti-Captcha, CapMonster)
  browser_solver.py  Solve CAPTCHAs yourself in a browser window
  engines.py   Parameter spec + validation, SerpAPI envelope, diagnostics
  app.py       FastAPI server (/search.json, /engines, /status, /docs)
  check.py     `scholar-api check` live test
tests/
  fixtures/    Saved Scholar pages the parsers are tested against
```
