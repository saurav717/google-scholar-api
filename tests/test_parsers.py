from scholar_api import parsers

from .conftest import fixture


def link(engine, **params):
    return f"api:{engine}:" + "&".join(f"{k}={v}" for k, v in params.items())


def test_search_results():
    data = parsers.parse_search(fixture("search.html"), link)
    assert data["search_information"] == {
        "total_results": 2340000,
        "time_taken_displayed": 0.07,
        "query_displayed": "attention is all you need",
    }
    results = data["organic_results"]
    assert len(results) == 3

    first = results[0]
    assert first["title"] == "Attention is all you need"
    assert first["result_id"] == "5Gohgn6QFikJ"
    assert first["link"].startswith("https://proceedings.neurips.cc/")
    assert "attention mechanism" in first["snippet"]
    assert first["publication_info"]["summary"].endswith("2017 - proceedings.neurips.cc")
    assert [a["author_id"] for a in first["publication_info"]["authors"]] == ["oR9sCGYAAAAJ", "wsGvgA8AAAAJ"]
    assert first["resources"] == [{"title": "arxiv.org", "link": "https://arxiv.org/pdf/1706.03762", "file_format": "PDF"}]
    links = first["inline_links"]
    assert links["cited_by"]["total"] == 167432
    assert links["cited_by"]["cites_id"] == "2960712678066186980"
    assert links["versions"] == {
        "total": 72,
        "link": "https://scholar.google.com/scholar?cluster=2960712678066186980&hl=en&as_sdt=0,5",
        "cluster_id": "2960712678066186980",
        "serpapi_scholar_link": "api:google_scholar:cluster=2960712678066186980",
    }
    assert links["serpapi_cite_link"] == "api:google_scholar_cite:q=5Gohgn6QFikJ"
    assert "related_pages_link" in links


def test_search_result_types():
    results = parsers.parse_search(fixture("search.html"))["organic_results"]
    assert results[1]["type"] == "Book" and results[1]["title"] == "Deep learning"
    assert results[2]["type"] == "Citation"
    assert results[2]["title"] == "Attention is all you need. arXiv 2017"
    assert "link" not in results[2]
    assert "serpapi_cite_link" not in results[0]["inline_links"]  # no api_link given


def test_search_pagination():
    pag = parsers.parse_search(fixture("search.html"))["pagination"]
    assert pag["current"] == 1
    assert pag["next"].endswith("start=10&q=attention+is+all+you+need&hl=en&as_sdt=0,5")
    assert set(pag["other_pages"]) == {"2", "3"}
    assert "previous" not in pag


def test_cite():
    data = parsers.parse_cite(fixture("cite.html"))
    assert [c["title"] for c in data["citations"]] == ["MLA", "APA"]
    assert data["citations"][0]["snippet"].startswith('Vaswani, Ashish, et al. "Attention is all you need."')
    assert [l["name"] for l in data["links"]] == ["BibTeX", "EndNote", "RefMan"]
    assert data["links"][0]["link"].startswith("https://scholar.googleusercontent.com/scholar.bib")


def test_author_profile():
    data = parsers.parse_author(fixture("author.html"), link)
    author = data["author"]
    assert author["name"] == "Ashish Vaswani"
    assert author["affiliations"] == "Essential AI"
    assert author["email"] == "Verified email at essential.ai"
    assert author["website"] == "https://example.org/ashish"
    assert [i["title"] for i in author["interests"]] == ["Deep Learning", "NLP"]

    table = data["cited_by"]["table"]
    assert table[0] == {"citations": {"all": 200123, "since_2021": 180456}}
    assert table[1] == {"h_index": {"all": 30, "since_2021": 25}}
    assert table[2] == {"i10_index": {"all": 40, "since_2021": 35}}
    # 2022 has no bar in the histogram -> 0 citations
    assert data["cited_by"]["graph"] == [
        {"year": 2021, "citations": 30000},
        {"year": 2022, "citations": 0},
        {"year": 2023, "citations": 45000},
        {"year": 2024, "citations": 60000},
    ]

    first, second = data["articles"]
    assert first["title"] == "Attention is all you need"
    assert first["citation_id"] == "oR9sCGYAAAAJ:u5HHmVD_uO8C"
    assert first["publication"] == "Advances in neural information processing systems 30"
    assert first["year"] == "2017"
    assert first["cited_by"]["value"] == 167432
    assert first["cited_by"]["cites_id"] == "2960712678066186980"
    assert second["cited_by"] == {"value": None}

    assert data["co_authors"][0]["name"] == "Noam Shazeer"
    assert data["co_authors"][0]["author_id"] == "wsGvgA8AAAAJ"
    assert data["co_authors"][0]["affiliations"] == "Google"
    assert data["more_articles"] is True


def test_citation_view():
    c = parsers.parse_citation(fixture("citation.html"))["citation"]
    assert c["title"] == "Attention is all you need"
    assert c["resources"][0] == {"title": "arxiv.org", "link": "https://arxiv.org/pdf/1706.03762", "file_format": "PDF"}
    assert c["publication_date"] == "2017"
    assert c["journal"] == "Advances in neural information processing systems"
    assert c["total_citations"]["value"] == 167432


def test_profiles():
    data = parsers.parse_profiles(fixture("profiles.html"), link)
    p = data["profiles"][0]
    assert p["name"] == "Geoffrey Hinton"
    assert p["author_id"] == "JicYPdAAAAAJ"
    assert p["cited_by"] == 850123
    assert [i["title"] for i in p["interests"]] == ["machine learning", "psychology"]
    assert data["pagination"]["next_page_token"] == "QnYlAHLB__8J"
    assert "previous" not in data["pagination"]  # button is disabled


def test_block_detection():
    assert parsers.is_blocked(fixture("captcha.html"))
    assert parsers.is_blocked("", "https://www.google.com/sorry/index?continue=x")
    for name in ("search.html", "author.html", "cite.html", "profiles.html"):
        assert not parsers.is_blocked(fixture(name))
