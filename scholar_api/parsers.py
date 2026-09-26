"""HTML parsers for Google Scholar pages.

Each parser takes raw HTML and returns plain dicts shaped like SerpAPI's
Google Scholar responses, so existing SerpAPI client code keeps working.

`api_link(engine, **params)` is an optional callback that builds a link back
into this API (SerpAPI's `serpapi_*` fields). When it is None those fields
are omitted.
"""

from __future__ import annotations

import re
from typing import Callable, Optional
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

SCHOLAR_BASE = "https://scholar.google.com"

ApiLink = Optional[Callable[..., str]]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text(node: Optional[Tag]) -> Optional[str]:
    if node is None:
        return None
    text = re.sub(r"\s+", " ", node.get_text().replace("\xa0", " ")).strip()
    return text or None


def _abs(href: Optional[str]) -> Optional[str]:
    if not href or href.startswith("javascript:"):
        return None
    return urljoin(SCHOLAR_BASE, href)


def _qs(href: Optional[str], key: str) -> Optional[str]:
    if not href:
        return None
    values = parse_qs(urlparse(href).query).get(key)
    return values[0] if values else None


def _int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


# --------------------------------------------------------------------------
# Search results (engine=google_scholar)
# --------------------------------------------------------------------------

def parse_search(html: str, api_link: ApiLink = None) -> dict:
    soup = _soup(html)
    results = []
    for position, node in enumerate(soup.select("div.gs_r.gs_or[data-cid]")):
        results.append(_parse_result(node, position, api_link))

    data = {
        "search_information": _parse_search_information(soup),
        "organic_results": results,
        "pagination": _parse_pagination(soup),
    }
    data["search_information"].update(_parse_spelling(soup, api_link))
    data["search_information"]["results_on_page"] = len(results)
    return data


def _parse_search_information(soup: BeautifulSoup) -> dict:
    info: dict = {}
    header = _text(soup.select_one("#gs_ab_md .gs_ab_mdw") or soup.select_one("#gs_ab_md"))
    if header:
        m = re.search(r"([\d][\d.,\s]*)\s*results?", header)
        if m:
            info["total_results"] = _int(m.group(1))
        t = re.search(r"\(([\d.]+)\s*sec", header)
        if t:
            info["time_taken_displayed"] = float(t.group(1))
    query = soup.select_one("#gs_hdr_tsi")
    if query and query.get("value"):
        info["query_displayed"] = query["value"]
    return info


def _parse_spelling(soup: BeautifulSoup, api_link: ApiLink) -> dict:
    """'Did you mean: ...' and 'Showing results for ...' banners."""
    out: dict = {}
    for heading in soup.select("h2.gs_rt, .gs_r h2, #gs_res_ccl_top h2"):
        text = _text(heading) or ""
        anchor = heading.find("a")
        if anchor is None:
            continue
        key = None
        if text.lower().startswith("did you mean"):
            key = "did_you_mean"
        elif text.lower().startswith("showing results for"):
            key = "showing_results_for"
        if key and key not in out:
            q = _qs(anchor.get("href"), "q")
            entry = {"query": q or _text(anchor), "link": _abs(anchor.get("href"))}
            if api_link and q:
                entry["serpapi_link"] = api_link("google_scholar", q=q)
            out[key] = entry
    return out


def _parse_result(node: Tag, position: int, api_link: ApiLink) -> dict:
    result_id = node.get("data-cid")
    item: dict = {"position": position, "result_id": result_id}

    title_node = node.select_one("h3.gs_rt")
    if title_node is not None:
        marker = _text(title_node.select_one(".gs_ct1"))
        if marker:
            item["type"] = marker.strip("[]").title()  # [CITATION] -> Citation
        for tag in title_node.select(".gs_ctc, .gs_ctu, .gs_ctg2"):
            tag.decompose()
        anchor = title_node.find("a")
        item["title"] = _text(title_node)
        if anchor is not None and _abs(anchor.get("href")):
            item["link"] = _abs(anchor.get("href"))

    snippet = _text(node.select_one(".gs_rs"))
    if snippet:
        item["snippet"] = snippet

    pub = node.select_one(".gs_a")
    if pub is not None:
        item["publication_info"] = _parse_publication_info(pub, api_link)

    resources = []
    for anchor in node.select(".gs_or_ggsm a"):
        fmt = _text(anchor.select_one(".gs_ctg2"))
        for tag in anchor.select(".gs_ctg2"):
            tag.decompose()
        resource = {"title": _text(anchor), "link": _abs(anchor.get("href"))}
        if fmt:
            resource["file_format"] = fmt.strip("[]")
        resources.append(resource)
    if resources:
        item["resources"] = resources

    item["inline_links"] = _parse_inline_links(node, result_id, api_link)
    return item


def _parse_publication_info(pub: Tag, api_link: ApiLink) -> dict:
    info: dict = {"summary": _text(pub)}
    authors = []
    for anchor in pub.find_all("a"):
        author_id = _qs(anchor.get("href"), "user")
        if not author_id:
            continue
        author = {
            "name": _text(anchor),
            "link": _abs(anchor.get("href")),
            "author_id": author_id,
        }
        if api_link:
            author["serpapi_scholar_link"] = api_link("google_scholar_author", author_id=author_id)
        authors.append(author)
    if authors:
        info["authors"] = authors
    info.update(split_publication_summary(info["summary"] or ""))
    return info


_YEAR = re.compile(r"(?:^|[\s,])((?:19|20)\d{2})$")


def split_publication_summary(summary: str) -> dict:
    """Split Scholar's grey line into its parts.

    'A Vaswani, N Shazeer, N Parmar… - Advances in neural …, 2017 - proceedings.neurips.cc'
      -> author_names, authors_truncated, venue, year, source
    """
    parts = [p.strip() for p in summary.split(" - ")]
    if not parts or not parts[0]:
        return {}
    out: dict = {}
    names = parts[0]
    out["authors_truncated"] = names.endswith("…") or names.endswith("...")
    out["author_names"] = [n.strip(" …") for n in names.rstrip("….").split(",") if n.strip(" …")]
    rest = parts[1:]
    if len(rest) >= 2 and re.fullmatch(r"[\w.-]+\.[a-z]{2,}", rest[-1]):
        out["source"] = rest.pop()
    middle = " - ".join(rest).strip()
    if middle:
        m = _YEAR.search(middle)
        if m:
            out["year"] = int(m.group(1))
            middle = middle[: m.start(1)].rstrip(" ,")
        if middle:
            out["venue"] = middle
    return out


def _parse_inline_links(node: Tag, result_id: Optional[str], api_link: ApiLink) -> dict:
    links: dict = {}
    if api_link and result_id:
        links["serpapi_cite_link"] = api_link("google_scholar_cite", q=result_id)

    footer = node.select_one(".gs_ri > .gs_fl") or node.select_one(".gs_ri .gs_flb")
    if footer is None:
        return links

    for anchor in footer.find_all("a"):
        href = anchor.get("href") or ""
        text = _text(anchor) or ""
        if "cites=" in href:
            cites_id = _qs(href, "cites")
            cited_by = {"total": _int(text), "link": _abs(href), "cites_id": cites_id}
            if api_link:
                cited_by["serpapi_scholar_link"] = api_link("google_scholar", cites=cites_id)
            links["cited_by"] = cited_by
        elif "q=related:" in href:
            links["related_pages_link"] = _abs(href)
            if api_link and result_id:
                links["serpapi_related_pages_link"] = api_link(
                    "google_scholar", q=f"related:{result_id}:scholar.google.com/"
                )
        elif "cluster=" in href:
            cluster_id = _qs(href, "cluster")
            versions = {"total": _int(text), "link": _abs(href), "cluster_id": cluster_id}
            if api_link:
                versions["serpapi_scholar_link"] = api_link("google_scholar", cluster=cluster_id)
            links["versions"] = versions
        elif "webofknowledge" in href or "webofscience" in href or text.startswith("Web of Science"):
            links["web_of_science"] = {"total": _int(text), "link": _abs(href)}
        elif "scholar.googleusercontent.com" in href or "q=cache:" in href:
            links["cached_page_link"] = _abs(href)
        elif "/scholar_url?" in href and text and "library" in text.lower():
            links.setdefault("library_links", []).append({"title": text, "link": _abs(href)})
    return links


def _parse_pagination(soup: BeautifulSoup) -> dict:
    nav = soup.select_one("#gs_n") or soup.select_one("#gs_nm")
    if nav is None:
        return {}
    pagination: dict = {}
    other_pages: dict = {}
    for anchor in nav.find_all("a"):
        href = _abs(anchor.get("href"))
        if not href:
            continue
        if anchor.select_one(".gs_ico_nav_next") or (_text(anchor) or "").lower() == "next":
            pagination["next"] = href
        elif anchor.select_one(".gs_ico_nav_previous") or (_text(anchor) or "").lower() == "previous":
            pagination["previous"] = href
        elif (_text(anchor) or "").isdigit():
            other_pages[_text(anchor)] = href
    current = nav.select_one(".gs_ico_nav_current")
    if current is not None:
        cell = current.find_parent("td") or current.parent
        page = _int(_text(cell))
        if page:
            pagination["current"] = page
    if other_pages:
        pagination["other_pages"] = other_pages
    return pagination


# --------------------------------------------------------------------------
# Cite popup (engine=google_scholar_cite)
# --------------------------------------------------------------------------

def parse_cite(html: str) -> dict:
    soup = _soup(html)
    citations = []
    for row in soup.select("#gs_citt tr"):
        style = _text(row.select_one("th"))
        snippet = _text(row.select_one(".gs_citr"))
        if style and snippet:
            citations.append({"title": style, "snippet": snippet})
    links = [
        {"name": _text(a), "link": _abs(a.get("href"))}
        for a in soup.select("#gs_citi a")
    ]
    return {"citations": citations, "links": links}


# --------------------------------------------------------------------------
# Author profile (engine=google_scholar_author)
# --------------------------------------------------------------------------

def parse_author(html: str, api_link: ApiLink = None) -> dict:
    soup = _soup(html)
    author: dict = {"name": _text(soup.select_one("#gsc_prf_in"))}

    lines = soup.select("#gsc_prf_i .gsc_prf_il")
    plain = [l for l in lines if l.get("id") not in ("gsc_prf_ivh", "gsc_prf_int")]
    if plain:
        author["affiliations"] = _text(plain[0])
    email = soup.select_one("#gsc_prf_ivh")
    if email is not None:
        homepage = email.select_one("a")
        if homepage is not None:
            author["website"] = _abs(homepage.get("href"))
            homepage.decompose()
        author["email"] = (_text(email) or "").rstrip(" -") or None

    interests = []
    for anchor in soup.select("#gsc_prf_int a"):
        interest = {"title": _text(anchor), "link": _abs(anchor.get("href"))}
        label = _qs(anchor.get("href"), "mauthors")
        if api_link and label:
            interest["serpapi_link"] = api_link("google_scholar_profiles", mauthors=label)
        interests.append(interest)
    author["interests"] = interests

    img = soup.select_one("#gsc_prf_pup-img")
    if img is not None and img.get("src"):
        author["thumbnail"] = _abs(img["src"])

    articles = []
    for row in soup.select("#gsc_a_b tr.gsc_a_tr"):
        title = row.select_one("a.gsc_a_at")
        if title is None:
            continue
        grays = row.select(".gsc_a_t .gs_gray")
        citation_id = _qs(title.get("href") or title.get("data-href"), "citation_for_view")
        article = {
            "title": _text(title),
            "link": _abs(title.get("href") or title.get("data-href")),
            "citation_id": citation_id,
            "authors": _text(grays[0]) if grays else None,
        }
        if len(grays) > 1:
            for tag in grays[1].select(".gs_oph"):
                tag.decompose()
            article["publication"] = _text(grays[1])
        cites = row.select_one("a.gsc_a_ac")
        cited_by: dict = {"value": _int(_text(cites)) if cites else None}
        if cites is not None and _abs(cites.get("href")):
            cited_by["link"] = _abs(cites.get("href"))
            cited_by["cites_id"] = _qs(cites.get("href"), "cites")
            if api_link and cited_by["cites_id"]:
                cited_by["serpapi_link"] = api_link("google_scholar", cites=cited_by["cites_id"])
        article["cited_by"] = cited_by
        article["year"] = _text(row.select_one(".gsc_a_y span")) or None
        if api_link and citation_id:
            article["serpapi_link"] = api_link(
                "google_scholar_author",
                author_id=citation_id.split(":")[0],
                view_op="view_citation",
                citation_id=citation_id,
            )
        articles.append(article)

    return {
        "author": author,
        "articles": articles,
        "cited_by": _parse_author_metrics(soup),
        "co_authors": _parse_co_authors(soup, api_link),
        "public_access": _parse_public_access(soup),
        "more_articles": soup.select_one("#gsc_bpf_more:not([disabled])") is not None,
    }


def _parse_public_access(soup: BeautifulSoup) -> Optional[dict]:
    box = soup.select_one("#gsc_rsb_mnd")
    if box is None:
        return None
    link = box.select_one("a[href*='view_op=list_mandates']") or box.select_one("a")
    return {
        "available": _int(_text(box.select_one(".gsc_rsb_m_a"))),
        "not_available": _int(_text(box.select_one(".gsc_rsb_m_na"))),
        "link": _abs(link.get("href")) if link is not None else None,
    }


def _bar_graph(container: Optional[Tag], year_cls: str, bar_cls: str, value_cls: str) -> list:
    """Scholar's citations-per-year histograms.

    Bars for zero-citation years are omitted; z-index counts back from the
    most recent year (z-index:1 == last year shown).
    """
    if container is None:
        return []
    years = [_int(_text(s)) for s in container.select(f".{year_cls}")]
    bars = container.select(f".{bar_cls}")
    counts = {}
    for i, bar in enumerate(bars):
        z = re.search(r"z-index:\s*(\d+)", bar.get("style", ""))
        idx = len(years) - int(z.group(1)) if z else None
        if idx is None or not 0 <= idx < len(years):
            idx = i if len(bars) == len(years) else None
        if idx is not None:
            counts[years[idx]] = _int(_text(bar.select_one(f".{value_cls}"))) or 0
    return [{"year": y, "citations": counts.get(y, 0)} for y in years if y]


def _parse_author_metrics(soup: BeautifulSoup) -> dict:
    headers = [_text(th) for th in soup.select("#gsc_rsb_st thead th.gsc_rsb_sth")]
    keys = []
    for h in headers:
        h = (h or "").lower()
        keys.append("all" if h == "all" else re.sub(r"\W+", "_", h).strip("_"))
    table = []
    for row in soup.select("#gsc_rsb_st tbody tr"):
        name = _text(row.select_one(".gsc_rsb_sc1"))
        values = [_int(_text(td)) for td in row.select("td.gsc_rsb_std")]
        if not name:
            continue
        metric = re.sub(r"\W+", "_", name.lower()).strip("_")  # "h-index" -> "h_index"
        table.append({metric: dict(zip(keys, values))})

    graph = _bar_graph(soup.select_one(".gsc_md_hist_b"), "gsc_g_t", "gsc_g_a", "gsc_g_al")
    return {"table": table, "graph": graph}


def _parse_co_authors(soup: BeautifulSoup, api_link: ApiLink) -> list:
    co_authors = []
    for li in soup.select("ul.gsc_rsb_a li"):
        anchor = li.select_one(".gsc_rsb_a_desc a")
        if anchor is None:
            continue
        author_id = _qs(anchor.get("href"), "user")
        exts = li.select(".gsc_rsb_a_ext")
        co = {
            "name": _text(anchor),
            "link": _abs(anchor.get("href")),
            "author_id": author_id,
            "affiliations": _text(exts[0]) if exts else None,
        }
        if len(exts) > 1:
            co["email"] = _text(exts[1])
        img = li.select_one("img")
        if img is not None and img.get("src"):
            co["thumbnail"] = _abs(img["src"])
        if api_link and author_id:
            co["serpapi_link"] = api_link("google_scholar_author", author_id=author_id)
        co_authors.append(co)
    return co_authors


def parse_citation(html: str) -> dict:
    """Single article view (view_op=view_citation)."""
    soup = _soup(html)
    title = soup.select_one("#gsc_oci_title")
    anchor = title.select_one("a") if title else None
    citation: dict = {"title": _text(title)}
    if anchor is not None:
        citation["link"] = _abs(anchor.get("href"))
    resources = []
    for a in soup.select("#gsc_oci_title_gg a"):
        fmt = _text(a.select_one(".gsc_vcd_title_ggt"))
        for tag in a.select(".gsc_vcd_title_ggt"):
            tag.decompose()
        resource = {"title": _text(a), "link": _abs(a.get("href"))}
        if fmt:
            resource["file_format"] = fmt.strip("[]")
        resources.append(resource)
    if resources:
        citation["resources"] = resources
    for row in soup.select("#gsc_oci_table .gs_scl"):
        field = _text(row.select_one(".gsc_oci_field"))
        value_node = row.select_one(".gsc_oci_value")
        if not field or value_node is None:
            continue
        key = re.sub(r"\W+", "_", field.lower()).strip("_")
        if key == "total_citations":
            cites = value_node.select_one("a")
            citation[key] = {
                "value": _int(_text(cites)) if cites else None,
                "link": _abs(cites.get("href")) if cites else None,
                "cites_id": _qs(cites.get("href"), "cites") if cites else None,
                "graph": _bar_graph(
                    value_node.select_one("#gsc_oci_graph_bars") or soup.select_one("#gsc_oci_graph_bars"),
                    "gsc_oci_g_t", "gsc_oci_g_a", "gsc_oci_g_al",
                ),
            }
        elif key == "scholar_articles":
            citation[key] = [
                {"title": _text(a), "link": _abs(a.get("href"))}
                for a in value_node.select(".gsc_oms_link, a.gsc_oci_title_link, .gsc_oci_merged_snippet > div > a")
            ] or _text(value_node)
        else:
            citation[key] = _text(value_node)
    return {"citation": citation}


# --------------------------------------------------------------------------
# Author search (engine=google_scholar_profiles)
# --------------------------------------------------------------------------

def parse_profiles(html: str, api_link: ApiLink = None) -> dict:
    soup = _soup(html)
    profiles = []
    for node in soup.select(".gsc_1usr"):
        anchor = node.select_one(".gs_ai_name a")
        if anchor is None:
            continue
        author_id = _qs(anchor.get("href"), "user")
        profile = {
            "name": _text(anchor),
            "link": _abs(anchor.get("href")),
            "author_id": author_id,
            "affiliations": _text(node.select_one(".gs_ai_aff")),
            "email": _text(node.select_one(".gs_ai_eml")),
            "cited_by": _int(_text(node.select_one(".gs_ai_cby"))),
            "interests": [],
        }
        if api_link and author_id:
            profile["serpapi_link"] = api_link("google_scholar_author", author_id=author_id)
        for a in node.select(".gs_ai_int a"):
            interest = {"title": _text(a), "link": _abs(a.get("href"))}
            label = _qs(a.get("href"), "mauthors")
            if api_link and label:
                interest["serpapi_link"] = api_link("google_scholar_profiles", mauthors=label)
            profile["interests"].append(interest)
        img = node.select_one(".gs_ai_pho img")
        if img is not None and img.get("src"):
            profile["thumbnail"] = _abs(img["src"])
        profiles.append(profile)

    pagination: dict = {}
    for direction in ("Next", "Previous"):
        button = soup.select_one(f'button[aria-label="{direction}"]')
        onclick = button.get("onclick") if button is not None else None
        if not onclick or button.has_attr("disabled"):
            continue
        m = re.search(r"window\.location='([^']+)'", onclick)
        if not m:
            continue
        href = m.group(1).encode().decode("unicode_escape")  # \x3d -> =, \x26 -> &
        key = direction.lower()
        pagination[key] = _abs(href)
        token = _qs(href, "after_author" if key == "next" else "before_author")
        if token:
            pagination[f"{key}_page_token"] = token
    return {"profiles": profiles, "pagination": pagination}


# --------------------------------------------------------------------------
# BibTeX (the file behind the cite popup's "BibTeX" link)
# --------------------------------------------------------------------------

def parse_bibtex(text: str) -> Optional[dict]:
    """Parse a single BibTeX entry into {type, key, fields}. Returns None if
    ``text`` doesn't look like BibTeX."""
    m = re.match(r"\s*@(\w+)\s*\{\s*([^,\s]*)\s*,", text)
    if not m:
        return None
    entry = {"type": m.group(1).lower(), "key": m.group(2), "fields": {}}
    i, n = m.end(), len(text)
    while i < n:
        fm = re.compile(r"\s*(\w+)\s*=\s*").match(text, i)
        if not fm:
            break
        name, i = fm.group(1).lower(), fm.end()
        if i < n and text[i] in "{\"":
            close = "}" if text[i] == "{" else "\""
            depth, j = 0, i
            while j < n:
                c = text[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                if (close == "}" and depth == 0 and c == "}") or (close == '"' and j > i and c == '"' and depth == 0):
                    break
                j += 1
            value, i = text[i + 1 : j], j + 1
        else:
            vm = re.compile(r"[^,}\s]+").match(text, i)
            value, i = (vm.group(0), vm.end()) if vm else ("", i)
        entry["fields"][name] = re.sub(r"\s+", " ", value.replace("{", "").replace("}", "")).strip()
        cm = re.compile(r"\s*,?").match(text, i)
        i = cm.end()
    if "author" in entry["fields"]:
        entry["authors"] = [a.strip() for a in entry["fields"]["author"].split(" and ") if a.strip()]
    return entry


# --------------------------------------------------------------------------
# Block detection
# --------------------------------------------------------------------------

_BLOCK_MARKERS = (
    'id="gs_captcha_ccl"',
    'id="gs_captcha_f"',
    "g-recaptcha",
    "Our systems have detected unusual traffic",
    "Please show you're not a robot",
)


def is_blocked(html: str, url: str = "") -> bool:
    if "/sorry/" in url:
        return True
    return any(marker in html for marker in _BLOCK_MARKERS)
