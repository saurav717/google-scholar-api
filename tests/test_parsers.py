from scholar_api import parsers

from .conftest import fixture


def link(engine, **params):
    return f"api:{engine}:" + "&".join(f"{k}={v}" for k, v in params.items())


def test_search_results():
    data = parsers.parse_search(fixture("search.html"), link)
    info = data["search_information"]
    assert info["total_results"] == 2340000
    assert info["time_taken_displayed"] == 0.07
    assert info["query_displayed"] == "attention is all you need"
    assert info["results_on_page"] == 3
    assert info["did_you_mean"]["query"] == "attention is all you need"
    assert info["did_you_mean"]["serpapi_link"].startswith("api:google_scholar:q=")
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
    assert links["web_of_science"]["total"] == 25000
    assert "webofknowledge" in links["web_of_science"]["link"]


def test_structured_publication_info():
    results = parsers.parse_search(fixture("search.html"))["organic_results"]
    first = results[0]["publication_info"]
    assert first["author_names"] == ["A Vaswani", "N Shazeer", "N Parmar"]
    assert first["authors_truncated"] is True
    assert first["venue"] == "Advances in neural …"
    assert first["year"] == 2017
    assert first["source"] == "proceedings.neurips.cc"
    book = results[1]["publication_info"]
    assert book["year"] == 2016 and book["source"] == "books.google.com" and "venue" not in book
    citation = results[2]["publication_info"]
    assert citation["venue"] == "arXiv preprint arXiv:1706.03762" and "source" not in citation


def test_split_publication_summary_edge_cases():
    split = parsers.split_publication_summary
    assert split("Y LeCun, Y Bengio, G Hinton - nature, 2015 - nature.com") == {
        "authors_truncated": False,
        "author_names": ["Y LeCun", "Y Bengio", "G Hinton"],
        "source": "nature.com",
        "year": 2015,
        "venue": "nature",
    }
    assert split("J Smith - Springer") == {"authors_truncated": False, "author_names": ["J Smith"], "venue": "Springer"}
    assert split("") == {}


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
    assert data["public_access"] == {
        "available": 12,
        "not_available": 2,
        "link": "https://scholar.google.com/citations?view_op=list_mandates&hl=en&user=oR9sCGYAAAAJ",
    }


def test_citation_view():
    c = parsers.parse_citation(fixture("citation.html"))["citation"]
    assert c["title"] == "Attention is all you need"
    assert c["resources"][0] == {"title": "arxiv.org", "link": "https://arxiv.org/pdf/1706.03762", "file_format": "PDF"}
    assert c["publication_date"] == "2017"
    assert c["journal"] == "Advances in neural information processing systems"
    assert c["total_citations"]["value"] == 167432
    # 2023 has no bar -> 0
    assert c["total_citations"]["graph"] == [
        {"year": 2022, "citations": 30000},
        {"year": 2023, "citations": 0},
        {"year": 2024, "citations": 52000},
    ]


def test_bibtex():
    entry = parsers.parse_bibtex(fixture("bibtex.bib"))
    assert entry["type"] == "article" and entry["key"] == "vaswani2017attention"
    assert entry["fields"]["title"] == "Attention is all you need"
    assert entry["fields"]["year"] == "2017"
    assert entry["authors"][:2] == ["Vaswani, Ashish", "Shazeer, Noam"]
    assert len(entry["authors"]) == 8
    quoted = parsers.parse_bibtex('@inproceedings{he2016, title="Deep {Residual} Learning", year=2016}')
    assert quoted["fields"] == {"title": "Deep Residual Learning", "year": "2016"}
    assert parsers.parse_bibtex("<html>nope</html>") is None


def test_name_matches():
    m = parsers.name_matches
    assert m("geoffrey hinton", "G Hinton") and m("geoffrey hinton", "GE Hinton")
    assert m("Geoffrey Hinton", "Geoffrey E. Hinton")
    assert not m("geoffrey hinton", "A Hinton")  # different first initial
    assert not m("geoffrey hinton", "I Sutskever")
    assert m("hinton", "A Hinton")  # surname-only query matches any initial
    assert m("jose garcia", "J García")  # accents folded
    assert not m("", "G Hinton")


def test_profiles_from_search():
    data = parsers.parse_profiles(fixture("profiles_search.html"), "geoffrey hinton", link)
    profiles = data["profiles"]
    assert [p["author_id"] for p in profiles] == ["JicYPdAAAAAJ", "GEOFFxxAAAAJ"]

    box = profiles[0]
    assert box == {
        "name": "Geoffrey Hinton",
        "link": "https://scholar.google.com/citations?user=JicYPdAAAAAJ&hl=en&oi=ao",
        "author_id": "JicYPdAAAAAJ",
        "source": "profile_box",
        "affiliations": "Emeritus Prof. Computer Science, University of Toronto",
        "email": "Verified email at cs.toronto.edu",
        "cited_by": 971020,
        "thumbnail": "https://scholar.google.com/citations/images/avatar_scholar_56.png",
        "serpapi_link": "api:google_scholar_author:author_id=JicYPdAAAAAJ",
        "papers_in_results": 3,
    }
    other = profiles[1]
    assert other["source"] == "search_results" and other["name"] == "G Hinton" and other["papers_in_results"] == 1
    assert data["pagination"]["next"].endswith("start=10&q=geoffrey+hinton&hl=en&as_sdt=0,5")


def test_profiles_without_box():
    """If Scholar shows no profile box, matching linked authors are still found."""
    html = fixture("profiles_search.html")
    html = html[: html.index('<div class="gs_r"><h3')] + html[html.index('<div class="gs_r gs_or'):]
    profiles = parsers.parse_profiles(html, "geoffrey hinton")["profiles"]
    assert [(p["author_id"], p["source"], p["papers_in_results"]) for p in profiles] == [
        ("JicYPdAAAAAJ", "search_results", 3),
        ("GEOFFxxAAAAJ", "search_results", 1),
    ]


def test_signin_detection():
    assert parsers.is_signin_page(fixture("signin.html"))
    assert parsers.is_signin_page("", "https://accounts.google.com/v3/signin/identifier?continue=x")
    for name in ("search.html", "author.html", "profiles_search.html"):
        assert not parsers.is_signin_page(fixture(name))


def test_block_detection():
    assert parsers.is_blocked(fixture("captcha.html"))
    assert parsers.is_blocked("", "https://www.google.com/sorry/index?continue=x")
    for name in ("search.html", "author.html", "cite.html", "profiles_search.html", "citation.html", "bibtex.bib", "signin.html"):
        assert not parsers.is_blocked(fixture(name))
