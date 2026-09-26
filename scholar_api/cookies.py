"""Cookie handling that makes the scraper look like a returning visitor
instead of a brand-new one on every run.

* CookieStore persists each outbound identity's Google cookies to a JSON file
  (mode 0600) and restores them on the next run.
* parse_browser_cookies() accepts a Cookie header copied from a browser, but
  keeps only the Scholar anti-abuse cookies. Google account / sign-in cookies
  (SID, HSID, SSID, APISID, SAPISID, __Secure-*, ...) are always dropped, so a
  Google account is never involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Iterable, Optional

# Cookies Scholar uses to recognise a returning, trusted visitor.
#   NID                      Google preferences / anti-abuse id (.google.com)
#   GSP                      Scholar settings (scholar.google.com)
#   GOOGLE_ABUSE_EXEMPTION   set after solving a CAPTCHA (.google.com)
ALLOWED_BROWSER_COOKIES = ("NID", "GSP", "GOOGLE_ABUSE_EXEMPTION")

# Only cookies for these domains are kept / persisted.
_GOOGLE_DOMAINS = ("google.com", "scholar.google.com", "googleusercontent.com")


def default_cookie_file() -> Path:
    return Path.home() / ".scholar-api" / "cookies.json"


def parse_browser_cookies(header: str) -> tuple[list[dict], list[str]]:
    """'NID=abc; GSP=def; SID=xyz' -> ([NID, GSP cookies], ['SID'] ignored)."""
    kept, ignored = [], []
    for part in header.split(";"):
        if "=" not in part:
            continue
        name, value = (s.strip() for s in part.split("=", 1))
        if not name:
            continue
        if name in ALLOWED_BROWSER_COOKIES and value:
            domain = "scholar.google.com" if name == "GSP" else ".google.com"
            kept.append({"name": name, "value": value, "domain": domain, "path": "/"})
        else:
            ignored.append(name)
    return kept, ignored


def _is_google(domain: str) -> bool:
    d = domain.lstrip(".")
    return any(d == g or d.endswith("." + g) for g in _GOOGLE_DOMAINS)


def export_jar(cookies) -> list[dict]:
    """Serialise an httpx / curl_cffi cookie jar (Google cookies only)."""
    now = time.time()
    out = []
    for c in cookies.jar:
        if c.expires is not None and c.expires < now:
            continue
        if not _is_google(c.domain or ""):
            continue
        out.append({"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/", "expires": c.expires})
    return out


def import_cookies(cookies, items: Iterable[dict]) -> int:
    now = time.time()
    n = 0
    for c in items:
        if c.get("expires") is not None and c["expires"] < now:
            continue
        cookies.set(c["name"], c["value"], domain=c.get("domain") or "", path=c.get("path") or "/")
        n += 1
    return n


def slot_key(proxy: Optional[str]) -> str:
    """Stable key per outbound identity that doesn't write proxy credentials to disk."""
    if not proxy:
        return "direct"
    return "proxy-" + hashlib.sha256(proxy.encode()).hexdigest()[:16]


class CookieStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser()

    def load(self) -> dict[str, list[dict]]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        slots = data.get("slots") if isinstance(data, dict) else None
        return slots if isinstance(slots, dict) else {}

    def save(self, slots: dict[str, list[dict]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "slots": slots}, f, indent=1)
        os.replace(tmp, self.path)
