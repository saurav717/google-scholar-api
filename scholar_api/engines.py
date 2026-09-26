"""SerpAPI-style 'engines': validate params, fetch the page(s), parse them and
wrap the result in SerpAPI's response envelope, plus diagnostics about how
the request went (cache hits, attempts, warnings)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from . import parsers
from .client import FetchResult, ScholarClient, ScholarError

ApiLink = Optional[Callable[..., str]]


class ParamError(ValueError):
    status_code = 400
    error_type = "invalid_parameter"

    def __init__(self, message: str, hint: Optional[str] = None):
        super().__init__(message)
        self.hint = hint


# Parameters every engine accepts (handled by the HTTP layer / run()).
COMMON_PARAMS = {
    "engine": {"description": "Which engine to run. Defaults to google_scholar.", "example": "google_scholar"},
    "hl": {"description": "Interface language (two-letter code). Parsing is tuned for 'en'.", "example": "en"},
    "no_cache": {"description": "true = bypass the response cache and always fetch fresh.", "example": "false"},
    "api_key": {"description": "Required only if the server was started with SCHOLAR_API_KEY."},
}

ENGINE_DOCS: dict[str, dict] = {
    "google_scholar": {
        "description": "Search Google Scholar: keyword search, papers citing a paper (cites), or all versions of a paper (cluster).",
        "scholar_page": "https://scholar.google.com/scholar",
        "params": {
            "q": {"description": "Search query. Supports Scholar operators: author:\"...\", source:\"...\", intitle:, \"exact phrase\", OR, -exclude. Required unless cites or cluster is given.", "example": "attention is all you need"},
            "cites": {"description": "Return articles that cite this paper. Use inline_links.cited_by.cites_id from a result.", "example": "2960712678066186980"},
            "cluster": {"description": "Return all versions of a paper. Use inline_links.versions.cluster_id from a result.", "example": "2960712678066186980"},
            "as_ylo": {"description": "Only results published in or after this year.", "example": "2018"},
            "as_yhi": {"description": "Only results published in or before this year.", "example": "2023"},
            "scisbd": {"description": "Sort by date: 1 = only abstracts added in the last year, 2 = everything, newest first. Omit to sort by relevance."},
            "as_sdt": {"description": "0 = exclude patents (default), 7 = include patents; with cites, 4,<court ids> selects case law."},
            "as_vis": {"description": "1 = exclude citation-only entries ([CITATION])."},
            "as_rr": {"description": "1 = review articles only."},
            "lr": {"description": "Restrict to languages, e.g. lang_en|lang_fr."},
            "safe": {"description": "active / off: adult-content filter."},
            "filter": {"description": "1 (default) = collapse similar/omitted results, 0 = show them."},
            "start": {"description": "Result offset for pagination (0, 10, 20 ...). Scholar stops at ~1000.", "example": "0"},
            "num": {"description": "Results per page, 1-20 (default 10).", "example": "10"},
        },
    },
    "google_scholar_cite": {
        "description": "Formatted citations (MLA, APA, Chicago, Harvard, Vancouver) and export links for one result.",
        "scholar_page": "https://scholar.google.com/scholar?q=info:<result_id>:scholar.google.com/&output=cite",
        "params": {
            "q": {"description": "The result_id of an organic result (required).", "example": "5Gohgn6QFikJ"},
            "include_bibtex": {"description": "true = also download the BibTeX file and return it raw and parsed (1 extra request).", "example": "true"},
        },
    },
    "google_scholar_author": {
        "description": "An author's profile: affiliation, interests, citation metrics (citations, h-index, i10-index), citations per year, public-access stats, co-authors and articles. With view_op=view_citation: the full record of one article.",
        "scholar_page": "https://scholar.google.com/citations?user=<author_id>",
        "params": {
            "author_id": {"description": "Scholar profile id (the user= value in a profile URL). Required unless view_op=view_citation.", "example": "JicYPdAAAAAJ"},
            "sort": {"description": "Article order: omit for most cited, 'pubdate' for newest, 'title' for A-Z."},
            "start": {"description": "Article offset for pagination.", "example": "0"},
            "num": {"description": "Articles per page, 1-100 (default 20).", "example": "20"},
            "all_articles": {"description": "true = keep paging (100 per request) until every article is fetched. Costs one Scholar request per 100 articles.", "example": "false"},
            "max_pages": {"description": "Upper bound on pages fetched when all_articles=true (default 10, max 50).", "example": "10"},
            "view_op": {"description": "Set to view_citation to fetch a single article's detail page."},
            "citation_id": {"description": "Article id (articles[].citation_id) for view_op=view_citation.", "example": "JicYPdAAAAAJ:u5HHmVD_uO8C"},
        },
    },
    "google_scholar_profiles": {
        "description": "Search Scholar author profiles by name or by label (label:machine_learning). Google has been restricting this page; it may return nothing.",
        "scholar_page": "https://scholar.google.com/citations?view_op=search_authors",
        "params": {
            "mauthors": {"description": "Author name, or label:<interest> (required).", "example": "geoffrey hinton"},
            "after_author": {"description": "Next-page token (pagination.next_page_token)."},
            "before_author": {"description": "Previous-page token (pagination.previous_page_token)."},
            "astart": {"description": "Result offset that accompanies after_author/before_author (set automatically in serpapi_pagination links)."},
        },
    },
}

SEARCH_PARAMS = tuple(ENGINE_DOCS["google_scholar"]["params"]) + ("hl",)


@dataclass
class Ctx:
    client: ScholarClient
    params: dict
    api_link: ApiLink = None
    use_cache: bool = True
    fetches: list[FetchResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    async def get(self, path: str, query: dict) -> FetchResult:
        result = await self.client.get(path, query, use_cache=self.use_cache)
        self.fetches.append(result)
        return result

    async def get_url(self, url: str) -> FetchResult:
        result = await self.client.get_url(url, use_cache=self.use_cache)
        self.fetches.append(result)
        return result

    @property
    def hl(self) -> str:
        return self.params.get("hl") or "en"


def _int_param(params: dict, name: str, default: int, lo: int, hi: int) -> int:
    raw = params.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ParamError(f"'{name}' must be an integer, got {raw!r}")
    if not lo <= value <= hi:
        raise ParamError(f"'{name}' must be between {lo} and {hi}, got {value}")
    return value


def _bool_param(params: dict, name: str) -> bool:
    return str(params.get(name, "")).lower() in ("1", "true", "yes", "on")


def _layout_warning(what: str) -> str:
    return (
        f"No {what} could be parsed. Either nothing matched, or Google changed Scholar's HTML. "
        "Open search_metadata.google_scholar_url in a browser to compare."
    )


# --------------------------------------------------------------------------

async def google_scholar(ctx: Ctx) -> dict:
    p = ctx.params
    if not any(p.get(k) for k in ("q", "cites", "cluster")):
        raise ParamError(
            "Missing query: provide 'q', 'cites' or 'cluster'",
            hint="e.g. /search.json?engine=google_scholar&q=graph+neural+networks",
        )
    start = _int_param(p, "start", 0, 0, 1000)
    num = _int_param(p, "num", 10, 1, 20)
    ylo, yhi = p.get("as_ylo"), p.get("as_yhi")
    if ylo and yhi and str(ylo).isdigit() and str(yhi).isdigit() and int(ylo) > int(yhi):
        raise ParamError(f"'as_ylo' ({ylo}) is after 'as_yhi' ({yhi})")

    query = {k: p.get(k) for k in SEARCH_PARAMS}
    query.update(hl=ctx.hl, start=start or None, num=num if num != 10 else None)
    fetched = await ctx.get("/scholar", query)
    data = parsers.parse_search(fetched.html, ctx.api_link)

    info = data["search_information"]
    if not data["organic_results"] and not info.get("did_you_mean"):
        ctx.warnings.append(_layout_warning("results"))
    if start >= 1000:
        ctx.warnings.append("Google Scholar does not serve results past start=1000.")

    if ctx.api_link:
        base = {k: v for k, v in p.items() if k in SEARCH_PARAMS and k != "start" and v}
        pag: dict = {"current": start // num + 1}
        if data["pagination"].get("next"):
            pag["next"] = ctx.api_link("google_scholar", **base, start=start + num)
        if start > 0:
            pag["previous"] = ctx.api_link("google_scholar", **base, start=max(start - num, 0))
        pages = data["pagination"].get("other_pages") or {}
        if pages:
            pag["other_pages"] = {
                n: ctx.api_link("google_scholar", **base, start=(int(n) - 1) * num) for n in pages
            }
        data["serpapi_pagination"] = pag
    return data


async def google_scholar_cite(ctx: Ctx) -> dict:
    result_id = ctx.params.get("q")
    if not result_id:
        raise ParamError(
            "Missing 'q': the result_id of an organic result",
            hint="Take organic_results[].result_id from a google_scholar search.",
        )
    query = {"q": f"info:{result_id}:scholar.google.com/", "output": "cite", "scirp": 0, "hl": ctx.hl}
    fetched = await ctx.get("/scholar", query)
    data = parsers.parse_cite(fetched.html)
    if not data["citations"]:
        ctx.warnings.append(_layout_warning("citations"))

    if _bool_param(ctx.params, "include_bibtex"):
        link = next((l["link"] for l in data["links"] if (l["name"] or "").lower() == "bibtex"), None)
        if not link:
            ctx.warnings.append("include_bibtex: no BibTeX link on the cite page.")
        else:
            try:
                bib = await ctx.get_url(link)
                parsed = parsers.parse_bibtex(bib.html)
                data["bibtex"] = {"raw": bib.html.strip(), "parsed": parsed}
                if parsed is None:
                    ctx.warnings.append("include_bibtex: the BibTeX link did not return BibTeX.")
            except ScholarError as exc:
                ctx.warnings.append(f"include_bibtex: could not download BibTeX ({exc}).")
    return data


async def google_scholar_author(ctx: Ctx) -> dict:
    p = ctx.params
    if p.get("view_op") == "view_citation":
        citation_id = p.get("citation_id")
        if not citation_id:
            raise ParamError(
                "view_op=view_citation requires 'citation_id'",
                hint="Take articles[].citation_id from a google_scholar_author response.",
            )
        query = {"view_op": "view_citation", "hl": ctx.hl, "citation_for_view": citation_id}
        fetched = await ctx.get("/citations", query)
        data = parsers.parse_citation(fetched.html)
        data["citation"]["citation_id"] = citation_id
        if not data["citation"].get("title"):
            ctx.warnings.append(_layout_warning("article details"))
        return data

    author_id = p.get("author_id")
    if not author_id:
        raise ParamError(
            "Missing 'author_id'",
            hint="The id is the user= value of a Scholar profile URL, e.g. JicYPdAAAAAJ.",
        )
    sort = p.get("sort") or None
    if sort not in (None, "title", "pubdate"):
        raise ParamError(f"'sort' must be 'title' or 'pubdate', got {sort!r}")
    fetch_all = _bool_param(p, "all_articles")
    start = _int_param(p, "start", 0, 0, 10000)
    num = 100 if fetch_all else _int_param(p, "num", 20, 1, 100)
    max_pages = _int_param(p, "max_pages", 10, 1, 50) if fetch_all else 1

    data: dict = {}
    articles: list = []
    more = False
    for page in range(max_pages):
        offset = start + page * num
        query = {"user": author_id, "hl": ctx.hl, "cstart": offset or None, "pagesize": num, "sortby": sort}
        fetched = await ctx.get("/citations", query)
        parsed = parsers.parse_author(fetched.html, ctx.api_link)
        if page == 0:
            data = parsed
        articles.extend(parsed["articles"])
        more = parsed.pop("more_articles") and len(parsed["articles"]) >= num
        if not more:
            break
    data.pop("more_articles", None)
    data["articles"] = articles
    data["author"]["author_id"] = author_id
    data["author"]["link"] = f"{parsers.SCHOLAR_BASE}/citations?user={author_id}&hl={ctx.hl}"

    if not data["author"].get("name"):
        ctx.warnings.append(_layout_warning("author profile"))
    if fetch_all and more:
        ctx.warnings.append(f"Stopped after max_pages={max_pages}; the author has more articles.")

    data["articles_summary"] = {
        "returned": len(articles),
        "start": start,
        "has_more": more,
        "total_citations_of_returned": sum((a["cited_by"].get("value") or 0) for a in articles),
    }
    if ctx.api_link and more:
        extra = {k: p[k] for k in ("hl", "sort") if p.get(k)}
        data["serpapi_pagination"] = {
            "next": ctx.api_link(
                "google_scholar_author", author_id=author_id, start=start + len(articles), num=num, **extra
            )
        }
    return data


async def google_scholar_profiles(ctx: Ctx) -> dict:
    p = ctx.params
    mauthors = p.get("mauthors")
    if not mauthors:
        raise ParamError("Missing 'mauthors'", hint="An author name, or label:<interest> e.g. label:robotics")
    query = {
        "view_op": "search_authors",
        "mauthors": mauthors,
        "hl": ctx.hl,
        "after_author": p.get("after_author"),
        "before_author": p.get("before_author"),
        "astart": p.get("astart"),
    }
    fetched = await ctx.get("/citations", query)
    data = parsers.parse_profiles(fetched.html, ctx.api_link)
    if not data["profiles"]:
        ctx.warnings.append(
            _layout_warning("profiles") + " Note: Google has been restricting author search."
        )
    if ctx.api_link:
        pag = {}
        for key, token_param in (("next", "after_author"), ("previous", "before_author")):
            href = data["pagination"].get(key)
            token = data["pagination"].get(f"{key}_page_token")
            if href and token:
                pag[key] = ctx.api_link(
                    "google_scholar_profiles", mauthors=mauthors,
                    **{token_param: token}, astart=parsers._qs(href, "astart"),
                )
        if pag:
            data["serpapi_pagination"] = pag
    return data


ENGINES = {
    "google_scholar": google_scholar,
    "google_scholar_cite": google_scholar_cite,
    "google_scholar_author": google_scholar_author,
    "google_scholar_profiles": google_scholar_profiles,
}


async def run(
    engine: str,
    client: ScholarClient,
    params: dict,
    api_link: ApiLink = None,
    use_cache: bool = True,
) -> dict:
    handler = ENGINES.get(engine)
    if handler is None:
        raise ParamError(
            f"Unsupported engine {engine!r}",
            hint=f"Supported engines: {', '.join(ENGINES)}. See GET /engines.",
        )
    params = {k: v for k, v in params.items() if v not in (None, "")}
    ctx = Ctx(client, params, api_link, use_cache)
    known = set(ENGINE_DOCS[engine]["params"]) | set(COMMON_PARAMS)
    for name in sorted(set(params) - known):
        ctx.warnings.append(f"Ignored unknown parameter {name!r} for engine {engine!r}.")

    created = datetime.now(timezone.utc)
    started = time.monotonic()
    data = await handler(ctx)
    elapsed = round(time.monotonic() - started, 3)

    metadata = {
        "id": uuid.uuid4().hex,
        "status": "Success",
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "processed_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "google_scholar_url": ctx.fetches[0].url if ctx.fetches else None,
        "total_time_taken": elapsed,
        "cached": bool(ctx.fetches) and all(f.cached for f in ctx.fetches),
        "scholar_requests": sum(f.attempts for f in ctx.fetches),
        "blocked_attempts": sum(f.blocked_attempts for f in ctx.fetches),
        "pages_fetched": len(ctx.fetches),
    }
    if len(ctx.fetches) > 1:
        metadata["google_scholar_urls"] = [f.url for f in ctx.fetches]
    if ctx.warnings:
        metadata["warnings"] = ctx.warnings
    return {
        "search_metadata": metadata,
        "search_parameters": {"engine": engine, **params},
        **data,
    }
