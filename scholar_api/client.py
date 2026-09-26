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
from urllib.parse import urlsplit

import httpx

from . import parsers
from .browser_solver import BrowserSolver
from .captcha import CaptchaError, CaptchaSolver, Challenge, parse_challenge
from .cookies import (
    BrowserCookieError,
    CookieStore,
    default_cookie_file,
    export_jar,
    import_cookies,
    parse_browser_cookies,
    read_browser_cookies,
    slot_key,
)

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
        "set SCHOLAR_PROXIES to rotating (ideally residential) proxies, or set "
        "SCHOLAR_CAPTCHA_PROVIDER to solve CAPTCHAs automatically. See GET /status."
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
    captcha_attempts: int = 0
    captchas_solved: int = 0
    status_code: Optional[int] = None


@dataclass
class _Slot:
    """One outbound identity: a proxy (or direct), its own cookies and pacing."""

    proxy: Optional[str]
    client: object  # httpx.AsyncClient or curl_cffi AsyncSession
    user_agent: Optional[str]  # None: let curl_cffi send its matching Chrome UA
    key: str = "direct"
    warmed: bool = False
    warmups: int = 0
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
        cookie_file: JSON file where Google's cookies are kept between runs, so
            the scraper looks like a returning visitor. None = don't persist.
        browser_cookies: a Cookie header copied from your browser. Only NID,
            GSP and GOOGLE_ABUSE_EXEMPTION are used; everything else (including
            Google sign-in cookies) is dropped.
        browser: read those same cookies automatically from a local browser's
            cookie store ("chrome", "firefox", "safari", ..., or "auto").
        warmup: visit the Scholar homepage once per connection before the first
            query, like a person opening the site. Defaults to on, except when a
            test ``transport`` is given.
        captcha_solver: what to do when Google serves a CAPTCHA page instead of
            benching the proxy. A CaptchaSolver sends it to a paid solving
            service and submits the returned token on the same connection; a
            BrowserSolver opens it in a browser window for a person to solve.
            Either way Google hands back a GOOGLE_ABUSE_EXEMPTION cookie that
            later requests reuse. None = don't solve.
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
        cookie_file: Optional[str] = None,
        browser_cookies: Optional[str] = None,
        browser: Optional[str] = None,
        warmup: Optional[bool] = None,
        captcha_solver: Optional[CaptchaSolver | BrowserSolver] = None,
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
        self.warmup = (transport is None) if warmup is None else warmup
        self.solver = captcha_solver
        self.cookie_store = CookieStore(cookie_file) if cookie_file else None
        self._saved_cookies = self.cookie_store.load() if self.cookie_store else {}
        self.browser_cookies, self.ignored_browser_cookies = parse_browser_cookies(browser_cookies or "")
        self.browser = browser
        self.browser_import_error: Optional[str] = None
        if browser:
            try:
                from_browser, ignored = read_browser_cookies(browser)
            except BrowserCookieError as exc:
                self.browser_import_error = str(exc)
            else:
                pasted = {c["name"] for c in self.browser_cookies}
                self.browser_cookies += [c for c in from_browser if c["name"] not in pasted]
                self.ignored_browser_cookies += [n for n in ignored if n not in self.ignored_browser_cookies]
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
            cookie_file=_cookie_file_from_env(),
            browser_cookies=os.getenv("SCHOLAR_COOKIES") or None,
            browser=os.getenv("SCHOLAR_BROWSER") or None,
            warmup=os.getenv("SCHOLAR_WARMUP", "1").lower() not in ("0", "false", "no", "off"),
            captcha_solver=_solver_from_env(),
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    def _make_slot(self, proxy, timeout, transport) -> _Slot:
        slot = self._new_slot(proxy, timeout, transport)
        slot.key = slot_key(proxy)
        slot.warmed = not self.warmup
        import_cookies(slot.client.cookies, self._saved_cookies.get(slot.key, []))
        import_cookies(slot.client.cookies, self.browser_cookies)  # explicit cookies win
        return slot

    def _new_slot(self, proxy, timeout, transport) -> _Slot:
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
        if self.solver is not None:
            await self.solver.aclose()

    def _save_cookies(self) -> None:
        if self.cookie_store is None:
            return
        slots = dict(self._saved_cookies)
        for slot in self._slots:
            slots[slot.key] = export_jar(slot.client.cookies)
        self._saved_cookies = slots
        try:
            self.cookie_store.save(slots)
        except OSError:
            pass  # persistence is best-effort

    async def _warm_up(self, slot: _Slot) -> None:
        """Open the Scholar homepage once, like a person would, to collect
        cookies before the first query. Failures are ignored: a block here
        shows up on the real request."""
        slot.warmed = True
        await self._wait_turn(slot)
        slot.requests += 1
        slot.warmups += 1
        headers = {"User-Agent": slot.user_agent} if slot.user_agent else None
        try:
            await slot.client.get(self.base_url + "/?hl=en", headers=headers)
        except self._network_errors:
            pass

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
                if not slot.warmed:
                    await self._warm_up(slot)
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
            blocked = resp.status_code == 429 or parsers.is_blocked(html, str(resp.url))
            if blocked and self.solver is not None and result.captcha_attempts < self.solver.max_solves:
                solved = await self._solve_block(slot, html, str(resp.url), url, result)
                if solved is not None:
                    resp, html = solved, solved.text
                    blocked = resp.status_code == 429 or parsers.is_blocked(html, str(resp.url))
                    if blocked:
                        self.solver.rejected += 1
                        self.solver.last_error = "Google rejected the solved token"
                    else:
                        result.captchas_solved += 1
            result.status_code = resp.status_code
            if blocked:
                # Bench this identity, get a fresh cookie jar + UA for later.
                slot.blocks += 1
                slot.last_error = f"blocked (HTTP {resp.status_code})"
                slot.blocked_until = time.monotonic() + self.block_cooldown
                slot.client.cookies.clear()
                import_cookies(slot.client.cookies, self.browser_cookies)
                self._save_cookies()
                if slot.user_agent:
                    slot.user_agent = random.choice(USER_AGENTS)
                result.blocked_attempts += 1
                solver_note = ""
                if self.solver is not None and result.captcha_attempts:
                    solver_note = f" CAPTCHA solving failed: {self.solver.last_error}."
                last_error = BlockedError(
                    f"Google Scholar returned a CAPTCHA / rate-limit page (HTTP {resp.status_code}) "
                    f"on all {result.attempts} attempt(s).{solver_note}",
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
            self._save_cookies()
            self.cache.set(url, html)
            result.html, result.url = html, url
            return result
        raise last_error

    async def _solve_block(self, slot: _Slot, html: str, page_url: str, url: str, result: FetchResult):
        """Solve the CAPTCHA on a block page and submit it on ``slot``'s connection.
        Returns the response for ``url`` afterwards, or None if the page has no
        solvable widget or the service failed."""
        challenge = parse_challenge(html, page_url)
        if isinstance(self.solver, BrowserSolver):
            # A person can deal with any block page, widget or not.
            challenge = challenge or Challenge(kind="unknown", sitekey="", page_url=page_url)
            result.captcha_attempts += 1
            return await self._clear_in_browser(slot, challenge, url, result)
        if challenge is None:
            return None  # e.g. a bare HTTP 429: nothing to solve
        result.captcha_attempts += 1
        async with slot.lock:
            try:
                token = await self.solver.solve(challenge, proxy=slot.proxy, user_agent=slot.user_agent)
            except CaptchaError:
                return None
            form = {**challenge.fields, **{name: token for name in challenge.response_fields}}
            headers = {"Referer": page_url}
            if slot.user_agent:
                headers["User-Agent"] = slot.user_agent
            try:
                slot.requests += 1
                result.attempts += 1
                if challenge.method == "GET":
                    resp = await slot.client.get(challenge.submit_url, params=form, headers=headers)
                else:
                    resp = await slot.client.post(challenge.submit_url, data=form, headers=headers)
                if parsers.is_blocked(resp.text, str(resp.url)) or resp.status_code == 429:
                    return resp
                if urlsplit(str(resp.url)).path != urlsplit(url).path:
                    # The form didn't redirect back to the page: ask for it again.
                    await self._wait_turn(slot)
                    slot.requests += 1
                    result.attempts += 1
                    headers.pop("Referer")
                    resp = await slot.client.get(url, headers=headers or None)
            except self._network_errors as exc:
                slot.errors += 1
                slot.last_error = f"{type(exc).__name__}: {exc}"
                return None
            return resp

    async def _clear_in_browser(self, slot: _Slot, challenge: Challenge, url: str, result: FetchResult):
        """Have a person solve the block page in a browser, copy the cookies
        Google set into ``slot`` and fetch ``url`` again."""
        blocked_at = time.monotonic()
        async with slot.lock:  # nothing else goes out on this IP while the person solves
            try:
                cookies = await self.solver.clear(
                    challenge, key=slot.key, blocked_at=blocked_at, proxy=slot.proxy,
                    user_agent=slot.user_agent, cookies=export_jar(slot.client.cookies),
                )
            except CaptchaError:
                return None
            import_cookies(slot.client.cookies, cookies)
            await self._wait_turn(slot)
            slot.requests += 1
            result.attempts += 1
            headers = {"User-Agent": slot.user_agent} if slot.user_agent else None
            try:
                return await slot.client.get(url, headers=headers)
            except self._network_errors as exc:
                slot.errors += 1
                slot.last_error = f"{type(exc).__name__}: {exc}"
                return None

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
                    "warmed_up": slot.warmed,
                    "warmup_requests": slot.warmups,
                    "cookies": sorted({c.name for c in slot.client.cookies.jar}),
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
            "session": {
                "cookie_file": str(self.cookie_store.path) if self.cookie_store else None,
                "warmup": self.warmup,
                "browser": self.browser,
                "browser_import_error": self.browser_import_error,
                "browser_cookies": [c["name"] for c in self.browser_cookies],
                "ignored_browser_cookies": self.ignored_browser_cookies,
            },
            "captcha": {"enabled": True, **self.solver.stats()} if self.solver else {"enabled": False},
            "pacing": {
                "min_interval_seconds": self.min_interval,
                "jitter_seconds": self.jitter,
                "max_retries": self.max_retries,
                "block_cooldown_seconds": self.block_cooldown,
            },
        }


def _cookie_file_from_env() -> Optional[str]:
    value = os.getenv("SCHOLAR_COOKIE_FILE")
    if value is None:
        return str(default_cookie_file())
    return None if value.strip().lower() in ("", "0", "none", "off") else value


def _solver_from_env() -> Optional[CaptchaSolver | BrowserSolver]:
    provider = os.getenv("SCHOLAR_CAPTCHA_PROVIDER", "").strip()
    if not provider or provider.lower() in ("none", "off", "0"):
        return None
    if provider.lower() == "browser":
        return BrowserSolver(
            timeout=float(os.getenv("SCHOLAR_CAPTCHA_TIMEOUT", "300")),
            max_solves=int(os.getenv("SCHOLAR_CAPTCHA_MAX_SOLVES", "1")),
            channel=os.getenv("SCHOLAR_CAPTCHA_BROWSER_CHANNEL", "chrome") or None,
            executable_path=os.getenv("SCHOLAR_CAPTCHA_BROWSER_PATH") or None,
        )
    api_key = os.getenv("SCHOLAR_CAPTCHA_API_KEY", "").strip()
    if not api_key:
        raise ValueError("SCHOLAR_CAPTCHA_PROVIDER is set but SCHOLAR_CAPTCHA_API_KEY is empty")
    return CaptchaSolver(
        provider,
        api_key,
        timeout=float(os.getenv("SCHOLAR_CAPTCHA_TIMEOUT", "180")),
        poll_interval=float(os.getenv("SCHOLAR_CAPTCHA_POLL_INTERVAL", "5")),
        max_solves=int(os.getenv("SCHOLAR_CAPTCHA_MAX_SOLVES", "1")),
        use_proxy=os.getenv("SCHOLAR_CAPTCHA_USE_PROXY", "1").lower() not in ("0", "false", "no", "off"),
        api_url=os.getenv("SCHOLAR_CAPTCHA_API_URL") or None,
    )


def _mask(proxy: str) -> str:
    """Hide credentials: http://user:pass@host:1 -> http://***@host:1"""
    return re.sub(r"//[^@/]+@", "//***@", proxy)
