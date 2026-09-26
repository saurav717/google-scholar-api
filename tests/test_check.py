import asyncio

import httpx

from scholar_api import ScholarClient
from scholar_api.check import run_check


def _client(handler):
    return ScholarClient(min_interval=0, jitter=0, max_retries=0, transport=httpx.MockTransport(handler))


def test_check_all_fields_present(fake, tmp_path, capsys):
    code = asyncio.run(run_check("attention", "oR9sCGYAAAAJ", "geoffrey hinton", tmp_path, _client(fake.handler)))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "MISSING" not in out
    assert "0 missing/failed" in out
    assert (tmp_path / "google_scholar.html").exists() and (tmp_path / "google_scholar.json").exists()
    # the citation_id from the author step feeds the article step
    assert any(r.url.params.get("citation_for_view") == "oR9sCGYAAAAJ:u5HHmVD_uO8C" for r in fake.requests)


def test_check_reports_missing_and_stops_when_blocked(fake, tmp_path, capsys):
    fake.blocked = True
    code = asyncio.run(run_check("attention", "x", "y", tmp_path, _client(fake.handler)))
    out = capsys.readouterr().out
    assert code == 1
    assert "blocking this IP" in out
    assert len(fake.requests) == 1
    assert (tmp_path / "blocked.html").read_text().startswith("<html><body><div id=\"gs_captcha_ccl\">")


def test_check_flags_broken_layout(tmp_path, capsys):
    empty = lambda request: httpx.Response(200, text="<html></html>")
    code = asyncio.run(run_check("attention", "x", "y", tmp_path, _client(empty)))
    out = capsys.readouterr().out
    assert code == 1
    assert "MISSING" in out and "fix the selector" in out
