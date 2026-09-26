"""Cookie handling that makes the scraper look like a returning visitor
instead of a brand-new one on every run.

* CookieStore persists each outbound identity's Google cookies to a JSON file
  (mode 0600) and restores them on the next run.
* parse_browser_cookies() accepts a Cookie header copied from a browser, and
  read_browser_cookies() reads them straight from a local browser's cookie
  store. Both keep only the Scholar anti-abuse cookies. Google account / sign-in cookies
  (SID, HSID, SSID, APISID, SAPISID, __Secure-*, ...) are always dropped, so a
  Google account is never involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
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


BROWSERS = ("chrome", "firefox", "safari", "edge", "brave", "chromium", "opera", "vivaldi")


class BrowserCookieError(RuntimeError):
    pass


def read_browser_cookies(browser: str, cookie_file: Optional[str] = None) -> tuple[list[dict], list[str]]:
    """Read Scholar's cookies from a local browser's cookie store.

    ``browser`` is one of BROWSERS, or "auto" to try each installed browser.
    Only google.com cookies are loaded, and of those only NID, GSP and
    GOOGLE_ABUSE_EXEMPTION are returned; the names of everything else (e.g.
    sign-in cookies) come back in the second list. Values are never logged.

    macOS: Chrome/Edge/Brave ask once for the Keychain password ("Chrome Safe
    Storage"); Safari needs Full Disk Access for the terminal app.
    """
    try:
        import browser_cookie3
    except ImportError:
        raise BrowserCookieError(
            "Reading browser cookies needs the browser-cookie3 package: pip install -e \".[browser]\""
        ) from None
    browser = browser.strip().lower()
    if browser == "auto":
        loader = browser_cookie3.load
    elif browser in BROWSERS:
        loader = getattr(browser_cookie3, browser)
    else:
        raise BrowserCookieError(f"Unknown browser {browser!r}; use one of: auto, {', '.join(BROWSERS)}")
    kwargs = {"domain_name": "google.com"}
    if cookie_file and browser != "auto":
        kwargs["cookie_file"] = cookie_file
    try:
        jar = loader(**kwargs)
    except Exception as exc:  # locked DB, Keychain denied, browser not installed, ...
        raise BrowserCookieError(f"Could not read {browser} cookies: {type(exc).__name__}: {exc}") from None

    kept: dict[str, dict] = {}
    ignored: list[str] = []
    now = time.time()
    for c in jar:
        if not _is_google(c.domain or "") or (c.expires is not None and c.expires < now):
            continue
        if c.name not in ALLOWED_BROWSER_COOKIES:
            if c.name not in ignored:
                ignored.append(c.name)
            continue
        # GSP belongs to scholar.google.com; prefer that copy if several exist.
        prev = kept.get(c.name)
        if prev is None or "scholar" in (c.domain or ""):
            kept[c.name] = {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path or "/", "expires": c.expires}
    ordered = [kept[n] for n in ALLOWED_BROWSER_COOKIES if n in kept]
    garbled = [c["name"] for c in ordered if not _looks_like_cookie(c["value"])]
    if garbled:
        raise BrowserCookieError(
            f"{browser} cookie values for {', '.join(garbled)} could not be decrypted correctly. "
            "Update browser-cookie3 (pip install -U browser-cookie3) or paste them via SCHOLAR_COOKIES."
        )
    return ordered, ignored


def _looks_like_cookie(value: str) -> bool:
    """Cookie values are printable ASCII; mis-decrypted bytes are not."""
    return bool(value) and all(33 <= ord(ch) <= 126 for ch in value)


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


def import_from_browser_cli(browser: str, cookie_file: Optional[str] = None) -> int:
    """`scholar-api import-cookies`: browser -> scraper cookie file."""
    target = Path(cookie_file or os.getenv("SCHOLAR_COOKIE_FILE") or default_cookie_file()).expanduser()
    if str(target).lower() in ("none", "0", "off"):
        print("SCHOLAR_COOKIE_FILE is disabled; nothing to import into.")
        return 1
    print(f"Reading Scholar cookies from {browser}...")
    if sys.platform == "darwin" and browser in ("chrome", "edge", "brave", "chromium", "opera", "vivaldi", "auto"):
        print("(macOS may ask for your login/Keychain password to unlock the browser's cookie store. Click 'Allow'.)")
    try:
        cookies, ignored = read_browser_cookies(browser)
    except BrowserCookieError as exc:
        print(f"Failed: {exc}")
        return 1
    if not cookies:
        print(
            "No Scholar cookies found. Open https://scholar.google.com in that browser once "
            "(solve a CAPTCHA if shown), close the tab, and run this again."
        )
        return 1
    store = CookieStore(target)
    slots = store.load()
    names = {c["name"] for c in cookies}
    slots["direct"] = [c for c in slots.get("direct", []) if c["name"] not in names] + cookies
    store.save(slots)
    print(f"Imported {', '.join(c['name'] for c in cookies)} into {target}")
    if ignored:
        print(f"Skipped {len(ignored)} other Google cookies (sign-in etc.); they were not saved.")
    print("Every scholar-api run on this machine will now use them. Re-run this if blocks come back.")
    return 0
