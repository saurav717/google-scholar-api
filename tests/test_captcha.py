import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from scholar_api import CaptchaSolver, ScholarClient
from scholar_api.app import create_app
from scholar_api.captcha import CaptchaError, parse_challenge

from .conftest import fixture

SORRY_URL = "https://www.google.com/sorry/index?continue=https://scholar.google.com/scholar%3Fq%3Dx&q=EhAgAQ"


class FakeSolverService:
    """createTask / getTaskResult, as 2Captcha, CapSolver, Anti-Captcha and CapMonster speak it."""

    def __init__(self, token="TOKEN", error=None, pending=1):
        self.token, self.error, self.pending = token, error, pending
        self.calls: list[tuple[str, dict]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((method, body))
        if self.error:
            return httpx.Response(200, json={"errorId": 1, "errorCode": self.error})
        if method == "createTask":
            return httpx.Response(200, json={"errorId": 0, "taskId": 42})
        if method == "getBalance":
            return httpx.Response(200, json={"errorId": 0, "balance": 3.5})
        if self.pending:
            self.pending -= 1
            return httpx.Response(200, json={"errorId": 0, "status": "processing"})
        return httpx.Response(200, json={"errorId": 0, "status": "ready", "solution": {"gRecaptchaResponse": self.token}})


class SorryScholar:
    """Blocks with Google's /sorry/ page until the right token is submitted."""

    def __init__(self, accept="TOKEN"):
        self.accept = accept
        self.requests: list[httpx.Request] = []
        self.exempt = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/sorry/index" and request.method == "POST":
            form = dict(httpx.QueryParams(request.content.decode()))
            if form.get("g-recaptcha-response") == self.accept and form.get("q") == "EhAgAQ":
                self.exempt = True
                return httpx.Response(302, headers={"Location": form["continue"], "Set-Cookie": "GOOGLE_ABUSE_EXEMPTION=ok; Path=/"})
            return httpx.Response(429, text=fixture("sorry.html"))
        if request.url.path == "/scholar" and not self.exempt:
            return httpx.Response(302, headers={"Location": SORRY_URL})
        if request.url.path == "/sorry/index":
            return httpx.Response(429, text=fixture("sorry.html"))
        return httpx.Response(200, text=fixture("search.html"))


def make(service, scholar, provider="2captcha", **kw):
    solver = CaptchaSolver(provider, "KEY", poll_interval=0, transport=httpx.MockTransport(service.handler), **kw)
    client = ScholarClient(
        base_url="https://scholar.google.com", min_interval=0, jitter=0, max_retries=1,
        block_cooldown=600, captcha_solver=solver, transport=httpx.MockTransport(scholar.handler),
    )
    return client, solver


def test_parse_sorry_page():
    c = parse_challenge(fixture("sorry.html"), SORRY_URL)
    assert c.kind == "recaptcha_v2" and c.sitekey.startswith("6Lfwuy") and c.data_s == "S-VALUE"
    assert c.submit_url == "https://www.google.com/sorry/index" and c.method == "POST"
    assert c.fields == {"q": "EhAgAQ", "continue": "https://scholar.google.com/scholar?q=x"}


def test_parse_scholar_captcha_and_nothing_to_solve():
    c = parse_challenge(fixture("captcha.html"), "https://scholar.google.com/scholar?q=x")
    assert c.kind == "recaptcha_v2" and c.sitekey == "x"
    assert parse_challenge("Too Many Requests", "https://scholar.google.com/") is None
    h = parse_challenge('<form action="/v"><div class="h-captcha" data-sitekey="hk"></div></form>', "https://a.com/p")
    assert h.kind == "hcaptcha" and h.submit_url == "https://a.com/v" and "h-captcha-response" in h.response_fields


def test_solves_sorry_page_and_returns_results():
    service, scholar = FakeSolverService(), SorryScholar()
    client, solver = make(service, scholar)
    with TestClient(create_app(client=client, api_key="")) as c:
        r = c.get("/search.json", params={"q": "x"})
        status = c.get("/status").json()["captcha"]
    assert r.status_code == 200, r.json()
    meta = r.json()["search_metadata"]
    assert meta["captchas_solved"] == 1 and meta["blocked_attempts"] == 0
    assert r.json()["organic_results"]
    method, body = service.calls[0]
    assert method == "createTask" and body["clientKey"] == "KEY"
    assert body["task"].pop("userAgent") == client._slots[0].user_agent
    assert body["task"] == {
        "type": "RecaptchaV2TaskProxyless", "websiteURL": SORRY_URL,
        "websiteKey": "6LfwuyUTAAAAAOAmoS0fdqijC2PbbdH4kjq62Y1b", "recaptchaDataSValue": "S-VALUE",
    }
    assert [m for m, _ in service.calls] == ["createTask", "getTaskResult", "getTaskResult"]
    assert client._slots[0].blocked_until == 0  # not benched
    assert "GOOGLE_ABUSE_EXEMPTION" in {c.name for c in client._slots[0].client.cookies.jar}
    assert status["enabled"] and status["provider"] == "2captcha" and status["solved"] == 1


def test_rejected_token_still_blocks():
    service, scholar = FakeSolverService(token="WRONG", pending=0), SorryScholar()
    client, solver = make(service, scholar)
    with TestClient(create_app(client=client, api_key="")) as c:
        r = c.get("/search.json", params={"q": "x"})
    assert r.status_code == 503 and "rejected" in r.json()["error"]
    assert solver.rejected == 1


def test_solver_error_falls_back_to_block():
    service, scholar = FakeSolverService(error="ERROR_ZERO_BALANCE"), SorryScholar()
    client, solver = make(service, scholar)
    with TestClient(create_app(client=client, api_key="")) as c:
        r = c.get("/search.json", params={"q": "x"})
    assert r.status_code == 503 and "ERROR_ZERO_BALANCE" in r.json()["error"]
    assert solver.failed == 1 and len(service.calls) == 1  # max_solves=1: no second task


def test_bare_429_is_not_sent_to_solver():
    service = FakeSolverService()
    solver = CaptchaSolver("capsolver", "KEY", poll_interval=0, transport=httpx.MockTransport(service.handler))
    client = ScholarClient(min_interval=0, jitter=0, max_retries=0, captcha_solver=solver,
                           transport=httpx.MockTransport(lambda r: httpx.Response(429, text="Too Many Requests")))
    with TestClient(create_app(client=client, api_key="")) as c:
        assert c.get("/search.json", params={"q": "x"}).status_code == 503
    assert service.calls == []


@pytest.mark.parametrize("provider,expected", [
    ("2captcha", "RecaptchaV2Task"), ("anti-captcha", "RecaptchaV2Task"),
    ("capmonster", "RecaptchaV2Task"), ("capsolver", "ReCaptchaV2Task"),
])
def test_proxy_tasks_per_provider(provider, expected):
    solver = CaptchaSolver(provider, "KEY")
    c = parse_challenge(fixture("sorry.html"), SORRY_URL)
    task = solver.task_for(c, proxy="http://u:p@proxy.example:8080", user_agent="UA")
    assert task["type"] == expected and task["userAgent"] == "UA"
    if provider == "capsolver":
        assert task["proxy"] == "http://u:p@proxy.example:8080" and task["enterprisePayload"] == {"s": "S-VALUE"}
    else:
        assert task["proxyAddress"] == "proxy.example" and task["proxyPort"] == 8080
        assert task["proxyLogin"] == "u" and task["proxyPassword"] == "p" and task["proxyType"] == "http"
    assert CaptchaSolver(provider, "KEY", use_proxy=False).task_for(c, proxy="http://h:1")["type"].lower().endswith("proxyless")


def test_timeout_and_balance():
    service = FakeSolverService(pending=10**6)
    solver = CaptchaSolver("2captcha", "KEY", timeout=0, poll_interval=0, transport=httpx.MockTransport(service.handler))
    with pytest.raises(CaptchaError, match="not solved"):
        asyncio.run(solver.solve(parse_challenge(fixture("sorry.html"), SORRY_URL)))
    assert asyncio.run(solver.balance()) == 3.5


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("SCHOLAR_CAPTCHA_PROVIDER", "capmonster")
    monkeypatch.setenv("SCHOLAR_CAPTCHA_API_KEY", "k")
    monkeypatch.setenv("SCHOLAR_CAPTCHA_MAX_SOLVES", "2")
    monkeypatch.setenv("SCHOLAR_COOKIE_FILE", "none")
    client = ScholarClient.from_env(backend="httpx")
    assert client.solver.provider == "capmonster" and client.solver.max_solves == 2
    assert client.solver.api_url == "https://api.capmonster.cloud"
    monkeypatch.setenv("SCHOLAR_CAPTCHA_API_KEY", "")
    with pytest.raises(ValueError):
        ScholarClient.from_env(backend="httpx")
    with pytest.raises(ValueError):
        CaptchaSolver("nope", "k")
