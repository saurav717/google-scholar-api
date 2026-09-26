from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from scholar_api import ScholarClient
from scholar_api.app import create_app

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


class FakeScholar:
    """Routes requests to fixture files and records what was requested."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.blocked = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.blocked:
            return httpx.Response(200, text=fixture("captcha.html"))
        p = request.url.params
        if request.url.path == "/scholar.bib":
            return httpx.Response(200, text=fixture("bibtex.bib"))
        if request.url.path == "/scholar":
            if p.get("output") == "cite":
                name = "cite.html"
            elif "hinton" in p.get("q", ""):
                name = "profiles_search.html"
            else:
                name = "search.html"
        elif p.get("view_op") == "view_citation":
            name = "citation.html"
        elif p.get("view_op") == "search_authors":
            name = "signin.html"  # what Google serves anonymous users now
        else:
            name = "author.html"
        return httpx.Response(200, text=fixture(name))


@pytest.fixture
def fake():
    return FakeScholar()


@pytest.fixture
def scholar_client(fake):
    return ScholarClient(
        min_interval=0, jitter=0, max_retries=1, block_cooldown=0,
        transport=httpx.MockTransport(fake.handler),
    )


@pytest.fixture
def api(scholar_client):
    with TestClient(create_app(client=scholar_client, api_key="")) as c:
        yield c
