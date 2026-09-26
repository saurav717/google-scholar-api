"""FastAPI server exposing a SerpAPI-compatible `/search.json` endpoint."""

from __future__ import annotations

import os
import secrets
import time
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, engines
from .client import ScholarClient, ScholarError


def _openapi_parameters() -> list[dict]:
    """Every engine's params as OpenAPI query params, so /docs can 'Try it out'."""
    seen: dict[str, dict] = {}
    for name, spec in engines.COMMON_PARAMS.items():
        seen[name] = {"description": spec["description"], "engines": ["all"], "example": spec.get("example")}
    for engine, doc in engines.ENGINE_DOCS.items():
        for name, spec in doc["params"].items():
            entry = seen.setdefault(name, {"description": "", "engines": [], "example": spec.get("example")})
            entry["engines"].append(engine)
            entry["description"] += f"[{engine}] {spec['description']} "
    params = []
    for name, entry in seen.items():
        schema: dict = {"type": "string"}
        if name == "engine":
            schema = {"type": "string", "enum": list(engines.ENGINES), "default": "google_scholar"}
        params.append({
            "name": name,
            "in": "query",
            "required": False,
            "description": entry["description"].strip(),
            "schema": schema,
        })
    return params


def _engine_catalog(base: str) -> dict:
    catalog = {}
    for engine, doc in engines.ENGINE_DOCS.items():
        example = {"engine": engine, **{k: v["example"] for k, v in doc["params"].items() if v.get("example") and k not in ("cites", "cluster", "citation_id", "start", "num", "max_pages", "all_articles")}}
        catalog[engine] = {
            **doc,
            "example": f"{base}/search.json?{urlencode(example)}",
        }
    return {"common_params": engines.COMMON_PARAMS, "engines": catalog}


def create_app(client: Optional[ScholarClient] = None, api_key: Optional[str] = None) -> FastAPI:
    api_key = api_key if api_key is not None else os.getenv("SCHOLAR_API_KEY") or None
    started = time.time()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = client or ScholarClient.from_env()
        yield
        await app.state.client.aclose()

    app = FastAPI(
        title="Scholar API",
        description=(
            "Self-hosted Google Scholar scraper with SerpAPI-compatible output.\n\n"
            "Everything goes through **GET /search.json** with an `engine` parameter. "
            "GET /engines lists every engine and parameter; GET /status shows proxy, "
            "cache and block statistics."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    def error(status: int, message: str, error_type: str, hint: Optional[str] = None) -> JSONResponse:
        body = {"error": message, "error_type": error_type}
        if hint:
            body["hint"] = hint
        return JSONResponse(body, status_code=status)

    def base_url(request: Request) -> str:
        return str(request.base_url).rstrip("/")

    @app.get("/", summary="Service info and quick links")
    async def index(request: Request):
        base = base_url(request)
        return {
            "service": "scholar-api",
            "version": __version__,
            "status": "ok",
            "auth_required": bool(api_key),
            "engines": list(engines.ENGINES),
            "links": {
                "search": f"{base}/search.json?engine=google_scholar&q=attention+is+all+you+need",
                "engines": f"{base}/engines",
                "status": f"{base}/status",
                "interactive_docs": f"{base}/docs",
            },
        }

    @app.get("/engines", summary="Every engine, its parameters and an example call")
    async def list_engines(request: Request):
        return _engine_catalog(base_url(request))

    @app.get("/status", summary="Proxy health, block counts, cache stats and pacing config")
    async def status(request: Request):
        return {
            "uptime_seconds": round(time.time() - started),
            **request.app.state.client.stats(),
        }

    @app.get(
        "/search.json",
        summary="Run a Google Scholar engine (SerpAPI-compatible)",
        description="Parameters depend on `engine`; each one is tagged with the engines that use it. See GET /engines.",
        openapi_extra={"parameters": _openapi_parameters()},
    )
    @app.get("/search", include_in_schema=False)
    async def search(request: Request):
        params = dict(request.query_params)
        supplied_key = params.pop("api_key", "")
        if api_key and not secrets.compare_digest(supplied_key, api_key):
            return error(401, "Invalid or missing API key.", "unauthorized", "Pass api_key=<SCHOLAR_API_KEY>.")
        params.pop("output", None)
        engine = params.pop("engine", None) or "google_scholar"
        use_cache = params.pop("no_cache", "false").lower() not in ("true", "1")
        base = base_url(request)

        def api_link(engine_name: str, **link_params) -> str:
            query = {"engine": engine_name, **{k: v for k, v in link_params.items() if v not in (None, "")}}
            return f"{base}/search.json?{urlencode(query)}"

        try:
            return await engines.run(engine, request.app.state.client, params, api_link, use_cache)
        except engines.ParamError as exc:
            return error(exc.status_code, str(exc), exc.error_type, exc.hint)
        except ScholarError as exc:
            return error(exc.status_code, str(exc), exc.error_type, exc.hint)

    return app


app = create_app()
