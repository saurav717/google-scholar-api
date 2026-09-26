import asyncio
import json
import os
import stat
import time

import httpx
import pytest

from scholar_api import ScholarClient
from scholar_api.cookies import CookieStore, export_jar, parse_browser_cookies

from .conftest import fixture


def _handler(seen, set_cookie=None, block=False):
    def handler(request):
        seen.append(request)
        headers = {"Set-Cookie": set_cookie} if set_cookie else {}
        if block:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200, text=fixture("search.html"), headers=headers)
    return handler


def _client(handler, **kw):
    kw.setdefault("min_interval", 0)
    kw.setdefault("jitter", 0)
    return ScholarClient(transport=httpx.MockTransport(handler), **kw)


def test_parse_browser_cookies_keeps_only_scholar_cookies():
    kept, ignored = parse_browser_cookies(
        "NID=abc; SID=secret; GSP=LM=1:S=x; __Secure-3PSID=secret2; GOOGLE_ABUSE_EXEMPTION=ID=1:TM=2; HSID=h; junk"
    )
    assert [(c["name"], c["value"], c["domain"]) for c in kept] == [
        ("NID", "abc", ".google.com"),
        ("GSP", "LM=1:S=x", "scholar.google.com"),
        ("GOOGLE_ABUSE_EXEMPTION", "ID=1:TM=2", ".google.com"),
    ]
    assert ignored == ["SID", "__Secure-3PSID", "HSID"]
    assert parse_browser_cookies("") == ([], [])


def test_cookies_persist_across_runs(tmp_path):
    path = tmp_path / "cookies.json"
    seen = []
    first = _client(_handler(seen, "NID=fromgoogle; Domain=.google.com; Path=/; Max-Age=3600"), cookie_file=path)
    asyncio.run(first.get("/scholar", {"q": "x"}))
    assert "Cookie" not in seen[0].headers  # brand-new visitor

    saved = json.loads(path.read_text())
    assert [c["name"] for c in saved["slots"]["direct"]] == ["NID"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    # A new process loads the file and comes back as a returning visitor.
    seen2 = []
    second = _client(_handler(seen2), cookie_file=path)
    asyncio.run(second.get("/scholar", {"q": "y"}))
    assert seen2[0].headers["Cookie"] == "NID=fromgoogle"


def test_cookie_file_never_contains_proxy_credentials(tmp_path):
    path = tmp_path / "cookies.json"
    client = _client(_handler([], "NID=v; Domain=.google.com; Path=/"), cookie_file=path,
                     proxies=["http://user:hunter2@proxy.example:8080"])
    asyncio.run(client.get("/scholar", {"q": "x"}))
    text = path.read_text()
    assert "hunter2" not in text and "proxy.example" not in text
    (key,) = json.loads(text)["slots"]
    assert key.startswith("proxy-")


def test_expired_and_foreign_cookies_are_not_saved():
    jar = httpx.Cookies()
    jar.set("NID", "ok", domain=".google.com")
    jar.set("OTHER", "x", domain="example.com")
    jar.jar._cookies[".google.com"]["/"]["NID"].expires = int(time.time()) - 10
    jar.set("GSP", "ok", domain="scholar.google.com")
    assert [c["name"] for c in export_jar(jar)] == ["GSP"]


def test_corrupt_cookie_file_is_ignored(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text("{not json")
    assert CookieStore(path).load() == {}
    seen = []
    asyncio.run(_client(_handler(seen), cookie_file=path).get("/scholar", {"q": "x"}))
    assert "Cookie" not in seen[0].headers


def test_browser_cookies_are_sent_and_survive_a_block():
    seen = []
    client = _client(_handler(seen, block=True), browser_cookies="NID=mine; SID=nope",
                     max_retries=0, block_cooldown=0)
    from scholar_api import BlockedError
    with pytest.raises(BlockedError):
        asyncio.run(client.get("/scholar", {"q": "x"}))
    assert seen[0].headers["Cookie"] == "NID=mine"  # SID dropped
    # A block clears Google's cookies but keeps the ones you supplied.
    assert [c.name for c in client._slots[0].client.cookies.jar] == ["NID"]
    session = client.stats()["session"]
    assert session["browser_cookies"] == ["NID"] and session["ignored_browser_cookies"] == ["SID"]


def test_warmup_visits_homepage_once():
    seen = []
    client = _client(_handler(seen, "NID=w; Domain=.google.com; Path=/"), warmup=True)
    asyncio.run(client.get("/scholar", {"q": "a"}))
    asyncio.run(client.get("/scholar", {"q": "b"}))
    assert [r.url.path for r in seen] == ["/", "/scholar", "/scholar"]
    assert seen[1].headers["Cookie"] == "NID=w"  # the query carries the warm-up cookie
    stats = client.stats()["proxies"][0]
    assert stats["warmup_requests"] == 1 and stats["warmed_up"] is True and stats["cookies"] == ["NID"]


def test_warmup_off_by_default_with_test_transport():
    seen = []
    asyncio.run(_client(_handler(seen)).get("/scholar", {"q": "a"}))
    assert [r.url.path for r in seen] == ["/scholar"]


def test_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHOLAR_COOKIE_FILE", str(tmp_path / "c.json"))
    monkeypatch.setenv("SCHOLAR_COOKIES", "GSP=abc; SAPISID=nope")
    monkeypatch.setenv("SCHOLAR_WARMUP", "0")
    client = ScholarClient.from_env(backend="httpx")
    session = client.stats()["session"]
    assert session == {
        "cookie_file": str(tmp_path / "c.json"),
        "warmup": False,
        "browser": None,
        "browser_import_error": None,
        "browser_cookies": ["GSP"],
        "ignored_browser_cookies": ["SAPISID"],
    }
    monkeypatch.setenv("SCHOLAR_COOKIE_FILE", "none")
    assert ScholarClient.from_env(backend="httpx").stats()["session"]["cookie_file"] is None
    monkeypatch.delenv("SCHOLAR_COOKIE_FILE")
    assert ScholarClient.from_env(backend="httpx").stats()["session"]["cookie_file"].endswith(
        os.path.join(".scholar-api", "cookies.json")
    )


# --------------------------------------------------------------------------
# Reading cookies straight from a browser
# --------------------------------------------------------------------------

def _firefox_db(path, rows):
    """A real Firefox cookies.sqlite (the schema browser_cookie3 reads)."""
    import sqlite3

    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE moz_cookies (id INTEGER PRIMARY KEY, originAttributes TEXT DEFAULT '', name TEXT, value TEXT,"
        " host TEXT, path TEXT, expiry INTEGER, lastAccessed INTEGER, creationTime INTEGER, isSecure INTEGER,"
        " isHttpOnly INTEGER, inBrowserElement INTEGER DEFAULT 0, sameSite INTEGER DEFAULT 0,"
        " rawSameSite INTEGER DEFAULT 0, schemeMap INTEGER DEFAULT 0)"
    )
    future = int(time.time()) + 86400
    for name, value, host in rows:
        con.execute(
            "INSERT INTO moz_cookies (name, value, host, path, expiry, lastAccessed, creationTime, isSecure, isHttpOnly)"
            " VALUES (?, ?, ?, '/', ?, 0, 0, 1, 1)",
            (name, value, host, future),
        )
    con.commit()
    con.close()


FIREFOX_ROWS = [
    ("NID", "nid-value", ".google.com"),
    ("GSP", "gsp-value", "scholar.google.com"),
    ("SID", "signin-secret", ".google.com"),
    ("SAPISID", "signin-secret-2", ".google.com"),
    ("__Secure-3PSID", "signin-secret-3", ".google.com"),
    ("session", "other-site", "example.com"),
]


def test_read_real_firefox_cookie_store(tmp_path):
    pytest.importorskip("browser_cookie3")
    from scholar_api.cookies import read_browser_cookies

    db = tmp_path / "cookies.sqlite"
    _firefox_db(db, FIREFOX_ROWS)
    kept, ignored = read_browser_cookies("firefox", cookie_file=str(db))
    assert [(c["name"], c["value"], c["domain"]) for c in kept] == [
        ("NID", "nid-value", ".google.com"),
        ("GSP", "gsp-value", "scholar.google.com"),
    ]
    assert sorted(ignored) == ["SAPISID", "SID", "__Secure-3PSID"]
    assert "signin-secret" not in repr(kept)


class _FakeCookie:
    def __init__(self, name, value, domain, expires=None):
        self.name, self.value, self.domain, self.path, self.expires = name, value, domain, "/", expires


def _fake_browser_module(monkeypatch, cookies, fail=None):
    import sys
    import types

    calls = []

    def loader(**kw):
        calls.append(kw)
        if fail:
            raise fail
        return list(cookies)

    mod = types.SimpleNamespace(chrome=loader, load=loader, firefox=loader, safari=loader)
    monkeypatch.setitem(sys.modules, "browser_cookie3", mod)
    return calls


CHROME_COOKIES = [
    _FakeCookie("NID", "n", ".google.com"),
    _FakeCookie("GSP", "g", "scholar.google.com"),
    _FakeCookie("GOOGLE_ABUSE_EXEMPTION", "e", ".google.com"),
    _FakeCookie("SID", "secret", ".google.com"),
    _FakeCookie("NID", "stale", ".google.com", expires=1),  # expired copy ignored
]


def test_read_chrome_and_auto(monkeypatch):
    from scholar_api.cookies import read_browser_cookies

    calls = _fake_browser_module(monkeypatch, CHROME_COOKIES)
    kept, ignored = read_browser_cookies("chrome")
    assert [(c["name"], c["value"]) for c in kept] == [("NID", "n"), ("GSP", "g"), ("GOOGLE_ABUSE_EXEMPTION", "e")]
    assert ignored == ["SID"]
    assert calls == [{"domain_name": "google.com"}]  # only google.com is ever loaded
    assert read_browser_cookies("auto")[0] == kept


def test_read_browser_errors(monkeypatch):
    from scholar_api.cookies import BrowserCookieError, read_browser_cookies

    with pytest.raises(BrowserCookieError, match="Unknown browser"):
        read_browser_cookies("netscape")
    _fake_browser_module(monkeypatch, [], fail=PermissionError("Keychain access denied"))
    with pytest.raises(BrowserCookieError, match="Keychain access denied"):
        read_browser_cookies("chrome")
    import sys
    monkeypatch.setitem(sys.modules, "browser_cookie3", None)  # not installed
    with pytest.raises(BrowserCookieError, match="pip install"):
        read_browser_cookies("chrome")


def test_client_uses_browser_cookies(monkeypatch):
    _fake_browser_module(monkeypatch, CHROME_COOKIES)
    seen = []
    client = _client(_handler(seen), browser="chrome", browser_cookies="NID=pasted")
    asyncio.run(client.get("/scholar", {"q": "x"}))
    sent = dict(kv.split("=", 1) for kv in seen[0].headers["Cookie"].split("; "))
    assert sent == {"NID": "pasted", "GSP": "g", "GOOGLE_ABUSE_EXEMPTION": "e"}  # pasted value wins
    session = client.stats()["session"]
    assert session["browser"] == "chrome" and session["browser_import_error"] is None
    assert session["ignored_browser_cookies"] == ["SID"]


def test_client_survives_browser_read_failure(monkeypatch):
    _fake_browser_module(monkeypatch, [], fail=PermissionError("Keychain access denied"))
    seen = []
    client = _client(_handler(seen), browser="chrome")
    asyncio.run(client.get("/scholar", {"q": "x"}))  # still works, just without those cookies
    assert "Keychain access denied" in client.stats()["session"]["browser_import_error"]


def test_import_cookies_command(tmp_path, capsys):
    pytest.importorskip("browser_cookie3")
    from unittest import mock

    from scholar_api import cookies as cookies_mod

    db = tmp_path / "cookies.sqlite"
    _firefox_db(db, FIREFOX_ROWS)
    target = tmp_path / "store" / "cookies.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"version": 1, "slots": {"direct": [
        {"name": "NID", "value": "old", "domain": ".google.com", "path": "/", "expires": None},
        {"name": "1P_JAR", "value": "keep", "domain": ".google.com", "path": "/", "expires": None},
    ]}}))

    real = cookies_mod.read_browser_cookies
    with mock.patch.object(cookies_mod, "read_browser_cookies", lambda b: real(b, cookie_file=str(db))):
        assert cookies_mod.import_from_browser_cli("firefox", cookie_file=str(target)) == 0
    out = capsys.readouterr().out
    assert "Imported NID, GSP" in out and "Skipped 3 other Google cookies" in out

    saved = json.loads(target.read_text())["slots"]["direct"]
    assert {(c["name"], c["value"]) for c in saved} == {("1P_JAR", "keep"), ("NID", "nid-value"), ("GSP", "gsp-value")}
    assert "signin-secret" not in target.read_text()
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600

    # The next run picks them up with no settings at all.
    seen = []
    asyncio.run(_client(_handler(seen), cookie_file=target).get("/scholar", {"q": "x"}))
    assert "NID=nid-value" in seen[0].headers["Cookie"] and "GSP=gsp-value" in seen[0].headers["Cookie"]


def test_import_cookies_command_nothing_found(monkeypatch, tmp_path, capsys):
    from scholar_api.cookies import import_from_browser_cli

    _fake_browser_module(monkeypatch, [_FakeCookie("SID", "s", ".google.com")])
    target = tmp_path / "c.json"
    assert import_from_browser_cli("chrome", cookie_file=str(target)) == 1
    assert "No Scholar cookies found" in capsys.readouterr().out
    assert not target.exists()


def test_garbled_browser_values_rejected(monkeypatch):
    from scholar_api.cookies import BrowserCookieError, read_browser_cookies

    _fake_browser_module(monkeypatch, [_FakeCookie("NID", "\x8f\x02garbage\x00", ".google.com")])
    with pytest.raises(BrowserCookieError, match="could not be decrypted"):
        read_browser_cookies("chrome")
