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
        "browser_cookies": ["GSP"],
        "ignored_browser_cookies": ["SAPISID"],
    }
    monkeypatch.setenv("SCHOLAR_COOKIE_FILE", "none")
    assert ScholarClient.from_env(backend="httpx").stats()["session"]["cookie_file"] is None
    monkeypatch.delenv("SCHOLAR_COOKIE_FILE")
    assert ScholarClient.from_env(backend="httpx").stats()["session"]["cookie_file"].endswith(
        os.path.join(".scholar-api", "cookies.json")
    )
