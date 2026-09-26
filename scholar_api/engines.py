"""SerpAPI-style 'engines': validate params, fetch the page, parse it and
wrap it in SerpAPI's response envelope."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from . import parsers
from .client import ScholarClient

ApiLink = Optional[Callable[..., str]]


class ParamError(ValueError):
    status_code = 400


SEARCH_PARAMS = (
    "q", "cites", "cluster", "as_ylo", "as_yhi", "scisbd", "hl", "lr",
    "start", "num", "as_sdt", "safe", "filter", "as_vis", "as_rr",
)


def _int_param(params: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ParamError(f"'{name}' must be an integer")
    if not lo <= value <= hi:
        raise ParamError(f"'{name}' must be between {lo} and {hi}")
    return value


async def google_scholar(client: ScholarClient, params: dict, api_link: ApiLink = None, use_cache=True) -> dict:
    if not any(params.get(k) for k in ("q", "cites", "cluster")):
        raise ParamError("Missing query: provide 'q', 'cites' or 'cluster'")
    start = _int_param(params, "start", 0, 0, 1000)
    num = _int_param(params, "num", 10, 1, 20)
    query = {k: params.get(k) for k in SEARCH_PARAMS}
    query.update(hl=params.get("hl") or "en", start=start or None, num=num if num != 10 else None)

    html, url = await client.get("/scholar", query, use_cache=use_cache)
    data = parsers.parse_search(html, api_link)

    if api_link:
        base = {k: v for k, v in params.items() if k in SEARCH_PARAMS and k not in ("start",) and v}
        pag = {"current": start // num + 1}
        if data["pagination"].get("next"):
            pag["next"] = api_link("google_scholar", **base, start=start + num)
        if start > 0:
            pag["previous"] = api_link("google_scholar", **base, start=max(start - num, 0))
        pages = data["pagination"].get("other_pages") or {}
        if pages:
            pag["other_pages"] = {
                p: api_link("google_scholar", **base, start=(int(p) - 1) * num) for p in pages
            }
        data["serpapi_pagination"] = pag
    return _envelope("google_scholar", params, url, data)


async def google_scholar_cite(client: ScholarClient, params: dict, api_link: ApiLink = None, use_cache=True) -> dict:
    result_id = params.get("q")
    if not result_id:
        raise ParamError("Missing 'q': the organic result's result_id")
    query = {
        "q": f"info:{result_id}:scholar.google.com/",
        "output": "cite",
        "scirp": 0,
        "hl": params.get("hl") or "en",
    }
    html, url = await client.get("/scholar", query, use_cache=use_cache)
    return _envelope("google_scholar_cite", params, url, parsers.parse_cite(html))


async def google_scholar_author(client: ScholarClient, params: dict, api_link: ApiLink = None, use_cache=True) -> dict:
    author_id = params.get("author_id")
    hl = params.get("hl") or "en"

    if params.get("view_op") == "view_citation":
        citation_id = params.get("citation_id")
        if not citation_id:
            raise ParamError("view_op=view_citation requires 'citation_id'")
        query = {"view_op": "view_citation", "hl": hl, "citation_for_view": citation_id}
        html, url = await client.get("/citations", query, use_cache=use_cache)
        return _envelope("google_scholar_author", params, url, parsers.parse_citation(html))

    if not author_id:
        raise ParamError("Missing 'author_id'")
    sort = params.get("sort")
    if sort not in (None, "", "title", "pubdate"):
        raise ParamError("'sort' must be 'title' or 'pubdate'")
    start = _int_param(params, "start", 0, 0, 10000)
    num = _int_param(params, "num", 20, 1, 100)
    query = {"user": author_id, "hl": hl, "cstart": start or None, "pagesize": num, "sortby": sort}

    html, url = await client.get("/citations", query, use_cache=use_cache)
    data = parsers.parse_author(html, api_link)
    more = data.pop("more_articles")
    if api_link and (more or len(data["articles"]) >= num):
        extra = {k: params[k] for k in ("hl", "sort") if params.get(k)}
        data["serpapi_pagination"] = {
            "next": api_link("google_scholar_author", author_id=author_id, start=start + num, num=num, **extra)
        }
    return _envelope("google_scholar_author", params, url, data)


async def google_scholar_profiles(client: ScholarClient, params: dict, api_link: ApiLink = None, use_cache=True) -> dict:
    mauthors = params.get("mauthors")
    if not mauthors:
        raise ParamError("Missing 'mauthors'")
    query = {
        "view_op": "search_authors",
        "mauthors": mauthors,
        "hl": params.get("hl") or "en",
        "after_author": params.get("after_author"),
        "before_author": params.get("before_author"),
        "astart": params.get("astart"),
    }
    html, url = await client.get("/citations", query, use_cache=use_cache)
    data = parsers.parse_profiles(html, api_link)
    if api_link:
        pag = {}
        for key, token_param in (("next", "after_author"), ("previous", "before_author")):
            href = data["pagination"].get(key)
            token = data["pagination"].get(f"{key}_page_token")
            if href and token:
                astart = parsers._qs(href, "astart")
                pag[key] = api_link(
                    "google_scholar_profiles", mauthors=mauthors, **{token_param: token}, astart=astart
                )
        if pag:
            data["serpapi_pagination"] = pag
    return _envelope("google_scholar_profiles", params, url, data)


ENGINES = {
    "google_scholar": google_scholar,
    "google_scholar_cite": google_scholar_cite,
    "google_scholar_author": google_scholar_author,
    "google_scholar_profiles": google_scholar_profiles,
}


def _envelope(engine: str, params: dict, url: str, data: dict) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return {
        "search_metadata": {
            "id": uuid.uuid4().hex,
            "status": "Success",
            "created_at": now,
            "processed_at": now,
            "google_scholar_url": url,
        },
        "search_parameters": {"engine": engine, **{k: v for k, v in params.items() if v not in (None, "")}},
        **data,
    }


async def run(engine: str, client: ScholarClient, params: dict, api_link: ApiLink = None, use_cache=True) -> dict:
    handler = ENGINES.get(engine)
    if handler is None:
        raise ParamError(f"Unsupported engine '{engine}'. Supported: {', '.join(ENGINES)}")
    started = time.monotonic()
    result = await handler(client, params, api_link, use_cache)
    result["search_metadata"]["total_time_taken"] = round(time.monotonic() - started, 2)
    return result
