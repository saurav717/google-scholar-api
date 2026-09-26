"""Solve Google's reCAPTCHA / hCaptcha block pages through a paid solving service.

2Captcha, CapSolver, Anti-Captcha and CapMonster Cloud all speak the same
``createTask`` / ``getTaskResult`` JSON protocol, so one client covers them:

1. parse the block page: widget type, site key, ``data-s`` and the form to submit
2. ``createTask`` with those details, then poll ``getTaskResult`` until ready
3. the caller submits the returned token in the page's form (see ScholarClient)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


class CaptchaError(Exception):
    """The solving service failed, timed out or rejected the task."""


@dataclass
class Challenge:
    """Everything needed to solve a block page and submit the answer."""

    kind: str  # "recaptcha_v2" or "hcaptcha"
    sitekey: str
    page_url: str
    data_s: Optional[str] = None  # Google /sorry/ pages bind the token to this value
    invisible: bool = False
    submit_url: str = ""
    method: str = "POST"
    fields: dict[str, str] = field(default_factory=dict)  # hidden form inputs

    @property
    def response_fields(self) -> list[str]:
        if self.kind == "hcaptcha":
            return ["h-captcha-response", "g-recaptcha-response"]
        return ["g-recaptcha-response"]


def parse_challenge(html: str, page_url: str) -> Optional[Challenge]:
    """Find a reCAPTCHA v2 / hCaptcha widget in a block page. None if there isn't one
    (e.g. a bare HTTP 429), in which case solving can't help."""
    soup = BeautifulSoup(html, "lxml")
    widget = soup.select_one(".g-recaptcha[data-sitekey], #recaptcha[data-sitekey]")
    kind = "recaptcha_v2"
    if widget is None:
        widget = soup.select_one(".h-captcha[data-sitekey]")
        kind = "hcaptcha"
    if widget is None or not widget.get("data-sitekey"):
        return None

    form = widget.find_parent("form") or soup.find("form")
    fields: dict[str, str] = {}
    submit_url, method = page_url, "POST"
    if form is not None:
        submit_url = urljoin(page_url, form.get("action") or page_url)
        method = (form.get("method") or "GET").upper()
        for inp in form.select("input[name]"):
            if (inp.get("type") or "text").lower() in ("hidden", "text"):
                fields[inp["name"]] = inp.get("value", "")
    return Challenge(
        kind=kind,
        sitekey=widget["data-sitekey"],
        page_url=page_url,
        data_s=widget.get("data-s") or None,
        invisible=widget.get("data-size") == "invisible",
        submit_url=submit_url,
        method=method,
        fields=fields,
    )


# provider -> (API base, reCAPTCHA v2 task type, hCaptcha task type).
# Proxy variants drop the "Proxyless"/"ProxyLess" suffix.
PROVIDERS: dict[str, tuple[str, str, str]] = {
    "2captcha": ("https://api.2captcha.com", "RecaptchaV2TaskProxyless", "HCaptchaTaskProxyless"),
    "capsolver": ("https://api.capsolver.com", "ReCaptchaV2TaskProxyLess", "HCaptchaTaskProxyLess"),
    "anticaptcha": ("https://api.anti-captcha.com", "RecaptchaV2TaskProxyless", "HCaptchaTaskProxyless"),
    "capmonster": ("https://api.capmonster.cloud", "RecaptchaV2TaskProxyless", "HCaptchaTaskProxyless"),
}
_ALIASES = {"twocaptcha": "2captcha", "anti-captcha": "anticaptcha", "capmonstercloud": "capmonster"}


def provider_name(value: str) -> str:
    name = value.strip().lower().replace("_", "")
    name = _ALIASES.get(name, name)
    if name not in PROVIDERS:
        raise ValueError(f"unknown CAPTCHA provider {value!r}; choose one of: {', '.join(PROVIDERS)}")
    return name


class CaptchaSolver:
    """Client for a createTask/getTaskResult CAPTCHA-solving service.

    Args:
        provider: "2captcha", "capsolver", "anticaptcha" or "capmonster".
        api_key: the service's client key.
        timeout: give up on a task after this many seconds.
        poll_interval: seconds between ``getTaskResult`` calls.
        max_solves: CAPTCHAs to solve per Scholar request before giving up.
        use_proxy: have the service solve through the same proxy that got
            blocked (tokens are sometimes tied to the solving IP). Only applies
            to requests that go through a proxy.
        api_url: override the provider's API base (for testing / self-hosted).
    """

    def __init__(
        self,
        provider: str,
        api_key: str,
        *,
        timeout: float = 180.0,
        poll_interval: float = 5.0,
        max_solves: int = 1,
        use_proxy: bool = True,
        api_url: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        if not api_key:
            raise ValueError("a CAPTCHA solver needs an API key")
        self.provider = provider_name(provider)
        base, self._recaptcha_task, self._hcaptcha_task = PROVIDERS[self.provider]
        self.api_url = (api_url or base).rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.max_solves = max_solves
        self.use_proxy = use_proxy
        self._http = httpx.AsyncClient(timeout=30.0, transport=transport)
        self.attempts = 0
        self.solved = 0
        self.failed = 0
        self.rejected = 0  # token came back but Google still showed a CAPTCHA
        self.solve_seconds = 0.0
        self.last_error: Optional[str] = None

    async def aclose(self) -> None:
        await self._http.aclose()

    def task_for(self, challenge: Challenge, *, proxy: Optional[str] = None,
                 user_agent: Optional[str] = None) -> dict:
        task_type = self._recaptcha_task if challenge.kind == "recaptcha_v2" else self._hcaptcha_task
        task: dict = {"websiteURL": challenge.page_url, "websiteKey": challenge.sitekey}
        if challenge.kind == "recaptcha_v2":
            if challenge.data_s:
                if self.provider == "capsolver":
                    task["enterprisePayload"] = {"s": challenge.data_s}
                else:
                    task["recaptchaDataSValue"] = challenge.data_s
            if challenge.invisible:
                task["isInvisible"] = True
        if user_agent:
            task["userAgent"] = user_agent
        if proxy and self.use_proxy:
            task_type = task_type.removesuffix("Proxyless").removesuffix("ProxyLess")
            task.update(_proxy_fields(proxy, self.provider))
        task["type"] = task_type
        return task

    async def solve(self, challenge: Challenge, *, proxy: Optional[str] = None,
                    user_agent: Optional[str] = None) -> str:
        """Return a response token for ``challenge``. Raises CaptchaError."""
        self.attempts += 1
        started = time.monotonic()
        try:
            token = await self._solve(self.task_for(challenge, proxy=proxy, user_agent=user_agent))
        except CaptchaError as exc:
            self.failed += 1
            self.last_error = str(exc)
            raise
        finally:
            self.solve_seconds += time.monotonic() - started
        self.solved += 1
        return token

    async def _solve(self, task: dict) -> str:
        created = await self._call("createTask", {"task": task})
        task_id = created.get("taskId")
        if task_id is None:
            raise CaptchaError(f"{self.provider}: createTask returned no taskId")
        deadline = time.monotonic() + self.timeout
        while True:
            await asyncio.sleep(self.poll_interval)
            result = await self._call("getTaskResult", {"taskId": task_id})
            if result.get("status") == "ready":
                solution = result.get("solution") or {}
                token = solution.get("gRecaptchaResponse") or solution.get("token")
                if not token:
                    raise CaptchaError(f"{self.provider}: task {task_id} finished without a token")
                return token
            if time.monotonic() >= deadline:
                raise CaptchaError(f"{self.provider}: task {task_id} not solved within {self.timeout:.0f}s")

    async def _call(self, method: str, payload: dict) -> dict:
        try:
            resp = await self._http.post(f"{self.api_url}/{method}", json={"clientKey": self.api_key, **payload})
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise CaptchaError(f"{self.provider}: {method} failed: {exc}") from exc
        if data.get("errorId"):
            code = data.get("errorCode") or data.get("errorId")
            desc = data.get("errorDescription") or ""
            raise CaptchaError(f"{self.provider}: {method} error {code} {desc}".strip())
        return data

    async def balance(self) -> float:
        """Account balance, as reported by the service's getBalance."""
        return float((await self._call("getBalance", {})).get("balance", 0))

    def stats(self) -> dict:
        return {
            "provider": self.provider,
            "attempts": self.attempts,
            "solved": self.solved,
            "failed": self.failed,
            "rejected_by_google": self.rejected,
            "avg_solve_seconds": round(self.solve_seconds / self.attempts, 1) if self.attempts else None,
            "last_error": self.last_error,
            "max_solves_per_request": self.max_solves,
            "timeout_seconds": self.timeout,
            "solve_through_proxy": self.use_proxy,
        }


def _proxy_fields(proxy: str, provider: str) -> dict:
    if provider == "capsolver":
        return {"proxy": proxy}
    parts = urlsplit(proxy)
    fields = {
        "proxyType": "socks5" if (parts.scheme or "").startswith("socks") else (parts.scheme or "http"),
        "proxyAddress": parts.hostname or "",
        "proxyPort": parts.port or 80,
    }
    if parts.username:
        fields["proxyLogin"] = parts.username
    if parts.password:
        fields["proxyPassword"] = parts.password
    return fields
