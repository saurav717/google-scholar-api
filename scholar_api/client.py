"""HTTP client for Google Scholar with rate limiting, proxy rotation,
block (CAPTCHA) detection, retries and an in-memory response cache."""

from __future__ import annotations

import asyncio
import itertools
import os
import random
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

import httpx

from . import parsers

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


class BlockedError(ScholarError):
    """Google served a CAPTCHA / 'unusual traffic' page on every attempt."""

    status_code = 503


@dataclass
class _Slot:
    """One outbound identity: a proxy (or direct), its own cookies and pacing."""

    proxy: Optional[str]
    client: httpx.AsyncClient
    user_agent: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_request: float = 0.0
    blocked_until: float = 0.0


class TTLCache:
    def __init__(self, ttl: float, maxsize: int):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: OrderedDict[str, tuple[float, str]] = OrderedDict()

    def get(self, key: str) -> Optional[str]:
        entry = self._data.get(key)
        if entry is None:
            return None
        stored, value = entry
        if time.monotonic() - stored > self.ttl:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

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
        max_retries: extra attempts after a block or transient error.
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
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
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
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    def _make_slot(self, proxy, timeout, transport) -> _Slot:
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
            await slot.client.aclose()

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

    async def get(self, path: str, params: dict, *, use_cache: bool = True) -> tuple[str, str]:
        """GET ``path`` with ``params``; returns ``(html, final_url)``."""
        params = {k: v for k, v in params.items() if v is not None and v != ""}
        request = httpx.Request("GET", self.base_url + path, params=params)
        url = str(request.url)
        if use_cache:
            cached = self.cache.get(url)
            if cached is not None:
                return cached, url

        last_error: Exception = ScholarError("no attempt made")
        for attempt in range(self.max_retries + 1):
            slot = self._next_slot()
            async with slot.lock:
                await self._wait_turn(slot)
                try:
                    resp = await slot.client.get(url, headers={"User-Agent": slot.user_agent})
                except httpx.HTTPError as exc:
                    last_error = ScholarError(f"network error: {exc}")
                    await asyncio.sleep(min(2 ** attempt, 10))
                    continue

            html = resp.text
            if resp.status_code == 429 or parsers.is_blocked(html, str(resp.url)):
                # Bench this identity, get a fresh cookie jar + UA for later.
                slot.blocked_until = time.monotonic() + self.block_cooldown
                slot.client.cookies.clear()
                slot.user_agent = random.choice(USER_AGENTS)
                last_error = BlockedError(
                    "Google Scholar returned a CAPTCHA / rate-limit page. "
                    "Slow down (SCHOLAR_MIN_INTERVAL) or configure SCHOLAR_PROXIES."
                )
                continue
            if resp.status_code == 404:
                raise ScholarError("Google Scholar returned 404 (unknown id?)")
            if resp.status_code >= 500:
                last_error = ScholarError(f"Google Scholar returned HTTP {resp.status_code}")
                await asyncio.sleep(min(2 ** attempt, 10))
                continue
            if resp.status_code >= 400:
                raise ScholarError(f"Google Scholar returned HTTP {resp.status_code}")

            self.cache.set(url, html)
            return html, url
        raise last_error
