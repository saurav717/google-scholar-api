"""The browser solver against real (headless) Chromium and a fake Scholar whose
block page 'solves itself' after a moment, standing in for the person."""

import asyncio
import http.server
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from scholar_api import BrowserSolver, ScholarClient
from scholar_api.client import BlockedError

from .conftest import fixture
from .test_e2e import _free_port

pytest.importorskip("playwright")

BUNDLED = "/opt/pw-browsers/chromium"
EXECUTABLE = BUNDLED if Path(BUNDLED).exists() else None

BLOCK_PAGE = """<html><body><div id="gs_captcha_ccl">Please show you're not a robot
<form id="f" method="post" action="/sorry/index">
<div class="g-recaptcha" data-sitekey="k"></div>
<input type="hidden" name="continue" value="{cont}"></form></div>
{script}</body></html>"""
PERSON = "<script>setTimeout(() => document.getElementById('f').submit(), 300)</script>"


@pytest.fixture
def scholar():
    state = {"solves": 0, "person": True}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body="", headers=()):
            data = body.encode()
            self.send_response(code)
            for k, v in headers:
                self.send_header(k, v)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if "GOOGLE_ABUSE_EXEMPTION=ok" in (self.headers.get("Cookie") or ""):
                return self._send(200, fixture("search.html"))
            page = BLOCK_PAGE.format(cont=self.path, script=PERSON if state["person"] else "")
            self._send(429, page)

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            state["solves"] += 1
            self._send(302, headers=[("Location", form["continue"][0]),
                                     ("Set-Cookie", "GOOGLE_ABUSE_EXEMPTION=ok; Path=/"),
                                     ("Set-Cookie", "SID=signed-in; Path=/")])

    server = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state
    server.shutdown()
    server.server_close()


def make(base, backend, timeout=20):
    solver = BrowserSolver(timeout=timeout, headless=True, channel=None, executable_path=EXECUTABLE)
    client = ScholarClient(base_url=base, min_interval=0, jitter=0, max_retries=1, block_cooldown=600,
                           backend=backend, warmup=False)
    client.solver = solver
    return client, solver


@pytest.mark.parametrize("backend", ["curl_cffi", "httpx"])
def test_person_solves_in_browser(scholar, backend):
    base, state = scholar
    client, solver = make(base, backend)

    async def go():
        try:
            # Two blocked requests at once: only one window opens.
            return await asyncio.gather(*(client.get("/scholar", {"q": q}) for q in ("a", "b")))
        finally:
            await client.aclose()

    results = asyncio.run(go())
    assert all("gs_res_ccl" in r.html for r in results)
    # Both got blocked, but the second reused the first solve instead of opening a window.
    assert state["solves"] == 1 and solver.solved == 1 and solver.attempts == 1
    jar = {c.name for c in client._slots[0].client.cookies.jar}
    assert "GOOGLE_ABUSE_EXEMPTION" in jar and "SID" not in jar  # sign-in cookies never copied
    assert client._slots[0].blocked_until == 0
    stats = client.stats()["captcha"]
    assert stats["provider"] == "browser" and stats["waiting_for_person"] is False


def test_nobody_solves_it(scholar):
    base, state = scholar
    state["person"] = False
    client, solver = make(base, "httpx", timeout=1)

    async def go():
        try:
            await client.get("/scholar", {"q": "x"})
        finally:
            await client.aclose()

    with pytest.raises(BlockedError, match="nobody solved"):
        asyncio.run(go())
    assert solver.failed == 1 and state["solves"] == 0


def test_browser_from_env(monkeypatch):
    monkeypatch.setenv("SCHOLAR_CAPTCHA_PROVIDER", "browser")
    monkeypatch.delenv("SCHOLAR_CAPTCHA_API_KEY", raising=False)
    monkeypatch.setenv("SCHOLAR_COOKIE_FILE", "none")
    monkeypatch.setenv("SCHOLAR_CAPTCHA_BROWSER_PATH", "/x/chrome")
    client = ScholarClient.from_env(backend="httpx")
    assert isinstance(client.solver, BrowserSolver)
    assert client.solver.timeout == 300 and client.solver.executable_path == "/x/chrome"
