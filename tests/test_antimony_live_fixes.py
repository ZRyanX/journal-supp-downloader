"""Regression tests for bugs identified in the 86-item Antimony live Zotero test."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import discovery_state as ds
import journal_downloader as jd
import repository_resolver as rr
import scansci_supp_downloader as sc
import supp_finder as sf


# ── 1. X25TUZ7P: GSA wrong-paper rejection ───────────────────────────────────

def test_gsa_fallback_rejects_wrong_paper_by_title_and_code():
    parent_doi = "10.1130/B36424.1"
    parent_title = "Contrasting fluid evolution of the Xikuangshan and Woxi Sb deposits"
    wrong_supp_doi = "10.1130/gsab.s.19423916.v2"
    wrong_supp_title = (
        "Supplemental Material: Fingerprinting the metal source and cycling of the "
        "world’s largest antimony deposit in Xikuangshan, China"
    )

    def fake_get(url, **kwargs):
        if f"/works/{parent_doi}" in url:
            return Mock(
                status_code=200,
                json=lambda: {"message": {"title": [parent_title], "relation": {}}},
            )
        if "query.bibliographic=" in url:
            return Mock(
                status_code=200,
                json=lambda: {
                    "message": {
                        "items": [
                            {
                                "DOI": wrong_supp_doi,
                                "title": [wrong_supp_title],
                                "relation": {},
                            }
                        ]
                    }
                },
            )
        raise AssertionError(f"Unexpected URL: {url}")

    session = Mock()
    session.get.side_effect = fake_get
    cands, title = rr.discover_gsa_figshare_candidates(parent_doi, session=session)
    assert cands == []
    assert title == parent_title


def test_gsa_rejects_mismatched_manuscript_code_in_filename():
    parent_doi = "10.1130/B36424.1"
    parent_title = "Antimony mineralization in South China"
    supp_doi = "10.1130/gsab.s.19423916.v2"

    def fake_get(url, **kwargs):
        if f"/works/{parent_doi}" in url:
            return Mock(
                status_code=200,
                json=lambda: {
                    "message": {
                        "title": [parent_title],
                        "relation": {"is-supplemented-by": [{"id": supp_doi}]},
                    }
                },
            )
        if f"doi.org/{supp_doi}" in url:
            return Mock(
                status_code=302,
                headers={"Location": "https://geosociety.figshare.com/articles/dataset/s/19423916"},
            )
        if "api.figshare.com/v2/articles/19423916" in url:
            return Mock(
                status_code=200,
                json=lambda: {
                    "title": f"Supplemental Material: {parent_title}",
                    "files": [
                        {
                            "name": "B36377_SuppMat.pdf",
                            "download_url": "https://ndownloader.figshare.com/files/999",
                            "size": 1000,
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected URL: {url}")

    session = Mock()
    session.get.side_effect = fake_get
    cands, _ = rr.discover_gsa_figshare_candidates(parent_doi, session=session)
    assert cands == []


# ── 2. ZJWN3SFD & 539UDUIW: Crossref chooser + Figshare collections ──────────

def test_crossref_chooser_detection_and_unwrapping():
    chooser_url = "https://chooser.crossref.org/?doi=10.1144%2Fgeochem2024-031"
    assert rr.is_crossref_chooser_url(chooser_url)
    assert not rr.is_crossref_chooser_url("https://www.lyellcollection.org/doi/10.1144/geochem2024-031")

    html = """
    <html><body>
      <div class="resource-line">
        <a href="https://www.lyellcollection.org/doi/10.1144/geochem2024-031">Geological Society</a>
      </div>
      <div class="resource-line">
        <a href="https://pubs.geoscienceworld.org/geochem/article/10.1144/geochem2024-031">GSW</a>
      </div>
    </body></html>
    """
    urls = rr.extract_crossref_chooser_urls(html_str=html)
    assert urls == [
        "https://www.lyellcollection.org/doi/10.1144/geochem2024-031",
        "https://pubs.geoscienceworld.org/geochem/article/10.1144/geochem2024-031",
    ]


def test_resolve_doi_url_unwraps_crossref_chooser():
    chooser_html = '<div class="resource-line"><a href="https://www.lyellcollection.org/doi/10.1144/geochem2024-031">Lyell</a></div>'
    head_resp = Mock(url="https://chooser.crossref.org/?doi=10.1144%2Fgeochem2024-031")
    get_resp = Mock(
        url="https://chooser.crossref.org/?doi=10.1144%2Fgeochem2024-031",
        status_code=200,
        text=chooser_html,
    )
    with patch.object(sc.requests, "head", return_value=head_resp), \
         patch.object(sc.requests, "get", return_value=get_resp):
        resolved = sc.resolve_doi_url("10.1144/geochem2024-031")
    assert resolved == "https://www.lyellcollection.org/doi/10.1144/geochem2024-031"


def test_figshare_collection_expansion_and_doi_discovery():
    col_doi = "https://doi.org/10.6084/m9.figshare.c.7537384"
    assert rr.is_repository_landing(col_doi)

    def fake_post(url, json=None, **kwargs):
        if url.endswith("/collections/search"):
            return Mock(status_code=200, json=lambda: [{"id": 7537384, "url": col_doi}])
        if url.endswith("/articles/search"):
            return Mock(status_code=200, json=lambda: [])
        raise AssertionError(f"Unexpected POST: {url}")

    def fake_get(url, **kwargs):
        if url.endswith("/collections/7537384"):
            return Mock(
                status_code=200,
                json=lambda: {
                    "title": "Geochem Supp Collection",
                    "references": ["https://doi.org/10.1144/geochem2024-031"],
                    "url_public_html": "https://geolsoc.figshare.com/collections/7537384",
                },
            )
        if "/collections/7537384/articles" in url:
            return Mock(status_code=200, json=lambda: [{"id": 27690001}])
        if url.endswith("/articles/27690001"):
            return Mock(
                status_code=200,
                json=lambda: {
                    "title": "Table S1",
                    "files": [
                        {
                            "name": "Table_S1.xlsx",
                            "download_url": "https://ndownloader.figshare.com/files/5050001",
                            "size": 12345,
                        }
                    ],
                },
            )
        raise AssertionError(f"Unexpected GET: {url}")

    session = Mock()
    session.post.side_effect = fake_post
    session.get.side_effect = fake_get

    cands, title = rr.discover_figshare_by_doi("10.1144/geochem2024-031", session=session)
    assert title == "Geochem Supp Collection"
    assert len(cands) == 1
    assert cands[0].url == "https://ndownloader.figshare.com/files/5050001"

    expanded = rr.expand_repository_candidates([sf.Candidate(col_doi, source_url="https://example.org")], session=session)
    assert len(expanded) == 1
    assert expanded[0].url == "https://ndownloader.figshare.com/files/5050001"


# ── 3. IVJKZMW7: jgeosci.org /supplementary HTML subpage expansion ───────────

def test_supplementary_subpage_expansion():
    subpage = "https://www.jgeosci.org/detail/jgeosci.279/supplementary"
    xlsx_url = "https://www.jgeosci.org/supplementary/jgeosci.279_supplementary_table.xlsx"
    assert rr.is_supplementary_subpage_url(subpage)
    assert not rr.is_supplementary_subpage_url(xlsx_url)
    assert rr.is_repository_landing(subpage)

    html = (
        f'<html><body><a href="{subpage}">Supplementary</a>'
        f'<a href="{xlsx_url}">Download Supplementary Table (xlsx)</a></body></html>'
    ).encode("utf-8")
    resp = Mock(
        url=subpage,
        status_code=200,
        encoding="utf-8",
        headers={"Content-Type": "text/html; charset=utf-8"},
    )
    resp.iter_content.return_value = [html]
    session = Mock()
    session.get.return_value = resp

    expanded = rr.expand_repository_candidates(
        [sf.Candidate(subpage, source_url="https://www.jgeosci.org/detail/jgeosci.279")],
        session=session,
    )
    assert [c.url for c in expanded] == [xlsx_url]


# ── 4. NP25HECE: javascript:void(0) + data="161" ignored on <a> ──────────────

def test_data_attr_ignored_on_anchor_kept_on_object():
    soup = BeautifulSoup(
        '<a href="javascript:void(0);" class="download_supplementary" data="161">附表1-3</a>'
        '<object data="/supp/chart.pdf">Chart</object>',
        "html.parser",
    )
    a_tag = soup.find("a")
    obj_tag = soup.find("object")
    assert sf.extract_target_url_from_tag(a_tag) is None
    assert sf.extract_target_url_from_tag(obj_tag) == "/supp/chart.pdf"

    page_html = """
    <section id="supplementary">
      <a href="javascript:void(0);" class="download_supplementary" data="161">250509李保亮-附表1-3</a>
      <a href="http://data.geophy.cn/aps/2025429.docx">附表1-3.docx</a>
    </section>
    """
    diag = sf.find_all_candidates(page_html, "http://www.ysxb.ac.cn/article/doi/10.18654/1000-0569/2025.05.09")
    assert [c.url for c in diag.candidates] == ["http://data.geophy.cn/aps/2025429.docx"]


# ── 5. ALN5K694: ?pageType=en with /en/ path prefix treated as same article ─

def test_same_article_page_url_handles_lang_path_prefix():
    base = "http://chinageology.cgs.cn/en/article/doi/10.31035/cg2025065"
    alt = "http://chinageology.cgs.cn/article/doi/10.31035/cg2025065?pageType=en"
    assert sf.is_same_article_page_url(alt, base)

    page_html = f'<section id="supplementary"><a href="{alt}">Appendix 1</a></section>'
    diag = sf.find_all_candidates(page_html, base)
    assert diag.candidates == []


# ── 6. KGPGFU3K: PMC inline table exclusion + verified Tier A dedup ──────────

def test_pmc_inline_table_and_figure_urls_excluded():
    assert sf.junk_candidate_reason("https://pmc.ncbi.nlm.nih.gov/articles/PMC7530294/table/tbl1/", text="Table 1") == "article_inline_table_page"
    assert sf.junk_candidate_reason("https://pmc.ncbi.nlm.nih.gov/articles/PMC7530294/figure/fig2/", text="Figure 2") == "article_figure_page"
    assert sf.junk_candidate_reason("https://pmc.ncbi.nlm.nih.gov/articles/instance/7530294/bin/mmc1.pdf", text="mmc1.pdf") is None


def test_tier_a_verified_filename_suppresses_oa_duplicate(tmp_path, capsys):
    supp_dir = tmp_path / "Test Paper"
    supp_dir.mkdir(parents=True)
    mmc1 = supp_dir / "mmc1.pdf"
    mmc1.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n")

    pmc_html = (
        '<html><head><title>Test Paper</title></head><body>'
        '<section id="supplementary">'
        '<a href="https://pmc.ncbi.nlm.nih.gov/articles/instance/7530294/bin/mmc1.pdf">mmc1.pdf</a>'
        '</section></body></html>'
    )
    argv = [
        "scansci_supp_downloader.py",
        "10.1016/j.isci.2020.101575",
        "-o", str(tmp_path),
        "--no-bsk",
        "--no-vpn",
        "--skip-cdn",
    ]
    with patch.object(sys, "argv", argv), \
         patch.object(sc, "load_config", return_value={}), \
         patch.object(sc, "resolve_doi_url", return_value="https://www.sciencedirect.com/science/article/pii/S2589004220307677"), \
         patch.object(sc, "try_elsevier_api_campus", return_value=([str(mmc1)], {1})), \
         patch.object(sc, "_setup_authenticated_session_and_cookies", return_value=(Mock(), {})), \
         patch.object(sc, "fetch_page_html", return_value=(pmc_html, 200, "https://pmc.ncbi.nlm.nih.gov/articles/PMC7530294/")), \
         patch.object(sc, "resolve_oa_info", return_value=None), \
         patch.object(sc, "download_file") as mock_dl:
        sc.main()
    out = capsys.readouterr().out
    mock_dl.assert_not_called()
    assert not (supp_dir / "mmc1_2.pdf").exists()
    assert "Skipped 1 duplicate candidate(s) already verified by Tier A/CDN" in out


# ── 7. Blocked sites + DiscoveryEvidence unresolved-section suppression ──────

def test_blocked_page_and_fetch_guards():
    aws_waf = "<html><head><script>window.gokuProps={};</script></head><body><div id='aws-waf-captcha'></div></body></html>"
    cnki_cap = "<html><head><title>安全验证-中国知网</title></head><body>请完成安全验证 clickWord captchaId=123</body></html>"
    assert sf.is_blocked_page(aws_waf)
    assert sf.is_blocked_page(cnki_cap)

    # HTTP 202 empty shell must NOT be returned as usable HTML by fetch_page_html
    sess_202 = Mock()
    sess_202.get.return_value = Mock(status_code=202, text="", url="https://www.degruyter.com/test", encoding="utf-8")
    with patch.object(sc, "HAS_SCRAPLING", False):
        html_out, status_out, _ = sc.fetch_page_html("https://www.degruyter.com/test", sess_202, {}, False)
    assert html_out == ""

    # CNKI /verify/ redirect must NOT be returned as usable HTML
    sess_cnki = Mock()
    sess_cnki.get.return_value = Mock(
        status_code=200,
        text="<html><body>ok</body></html>",
        url="https://kns.cnki.net/verify/home?captchaType=clickWord",
        encoding="utf-8",
        apparent_encoding="utf-8",
    )
    with patch.object(sc, "HAS_SCRAPLING", False):
        html_cnki, _, _ = sc.fetch_page_html("https://kns.cnki.net/kcms2/article", sess_cnki, {}, False)
    assert html_cnki == ""


def test_discovery_evidence_suppresses_stale_prehydration_unresolved_section():
    ev = ds.DiscoveryEvidence()
    ssr_html = '<html><body><div class="Statement__Supplementarymaterial"><span id="SM1" class="SupplementaryMaterial"></span></div></body></html>'
    hydrated_html = (
        '<html><body><div class="Statement__Supplementarymaterial">'
        '<a href="https://www.frontiersin.org/api/v3/articles/1033124/file/Table_1.docx/download">Table 1.docx</a>'
        '</div></body></html>'
    )
    ev.observe("publisher", ssr_html, "https://www.frontiersin.org/articles/10.3389/feart.2022.1033124/full")
    ev.observe("publisher", hydrated_html, "https://www.frontiersin.org/articles/10.3389/feart.2022.1033124/full")

    cands = [
        sf.Candidate(
            url="https://www.frontiersin.org/api/v3/articles/1033124/file/Table_1.docx/download",
            filename="Table_1.docx",
            link_text="Table 1.docx",
        )
    ]
    inc, reason = ev.check(cands)
    assert not inc, f"Expected complete after hydration, got: {reason}"


def test_journal_downloader_exposes_consistent_helpers():
    assert jd.is_crossref_chooser_url is rr.is_crossref_chooser_url
    assert jd.extract_crossref_chooser_urls is rr.extract_crossref_chooser_urls
    assert jd.discover_figshare_by_doi is rr.discover_figshare_by_doi


def test_normalize_doi_handles_publisher_and_non_doi_urls():
    assert rr._normalize_doi("https://www.lyellcollection.org/doi/10.1144/geochem2024-031") == "10.1144/geochem2024-031"
    assert rr._normalize_doi("https://www.lyellcollection.org/doi/full/10.1144/geochem2024-031/full") == "10.1144/geochem2024-031"
    assert rr._normalize_doi("https://chooser.crossref.org/?doi=10.1144%2Fgeochem2024-031") == "10.1144/geochem2024-031"
    assert rr._normalize_doi("10.1130/B36424.1.") == "10.1130/b36424.1"
    assert rr._normalize_doi("https://www.example.com/article/12345") == ""


def test_discover_figshare_by_doi_normalizes_publisher_url_before_query(monkeypatch):
    seen_payloads = []

    class _FakeResp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def _fake_req(http, url, **kwargs):
        if kwargs.get("method") == "POST":
            seen_payloads.append((url, kwargs.get("json_body")))
            if url.endswith("/collections/search"):
                return _FakeResp(200, [])
            if url.endswith("/articles/search"):
                return _FakeResp(200, [{"id": 999111}])
        if url.endswith("/articles/999111"):
            return _FakeResp(200, {
                "title": "Test Figshare Supplement",
                "references": ["https://doi.org/10.1144/geochem2024-031"],
                "files": [{"id": 1, "name": "SuppTable.xlsx", "download_url": "https://ndownloader.figshare.com/files/1"}],
            })
        return None

    monkeypatch.setattr(rr, "_request_with_routes", _fake_req)
    cands, title = rr.discover_figshare_by_doi("https://www.lyellcollection.org/doi/10.1144/geochem2024-031")
    assert title == "Test Figshare Supplement"
    assert [c.filename for c in cands] == ["SuppTable.xlsx"]
    assert all(body["search_for"] == "10.1144/geochem2024-031" for _, body in seen_payloads)


def test_scansci_downloader_extracts_figshare_doi_from_publisher_url(monkeypatch, tmp_path):
    import types
    captured = {}
    empty_html = "<html><head><title>Akcal deposit</title></head><body><p>No links here</p></body></html>"

    monkeypatch.setattr(sc, "resolve_doi_url", lambda u, **kw: u)
    monkeypatch.setattr(sc, "normalize_url", lambda u: u)
    monkeypatch.setattr(sc, "fetch_page_html", lambda url, *a, **kw: (empty_html, 200, url))
    monkeypatch.setattr(sc, "discover_gsa_figshare_candidates", lambda *a, **kw: ([], ""))
    monkeypatch.setattr(sc, "discover_diva_candidates", lambda *a, **kw: ([], ""))
    monkeypatch.setattr(sc, "fetch_sciengine_api_candidates", lambda *a, **kw: ([], ""))

    def _fake_figshare(doi, **kw):
        captured["doi"] = doi
        return [
            sf.Candidate(
                url="https://ndownloader.figshare.com/files/555",
                filename="Table_S1.xlsx",
                link_text="Table_S1.xlsx",
                section="figshare_api",
                match_rule="figshare_api_v2",
                source_url="https://figshare.com/articles/dataset/555",
            )
        ], "Akcal deposit"

    monkeypatch.setattr(rr, "discover_figshare_by_doi", _fake_figshare)
    monkeypatch.setattr(sc, "discover_figshare_by_doi", _fake_figshare)

    args = types.SimpleNamespace(
        url_or_doi="https://www.lyellcollection.org/doi/10.1144/geochem2024-031",
        output_dir=str(tmp_path),
        list_only=True,
        headful=False,
        proxy=None,
        no_vpn=True,
        no_cookies=True,
        no_api=True,
        skip_cdn=True,
        max_mmc=5,
        max_misses=2,
        tunnel_fail_threshold=3,
        no_cloudflare=True,
        no_oa=True,
        oa_only=False,
        oa=False,
        cdp_url=None,
        cdp=False,
        browserskill=False,
        no_bsk=True,
        bsk_browser=None,
        bsk_tab=None,
        phase_timeout=30,
        browser_fallback_budget=15,
        browser_goto_timeout=10,
        timeout=10000,
        cookie_file=None,
        wait_selector=None,
        convert_markdown=False,
    )
    sc.run_download(args, bsk_runtime=types.SimpleNamespace(enabled=False, browser=None, tab_id=None))
    assert captured.get("doi") == "10.1144/geochem2024-031"


def test_journal_downloader_fast_http_chooser_unwrap_encoding_and_dedupe(monkeypatch, tmp_path):
    import types

    chooser_html = '<html><body><a href="https://pub.example.org/article/10.1144/geochem2024-031">Publisher</a></body></html>'
    # Chinese UTF-8 bytes mislabelled as ISO-8859-1 by server
    utf8_article = (
        '<html><head><title>测试文章</title></head><body>'
        '<a href="https://pub.example.org/supp/page">附表1</a>'
        '<a href="https://pub.example.org/files/Table_S1.xlsx">Table S1</a>'
        '</body></html>'
    ).encode("utf-8")

    class _FakeHttpResp:
        def __init__(self, url, text=None, raw_bytes=None, encoding="utf-8", apparent_encoding="utf-8", status_code=200):
            self.url = url
            self._raw = raw_bytes
            self._text = text
            self.encoding = encoding
            self.apparent_encoding = apparent_encoding
            self.status_code = status_code

        @property
        def text(self):
            if self._raw is not None:
                return self._raw.decode(self.encoding or "utf-8", errors="replace")
            return self._text

    class _FakeSession:
        def get(self, url, **kwargs):
            if "chooser.crossref.org" in url:
                return _FakeHttpResp("https://chooser.crossref.org/?doi=10.1144%2Fgeochem2024-031", text=chooser_html)
            return _FakeHttpResp(
                "https://pub.example.org/article/10.1144/geochem2024-031",
                raw_bytes=utf8_article,
                encoding="ISO-8859-1",
                apparent_encoding="utf-8",
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def close(self):
            pass

    monkeypatch.setattr("requests.Session", lambda: _FakeSession())
    monkeypatch.setattr(jd, "stealthy_fetch_bounded", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("unexpected StealthyFetcher call")))
    monkeypatch.setattr(jd, "load_saved_cookies", lambda: [])
    monkeypatch.setattr(jd, "load_local_skill_config", lambda: {})
    # Simulate subpage expansion returning the same file URL already on the page to verify dedupe_candidates runs
    monkeypatch.setattr(
        jd,
        "expand_repository_candidates",
        lambda cands, **kw: list(cands) + [
            sf.Candidate(
                url="https://pub.example.org/files/Table_S1.xlsx",
                filename="Table_S1.xlsx",
                link_text="Duplicate Table S1",
                section="repository_expanded",
                source_url="https://pub.example.org/supp/page",
            )
        ],
    )

    recorded = {}
    orig_finalize = jd.finalize_candidates

    def _capture_finalize(article_dir, cands, **kw):
        recorded["article_dir"] = article_dir
        recorded["cands"] = list(cands)
        return orig_finalize(article_dir, cands, **kw)

    monkeypatch.setattr(jd, "finalize_candidates", _capture_finalize)

    args = types.SimpleNamespace(
        url="https://chooser.crossref.org/?doi=10.1144%2Fgeochem2024-031",
        output_dir=str(tmp_path),
        list_only=True,
        headful=False,
        real_chrome=False,
        proxy=None,
        no_cloudflare=True,
        solve_cloudflare=False,
        no_oa=True,
        oa=False,
        cdp_url=None,
        cdp=False,
        browserskill=False,
        bsk_browser=None,
        bsk_tab=None,
        phase_timeout=30,
        browser_budget=15,
        browser_goto_timeout=10,
        timeout=10000,
        wait_selector=None,
    )
    jd.run_download(args, bsk_runtime=types.SimpleNamespace(enabled=False, browser=None, tab_id=None))
    urls = [c.url for c in recorded["cands"]]
    assert urls.count("https://pub.example.org/files/Table_S1.xlsx") == 1
    assert "测试文章" in os.path.basename(recorded["article_dir"])


def test_journal_downloader_rejects_202_in_fetcher_recovery_and_uses_figshare_doi(monkeypatch, tmp_path):
    import types

    class _FailSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url, **kwargs):
            raise RuntimeError("fast-http failed")

        def close(self):
            pass

    class _FakeFetcherResp:
        status = 202
        text = "<html><head></head><body><script>window.gokuProps={};</script></body></html>"
        html_content = text
        url = "https://www.degruyterbrill.com/document/doi/10.2138/am-2019-6674/html"

    captured = {}

    def _fake_figshare(doi, **kw):
        captured["doi"] = doi
        return [
            sf.Candidate(
                url="https://ndownloader.figshare.com/files/777",
                filename="Supplement_S1.xlsx",
                link_text="Supplement_S1.xlsx",
                section="figshare_api",
                match_rule="figshare_api_v2",
                source_url="https://figshare.com/articles/dataset/777",
            )
        ], "Gutaishan Au-Sb deposit"

    monkeypatch.setattr("requests.Session", lambda: _FailSession())
    monkeypatch.setattr(jd, "load_saved_cookies", lambda: [])
    monkeypatch.setattr(jd, "load_local_skill_config", lambda: {})
    monkeypatch.setattr(jd, "stealthy_fetch_bounded", lambda *a, **kw: None)
    monkeypatch.setattr(jd.Fetcher, "get", lambda *a, **kw: _FakeFetcherResp())
    monkeypatch.setattr(jd, "discover_gsa_figshare_candidates", lambda *a, **kw: ([], ""))
    monkeypatch.setattr(jd, "discover_diva_candidates", lambda *a, **kw: ([], ""))
    monkeypatch.setattr(jd, "discover_figshare_by_doi", _fake_figshare)

    args = types.SimpleNamespace(
        url="10.2138/am-2019-6674",
        output_dir=str(tmp_path),
        list_only=True,
        headful=False,
        real_chrome=False,
        proxy=None,
        no_cloudflare=True,
        solve_cloudflare=False,
        no_oa=True,
        oa=False,
        cdp_url=None,
        cdp=False,
        browserskill=False,
        bsk_browser=None,
        bsk_tab=None,
        phase_timeout=30,
        browser_budget=15,
        browser_goto_timeout=10,
        timeout=10000,
        wait_selector=None,
    )
    jd.run_download(args, bsk_runtime=types.SimpleNamespace(enabled=False, browser=None, tab_id=None))
    assert captured.get("doi") == "10.2138/am-2019-6674"


def test_discovery_evidence_retains_unresolved_container_when_later_observation_is_empty():
    ev = ds.DiscoveryEvidence()
    ssr_html = '<html><body><div class="supplementary-material"><a href="/dl/supp">Download supplementary data</a></div></body></html>'
    empty_html = '<html><body><p>Empty fallback</p></body></html>'
    ev.observe("publisher", ssr_html, "https://example.org/article")
    ev.observe("publisher", empty_html, "https://example.org/article")
    inc, reason = ev.check([])
    assert inc is True
    assert "Supplementary container found in HTML" in reason


def test_pmc_inline_table_and_figure_dashed_and_short_prefixes():
    assert sf.junk_candidate_reason("https://pmc.ncbi.nlm.nih.gov/articles/PMC7530294/table/tbl-1/", text="Table 1") == "article_inline_table_page"
    assert sf.junk_candidate_reason("https://pmc.ncbi.nlm.nih.gov/articles/PMC7530294/table/T1/", text="Table 1") == "article_inline_table_page"
    assert sf.junk_candidate_reason("https://pmc.ncbi.nlm.nih.gov/articles/PMC7530294/figure/fig-1/", text="Fig 1") == "article_figure_page"
    assert sf.junk_candidate_reason("https://pmc.ncbi.nlm.nih.gov/articles/PMC12208662/table/app7table1/", text="Appendix 7") is None
    assert sf.junk_candidate_reason("https://pmc.ncbi.nlm.nih.gov/articles/PMC7530294/table/S1/", text="Table S1") is None
