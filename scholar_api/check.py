"""`scholar-api check`: hit live Google Scholar with every engine and report,
field by field, what was parsed. Saves the raw HTML of each page so a broken
selector can be fixed against the real markup."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from . import engines
from .client import ScholarClient, ScholarError

OK, MISSING = "ok", "MISSING"


def _get(data: Any, path: str) -> Any:
    for part in path.split("."):
        if isinstance(data, list):
            data = data[int(part)] if part.isdigit() and int(part) < len(data) else None
        elif isinstance(data, dict):
            data = data.get(part)
        else:
            return None
    return data


# (label, path into the response, required?)  Optional fields are often
# legitimately absent (e.g. no PDF for a paper), so they only get a note.
CHECKS: dict[str, list[tuple[str, str, bool]]] = {
    "google_scholar": [
        ("total results", "search_information.total_results", True),
        ("results parsed", "organic_results.0", True),
        ("title", "organic_results.0.title", True),
        ("result_id", "organic_results.0.result_id", True),
        ("link", "organic_results.0.link", True),
        ("snippet", "organic_results.0.snippet", True),
        ("publication summary", "organic_results.0.publication_info.summary", True),
        ("author names", "organic_results.0.publication_info.author_names", True),
        ("year", "organic_results.0.publication_info.year", True),
        ("linked authors", "organic_results.0.publication_info.authors", False),
        ("cited by", "organic_results.0.inline_links.cited_by.total", True),
        ("cites_id", "organic_results.0.inline_links.cited_by.cites_id", True),
        ("versions", "organic_results.0.inline_links.versions.cluster_id", True),
        ("related", "organic_results.0.inline_links.related_pages_link", True),
        ("pdf/html resource", "organic_results.0.resources", False),
        ("next page", "serpapi_pagination.next", True),
    ],
    "google_scholar_cite": [
        ("citation styles", "citations.0.snippet", True),
        ("export links", "links.0.link", True),
        ("bibtex parsed", "bibtex.parsed.fields.title", True),
    ],
    "google_scholar_author": [
        ("name", "author.name", True),
        ("affiliation", "author.affiliations", True),
        ("email", "author.email", False),
        ("interests", "author.interests.0.title", False),
        ("citations table", "cited_by.table.0.citations.all", True),
        ("h-index", "cited_by.table.1.h_index.all", True),
        ("citation graph", "cited_by.graph.0.year", True),
        ("articles", "articles.0.title", True),
        ("article citation_id", "articles.0.citation_id", True),
        ("article year", "articles.0.year", True),
        ("article cited by", "articles.0.cited_by.value", True),
        ("co-authors", "co_authors.0.name", False),
        ("public access", "public_access.available", False),
    ],
    "article (view_citation)": [
        ("title", "citation.title", True),
        ("authors", "citation.authors", True),
        ("publication date", "citation.publication_date", True),
        ("total citations", "citation.total_citations.value", True),
        ("citations per year", "citation.total_citations.graph.0.year", False),
    ],
    "google_scholar_profiles": [
        ("profiles", "profiles.0.name", True),
        ("author_id", "profiles.0.author_id", True),
        ("found via", "profiles.0.source", True),
        ("profile box (optional)", "profiles.0.affiliations", False),
        ("cited by (box only)", "profiles.0.cited_by", False),
    ],
}


def _report(name: str, data: dict) -> tuple[int, int]:
    passed = failed = 0
    for label, path, required in CHECKS[name]:
        value = _get(data, path)
        present = value not in (None, "", [], {})
        mark = OK if present else (MISSING if required else "absent (optional)")
        shown = json.dumps(value, ensure_ascii=False)[:70] if present else ""
        print(f"    {mark:<18} {label:<22} {shown}")
        if required:
            passed += present
            failed += not present
    for warning in data.get("search_metadata", {}).get("warnings", []):
        print(f"    warning: {warning}")
    return passed, failed


async def run_check(query: str, author_id: str, profile_query: str, save_dir: Path, client: ScholarClient) -> int:
    save_dir.mkdir(parents=True, exist_ok=True)
    total_pass = total_fail = 0
    context: dict = {}

    steps: list[tuple[str, str, Callable[[], dict]]] = [
        ("google_scholar", "google_scholar", lambda: {"q": query}),
        ("google_scholar_cite", "google_scholar_cite",
         lambda: {"q": context.get("result_id"), "include_bibtex": "true"}),
        ("google_scholar_author", "google_scholar_author", lambda: {"author_id": author_id}),
        ("article (view_citation)", "google_scholar_author",
         lambda: {"view_op": "view_citation", "citation_id": context.get("citation_id")}),
        ("google_scholar_profiles", "google_scholar_profiles", lambda: {"mauthors": profile_query}),
    ]

    def api_link(engine, **params):  # makes serpapi_* fields appear like on the server
        return "check://" + engine

    for i, (name, engine, make_params) in enumerate(steps, 1):
        params = make_params()
        print(f"\n[{i}/{len(steps)}] {name}  {params}")
        if any(v is None for v in params.values()):
            print("    skipped: depends on a previous step that failed")
            total_fail += 1
            continue
        try:
            data = await engines.run(engine, client, params, api_link, use_cache=False)
        except (ScholarError, engines.ParamError) as exc:
            print(f"    FAILED: {exc}")
            if getattr(exc, "hint", None):
                print(f"    hint: {exc.hint}")
            total_fail += 1
            kind = getattr(exc, "error_type", "")
            if kind == "blocked" and getattr(exc, "page", ""):
                (save_dir / "blocked.html").write_text(exc.page)
                print(f"    blocked page: {exc.page_url}")
                print(f"    saved to {save_dir / 'blocked.html'}")
            if kind == "blocked":
                print("\nGoogle is blocking this IP; stopping early. Try again later or configure SCHOLAR_PROXIES.")
                break
            if kind == "upstream_error":
                print("\nCannot reach Google Scholar; stopping early. Check your internet connection / proxies.")
                break
            continue

        url = data["search_metadata"]["google_scholar_url"]
        cached = client.cache.get(url)
        slug = name.split(" ")[0]
        if cached:
            (save_dir / f"{slug}.html").write_text(cached)
        (save_dir / f"{slug}.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
        print(f"    scholar url: {url}")
        p, f = _report(name, data)
        total_pass += p
        total_fail += f

        if engine == "google_scholar":
            context["result_id"] = _get(data, "organic_results.0.result_id")
        if name == "google_scholar_author":
            context["citation_id"] = _get(data, "articles.0.citation_id")

    print(f"\nHTTP backend: {client.backend}" + (f" (impersonating {client.impersonate})" if client.backend == "curl_cffi" else ""))
    session = client.stats()["session"]
    cookies = sorted({name for slot in client.stats()["proxies"] for name in slot["cookies"]})
    print(f"Cookies held: {', '.join(cookies) or 'none'}"
          + (f" (saved to {session['cookie_file']})" if session["cookie_file"] else " (not persisted)"))
    if session["browser_cookies"]:
        print(f"Using your browser cookies: {', '.join(session['browser_cookies'])}")
    if session["ignored_browser_cookies"]:
        print(f"Ignored browser cookies (not needed / sign-in): {', '.join(session['ignored_browser_cookies'])}")
    print(f"{total_pass} required fields ok, {total_fail} missing/failed.")
    print(f"Raw HTML and JSON saved in {save_dir.resolve()}")
    if total_fail:
        print("If fields are MISSING, copy the matching .html into tests/fixtures/ and fix the selector in scholar_api/parsers.py.")
    return 1 if total_fail else 0


def main(args) -> int:
    async def _run() -> int:
        client = ScholarClient.from_env()
        try:
            return await run_check(args.query, args.author_id, args.profiles, Path(args.save_dir), client)
        finally:
            await client.aclose()

    return asyncio.run(_run())
