"""FastAPI server exposing a SerpAPI-compatible `/search.json` endpoint."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from typing import Optional
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import engines
from .client import ScholarClient, ScholarError


def create_app(client: Optional[ScholarClient] = None, api_key: Optional[str] = None) -> FastAPI:
    api_key = api_key if api_key is not None else os.getenv("SCHOLAR_API_KEY") or None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = client or ScholarClient.from_env()
        yield
        await app.state.client.aclose()

    app = FastAPI(
        title="Scholar API",
        description="Self-hosted Google Scholar scraper with SerpAPI-compatible output.",
        version="0.1.0",
        lifespan=lifespan,
    )

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    @app.get("/")
    async def index():
        return {"status": "ok", "engines": list(engines.ENGINES), "endpoint": "/search.json"}

    @app.get("/search")
    @app.get("/search.json")
    async def search(request: Request):
        params = dict(request.query_params)
        if api_key and not secrets.compare_digest(params.pop("api_key", ""), api_key):
            return error(401, "Invalid API key.")
        params.pop("api_key", None)
        params.pop("output", None)
        engine = params.pop("engine", "google_scholar")
        use_cache = params.pop("no_cache", "false").lower() not in ("true", "1")
        base = str(request.base_url).rstrip("/")

        def api_link(engine_name: str, **link_params) -> str:
            query = {"engine": engine_name, **{k: v for k, v in link_params.items() if v not in (None, "")}}
            return f"{base}/search.json?{urlencode(query)}"

        try:
            return await engines.run(engine, request.app.state.client, params, api_link, use_cache)
        except engines.ParamError as exc:
            return error(exc.status_code, str(exc))
        except ScholarError as exc:
            return error(exc.status_code, str(exc))

    return app


app = create_app()
