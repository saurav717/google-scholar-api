# scholar-api

A self-hosted Google Scholar scraper with a **SerpAPI-compatible JSON API**. It gives you
the same response shape as SerpAPI's Google Scholar engines, so client code written for
SerpAPI mostly just needs a new base URL.

| Engine | What it returns | Main params |
|---|---|---|
| `google_scholar` (default) | Search results, "cited by" lists, all versions of a paper | `q`, `cites`, `cluster`, `as_ylo`, `as_yhi`, `scisbd`, `hl`, `lr`, `start`, `num` (≤20), `as_sdt`, `safe`, `filter`, `as_vis`, `as_rr` |
| `google_scholar_cite` | MLA/APA/… citations and BibTeX/EndNote/RefMan links | `q` = a result's `result_id` |
| `google_scholar_author` | Profile, metrics table, citation graph, articles, co-authors | `author_id`, `sort` (`title`/`pubdate`), `start`, `num` (≤100); or `view_op=view_citation&citation_id=…` |
| `google_scholar_profiles` | Author search | `mauthors`, `after_author`, `before_author` |

Other params: `no_cache=true` skips the cache, and `api_key` is required only if you set `SCHOLAR_API_KEY`.

## Run

```bash
pip install -e ".[dev]"
scholar-api --host 0.0.0.0 --port 8000        # or: docker build -t scholar-api . && docker run -p 8000:8000 scholar-api
```

```bash
curl "http://localhost:8000/search.json?engine=google_scholar&q=attention+is+all+you+need&as_ylo=2017"
curl "http://localhost:8000/search.json?engine=google_scholar_cite&q=5Gohgn6QFikJ"
curl "http://localhost:8000/search.json?engine=google_scholar_author&author_id=oR9sCGYAAAAJ&num=100"
```

Every `serpapi_*` link in a response points back at your own server, so you can follow
pagination, "cited by", versions, and author links the same way you would with SerpAPI.
Interactive docs are at `/docs`.

### Use as a library

```python
import asyncio
from scholar_api import ScholarClient, run

async def main():
    client = ScholarClient(min_interval=5)
    data = await run("google_scholar", client, {"q": "graph neural networks", "num": 20})
    for r in data["organic_results"]:
        print(r["title"], r["inline_links"].get("cited_by", {}).get("total"))
    await client.aclose()

asyncio.run(main())
```

## Configuration (env vars)

| Variable | Default | Meaning |
|---|---|---|
| `SCHOLAR_PROXIES` | *(direct)* | Comma-separated proxy URLs, e.g. `http://user:pass@host:port,...`. Requests rotate across them. |
| `SCHOLAR_MIN_INTERVAL` | `3` | Minimum seconds between requests on one proxy |
| `SCHOLAR_JITTER` | `2` | Random extra delay (0–N s) on each request |
| `SCHOLAR_MAX_RETRIES` | `3` | Retries after a CAPTCHA/429/5xx (each retry uses the next proxy) |
| `SCHOLAR_BLOCK_COOLDOWN` | `600` | Seconds a blocked proxy is skipped |
| `SCHOLAR_CACHE_TTL` | `3600` | In-memory cache TTL in seconds (`0` disables it) |
| `SCHOLAR_TIMEOUT` | `20` | HTTP timeout in seconds |
| `SCHOLAR_API_KEY` | *(none)* | If set, requests must include `api_key=<value>` |

Run **one worker process**, because the rate limiter and cache live in memory.

## The honest part: blocking

What you pay SerpAPI for is mostly not the parsing. It's the **proxy pool and CAPTCHA
solving** that keep requests from getting blocked. Google Scholar has no official API and
rate-limits aggressively. Expect this:

* **One home or office IP, slow pace** (≥3–5 s between requests): fine for personal
  research, maybe a few hundred requests a day before you see a CAPTCHA.
* **Cloud/datacenter IPs** (AWS, GCP, …): often blocked almost immediately.
* **Volume**: you need rotating **residential** proxies (`SCHOLAR_PROXIES`). That costs
  money too, but usually far less than SerpAPI per request.

When every attempt is blocked, the API returns HTTP `503` with
`{"error": "Google Scholar returned a CAPTCHA ..."}`. The blocked proxy is benched,
and its cookies and user-agent are rotated.

Scraping Google Scholar is against Google's Terms of Service. Use it responsibly, at low
volume, and for your own research.

## Maintenance

Google changes Scholar's HTML from time to time. All selectors live in
`scholar_api/parsers.py`, and `tests/fixtures/` holds sample pages. If a field comes back empty,
save the live page (`curl … > tests/fixtures/x.html`), update the selector, and run `pytest`.
