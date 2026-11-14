import json
import os
import sys
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import journal_downloader as jd
import playwright_utils as pu
import scansci_supp_downloader as ssd
import supp_finder as sf


class _DisabledRuntime:
    enabled = False
    browser = None
    tab_id = None


def _jd_args(tmp_path, **overrides):
    base = dict(
        url="https://pubs.acs.org/doi/10.1021/acsmacrolett.1c00553",
        output_dir=str(tmp_path),
        headful=False,
        proxy=None,
        timeout=60000,
        solve_cloudflare=True,
        no_cloudflare=False,
        wait_selector=None,
        real_chrome=True,
        cdp=False,
        cdp_url="ws://127.0.0.1:9222/devtools/browser/test",
        browserskill=False,
        bsk_browser=None,
        bsk_tab=None,
        list_only=True,
        oa=False,
        no_oa=True,
        convert_markdown=False,
        phase_timeout=150,
        browser_goto_timeout=30,
        browser_fallback_budget=60,
    )
    base.update(overrides)
    return NS(**base)


def _ssd_args(tmp_path, **overrides):
    base = dict(
        url_or_doi="https://www.sciencedirect.com/science/article/pii/S0169136824005171",
        output_dir=str(tmp_path),
        headful=False,
        proxy=None,
        cdp=False,
        cdp_url=None,
        browserskill=False,
        bsk_browser=None,
        bsk_tab=None,
        no_api=True,
        skip_cdn=False,
        no_vpn=True,
        no_cookies=True,
        no_oa=True,
        oa=False,
        list_only=True,
        max_mmc=5,
        max_misses=2,
        convert_markdown=False,
        phase_timeout=30,
        browser_goto_timeout=10,
        browser_fallback_budget=10,
        tunnel_fail_threshold=3,
    )
    base.update(overrides)
    return NS(**base)


# ── Group 1: CDP / StealthyFetcher Fallback & Profile/Cookie Discovery ────────


def test_profile_and_cookie_discovery_includes_zotero_and_cnki(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.chdir(tmp_path)

    for rel in (
        ".zotero_chrome_debug_profile",
        ".zotero_playwright_profile",
        ".zotero_test_chrome_profile",
    ):
        (fake_home / rel).mkdir()

    (fake_home / ".zotero_crawl4ai_cookies.json").write_text(
        json.dumps([{"name": "crawl_tok", "value": "v1", "domain": ".example.com", "path": "/"}]),
        encoding="utf-8",
    )
    (fake_home / "cnki_cookies.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        ".cnki.net\tTRUE\t/\tFALSE\t1999999999\tcnki_sid\tabc123\n",
        encoding="utf-8",
    )

    with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", str(fake_home))):
        dirs = pu.get_common_user_data_dirs()
        for rel in (
            ".zotero_chrome_debug_profile",
            ".zotero_playwright_profile",
            ".zotero_test_chrome_profile",
        ):
            assert str(fake_home / rel) in dirs

        jd_cookies = jd.load_saved_cookies()
        ssd_cookies = ssd.load_saved_cookies_standalone()
        jd_names = {c["name"] for c in (jd_cookies or [])}
        ssd_names = {c["name"] for c in (ssd_cookies or [])}
        assert {"crawl_tok", "cnki_sid"} <= jd_names
        assert {"crawl_tok", "cnki_sid"} <= ssd_names


def test_fetch_page_via_cdp_settles_transient_403_challenge_to_200():
    listeners = {}
    state = {"settled": False}

    class _FakePage:
        url = "https://pubs.acs.org/doi/10.1021/acsmacrolett.1c00553"
        main_frame = object()

        def on(self, event, fn):
            listeners[event] = fn

        def goto(self, target_url, wait_until=None, timeout=None):
            return NS(status=403)

        def wait_for_load_state(self, *a, **k):
            pass

        def wait_for_timeout(self, ms):
            state["settled"] = True
            cb = listeners.get("response")
            if cb:
                cb(NS(status=200, frame=self.main_frame, request=NS(resource_type="document")))

        def evaluate(self, script):
            return 0

        def content(self):
            if not state["settled"]:
                return "<html><head><title>Just a moment...</title><script>window.cf=1;</script></head></html>"
            return "<html><head><title>Real ACS Article</title></head><body><div class='article'>Full text</div></body></html>"

        def close(self):
            pass

    fake_ctx = NS(pages=[], new_page=lambda: _FakePage(), cookies=lambda *a, **k: [])
    fake_browser = NS(contexts=[fake_ctx], close=lambda: None)
    fake_pw = NS(chromium=NS(connect_over_cdp=lambda endpoint, **kwargs: fake_browser))
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_pw
    fake_cm.__exit__.return_value = False

    with patch("playwright.sync_api.sync_playwright", return_value=fake_cm):
        html_out, status, final_url, _ = pu.fetch_page_via_cdp(
            "ws://127.0.0.1:9222/devtools/browser/abc",
            "https://pubs.acs.org/doi/10.1021/acsmacrolett.1c00553",
            timeout=10,
        )
    assert status == 200
    assert "Real ACS Article" in html_out
    assert final_url == "https://pubs.acs.org/doi/10.1021/acsmacrolett.1c00553"


def test_journal_downloader_cdp_blocked_page_retries_standalone_with_real_chrome(monkeypatch, tmp_path):
    calls = []

    class _FakeFetcher:
        @staticmethod
        def fetch(url, **kwargs):
            calls.append(dict(kwargs))
            if "cdp_url" in kwargs:
                return NS(
                    html_content="<html><head><title>403</title><script>var a=1;</script></head></html>",
                    text="",
                    url=url,
                    status=403,
                )
            return NS(
                html_content=(
                    "<html><head><title>ACS Paper</title></head><body>"
                    "<div class='widget-ArticleDataSupplements'>"
                    "<a href='/doi/suppl/10.1021/acsmacrolett.1c00553/article-supplement/1/pdf/si_001/'>Supporting Info</a>"
                    "</div></body></html>"
                ),
                text="",
                url=url,
                status=200,
            )

    monkeypatch.setattr(jd, "StealthyFetcher", _FakeFetcher)
    monkeypatch.setattr(jd, "load_saved_cookies", lambda: None)
    monkeypatch.setattr(jd, "resolve_cdp_ws_url", lambda target: target)
    monkeypatch.setattr(jd, "extract_cdp_cookies", lambda target: [])
    monkeypatch.setattr(jd, "fetch_page_via_cdp", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cdp failed")))

    jd.run_download(_jd_args(tmp_path, real_chrome=True), _DisabledRuntime())

    assert len(calls) == 2
    assert "cdp_url" in calls[0]
    assert "real_chrome" not in calls[0]
    assert "cdp_url" not in calls[1]
    assert calls[1].get("real_chrome") is True


# ── Group 2: <NO BODY> 403 Shells & F5 /TSPD/ Interstitials ──────────────────


def test_is_blocked_page_detects_nobody_403_and_tspd_interstitials():
    nobody_shell = (
        "<!DOCTYPE html><html><head>"
        "<meta charset='utf-8'><title>Article</title>"
        "<script src='/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1'></script>"
        "</head></html>"
    )
    assert sf.is_blocked_page(nobody_shell) is True

    tspd_interstitial = (
        "<!DOCTYPE html><html><head><title>IEEE Xplore</title>"
        "<script type='text/javascript' src='/TSPD/0812be273cab2000abc?type=12'></script>"
        "</head><body><noscript>Enable JS</noscript>"
        "<script>window.apm_do_not_touch = 1;</script></body></html>"
    )
    assert sf.is_blocked_page(tspd_interstitial) is True

    # Real IEEE Xplore article also loads /TSPD/ scripts, but has full article body
    real_ieee = (
        "<!DOCTYPE html><html><head><title><!----><!----></title>"
        "<script src='/TSPD/0812be273cab2000abc?type=12'></script></head><body>"
        "<main><h1>Mineral Mapping Study</h1><p>" + ("Detailed scientific methodology and results. " * 25) + "</p></main>"
        "</body></html>"
    )
    assert sf.is_blocked_page(real_ieee) is False
    # Minimal synthetic test HTML stubs without <head>+<script> stay unblocked
    assert sf.is_blocked_page("<html></html>") is False
    assert sf.is_blocked_page("<html>article</html>") is False


# ── Group 3 & New Principle: Silverchair/GSW & Structural-Candidate Preservation ─


def test_gsw_figshare_widget_extracted_and_geosapien_and_cloudfront_figures_rejected():
    html = """
    <html><body>
      <div class="content-inner-wrap">
        <div class="geosapien-widget">
          <a href="https://geosapien.ai?X-DOI=10.1130%2FG31272.1">Try GeoSapien AI</a>
        </div>
        <div class="article-body">
          <p>See Appendix for geochemical methods and Table S1 for full dataset.</p>
          <div class="fig-toolbar">
            <a href="https://cdn.example.com/g53947.1_f03.png?Signature=abc128ESMxyz&amp;Key-Pair-Id=K1">View large</a>
            <a href="/DownloadFile/DownloadImage.aspx?image=/g53947/f03.png&amp;Signature=128ESM">Download slide</a>
          </div>
        </div>
        <div class="figshare-wrapper hascontent" id="articlefulltext_figshare">
          <figshare-widget>
            <h3>Supplemental material: Short-wavelength infrared spectroscopy</h3>
            <button type="button">Skip to fig share navigation</button>
            <a href="https://ndownloader.figstatic.com/files/22759988" aria-label="Download">Download</a>
            <a href="https://figshare.com/articles/journal_contribution/Supplemental_material/12349100" aria-label="Open in figshare">fig share</a>
          </figshare-widget>
        </div>
      </div>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://pubs.geoscienceworld.org/gsa/geology/article/38/10/927/130381")
    urls = [c.url for c in diag.candidates]
    assert "https://ndownloader.figstatic.com/files/22759988" in urls
    assert "https://figshare.com/articles/journal_contribution/Supplemental_material/12349100" in urls
    assert not any("geosapien.ai" in u for u in urls)
    assert not any("g53947.1_f03.png" in u or "DownloadImage.aspx" in u for u in urls)
    assert diag.is_incomplete is False
    assert diag.needs_browser is False


def test_structural_supp_candidates_preserved_against_downstream_figure_heuristics():
    """Design principle: recall > precision. Files extracted from explicit HTML
    supplementary containers MUST NOT be dropped by heuristic figure/filename rules
    (e.g., 'gr1', 'fig2', 'fx1', '_f03.png') in add_candidate or post-filters."""
    html = """
    <html><body>
      <section id="supplementary-material" class="supplementary-material">
        <h2>Supplementary Material</h2>
        <a href="https://example.org/files/supp_gr1_dataset.png">Supplementary Fig. 1 dataset</a>
        <a href="https://example.org/files/fx1_spectra_supplement.svg">Supporting Data File FX1</a>
        <a href="https://example.org/files/fig1_measurements.pdf">Supplemental Data for Figure 99</a>
      </section>
      <div class="widget-ArticleDataSupplements">
        <a href="https://example.org/content/g53947_f03.png">Supplemental Table Archive</a>
      </div>
      <!-- Regular inline figure outside supplementary containers IS still excluded -->
      <div class="figure">
        <a href="https://example.org/img/gr1_lrg.jpg">View large</a>
      </div>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://pubs.geoscienceworld.org/gsa/geology/article/1/1/1/1")
    urls = {c.url for c in diag.candidates}
    assert "https://example.org/files/supp_gr1_dataset.png" in urls
    assert "https://example.org/files/fx1_spectra_supplement.svg" in urls
    assert "https://example.org/files/fig1_measurements.pdf" in urls
    assert "https://example.org/content/g53947_f03.png" in urls
    assert "https://example.org/img/gr1_lrg.jpg" not in urls


# ── Group 4: ChinaGeology (RHHZ2) Dual basePath & Empty Containers ────────────


def test_rhhz2_dual_basepath_prefers_root_and_ignores_pagetype_lang_link():
    html = """
    <html><body>
      <input id="basePath" type="hidden" value="/fileZGDZYW/journal/article/zgdzyw/2026/4/">
      <a href="http://chinageology.cgs.cn/article/doi/10.31035/cg2025065?pageType=en">Appendix 1</a>
      <div class="Supplements-left-list">
        <a href="javascript:;" onclick="exportSupplementary('25a04aad-b709-4b7a-826b-9ceae0928c38')">Appendix 1</a>
      </div>
      <input type="hidden" id="basePath" value="/">
    </body></html>
    """
    diag = sf.find_all_candidates(html, "http://chinageology.cgs.cn/article/doi/10.31035/cg2025065")
    assert len(diag.candidates) == 1
    assert diag.candidates[0].url == "http://chinageology.cgs.cn/article/exportSupplementary?id=25a04aad-b709-4b7a-826b-9ceae0928c38"
    assert diag.is_incomplete is False
    assert diag.needs_browser is False


def test_empty_rhhz2_supplements_left_list_not_unresolved_or_needs_browser():
    html = """
    <html><body>
      <div class="article-content"><h1>Geology Paper</h1><p>Full text without supplements.</p></div>
      <div class="Supplements-left-list"><a href="javascript:;"></a></div>
      <div class="supplementary-empty"><a href=""></a></div>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "http://chinageology.cgs.cn/article/doi/10.31035/cg2025187")
    assert diag.candidates == []
    assert diag.needs_browser is False
    assert "unresolved section" not in diag.incomplete_reason


# ── Group 5: Opaque Bundles, 'esm' Substring, Lyell Menu, IOP Inline Tables ───


def test_opaque_bundles_wiley_frontiers_sciengine_and_esm_substring_fixes():
    wiley_c = sf.Candidate(
        url="https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002%2Fadma.201908311&file=adma201908311-sup-0001-SuppMat.pdf",
        filename="adma201908311-sup-0001-SuppMat.pdf",
        link_text="Supporting Information",
        section="wiley_supplement",
    )
    frontiers_c = sf.Candidate(
        url="https://public-pages-files-2025.frontiersin.org/articles/1366727/file/Data_Sheet_1.docx/13",
        filename="Data_Sheet_1.docx",
        link_text="Data Sheet 1.docx",
        section="frontiers_supplement",
    )
    sciengine_c = sf.Candidate(
        url="https://www.sciengine.com/doi/10.1360/N072021-0099/supp/附录_1.xls",
        filename="附录_1.xls",
        link_text="附录 1",
        section="chinese_platform_supplement",
        covered_table_ids={"A1"},
    )
    assert sf.has_opaque_bundle_candidate([wiley_c]) is True
    assert sf.has_opaque_bundle_candidate([frontiers_c]) is True
    assert sf.has_opaque_bundle_candidate([sciengine_c]) is True

    # SciEngine bundle covers declared 附表S1 via opaque bundle exemption
    sci_html = "<html><body><div class='suppl-sec'><a href='/supp/附录_1.xls'>附录 1</a></div><p>数据见附表S1。</p></body></html>"
    sci_diag = sf.find_all_candidates(sci_html, "https://www.sciengine.com/doi/10.1360/N072021-0099")
    assert sci_diag.is_incomplete is False

    # De Gruyter <div id="servicesMenu"> must NOT match 'esm' substring
    dg_html = (
        "<html><body>"
        "<div id='servicesMenu'><a href='/publishing/services/for-booksellers'>For Booksellers &amp; Library Suppliers</a></div>"
        "<article><p>Mineral physics article.</p></article>"
        "</body></html>"
    )
    dg_diag = sf.find_all_candidates(dg_html, "https://www.degruyter.com/document/doi/10.1515/am-2020-7075/html")
    assert dg_diag.needs_browser is False
    assert "unresolved section" not in dg_diag.incomplete_reason


def test_lyell_publication_menu_and_iop_inline_appendix_tables():
    lyell_html = """
    <html><body>
      <div class="publication__menu">
        <ul class="publication__sup__menu rlist dropdown__menu">
          <li><a href="/toc/geea/0/ja">Accepted manuscripts</a></li>
          <li><a href="https://geolsoc.figshare.com/">Supplementary material</a></li>
        </ul>
      </div>
      <div class="core-collateral" id="core-collateral-info"><p>Published In GEEA</p></div>
      <article><p>Main geochemistry text with no downloadable supplements.</p></article>
    </body></html>
    """
    lyell_diag = sf.find_all_candidates(lyell_html, "https://www.lyellcollection.org/doi/10.1144/geea2017-015")
    assert lyell_diag.needs_browser is False

    iop_html = """
    <html><body>
      <article>
        <h2>Data availability statement</h2>
        <p>The data cannot be made publicly available upon publication because they are owned by a third party.</p>
        <h2>Appendix A: Performance statistics for models M1 to M6</h2>
        <p>Table A1. Performance statistics for model M1.</p>
        <div class="boxout"><table id="mlstad3c0ftA1"><tr><th>Metric</th><th>Val</th></tr></table></div>
        <p>Table A2. Performance statistics for model M2.</p>
        <div class="boxout"><table id="mlstad3c0ftA2"><tr><th>Metric</th><th>Val</th></tr></table></div>
      </article>
    </body></html>
    """
    iop_diag = sf.find_all_candidates(iop_html, "https://iopscience.iop.org/article/10.1088/2632-2153/ad3c0f")
    assert "Missing supplementary tables" not in iop_diag.incomplete_reason
    assert iop_diag.needs_browser is False


# ── Group 6: IEEE Title Extraction & Elsevier --list-only Message ─────────────


def test_ieee_title_ignores_angular_comments_and_uses_xplglobal_metadata():
    ieee_html = """
    <!DOCTYPE html>
    <html>
      <head>
        <title><!----><!----></title>
        <script>
          xplGlobal.document.metadata={"title":"Mineral Identification and Mapping by Synthesis of Hyperspectral Data","doi":"10.1109/JSTARS.2020.2998091"};
        </script>
      </head>
      <body><h1>Article</h1></body>
    </html>
    """
    title = sf.extract_article_title(ieee_html)
    assert title == "Mineral Identification and Mapping by Synthesis of Hyperspectral Data"
    assert "!----" not in title


def test_elsevier_list_only_does_not_claim_cdn_brute_force_ran(monkeypatch, tmp_path, capsys):
    html = "<html><head><title>Elsevier Test</title></head><body><article><p>No supplements.</p></article></body></html>"
    monkeypatch.setattr(ssd, "HAS_SCRAPLING", False)
    monkeypatch.setattr(ssd, "_setup_authenticated_session_and_cookies", lambda *a, **k: (MagicMock(), []))
    monkeypatch.setattr(ssd, "fetch_page_html", lambda *a, **k: (html, 200, _ssd_args(tmp_path).url_or_doi))
    ssd.run_download(_ssd_args(tmp_path, list_only=True, no_api=True, skip_cdn=False), _DisabledRuntime())
    out = capsys.readouterr().out
    assert "CDN brute-force also found nothing" not in out


def test_cdp_settles_during_networkidle_refreshes_cookies_and_handles_inplace_dom_settle():
    listeners = {}
    state = {"settled": False, "cookies": [{"name": "init", "value": "1", "domain": ".example.com", "path": "/"}]}

    class _FakePage:
        url = "https://pubs.acs.org/doi/10.1021/acsmacrolett.1c00553"
        main_frame = object()

        def on(self, event, fn):
            listeners[event] = fn

        def goto(self, target_url, wait_until=None, timeout=None):
            return NS(status=403)

        def wait_for_load_state(self, *a, **k):
            state["settled"] = True
            state["cookies"].append({"name": "cf_clearance", "value": "solved", "domain": ".acs.org", "path": "/"})
            cb = listeners.get("response")
            if cb:
                cb(NS(status=200, frame=self.main_frame, request=NS(resource_type="document")))

        def wait_for_timeout(self, ms):
            pass

        def evaluate(self, script):
            return 0

        def content(self):
            if not state["settled"]:
                return "<html><head><title>Just a moment...</title><script>window.cf=1;</script></head></html>"
            return "<html><head><title>Settled Article</title></head><body><p>Full text</p></body></html>"

        def close(self):
            pass

    fake_ctx = NS(pages=[], new_page=lambda: _FakePage(), cookies=lambda *a, **k: list(state["cookies"]))
    fake_browser = NS(contexts=[fake_ctx], close=lambda: None)
    fake_pw = NS(chromium=NS(connect_over_cdp=lambda endpoint, **kwargs: fake_browser))
    fake_cm = MagicMock()
    fake_cm.__enter__.return_value = fake_pw
    fake_cm.__exit__.return_value = False

    with patch("playwright.sync_api.sync_playwright", return_value=fake_cm):
        html_out, status, _, cookies_out = pu.fetch_page_via_cdp(
            "ws://127.0.0.1:9222/devtools/browser/abc",
            "https://pubs.acs.org/doi/10.1021/acsmacrolett.1c00553",
            timeout=10,
        )
    assert status == 200
    assert "Settled Article" in html_out
    assert {c["name"] for c in cookies_out} >= {"init", "cf_clearance"}

    # In-place XHR/JS settle without a second document response event clears stale 403
    state2 = {"settled": False}

    class _FakeInPlacePage:
        url = "https://example.org/article"
        main_frame = object()

        def on(self, event, fn):
            pass

        def goto(self, target_url, wait_until=None, timeout=None):
            return NS(status=403)

        def wait_for_load_state(self, *a, **k):
            pass

        def wait_for_timeout(self, ms):
            state2["settled"] = True

        def evaluate(self, script):
            return 0

        def content(self):
            if not state2["settled"]:
                return "<html><head><title>Challenge</title><script>var c=1;</script></head></html>"
            return "<html><head><title>Paper</title></head><body><main>" + ("Science text. " * 20) + "</main></body></html>"

        def close(self):
            pass

    fake_ctx2 = NS(pages=[], new_page=lambda: _FakeInPlacePage(), cookies=lambda *a, **k: [])
    fake_browser2 = NS(contexts=[fake_ctx2], close=lambda: None)
    fake_pw2 = NS(chromium=NS(connect_over_cdp=lambda endpoint, **kwargs: fake_browser2))
    fake_cm2 = MagicMock()
    fake_cm2.__enter__.return_value = fake_pw2
    fake_cm2.__exit__.return_value = False

    with patch("playwright.sync_api.sync_playwright", return_value=fake_cm2):
        html2, status2, _, _ = pu.fetch_page_via_cdp(
            "ws://127.0.0.1:9222/devtools/browser/abc",
            "https://example.org/article",
            timeout=10,
        )
    assert status2 is None
    assert "Science text." in html2
    assert sf.page_fetch_is_failure(html2, status2) is False


def test_journal_downloader_cdp_blocked_then_standalone_error_falls_back_to_http(monkeypatch, tmp_path):
    http_calls = []

    class _FakeFetcher:
        @staticmethod
        def fetch(url, **kwargs):
            if "cdp_url" in kwargs:
                return NS(
                    html_content="<html><head><title>403</title><script>var a=1;</script></head></html>",
                    text="",
                    url=url,
                    status=403,
                )
            raise RuntimeError("standalone crashed")

    class _FakeHttpFetcher:
        @staticmethod
        def get(url, **kwargs):
            http_calls.append(url)
            return NS(
                html_content=(
                    "<html><head><title>Recovered Paper</title></head><body>"
                    "<div class='widget-ArticleDataSupplements'>"
                    "<a href='/doi/suppl/10.1021/acsmacrolett.1c00553/article-supplement/1/pdf/si_001/'>Supporting Info</a>"
                    "</div></body></html>"
                ),
                text="",
                url=url,
                status=200,
            )

    monkeypatch.setattr(jd, "StealthyFetcher", _FakeFetcher)
    monkeypatch.setattr(jd, "Fetcher", _FakeHttpFetcher)
    monkeypatch.setattr(jd, "load_saved_cookies", lambda: None)
    monkeypatch.setattr(jd, "resolve_cdp_ws_url", lambda target: target)
    monkeypatch.setattr(jd, "extract_cdp_cookies", lambda target: [])
    monkeypatch.setattr(jd, "fetch_page_via_cdp", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("cdp failed")))

    jd.run_download(_jd_args(tmp_path), _DisabledRuntime())
    assert len(http_calls) == 1


def test_netscape_empty_value_cookie_and_rhhz2_zero_vs_nonzero_supplements(tmp_path):
    jar = tmp_path / "cnki_cookies.txt"
    jar.write_text(
        "# Netscape HTTP Cookie File\n"
        ".cnki.net\tTRUE\t/\tFALSE\t1999999999.5\tempty_tok\t\n"
        "#HttpOnly_.example.org\tTRUE\t/\tTRUE\t1999999999\tsid\tval123\n",
        encoding="utf-8",
    )
    items = jd._parse_cookie_file_items(str(jar))
    items_sa = ssd._parse_cookie_file_items_standalone(str(jar))
    assert {(c["name"], c["value"], c.get("expires")) for c in items} == {
        ("empty_tok", "", 1999999999),
        ("sid", "val123", 1999999999),
    }
    assert items == items_sa

    rhhz2_zero_html = """
    <html><body>
      <ul class="article-tab-nav">
        <li><a href="#Supplements" data-toggle="tab">Supplements</a></li>
      </ul>
      <div class="phone-title"><a href="javascript:void(0);">Supplements <num>(0)</num></a></div>
      <li class="tab-pane" id="Supplements"></li>
      <div class="Supplements-left-list"><a href=""></a></div>
    </body></html>
    """
    diag_zero = sf.find_all_candidates(rhhz2_zero_html, "http://chinageology.cgs.cn/article/doi/10.31035/cg2025187")
    assert diag_zero.needs_browser is False

    rhhz2_one_html = rhhz2_zero_html.replace("<num>(0)</num>", "<num>(1)</num>")
    diag_one = sf.find_all_candidates(rhhz2_one_html, "http://chinageology.cgs.cn/article/doi/10.31035/cg2025065")
    assert diag_one.needs_browser is True


def test_strategy3_publisher_extractors_mark_structural_supp_and_preserve_figure_like_names():
    html = """
    <html><body>
      <div class="Appendices">
        <a class="download-link" href="/cms/attachment/123/fx1_appendix.pdf">Appendix Data FX1</a>
      </div>
      <div class="c-article-supplementary">
        <a data-test="supplementary-file-link" href="https://static-content.springer.com/esm/art/gr1_supplement.png">Supplementary Item 1</a>
      </div>
      <a href="/doi/suppl/10.1021/test/suppl_file/fig1_spectra.png">Supporting Fig 1 Data</a>
      <section class="supplementary-materials">
        <a href="/pmc/articles/PMC1/bin/g53947_f03.png">Additional File 1</a>
      </section>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.sciencedirect.com/science/article/pii/S0000000000000001")
    by_fname = {c.filename: c for c in diag.candidates}
    for fname in ("fx1_appendix.pdf", "gr1_supplement.png", "fig1_spectra.png", "g53947_f03.png"):
        assert fname in by_fname
        assert by_fname[fname].extra_info.get("structural_supp") is True

    # IEEE Scrapling-like response object with semicolons inside metadata JSON
    resp_obj = NS(
        html_content=(
            "<html><head><title><!----><!----></title>"
            '<script>xplGlobal.document.metadata={"abstract":"A; B}; C","title":"IEEE Robust Title"};</script>'
            "</head></html>"
        ),
        css=lambda sel: NS(get=lambda: ""),
    )
    assert sf.extract_article_title(resp_obj) == "IEEE Robust Title"
