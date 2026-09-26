"""HTTP client for Google Scholar with rate limiting, proxy rotation,
block (CAPTCHA) detection, retries and an in-memory response cache."""

from __future__ import annotations

import asyncio
import itertools
import os
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import httpx

from . import parsers

try:  # Chrome-impersonating HTTP client; plain httpx gets 429'd by Google quickly.
    from curl_cffi import CurlError
    from curl_cffi.requests import AsyncSession as CurlSession
except ImportError:  # pragma: no cover - curl_cffi is a hard dependency, but stay importable
    CurlSession = None
    CurlError = None

SCHOLAR_BASE = parsers.SCHOLAR_BASE

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:129.0) Gecko/20100101 Firefox/129.0",
]


class ScholarError(Exception):
    status_code = 502
    error_type = "upstream_error"
    hint = "Google Scholar could not be reached or returned an error. Check your network / proxies and retry."

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code


class NotFoundError(ScholarError):
    status_code = 404
    error_type = "not_found"
    hint = "Scholar has no page for that id. Check author_id / citation_id / result_id."


class SignInRequiredError(ScholarError):
    status_code = 403
    error_type = "sign_in_required"
    hint = "Google now requires a signed-in Google account for this Scholar page, so it can't be scraped anonymously."


class BlockedError(ScholarError):
    """Google served a CAPTCHA / 'unusual traffic' page on every attempt."""

    status_code = 503
    error_type = "blocked"
    hint = (
        "Google is rate-limiting this IP. Wait a while, raise SCHOLAR_MIN_INTERVAL, "
        "or set SCHOLAR_PROXIES to rotating (ideally residential) proxies. See GET /status."
    )

    def __init__(self, message: str, *, page: str = "", page_url: str = ""):
        super().__init__(message)
        self.page = page  # the CAPTCHA / 'unusual traffic' page Google returned
        self.page_url = page_url


@dataclass
class FetchResult:
    html: str
    url: str
    cached: bool = False
    attempts: int = 0
    blocked_attempts: int = 0
    status_code: Optional[int] = None


@dataclass
class _Slot:
    """One outbound identity: a proxy (or direct), its own cookies and pacing."""

    proxy: Optional[str]
    client: object  # httpx.AsyncClient or curl_cffi AsyncSession
    user_agent: Optional[str]  # None: let curl_cffi send its matching Chrome UA
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float = 0.0
    blocked_until: float = 0.0
    requests: int = 0
    successes: int = 0
    blocks: int = 0
    errors: int = 0
    last_error: Optional[str] = None


class TTLCache:
    def __init__(self, ttl: float, maxsize: int):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[str]:
        entry = self._data.get(key)
        if entry is None or time.monotonic() - entry[0] > self.ttl:
            self._data.pop(key, None)
            self.misses += 1
            return None
        self._data.move_to_end(key)
        self.hits += 1
        return entry[1]

    def __len__(self) -> int:
        return len(self._data)

    def set(self, key: str, value: str) -> None:
        if self.ttl <= 0 or self.maxsize <= 0:
            return
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)


class ScholarClient:
    """Fetches raw Scholar HTML.

    Args:
        proxies: proxy URLs (``http://user:pass@host:port``). Requests rotate
            across them; a proxy that gets blocked is benched for
            ``block_cooldown`` seconds. ``None``/empty means direct.
        min_interval: minimum seconds between two requests on the same proxy.
        jitter: extra random delay (0..jitter seconds) added to each wait.
        max_retries: extra attempts after a block or transient error. After a
            block, a retry only happens if another proxy is not benched;
            retrying the same blocked IP right away just extends the block.
        backend: "curl_cffi" (default when installed) sends requests with a
            real Chrome TLS/HTTP2 fingerprint, which Google blocks far less
            than plain Python clients. "httpx" is the plain fallback.
        impersonate: curl_cffi browser profile, e.g. "chrome", "safari", "edge".
    """

    def __init__(
        self,
        proxies: Optional[list[str]] = None,
        *,
        min_interval: float = 3.0,
        jitter: float = 2.0,
        max_retries: int = 3,
        timeout: float = 20.0,
        block_cooldown: float = 600.0,
        cache_ttl: float = 3600.0,
        cache_size: int = 1024,
        base_url: str = SCHOLAR_BASE,
        backend: str = "auto",
        impersonate: str = "chrome",
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if backend == "auto":
            backend = "httpx" if transport is not None or CurlSession is None else "curl_cffi"
        if backend not in ("curl_cffi", "httpx"):
            raise ValueError(f"backend must be 'curl_cffi' or 'httpx', got {backend!r}")
        if backend == "curl_cffi" and CurlSession is None:
            raise ValueError("backend='curl_cffi' requires the curl_cffi package: pip install curl_cffi")
        if backend == "curl_cffi" and transport is not None:
            raise ValueError("transport= is only supported with backend='httpx'")
        self.backend = backend
        self.impersonate = impersonate
        self.base_url = base_url.rstrip("/")
        self.min_interval = min_interval
        self.jitter = jitter
        self.max_retries = max_retries
        self.block_cooldown = block_cooldown
        self.cache = TTLCache(cache_ttl, cache_size)
        self._slots = [
            self._make_slot(proxy, timeout, transport) for proxy in (proxies or [None])
        ]
        self._cycle = itertools.cycle(range(len(self._slots)))

    @classmethod
    def from_env(cls, **overrides) -> "ScholarClient":
        proxies = [p.strip() for p in os.getenv("SCHOLAR_PROXIES", "").split(",") if p.strip()]
        kwargs = dict(
            proxies=proxies,
            min_interval=float(os.getenv("SCHOLAR_MIN_INTERVAL", "3")),
            jitter=float(os.getenv("SCHOLAR_JITTER", "2")),
            max_retries=int(os.getenv("SCHOLAR_MAX_RETRIES", "3")),
            timeout=float(os.getenv("SCHOLAR_TIMEOUT", "20")),
            block_cooldown=float(os.getenv("SCHOLAR_BLOCK_COOLDOWN", "600")),
            cache_ttl=float(os.getenv("SCHOLAR_CACHE_TTL", "3600")),
            backend=os.getenv("SCHOLAR_HTTP_BACKEND", "auto"),
            impersonate=os.getenv("SCHOLAR_IMPERSONATE", "chrome"),
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    def _make_slot(self, proxy, timeout, transport) -> _Slot:
        if self.backend == "curl_cffi":
            # No custom headers: curl_cffi sends Chrome's own header set, and a
            # UA that disagrees with the TLS fingerprint is itself a red flag.
            session = CurlSession(
                impersonate=self.impersonate, timeout=timeout, allow_redirects=True, proxy=proxy
            )
            return _Slot(proxy=proxy, client=session, user_agent=None)
        kwargs = dict(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if transport is not None:
            kwargs["transport"] = transport
        elif proxy:
            kwargs["proxy"] = proxy
        return _Slot(proxy=proxy, client=httpx.AsyncClient(**kwargs), user_agent=random.choice(USER_AGENTS))

    async def aclose(self) -> None:
        for slot in self._slots:
            if self.backend == "curl_cffi":
                await slot.client.close()
            else:
                await slot.client.aclose()

    def _available_slot(self) -> bool:
        now = time.monotonic()
        return any(slot.blocked_until <= now for slot in self._slots)

    def _next_slot(self) -> _Slot:
        now = time.monotonic()
        for _ in range(len(self._slots)):
            slot = self._slots[next(self._cycle)]
            if slot.blocked_until <= now:
                return slot
        # Everything is benched: use whichever recovers first.
        return min(self._slots, key=lambda s: s.blocked_until)

    async def _wait_turn(self, slot: _Slot) -> None:
        delay = slot.last_request + self.min_interval + random.uniform(0, self.jitter) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        slot.last_request = time.monotonic()

    async def get(self, path: str, params: dict, *, use_cache: bool = True) -> FetchResult:
        """GET ``path`` (relative to Scholar) with ``params``."""
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        url = str(httpx.Request("GET", self.base_url + path, params=params).url)
        return await self.get_url(url, use_cache=use_cache)

    async def get_url(self, url: str, *, use_cache: bool = True) -> FetchResult:
        """GET an absolute URL through the paced / rotated / retried pipeline."""
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return FetchResult(cached, url, cached=True)

        result = FetchResult("", url)
        last_error: Exception = ScholarError("no attempt made")
        for attempt in range(self.max_retries + 1):
            slot = self._next_slot()
            result.attempts += 1
            async with slot.lock:
                await self._wait_turn(slot)
                slot.requests += 1
                headers = {"User-Agent": slot.user_agent} if slot.user_agent else None
                try:
                    resp = await slot.client.get(url, headers=headers)
                except self._network_errors as exc:
                    slot.errors += 1
                    slot.last_error = f"{type(exc).__name__}: {exc}"
                    last_error = ScholarError(f"network error: {exc}")
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue

            html = resp.text
            result.status_code = resp.status_code
            if resp.status_code == 429 or parsers.is_blocked(html, str(resp.url)):
                # Bench this identity, get a fresh cookie jar + UA for later.
                slot.blocks += 1
                slot.last_error = f"blocked (HTTP {resp.status_code})"
                slot.blocked_until = time.monotonic() + self.block_cooldown
                slot.client.cookies.clear()
                if slot.user_agent:
                    slot.user_agent = random.choice(USER_AGENTS)
                result.blocked_attempts += 1
                last_error = BlockedError(
                    f"Google Scholar returned a CAPTCHA / rate-limit page (HTTP {resp.status_code}) "
                    f"on all {result.attempts} attempt(s).",
                    page=html,
                    page_url=str(resp.url),
                )
                if not self._available_slot():
                    break  # every proxy is benched; hammering the same IP extends the block
                continue
            if parsers.is_signin_page(html, str(resp.url)):
                slot.successes += 1  # not a block: retrying won't help
                raise SignInRequiredError(f"Google redirected to a sign-in page: {resp.url}")
            if resp.status_code == 404:
                slot.errors += 1
                raise NotFoundError("Google Scholar returned 404 for this request.")
            if resp.status_code >= 500:
                slot.errors += 1
                slot.last_error = f"HTTP {resp.status_code}"
                last_error = ScholarError(f"Google Scholar returned HTTP {resp.status_code}")
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            if resp.status_code >= 400:
                slot.errors += 1
                raise ScholarError(f"Google Scholar returned HTTP {resp.status_code}")

            slot.successes += 1
            self.cache.set(url, html)
            result.html, result.url = html, url
            return result
        raise last_error

    @property
    def _network_errors(self) -> tuple:
        return (httpx.HTTPError,) + ((CurlError,) if CurlError is not None else ())

    def stats(self) -> dict:
        now = time.monotonic()
        return {
            "http_backend": self.backend,
            "impersonate": self.impersonate if self.backend == "curl_cffi" else None,
            "proxies": [
                {
                    "proxy": _mask(slot.proxy) if slot.proxy else "direct",
                    "requests": slot.requests,
                    "successes": slot.successes,
                    "blocks": slot.blocks,
                    "errors": slot.errors,
                    "benched_for_seconds": max(0, round(slot.blocked_until - now)),
                    "last_error": slot.last_error,
                }
                for slot in self._slots
            ],
            "cache": {
                "entries": len(self.cache),
                "max_entries": self.cache.maxsize,
                "ttl_seconds": self.cache.ttl,
                "hits": self.cache.hits,
                "misses": self.cache.misses,
            },
            "pacing": {
                "min_interval_seconds": self.min_interval,
                "jitter_seconds": self.jitter,
                "max_retries": self.max_retries,
                "block_cooldown_seconds": self.block_cooldown,
            },
        }


def _mask(proxy: str) -> str:
    """Hide credentials: http://user:pass@host:1 -> http://***@host:1"""
    return re.sub(r"//[^@/]+@", "//***@", proxy)
