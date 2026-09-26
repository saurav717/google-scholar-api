"""Self-hosted CAPTCHA solving with a human in the loop.

When Google blocks a request, open the block page in a real browser window
(through the same proxy as the blocked request), let the person at the
machine solve it, then hand the resulting GOOGLE_ABUSE_EXEMPTION cookie back
to the scraper. No paid service, and Google's check is answered by a person.

Needs Playwright: pip install -e ".[solver]" && playwright install chromium
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Optional
from urllib.parse import unquote, urlsplit

from . import parsers
from .captcha import CaptchaError, Challenge
from .cookies import ALLOWED_BROWSER_COOKIES

EXEMPTION_COOKIE = "GOOGLE_ABUSE_EXEMPTION"


class BrowserSolver:
    """Opens block pages in a browser window for a person to solve.

    Args:
        timeout: seconds to wait for the person before giving up.
        max_solves: browser windows to open per Scholar request.
        headless: only for tests; a person can't solve a hidden window.
        channel: Playwright browser channel. "chrome" uses your installed
            Google Chrome, which Google challenges less than bundled Chromium;
            falls back to bundled Chromium when Chrome isn't installed.
        executable_path: explicit browser binary (overrides ``channel``).
        reuse_window: seconds after a solve during which other requests on the
            same connection reuse its cookies instead of opening a new window.
    """

    provider = "browser"

    def __init__(
        self,
        *,
        timeout: float = 300.0,
        max_solves: int = 1,
        headless: bool = False,
        channel: Optional[str] = "chrome",
        executable_path: Optional[str] = None,
        reuse_window: float = 60.0,
    ):
        self.timeout = timeout
        self.max_solves = max_solves
        self.headless = headless
        self.channel = channel
        self.executable_path = executable_path
        self.reuse_window = reuse_window
        self._lock = asyncio.Lock()  # one window at a time
        self._recent: dict[str, tuple[float, list[dict]]] = {}  # slot key -> (solved at, cookies)
        self.waiting_since: Optional[float] = None
        self.waiting_url: Optional[str] = None
        self.attempts = 0
        self.solved = 0
        self.failed = 0
        self.rejected = 0
        self.solve_seconds = 0.0
        self.last_error: Optional[str] = None

    async def aclose(self) -> None:
        pass  # a browser only lives for the duration of one solve

    async def clear(self, challenge: Challenge, *, key: str, blocked_at: float, proxy: Optional[str] = None,
                    user_agent: Optional[str] = None, cookies: Optional[list[dict]] = None) -> list[dict]:
        """Have a person solve ``challenge``; return the cookies Google set
        (NID / GSP / GOOGLE_ABUSE_EXEMPTION only). Raises CaptchaError."""
        async with self._lock:
            recent = self._recent.get(key)
            if recent and recent[0] >= blocked_at - self.reuse_window:
                return recent[1]  # someone just solved one for this connection
            self.attempts += 1
            started = time.monotonic()
            self.waiting_since, self.waiting_url = time.time(), challenge.page_url
            try:
                result = await self._solve_in_browser(challenge, proxy, user_agent, cookies or [])
            except CaptchaError as exc:
                self.failed += 1
                self.last_error = str(exc)
                raise
            finally:
                self.waiting_since = self.waiting_url = None
                self.solve_seconds += time.monotonic() - started
            self.solved += 1
            self._recent[key] = (time.monotonic(), result)
            return result

    async def _solve_in_browser(self, challenge: Challenge, proxy, user_agent, cookies) -> list[dict]:
        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError:
            raise CaptchaError(
                'The browser solver needs Playwright: pip install -e ".[solver]" && playwright install chromium'
            ) from None

        if not self.headless and not _has_display():
            raise CaptchaError(
                "browser: no display to open a window on (DISPLAY/WAYLAND_DISPLAY unset). The browser "
                "solver needs a desktop session; on a headless server use a paid provider instead."
            )
        _notify(f"Google is showing a CAPTCHA. Solve it in the browser window that just opened "
                f"(waiting up to {self.timeout:.0f}s): {challenge.page_url}")
        try:
            async with async_playwright() as pw:
                browser = await self._launch(pw, proxy)
                try:
                    context = await browser.new_context(user_agent=user_agent) if user_agent else await browser.new_context()
                    seed = [_to_playwright(c) for c in cookies]
                    if seed:
                        await context.add_cookies([c for c in seed if c])
                    page = await context.new_page()
                    await page.goto(challenge.page_url, wait_until="domcontentloaded")
                    await page.bring_to_front()
                    return await self._wait_for_person(page, context)
                finally:
                    await browser.close()
        except PlaywrightError as exc:
            message = str(exc).splitlines()[0]
            raise CaptchaError(f"browser: {message}") from None

    async def _launch(self, pw, proxy):
        kwargs: dict = {"headless": self.headless}
        if proxy:
            kwargs["proxy"] = _playwright_proxy(proxy)
        if self.executable_path:
            return await pw.chromium.launch(executable_path=self.executable_path, **kwargs)
        if self.channel:
            try:
                return await pw.chromium.launch(channel=self.channel, **kwargs)
            except Exception:
                pass  # channel not installed: use Playwright's own Chromium
        return await pw.chromium.launch(**kwargs)

    async def _wait_for_person(self, page, context) -> list[dict]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if page.is_closed():
                raise CaptchaError("browser: the window was closed before the CAPTCHA was solved")
            jar = await context.cookies()
            if any(c["name"] == EXEMPTION_COOKIE for c in jar) or await _left_block_page(page):
                return [
                    {"name": c["name"], "value": c["value"], "domain": c["domain"], "path": c.get("path") or "/",
                     "expires": c["expires"] if c.get("expires", -1) > 0 else None}
                    for c in jar if c["name"] in ALLOWED_BROWSER_COOKIES
                ]
            await asyncio.sleep(0.5)
        raise CaptchaError(f"browser: nobody solved the CAPTCHA within {self.timeout:.0f}s")

    def stats(self) -> dict:
        return {
            "provider": self.provider,
            "waiting_for_person": self.waiting_since is not None,
            "waiting_seconds": round(time.time() - self.waiting_since) if self.waiting_since else None,
            "waiting_url": self.waiting_url,
            "attempts": self.attempts,
            "solved": self.solved,
            "failed": self.failed,
            "rejected_by_google": self.rejected,
            "avg_solve_seconds": round(self.solve_seconds / self.attempts, 1) if self.attempts else None,
            "last_error": self.last_error,
            "max_solves_per_request": self.max_solves,
            "timeout_seconds": self.timeout,
        }


async def _left_block_page(page) -> bool:
    """True once the window shows a normal page again (the person solved it)."""
    try:
        return not parsers.is_blocked(await page.content(), page.url)
    except Exception:
        return False  # mid-navigation


def _has_display() -> bool:
    if not sys.platform.startswith("linux"):
        return True  # macOS / Windows always have a window server for a desktop user
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _notify(message: str) -> None:
    print(f"\a[scholar-api] {message}", file=sys.stderr, flush=True)


def _playwright_proxy(proxy: str) -> dict:
    parts = urlsplit(proxy)
    server = f"{parts.scheme or 'http'}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    out = {"server": server}
    if parts.username:
        out["username"] = unquote(parts.username)
    if parts.password:
        out["password"] = unquote(parts.password)
    return out


def _to_playwright(cookie: dict) -> Optional[dict]:
    if not cookie.get("domain") or not cookie.get("value"):
        return None
    out = {"name": cookie["name"], "value": cookie["value"], "domain": cookie["domain"], "path": cookie.get("path") or "/"}
    if cookie.get("expires"):
        out["expires"] = float(cookie["expires"])
    return out
