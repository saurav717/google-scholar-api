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

    meta = body["search_metadata"]
    assert meta["cached"] is False and meta["scholar_requests"] == 1 and meta["blocked_attempts"] == 0
    assert meta["google_scholar_url"].startswith("https://scholar.google.com/scholar?")
    assert "warnings" not in meta


def test_default_engine_and_cites(api, fake):
    r = api.get("/search.json", params={"cites": "2960712678066186980"})
    assert r.status_code == 200
    assert fake.requests[0].url.params["cites"] == "2960712678066186980"


def test_cache(api, fake):
    api.get("/search.json", params={"q": "x"})
    second = api.get("/search.json", params={"q": "x"}).json()
    assert len(fake.requests) == 1
    assert second["search_metadata"]["cached"] is True
    assert second["search_metadata"]["scholar_requests"] == 0
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


def test_author_all_articles(api, fake):
    body = api.get(
        "/search.json",
        params={"engine": "google_scholar_author", "author_id": "oR9sCGYAAAAJ", "all_articles": "true", "max_pages": 3},
    ).json()
    # Fixture returns 2 articles < page size 100, so paging stops after one request.
    assert len(fake.requests) == 1
    assert fake.requests[0].url.params["pagesize"] == "100"
    assert body["articles_summary"] == {
        "returned": 2, "start": 0, "has_more": False, "total_citations_of_returned": 167432,
    }
    assert body["author"]["author_id"] == "oR9sCGYAAAAJ"
    assert body["public_access"]["available"] == 12
    assert "serpapi_pagination" not in body


def test_author_citation_view(api, fake):
    body = api.get(
        "/search.json",
        params={"engine": "google_scholar_author", "view_op": "view_citation", "citation_id": "oR9sCGYAAAAJ:u5HHmVD_uO8C"},
    ).json()
    assert body["citation"]["title"] == "Attention is all you need"


def test_cite_with_bibtex(api, fake):
    body = api.get(
        "/search.json", params={"engine": "google_scholar_cite", "q": "5Gohgn6QFikJ", "include_bibtex": "true"}
    ).json()
    assert body["bibtex"]["raw"].startswith("@article{vaswani2017attention,")
    assert body["bibtex"]["parsed"]["fields"]["journal"] == "Advances in neural information processing systems"
    assert body["search_metadata"]["pages_fetched"] == 2
    assert fake.requests[1].url.host == "scholar.googleusercontent.com"


def test_unknown_param_warning(api):
    body = api.get("/search.json", params={"q": "x", "author_id": "abc", "foo": "1"}).json()
    assert body["search_metadata"]["warnings"] == [
        "Ignored unknown parameter 'author_id' for engine 'google_scholar'.",
        "Ignored unknown parameter 'foo' for engine 'google_scholar'.",
    ]


def test_empty_page_warning():
    empty = httpx.MockTransport(lambda request: httpx.Response(200, text="<html><body>nothing here</body></html>"))
    client = ScholarClient(min_interval=0, jitter=0, transport=empty)
    with TestClient(create_app(client=client, api_key="")) as c:
        body = c.get("/search.json", params={"q": "x"}).json()
    assert body["organic_results"] == []
    assert "changed Scholar's HTML" in body["search_metadata"]["warnings"][0]


def test_info_engines_status(api):
    index = api.get("/").json()
    assert index["status"] == "ok" and "google_scholar_author" in index["engines"]
    catalog = api.get("/engines").json()
    assert "q" in catalog["engines"]["google_scholar"]["params"]
    assert catalog["engines"]["google_scholar_cite"]["example"].startswith("http://testserver/search.json?engine=google_scholar_cite")
    api.get("/search.json", params={"q": "x"})
    status = api.get("/status").json()
    assert status["proxies"][0]["proxy"] == "direct"
    assert status["proxies"][0]["successes"] == 1
    assert status["cache"]["entries"] == 1


def test_openapi_documents_params(api):
    spec = api.get("/openapi.json").json()
    names = {p["name"] for p in spec["paths"]["/search.json"]["get"]["parameters"]}
    assert {"engine", "q", "cites", "author_id", "mauthors", "include_bibtex", "all_articles"} <= names


def test_profiles_endpoint(api, fake):
    body = api.get("/search.json", params={"engine": "google_scholar_profiles", "mauthors": "hinton"}).json()
    assert body["profiles"][0]["name"] == "Geoffrey Hinton"
    assert "after_author=QnYlAHLB__8J" in body["serpapi_pagination"]["next"]


def test_param_errors(api):
    r = api.get("/search.json")
    assert r.status_code == 400
    assert r.json()["error_type"] == "invalid_parameter" and "hint" in r.json()
    r = api.get("/search.json", params={"q": "x", "as_ylo": 2020, "as_yhi": 2010})
    assert r.status_code == 400 and "as_ylo" in r.json()["error"]
    assert api.get("/search.json", params={"engine": "nope", "q": "x"}).status_code == 400
    r = api.get("/search.json", params={"q": "x", "num": 50})
    assert r.status_code == 400 and "num" in r.json()["error"]
    assert api.get("/search.json", params={"engine": "google_scholar_author"}).status_code == 400


def test_blocked_returns_503(api, fake):
    fake.blocked = True
    r = api.get("/search.json", params={"q": "x"})
    assert r.status_code == 503
    assert "CAPTCHA" in r.json()["error"]
    assert r.json()["error_type"] == "blocked" and "SCHOLAR_PROXIES" in r.json()["hint"]
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
    stats = client.stats()["proxies"]
    assert stats[0]["blocks"] == 1 and stats[1]["successes"] == 1
    assert stats[0]["proxy"] == "http://p1:1"


def test_proxy_credentials_masked():
    client = ScholarClient(proxies=["http://user:secret@proxy.example:8080"], transport=httpx.MockTransport(lambda r: None))
    assert client.stats()["proxies"][0]["proxy"] == "http://***@proxy.example:8080"


def test_blocked_single_ip_does_not_hammer():
    """With one IP and a real cooldown, a CAPTCHA must not trigger instant retries."""
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(429, text="Too Many Requests")

    client = ScholarClient(min_interval=0, jitter=0, max_retries=3, block_cooldown=600,
                           transport=httpx.MockTransport(handler))
    with TestClient(create_app(client=client, api_key="")) as c:
        r = c.get("/search.json", params={"q": "x"})
    assert r.status_code == 503 and len(seen) == 1
    assert "HTTP 429" in r.json()["error"]


def test_backend_selection():
    from scholar_api.client import CurlSession

    mock = httpx.MockTransport(lambda r: httpx.Response(200))
    assert ScholarClient(transport=mock).backend == "httpx"
    assert ScholarClient(backend="httpx").backend == "httpx"
    if CurlSession is not None:
        assert ScholarClient().backend == "curl_cffi"
        assert ScholarClient().stats()["impersonate"] == "chrome"
    import pytest
    with pytest.raises(ValueError):
        ScholarClient(backend="nope")
