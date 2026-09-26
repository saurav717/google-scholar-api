"""End-to-end over real sockets: a fake Scholar HTTP server serving the
fixtures, the real uvicorn server, and a client following the API's own links."""

import http.server
import socket
import threading
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import uvicorn

from scholar_api import ScholarClient
from scholar_api.app import create_app

from .conftest import fixture


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(params=["curl_cffi", "httpx"])
def live(request):
    state = {"hits": [], "block": 0, "cookies": []}

    class FakeScholar(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            state["hits"].append((time.monotonic(), self.path))
            state["cookies"].append(self.headers.get("Cookie"))
            if url.path == "/":  # homepage warm-up hands out a cookie, like Google does
                body = b"<html><title>Google Scholar</title></html>"
                self.send_response(200)
                self.send_header("Set-Cookie", "NID=warm; Path=/")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if state["block"]:
                state["block"] -= 1
                name = "captcha.html"
            elif url.path == "/scholar":
                if q.get("output") == "cite":
                    name = "cite.html"
                elif "hinton" in q.get("q", ""):
                    name = "profiles_search.html"
                else:
                    name = "search.html"
            elif q.get("view_op") == "view_citation":
                name = "citation.html"
            elif q.get("view_op") == "search_authors":
                name = "signin.html"
            else:
                name = "author.html"
            body = fixture(name).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    fake = http.server.ThreadingHTTPServer(("127.0.0.1", _free_port()), FakeScholar)
    threading.Thread(target=fake.serve_forever, daemon=True).start()

    client = ScholarClient(
        base_url=f"http://127.0.0.1:{fake.server_address[1]}",
        min_interval=0.2, jitter=0, max_retries=2, block_cooldown=0, backend=request.param,
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(client=client, api_key=""), port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)

    base = f"http://127.0.0.1:{port}"
    with httpx.Client(base_url=base, timeout=30, trust_env=False) as http_client:
        yield http_client, base, state, fake, request.param
    server.should_exit = True
    thread.join(timeout=5)
    fake.shutdown()
    fake.server_close()


def test_end_to_end(live):
    api, base, state, fake, backend = live
    assert api.get("/status").json()["http_backend"] == backend

    first = api.get("/search.json", params={"q": "warm up first"})
    assert first.status_code == 200
    # Warm-up hit the homepage first, and the query carried its cookie back.
    assert [p.split("?")[0] for _, p in state["hits"][:2]] == ["/", "/scholar"]
    assert state["cookies"][0] is None and state["cookies"][1] == "NID=warm"

    body = api.get("/search.json", params={"q": "attention is all you need", "as_ylo": 2017}).json()
    assert len(body["organic_results"]) == 3
    first = body["organic_results"][0]

    # A SerpAPI-style client follows the links the API hands back.
    links = [
        first["inline_links"]["serpapi_cite_link"],
        first["inline_links"]["cited_by"]["serpapi_scholar_link"],
        first["inline_links"]["versions"]["serpapi_scholar_link"],
        first["publication_info"]["authors"][0]["serpapi_scholar_link"],
        body["serpapi_pagination"]["next"],
    ]
    for link in links:
        assert link.startswith(base)
        assert api.get(link.removeprefix(base)).status_code == 200, link

    author = api.get("/search.json", params={"engine": "google_scholar_author", "author_id": "oR9sCGYAAAAJ"}).json()
    article = api.get(author["articles"][0]["serpapi_link"].removeprefix(base)).json()
    assert article["citation"]["title"] == "Attention is all you need"

    profiles = api.get("/search.json", params={"engine": "google_scholar_profiles", "mauthors": "geoffrey hinton"}).json()
    assert profiles["profiles"][0]["author_id"] == "JicYPdAAAAAJ"
    assert api.get(profiles["serpapi_pagination"]["next"].removeprefix(base)).status_code == 200
    hinton = api.get(profiles["profiles"][0]["serpapi_link"].removeprefix(base)).json()
    assert "author" in hinton

    # Pacing: consecutive upstream requests are >= min_interval apart
    # (small tolerance: the timer starts when the request is sent, not received).
    times = [t for t, _ in state["hits"]]
    assert min(b - a for a, b in zip(times, times[1:])) >= 0.15

    # Cache: identical query makes no upstream request.
    n = len(state["hits"])
    again = api.get("/search.json", params={"q": "attention is all you need", "as_ylo": 2017}).json()
    assert len(state["hits"]) == n and again["search_metadata"]["cached"] is True

    # One CAPTCHA, then success on retry.
    state["block"] = 1
    r = api.get("/search.json", params={"q": "captcha once"})
    assert r.status_code == 200 and r.json()["search_metadata"]["blocked_attempts"] == 1

    # CAPTCHA on every attempt -> 503 with guidance.
    state["block"] = 99
    r = api.get("/search.json", params={"q": "always blocked"})
    assert r.status_code == 503 and r.json()["error_type"] == "blocked"
    state["block"] = 0

    status = api.get("/status").json()
    assert status["proxies"][0]["blocks"] == 4  # 1 + 3 attempts
    assert status["proxies"][0]["warmup_requests"] == 1

    # Scholar unreachable -> clean JSON error, no crash.
    fake.shutdown()
    fake.server_close()
    r = api.get("/search.json", params={"q": "scholar down"})
    assert r.status_code == 502 and r.json()["error_type"] == "upstream_error"
