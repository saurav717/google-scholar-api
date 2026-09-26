import httpx
from fastapi.testclient import TestClient

from scholar_api import ScholarClient
from scholar_api.app import create_app


def test_search_endpoint(api, fake):
    r = api.get("/search.json", params={"engine": "google_scholar", "q": "attention", "as_ylo": 2017, "num": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["search_metadata"]["status"] == "Success"
    assert body["search_parameters"]["q"] == "attention"
    assert len(body["organic_results"]) == 3
    assert body["serpapi_pagination"]["current"] == 1
    assert "start=5" in body["serpapi_pagination"]["next"]

    sent = fake.requests[0].url.params
    assert sent["q"] == "attention" and sent["as_ylo"] == "2017" and sent["num"] == "5"
    assert sent["hl"] == "en"
    # links point back to this server
    cite = body["organic_results"][0]["inline_links"]["serpapi_cite_link"]
    assert cite == "http://testserver/search.json?engine=google_scholar_cite&q=5Gohgn6QFikJ"


def test_default_engine_and_cites(api, fake):
    r = api.get("/search.json", params={"cites": "2960712678066186980"})
    assert r.status_code == 200
    assert fake.requests[0].url.params["cites"] == "2960712678066186980"


def test_cache(api, fake):
    api.get("/search.json", params={"q": "x"})
    api.get("/search.json", params={"q": "x"})
    assert len(fake.requests) == 1
    api.get("/search.json", params={"q": "x", "no_cache": "true"})
    assert len(fake.requests) == 2


def test_cite_endpoint(api, fake):
    body = api.get("/search.json", params={"engine": "google_scholar_cite", "q": "5Gohgn6QFikJ"}).json()
    assert body["citations"][0]["title"] == "MLA"
    params = fake.requests[0].url.params
    assert params["q"] == "info:5Gohgn6QFikJ:scholar.google.com/" and params["output"] == "cite"


def test_author_endpoint(api, fake):
    body = api.get(
        "/search.json",
        params={"engine": "google_scholar_author", "author_id": "oR9sCGYAAAAJ", "sort": "pubdate", "num": 2},
    ).json()
    assert body["author"]["name"] == "Ashish Vaswani"
    assert "start=2" in body["serpapi_pagination"]["next"]
    params = fake.requests[0].url.params
    assert params["user"] == "oR9sCGYAAAAJ" and params["sortby"] == "pubdate" and params["pagesize"] == "2"


def test_author_citation_view(api, fake):
    body = api.get(
        "/search.json",
        params={"engine": "google_scholar_author", "view_op": "view_citation", "citation_id": "oR9sCGYAAAAJ:u5HHmVD_uO8C"},
    ).json()
    assert body["citation"]["title"] == "Attention is all you need"


def test_profiles_endpoint(api, fake):
    body = api.get("/search.json", params={"engine": "google_scholar_profiles", "mauthors": "hinton"}).json()
    assert body["profiles"][0]["name"] == "Geoffrey Hinton"
    assert "after_author=QnYlAHLB__8J" in body["serpapi_pagination"]["next"]


def test_param_errors(api):
    assert api.get("/search.json").status_code == 400
    assert api.get("/search.json", params={"engine": "nope", "q": "x"}).status_code == 400
    r = api.get("/search.json", params={"q": "x", "num": 50})
    assert r.status_code == 400 and "num" in r.json()["error"]
    assert api.get("/search.json", params={"engine": "google_scholar_author"}).status_code == 400


def test_blocked_returns_503(api, fake):
    fake.blocked = True
    r = api.get("/search.json", params={"q": "x"})
    assert r.status_code == 503
    assert "CAPTCHA" in r.json()["error"]
    assert len(fake.requests) == 2  # original + 1 retry


def test_api_key(scholar_client):
    with TestClient(create_app(client=scholar_client, api_key="secret")) as c:
        assert c.get("/search.json", params={"q": "x"}).status_code == 401
        assert c.get("/search.json", params={"q": "x", "api_key": "wrong"}).status_code == 401
        r = c.get("/search.json", params={"q": "x", "api_key": "secret"})
        assert r.status_code == 200
        assert "api_key" not in r.json()["search_parameters"]


def test_retry_rotates_proxies():
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(200, text="<html></html>")

    client = ScholarClient(
        proxies=["http://p1:1", "http://p2:1"], min_interval=0, jitter=0,
        max_retries=2, transport=httpx.MockTransport(handler),
    )
    with TestClient(create_app(client=client, api_key="")) as c:
        r = c.get("/search.json", params={"q": "x"})
    assert r.status_code == 200 and len(seen) == 2
    assert client._slots[0].blocked_until > 0  # first proxy benched
