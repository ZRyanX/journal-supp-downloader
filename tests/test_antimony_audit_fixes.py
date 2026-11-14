"""Regression tests for every bug & integration fix from the Zotero antimony audit."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import browserskill_adapter as bsk
import discovery_state as ds
import journal_downloader as jd
import markitdown_converter as mdc
import playwright_utils as pu
import scansci_supp_downloader as ssd
import supp_finder as sf


# ── Group 1: Page-level & download-validation false positives ────────────────

def test_is_blocked_page_ignores_script_turnstile_and_navbar_institutional_login():
    mdpi_html = (
        "<html><head><title>Minerals | Antimony Isotopes</title>"
        "<script>window.__NUXT__={captcha:'turnstile',cf:'challenge-platform'};</script>"
        "</head><body><header><nav><a href='/login'>Institutional Login</a></nav></header>"
        "<main><article><h1>Antimony Deposits</h1>"
        + ("Geochemistry of stibnite deposits in South China. " * 20)
        + "<section id='supplementary'><h2>Supplementary Materials</h2>"
        "<a href='/2075-163X/15/8/814/s1'>Supplementary Table S1</a></section>"
        "</article></main></body></html>"
    )
    assert not sf.is_blocked_page(mdpi_html)
    assert not sf.page_fetch_is_failure(mdpi_html, 200)


def test_is_blocked_page_still_detects_real_challenge_and_login_wall():
    cf_wall = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"
    login_wall = (
        "<html><head><title>Sign in</title></head>"
        "<body><div class='login-box'><h1>Institutional Login</h1>"
        "<p>Please sign in to access this article.</p></div></body></html>"
    )
    assert sf.is_blocked_page(cf_wall)
    assert sf.is_blocked_page(login_wall)


def test_validate_downloaded_file_allows_standalone_html_table_with_script_markers(tmp_path):
    html_table = (
        "<!DOCTYPE html><html><head><title>Table S1</title>"
        "<script>window.cf='challenge-platform';window.t='turnstile';</script>"
        "</head><body><header><nav>Institutional Login | Sign in</nav></header>"
        "<main><table><tr><th>Sample</th><th>Sb (ppm)</th></tr>"
        "<tr><td>ST-01</td><td>1420</td></tr><tr><td>ST-02</td><td>980</td></tr>"
        "</table></main></body></html>"
    ).encode("utf-8")
    p = tmp_path / "table_s1.html"
    p.write_bytes(html_table)
    ok, reason = sf.validate_downloaded_file(str(p), headers={"content-type": "text/html"})
    assert ok, reason
    assert "HTML table attachment" in reason


def test_validate_downloaded_file_still_rejects_csv_challenge_and_html_challenge(tmp_path):
    csv_wall = tmp_path / "S1.csv"
    csv_wall.write_text("Client Challenge - please verify you are human", encoding="utf-8")
    ok_csv, _ = sf.validate_downloaded_file(str(csv_wall))
    assert not ok_csv

    html_wall = tmp_path / "table1.html"
    html_wall.write_text(
        "<html><head><title>Client Challenge</title></head>"
        "<body><table><tr><td>Checking your browser</td></tr></table></body></html>",
        encoding="utf-8",
    )
    ok_html, _ = sf.validate_downloaded_file(str(html_wall))
    assert not ok_html


# ── Group 2: Table ID extraction false negatives & completeness ──────────────

@pytest.mark.parametrize("text,ctx,expected", [
    ("Data are available in ESM 2 Table S1.", False, {"S1"}),
    ("See details in ESM 2 Tables S2 and S3.", False, {"S2", "S3"}),
    ("ESM Tables 1", False, {"S1"}),
    ("Supp. Table 1", False, {"S1"}),
    ("Supp. Tables 1-4", False, {"S1", "S2", "S3", "S4"}),
    ("Electronic Supplementary Material Table 2", False, {"S2"}),
    ("Table_S1.xlsx", True, {"S1"}),
    ("table-s1.pdf", True, {"S1"}),
    ("S1.xlsx", True, {"S1"}),
    ("Table S1_S3.xlsx", True, {"S1", "S2", "S3"}),
])
def test_supplementary_table_ids_handles_esm_supp_and_filename_variants(text, ctx, expected):
    assert sf.supplementary_table_ids(text, in_supplementary_context=ctx) == expected


def test_prose_esm_number_and_bare_underscore_do_not_false_mint():
    assert sf.supplementary_table_ids("ESM 2 contains raw spectra.") == set()
    assert sf.supplementary_table_ids("gene_S1_S5 expression") == set()
    assert sf.supplementary_table_ids("S1") == set()


def test_bundled_files_do_not_trigger_missing_tables_false_incomplete():
    html = (
        "<html><body><section id='supplementary'>"
        "<p>Electronic Supplementary Material includes Tables S1-S6.</p>"
        "<a href='https://static-content.springer.com/esm/art/MOESM2_ESM.xlsx'>ESM 2</a>"
        "</section></body></html>"
    )
    cands = [sf.Candidate(
        url="https://static-content.springer.com/esm/art/MOESM2_ESM.xlsx",
        filename="MOESM2_ESM.xlsx",
        link_text="ESM 2",
        section="springer_nature_download",
    )]
    inc, reason = sf.is_result_obviously_incomplete(cands, html)
    assert not inc, reason

    ev = ds.DiscoveryEvidence()
    ev.observe("publisher", html, "https://link.springer.com/article/10.1007/s00126-025-01387-3")
    ev_inc, ev_reason = ev.check(cands)
    assert not ev_inc, ev_reason


def test_table_s1_html_not_filtered_as_nav():
    html = (
        "<html><body><section id='supplementary'>"
        "<a href='https://example.com/article/table_s1.html'>Table S1</a>"
        "<a href='https://example.com/files/Appendix%207.html'>Appendix 7</a>"
        "</section></body></html>"
    )
    diag = sf.find_all_candidates(html, "https://example.com/article/")
    urls = [c.url for c in diag.candidates]
    assert "https://example.com/article/table_s1.html" in urls
    assert "https://example.com/files/Appendix%207.html" not in urls


def test_publisher_extractors_preserve_spaces_across_child_tags():
    html = (
        "<html><body>"
        "<a data-track-action='download' href='https://static-content.springer.com/esm/MOESM2_ESM.xlsx'>"
        "<span>ESM 2</span><span>Table S1</span></a>"
        "<a href='/action/downloadSupplement?doi=10.1111/test&file=sup1.xlsx'>"
        "<span>Supp.</span><span>Table S2</span></a>"
        "<a href='/article/file?id=10.1371/test.s001'>"
        "<span>S3</span><span>Table</span></a>"
        "<a data-component='attachment' href='/mmc1.xlsx'><span>MMC</span><span>1</span></a>"
        "</body></html>"
    )
    diag = sf.find_all_candidates(html, "https://example.com/article")
    by_text = {c.link_text for c in diag.candidates}
    assert "ESM 2 Table S1" in by_text
    assert "Supp. Table S2" in by_text
    assert "S3 Table" in by_text
    assert "MMC 1" in by_text


# ── Group 3: False positives ─────────────────────────────────────────────────

def test_stable_and_support_supply_and_sciencedirect_pii_and_wiley_m_jpg():
    # "stable" / "Stable 13C" must not match STable
    assert not sf.is_explicit_supp_table("Stable 13C and 34S isotopes")
    assert not sf.is_explicit_supp_table("stable isotope ratios")
    assert sf.is_explicit_supp_table("STable 1")
    assert sf.is_explicit_supp_table("S-Table 2")
    assert sf.TABLE_LABEL_PATTERN.search("Stable 13C isotope table") is None
    assert sf.TABLE_FILENAME_PATTERN.search("stable_isotopes_1.xlsx") is None

    # "support" / "supply" must not match SUPP_KEYWORD_PATTERN
    assert sf.SUPP_KEYWORD_PATTERN.search("Help and support") is None
    assert sf.SUPP_KEYWORD_PATTERN.search("Ore fluid supply") is None
    assert sf.SUPP_KEYWORD_PATTERN.search("Supplementary Data") is not None
    assert sf.SUPP_KEYWORD_PATTERN.search("supp_table_1.xlsx") is not None
    assert sf.SUPP_KEYWORD_PATTERN.search("suppData.xlsx") is not None

    # ScienceDirect PII must not match MDPI /s\d+ path
    pii_url = "https://www.sciencedirect.com/science/article/pii/S0169136824001234"
    matched, rule = sf.should_match_candidate(pii_url, text="Article link", attrs={})
    assert not matched and rule != "mdpi_supp_path"
    assert sf.extract_filename_from_url(pii_url) != "S0169136824001234.zip"

    mdpi_url = "https://www.mdpi.com/2075-163X/15/8/814/s1"
    matched_m, rule_m = sf.should_match_candidate(mdpi_url, text="Supplementary Materials", attrs={})
    assert matched_m and rule_m == "mdpi_supp_path"
    assert sf.extract_filename_from_url(mdpi_url) == "s1.zip"

    # Wiley -m.jpg figure suffix must be recognized as article figure
    assert sf.is_article_figure_url("https://onlinelibrary.wiley.com/cms/asset/abc/ecog1234-fig-0004-m.jpg")


# ── Group 4: DiscoveryEvidence, MirrorDiscovery, Tier 0 deadline, retries ────

def test_discovery_evidence_ignores_references_section_and_caches_parse():
    html = (
        "<html><body>"
        "<main><section id='supplementary'><p>Tables S1-S2</p>"
        "<a href='https://example.com/S1.xlsx'>Table S1</a>"
        "<a href='https://example.com/S2.xlsx'>Table S2</a>"
        "</section>"
        "<section id='references' class='references'>"
        "<p>Smith et al. (2020) Supplementary Tables S1-S9.</p>"
        "</section></main></body></html>"
    )
    ev = ds.DiscoveryEvidence()
    ev.observe("publisher", html, "https://example.com/article")
    assert ev.tables == {"S1", "S2"}
    cands = [
        sf.Candidate(url="https://example.com/S1.xlsx", filename="S1.xlsx", link_text="Table S1"),
        sf.Candidate(url="https://example.com/S2.xlsx", filename="S2.xlsx", link_text="Table S2"),
    ]
    inc, reason = ev.check(cands)
    assert not inc, reason


def test_mirror_discovery_skips_seeded_publisher_and_final_urls():
    fetched = []
    ev = ds.DiscoveryEvidence()
    mirrors = ds.MirrorDiscovery(
        "10.3390/min15080814",
        True,
        lambda d: SimpleNamespace(oa_landing_urls=[
            "https://www.mdpi.com/2075-163X/15/8/814/",
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC123456/",
        ]),
        lambda u: (fetched.append(u) or "", 404, u),
        ev,
        initial_urls=["https://www.mdpi.com/2075-163X/15/8/814"],
    )
    mirrors.discover()
    assert fetched == ["https://pmc.ncbi.nlm.nih.gov/articles/PMC123456/"]


def test_try_elsevier_cdn_brute_force_respects_expired_deadline(tmp_path):
    with patch.object(ssd.requests.Session, "head") as mock_head:
        files = ssd.try_elsevier_cdn_brute_force(
            "S0169136824001234",
            str(tmp_path),
            max_mmc=10,
            deadline=0.0,  # already expired
        )
        assert files == []
        assert mock_head.call_count == 0


def test_scansci_fetch_page_html_passes_retries_1_and_strips_script_for_scansci_challenge(monkeypatch):
    seen_kwargs = {}
    mdpi_like = (
        "<html><head><title>Antimony Ore</title>"
        "<script>const cf='challenge-platform';</script></head>"
        "<body><main><p>Full article content</p></main></body></html>"
    )

    class _FakeResp:
        status_code = 200
        url = "https://www.mdpi.com/2075-163X/15/8/814"
        text = mdpi_like

    fake_session = MagicMock()
    fake_session.get.return_value = _FakeResp()
    html, status, _ = ssd.fetch_page_html(
        "https://www.mdpi.com/2075-163X/15/8/814",
        fake_session,
        [],
        headful=False,
    )
    assert status == 200 and "Full article content" in html

    # Now test browser path gets retries=1
    monkeypatch.setattr(ssd, "HAS_SCRAPLING", True)
    monkeypatch.setattr(
        ssd.StealthyFetcher,
        "fetch",
        staticmethod(lambda u, **kw: (seen_kwargs.update(kw) or SimpleNamespace(html_content="<html><title>OK</title><body><p>Done</p></body></html>", status=200, url=u))),
    )
    html2, st2, _ = ssd.fetch_page_html("https://example.com/a", None, [], headful=False)
    assert st2 == 200
    assert seen_kwargs.get("retries") == 1


# ── Group 5: MarkItDown, BrowserSkill, Playwright request ────────────────────

def test_markitdown_ignores_candidate_json_unwraps_preview_and_flattens_excel(tmp_path):
    assert not mdc.is_convertible_file("supplementary_candidates.json")
    assert not mdc.is_convertible_file("supplementary_candidates.partial.json")

    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Table S1"
    ws.append(["Table S1. Stibnite trace elements (ppm)", None, None, None])
    ws.merge_cells("A1:D1")
    ws.append(["Sample", "Trace elements", None, None])
    ws.merge_cells("B2:D2")
    ws.append(["ID", "Sb", "As", "Au"])
    ws.append(["ST-1", 1200.5, 45.2, 0.85])
    xlsx_path = tmp_path / "Table_S1.xlsx"
    wb.save(xlsx_path)

    converted = mdc.convert_directory_supplements(str(tmp_path))
    assert len(converted) == 1
    md_text = Path(converted[0][1]).read_text(encoding="utf-8")
    assert "Unnamed:" not in md_text
    assert "Trace elements / Sb" in md_text
    assert "Sample / ID" in md_text

    summary_text = (tmp_path / "SUPPLEMENTS_MARKDOWN_SUMMARY.md").read_text(encoding="utf-8")
    assert "```markdown" not in summary_text
    assert "| Sample / ID |" in summary_text


def test_browserskill_download_url_in_session_preserves_credentials_include():
    sess = bsk.BrowserSkillSession(bsk_bin="/bin/bsk")
    sess.session_id = "s1"
    sess.tab_id = "1"
    captured = {}

    def _fake_eval(script, timeout=None):
        captured["script"] = script
        return {"ok": False}

    with patch.object(sess, "evaluate", side_effect=_fake_eval):
        with pytest.raises(bsk.BrowserSkillExecutionError):
            sess.download_url_in_session("https://example.org/file.xlsx", "/tmp/out.xlsx")

    assert "credentials: 'include'" in captured["script"]
    assert "credentials: 'omit'" not in captured["script"]


def test_playwright_request_download_helper(tmp_path):
    dest = tmp_path / "supp.csv"
    resp = SimpleNamespace(
        status=200,
        ok=True,
        body=lambda: b"col1,col2\n1,2\n",
        headers={"Content-Type": "text/csv"},
    )
    ctx = SimpleNamespace(request=SimpleNamespace(get=lambda url, **kw: resp))
    ok, ct = pu.download_via_playwright_request(ctx, "https://example.com/supp.csv", str(dest))
    assert ok and ct == "text/csv"
    assert dest.read_bytes() == b"col1,col2\n1,2\n"


def test_scansci_download_file_reuses_stealthy_session_holder(monkeypatch, tmp_path):
    starts = []
    closes = []

    class _FakeDownload:
        suggested_filename = None

        def save_as(self, path):
            Path(path).write_bytes(b"col1,col2\n1,2\n")

    class _FakeExpect:
        value = _FakeDownload()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class _FakePage:
        def goto(self, url, **kw):
            return None

        def expect_download(self, **kw):
            return _FakeExpect()

        def close(self):
            return None

    class _FakeSession:
        def __init__(self, **kw):
            self.context = SimpleNamespace(
                add_cookies=lambda c: None,
                new_page=lambda: _FakePage(),
            )

        def start(self):
            starts.append(1)

        def close(self):
            closes.append(1)

    monkeypatch.setattr(ssd, "HAS_SCRAPLING", True)
    monkeypatch.setattr(
        ssd.Fetcher,
        "get",
        staticmethod(lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("skip"))),
    )
    monkeypatch.setattr("scrapling.fetchers.StealthySession", _FakeSession)

    holder = {"session": None, "visited_origins": set()}
    r1 = ssd.download_file(
        "https://example.com/s1.csv", str(tmp_path), session=None, stealth_session_holder=holder
    )
    r2 = ssd.download_file(
        "https://example.com/s2.csv", str(tmp_path), session=None, stealth_session_holder=holder
    )
    assert r1 and r2
    assert len(starts) == 1
    assert len(closes) == 0


def test_chinese_pdf_embedded_supplement_diagnostic_note(monkeypatch, tmp_path, capsys):
    html = (
        "<html><head><title>湘中锡矿山锑矿床成矿机制</title></head>"
        "<body><main><p>样品分析数据见附表1和附表2（正文PDF内嵌表格）。</p></main></body></html>"
    )
    args = SimpleNamespace(
        url_or_doi="https://www.geotectonica.com/article/123",
        output_dir=str(tmp_path),
        headful=False,
        proxy=None,
        cdp=False,
        cdp_url=None,
        browserskill=False,
        bsk_browser=None,
        bsk_tab=None,
        no_api=True,
        skip_cdn=True,
        no_vpn=True,
        no_cookies=True,
        no_oa=True,
        oa=False,
        list_only=False,
        max_mmc=5,
        max_misses=2,
        convert_markdown=False,
        phase_timeout=30,
        browser_goto_timeout=10,
        browser_fallback_budget=10,
        tunnel_fail_threshold=3,
    )
    monkeypatch.setattr(ssd, "HAS_SCRAPLING", False)
    monkeypatch.setattr(ssd, "_setup_authenticated_session_and_cookies", lambda *a, **k: (MagicMock(), []))
    monkeypatch.setattr(ssd, "fetch_page_html", lambda *a, **k: (html, 200, args.url_or_doi))
    ssd.run_download(args, bsk.BrowserSkillRuntime(enabled=False))
    out = capsys.readouterr().out
    assert "embedded in the main article PDF" in out


def test_wiley_div_institutional_login_and_html_table_auth_chrome(tmp_path):
    wiley_html = (
        "<html><head><title>Ore Geology Reviews | Stibnite</title>"
        "<script>window.cf='challenge-platform';</script></head>"
        "<body><div class='top-header'><a href='/login'>Institutional Login</a></div>"
        "<main><article><h1>Mineralization</h1><p>"
        + ("Antimony mineralization in the Xiangzhong metallogenic belt. " * 30)
        + "</p></article></main></body></html>"
    )
    assert not sf.is_blocked_page(wiley_html)

    table_html = (
        "<!DOCTYPE html><html><head><title>Supplementary Table S1</title></head>"
        "<body><div class='header-bar'>Institutional Login</div>"
        "<table><tr><th>Sample</th><th>Note</th></tr>"
        "<tr><td>ST-1</td><td>No paywall restriction</td></tr>"
        "<tr><td>ST-2</td><td>Verified</td></tr></table></body></html>"
    ).encode("utf-8")
    p = tmp_path / "table_s1.html"
    p.write_bytes(table_html)
    ok, reason = sf.validate_downloaded_file(str(p), headers={"content-type": "text/html"})
    assert ok, reason


def test_tables1_compact_token_junk_s1a_and_non_mdpi_s1():
    assert sf.supplementary_table_ids("TableS1.xlsx", in_supplementary_context=True) == {"S1"}
    assert sf.supplementary_table_ids("See TableS1 for details.") == {"S1"}
    assert sf.junk_candidate_reason(
        "https://example.com/article/table_s1a.html",
        text="Table S1a",
        is_explicit_supp=True,
    ) is None

    matched, rule = sf.should_match_candidate("https://example.com/articles/s1", text="Section 1", attrs={})
    assert not matched and rule == ""
    assert sf.extract_filename_from_url("https://example.com/articles/s1") != "s1.zip"


def test_find_all_candidates_and_evidence_ignore_comments_and_references():
    html = (
        "<html><body>"
        "<!-- Table S9 deprecated draft -->"
        "<main><section id='supplementary'><p>Tables S1-S2</p>"
        "<a href='https://example.com/S1.xlsx'>Table S1</a>"
        "<a href='https://example.com/S2.xlsx'>Table S2</a>"
        "</section>"
        "<section id='references' class='references'>"
        "<p>Jones et al. (2021) Supplementary Tables S1-S10.</p>"
        "</section></main></body></html>"
    )
    diag = sf.find_all_candidates(html, "https://example.com/article")
    assert not diag.is_incomplete, diag.incomplete_reason
    ev = ds.DiscoveryEvidence()
    ev.observe("publisher", html, "https://example.com/article", soup=getattr(diag, "_soup", None), article_text=getattr(diag, "_article_text", None))
    assert ev.tables == {"S1", "S2"}


def test_scansci_doi_url_not_clobbered_to_none_and_migrated_tier0_counted_in_evidence(monkeypatch, tmp_path, capsys):
    seen_urls = []
    args = SimpleNamespace(
        url_or_doi="https://doi.org/10.1016/j.oregeorev.2025.106001",
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
        list_only=False,
        max_mmc=5,
        max_misses=2,
        convert_markdown=False,
        phase_timeout=30,
        browser_goto_timeout=10,
        browser_fallback_budget=10,
        tunnel_fail_threshold=3,
    )
    supp_dir = tmp_path / "supplements"
    supp_dir.mkdir()

    def _fake_resolve(d):
        seen_urls.append(d)
        return "https://www.sciencedirect.com/science/article/pii/S0169136825001234"

    def _fake_cdn(pii, output_dir, **kwargs):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        f = Path(output_dir) / "mmc1.xlsx"
        f.write_bytes(b"PK\x03\x04dummy")
        return [str(f)]

    html = (
        "<html><head><title>Antimony Ore Study</title></head>"
        "<body><section id='supplementary'><p>Tables S1-S2</p>"
        "<a href='https://ars.els-cdn.com/content/image/1-s2.0-S0169136825001234-mmc1.xlsx'>ESM 1</a>"
        "</section></body></html>"
    )
    monkeypatch.setattr(ssd, "HAS_SCANSCI", True)
    monkeypatch.setattr(ssd, "HAS_SCRAPLING", False)
    monkeypatch.setattr(ssd, "load_config", lambda: {})
    monkeypatch.setattr(ssd, "resolve_doi_url", _fake_resolve)
    monkeypatch.setattr(ssd, "try_elsevier_cdn_brute_force", _fake_cdn)
    monkeypatch.setattr(ssd, "_setup_authenticated_session_and_cookies", lambda *a, **k: (MagicMock(), []))
    monkeypatch.setattr(ssd, "fetch_page_html", lambda *a, **k: (html, 200, "https://www.sciencedirect.com/science/article/pii/S0169136825001234"))
    monkeypatch.setattr(ssd, "download_file", lambda *a, **k: None)

    ssd.run_download(args, bsk.BrowserSkillRuntime(enabled=False))
    out = capsys.readouterr().out
    assert seen_urls == ["https://doi.org/10.1016/j.oregeorev.2025.106001"]
    assert "Downloaded set remains incomplete" not in out


def test_markitdown_does_not_swallow_text_first_data_row_into_header(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Samples"
    ws.append(["Table S1. Sample descriptions", None, None, None])
    ws.merge_cells("A1:D1")
    ws.append(["Sample", "Location", "Lithology", "Grade"])
    ws.append(["ST-1", "Xikuangshan", "Limestone", "High"])
    ws.append(["ST-2", "Banpo", "Shale", 3.5])
    xlsx_path = tmp_path / "samples.xlsx"
    wb.save(xlsx_path)

    md = mdc.convert_file_to_markdown(str(xlsx_path))
    assert md is not None
    assert "Sample / ST-1" not in md
    assert "| Sample | Location | Lithology | Grade |" in md
    assert "| ST-1 | Xikuangshan | Limestone | High |" in md


def test_wiley_hidden_password_modal_not_blocked_and_epdf_filtered():
    filler = "Antimony mineralization trace element geochemistry and stibnite isotope systematics. " * 90
    html = (
        "<html><head><title>Xialonggang Deposit - Wiley</title></head><body>"
        "<div class='login-modal' style='display:none'>"
        "<a href='/action/ssostart'>Institutional Login</a>"
        "<form><input type='password' name='password'/></form></div>"
        f"<main><p>{filler}</p>"
        "<a href='https://onlinelibrary.wiley.com/doi/epdf/10.1111/1755-6724.15359'>1755-6724.15359</a>"
        "<section id='supporting-info'>"
        "<a href='/action/downloadSupplement?doi=10.1111%2F1755-6724.15359&amp;file=acgs15359-sup-0001-S1.docx'>acgs15359-sup-0001-S1.docx</a>"
        "</section></main></body></html>"
    )
    assert sf.is_blocked_page(html) is False
    diag = sf.find_all_candidates(html, "https://onlinelibrary.wiley.com/doi/10.1111/1755-6724.15359")
    urls = [c.url for c in diag.candidates]
    assert all("/doi/epdf/" not in u for u in urls)
    assert any("acgs15359-sup-0001-S1.docx" in u for u in urls)

    login_wall = (
        "<html><body><div><a href='/login'>Institutional Login</a>"
        "<form><input type='password' name='pw'/></form></div></body></html>"
    )
    assert sf.is_blocked_page(login_wall) is True


def test_mdpi_previous_sibling_link_and_jats_xml_not_extracted():
    html = (
        "<html><head>"
        "<script type='application/ld+json'>"
        '{"url":"https://www.mdpi.com/2075-163X/15/8/814/minerals-15-00814.xml",'
        '"supplementary":"https://example.org/files/Table_S1_supplement.xml"}'
        "</script></head><body>"
        "<p>See Table S2 for trace element concentrations. "
        "<a href='https://www.mdpi.com/2075-163X/15/8/813'>... Previous</a></p>"
        "<section id='supplementary'>"
        "<a href='https://www.mdpi.com/article/10.3390/min15080814/s1'>Supplementary Materials</a>"
        "</section></body></html>"
    )
    diag = sf.find_all_candidates(html, "https://www.mdpi.com/2075-163X/15/8/814")
    urls = [c.url for c in diag.candidates]
    assert "https://www.mdpi.com/2075-163X/15/8/813" not in urls
    assert not any(u.endswith("minerals-15-00814.xml") for u in urls)
    assert "https://www.mdpi.com/article/10.3390/min15080814/s1" in urls
    assert "https://example.org/files/Table_S1_supplement.xml" in urls


def test_nested_supp_header_div_does_not_trigger_unresolved_section():
    html = (
        "<html><body>"
        "<section id='supplementary'>"
        "<div class='supplementary-header'><h3>Supplementary Information</h3></div>"
        "<div class='supplementary-item'>"
        "<a href='https://static-content.springer.com/esm/art%3A10.1007%2Fs00126-025-01387-3/MediaObjects/126_2025_1387_MOESM1_ESM.docx'>Supplementary file 1</a>"
        "</div>"
        "</section></body></html>"
    )
    diag = sf.find_all_candidates(html, "https://link.springer.com/article/10.1007/s00126-025-01387-3")
    assert len(diag.candidates) == 1
    assert not diag.is_incomplete


def test_journal_downloader_stealthy_session_fallback_and_error_reason(tmp_path, monkeypatch):
    bad_resp = SimpleNamespace(status=200, body=b"<html><body>Blocked</body></html>", headers={"content-type": "text/html"})
    monkeypatch.setattr(jd.Fetcher, "get", lambda *a, **k: bad_resp)

    # Standalone caller without stealth_session_holder returns None without starting Chromium
    assert jd.download_file("https://www.mdpi.com/article/10.3390/min15080814/s1", str(tmp_path)) is None

    # With stealth_session_holder and failing browser download, error_out carries the real reason
    class _FailingPage:
        def set_default_timeout(self, ms): pass
        def goto(self, *a, **k): raise RuntimeError("Timeout 15000ms exceeded")
        def expect_download(self, **k): raise RuntimeError("Timeout 15000ms exceeded")
        def close(self): pass

    class _FailingSession:
        def __init__(self, **kw):
            self.context = SimpleNamespace(new_page=lambda: _FailingPage(), add_cookies=lambda c: None, request=None)
        def start(self): pass
        def close(self): pass

    import scrapling.fetchers
    monkeypatch.setattr(scrapling.fetchers, "StealthySession", _FailingSession)
    err = {}
    res = jd.download_file(
        "https://www.mdpi.com/article/10.3390/min15080814/s1",
        str(tmp_path),
        stealth_session_holder={},
        error_out=err,
    )
    assert res is None
    assert "HTML" in err["reason"] and "Timeout 15000ms exceeded" in err["reason"]

    # With working StealthySession.context.request delivering valid ZIP bytes, fallback succeeds
    import io as _io
    import zipfile as _zipfile
    zip_buf = _io.BytesIO()
    with _zipfile.ZipFile(zip_buf, "w") as zf:
        zf.writestr("supp.txt", "data")
    valid_zip = zip_buf.getvalue()

    class _WorkingContext:
        request = object()
        def new_page(self): return _FailingPage()
        def add_cookies(self, c): pass

    class _WorkingSession:
        def __init__(self, **kw):
            self.context = _WorkingContext()
        def start(self): pass
        def close(self): pass

    def _fake_pw_dl(ctx, url, dest, headers=None, timeout_ms=30000):
        Path(dest).write_bytes(valid_zip)
        return True, "application/zip"

    monkeypatch.setattr(scrapling.fetchers, "StealthySession", _WorkingSession)
    monkeypatch.setattr(jd, "download_via_playwright_request", _fake_pw_dl)
    ok_path = jd.download_file(
        sf.Candidate(url="https://www.mdpi.com/article/10.3390/min15080814/s1", filename="s1.zip"),
        str(tmp_path),
        stealth_session_holder={},
        error_out={},
    )
    assert ok_path is not None and Path(ok_path).exists()


def test_journal_downloader_fast_http_precheck_skips_stealthy_fetcher(tmp_path, monkeypatch, capsys):
    springer_html = (
        "<html><head><title>Zhaxikang Sb Deposit</title></head><body>"
        "<section id='supplementary'>"
        "<a href='https://static-content.springer.com/esm/art%3A10.1007%2Fs00126-025-01387-3/MediaObjects/126_2025_1387_MOESM1_ESM.docx'>Supplementary file 1</a>"
        "</section></body></html>"
    )
    import requests as _req

    class _FakeSess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass
        def get(self, url, **kw):
            return SimpleNamespace(status_code=200, text=springer_html, url="https://link.springer.com/article/10.1007/s00126-025-01387-3")

    stealthy_calls = []
    fetcher_calls = []
    monkeypatch.setattr(_req, "Session", _FakeSess)
    monkeypatch.setattr(
        jd.StealthyFetcher,
        "fetch",
        staticmethod(lambda *a, **k: stealthy_calls.append(a) or (_ for _ in ()).throw(AssertionError("StealthyFetcher should not run"))),
    )
    monkeypatch.setattr(
        jd.Fetcher,
        "get",
        staticmethod(lambda *a, **k: fetcher_calls.append(a) or (_ for _ in ()).throw(AssertionError("Fetcher.get should not run"))),
    )
    monkeypatch.setattr(jd, "load_saved_cookies", lambda *a, **k: [])

    args = SimpleNamespace(
        url="10.1007/s00126-025-01387-3",
        output_dir=str(tmp_path),
        headful=False,
        solve_cloudflare=True,
        no_cloudflare=False,
        real_chrome=False,
        proxy=None,
        wait_selector=None,
        timeout=30000,
        cdp=False,
        cdp_url=None,
        browserskill=False,
        no_oa=True,
        oa=False,
        list_only=True,
        convert_markdown=False,
        phase_timeout=30,
        browser_goto_timeout=15,
        browser_fallback_budget=20,
    )
    jd.run_download(args, bsk.BrowserSkillRuntime(enabled=False))
    out = capsys.readouterr().out
    assert not stealthy_calls, "Fast-HTTP precheck failed and fell back to StealthyFetcher"
    assert not fetcher_calls, "Fast-HTTP precheck failed and fell back to Fetcher.get"
    assert "[Fast-HTTP]" in out
    assert "126_2025_1387_MOESM1_ESM.docx" in out


def test_stealthy_fetch_bounded_preserves_sub60s_timeout_with_solve_cloudflare(monkeypatch):
    import scrapling.fetchers as scrap_fetch

    recorded = {}

    class _FakeStealthSession:
        def __init__(self, **kw):
            recorded["session_kw"] = kw
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def fetch(self, url, **kw):
            recorded["fetch_url"] = url
            recorded["fetch_kw"] = kw
            return SimpleNamespace(status=200, html_content="<html>ok</html>", url=url)

    monkeypatch.setattr(scrap_fetch, "StealthySession", _FakeStealthSession)
    res = sf.stealthy_fetch_bounded(
        scrap_fetch.StealthyFetcher,
        "https://example.org/article",
        headless=True,
        solve_cloudflare=True,
        timeout=25000,
        wait=2000,
    )
    assert res.status == 200
    assert recorded["session_kw"]["solve_cloudflare"] is True
    assert "timeout" not in recorded["session_kw"]
    assert recorded["fetch_kw"]["timeout"] == 25000


def test_markitdown_xls_and_zip_flatten_multirow_headers(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Table S2"
    ws.append(["Table S2. Trace elements concentration (ppm) of stibnite", None, None])
    ws.merge_cells("A1:C1")
    ws.append(["Sample", "As", "Sb"])
    ws.append([None, "ppm", "ppm"])
    ws.append(["ZK-1", 120.5, 680000])
    import io as _io
    import zipfile as _zipfile
    inner_buf = _io.BytesIO()
    wb.save(inner_buf)

    zip_path = tmp_path / "s1.zip"
    with _zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("minerals-3750560-supplementary.xlsx", inner_buf.getvalue())

    md_zip = mdc.convert_file_to_markdown(str(zip_path))
    assert md_zip is not None
    assert "Unnamed:" not in md_zip
    assert "As / ppm" in md_zip and "Sb / ppm" in md_zip

    # Also test .xls via xlrd mock with merged title banner
    import xlrd

    class _Cell:
        def __init__(self, ctype, value):
            self.ctype = ctype
            self.value = value

    rows = [
        [_Cell(xlrd.XL_CELL_TEXT, "Table S1. Trace elements"), _Cell(xlrd.XL_CELL_EMPTY, ""), _Cell(xlrd.XL_CELL_EMPTY, "")],
        [_Cell(xlrd.XL_CELL_TEXT, "Sample"), _Cell(xlrd.XL_CELL_TEXT, "Au"), _Cell(xlrd.XL_CELL_TEXT, "Sb")],
        [_Cell(xlrd.XL_CELL_EMPTY, ""), _Cell(xlrd.XL_CELL_TEXT, "ppm"), _Cell(xlrd.XL_CELL_TEXT, "wt%")],
        [_Cell(xlrd.XL_CELL_TEXT, "M-1"), _Cell(xlrd.XL_CELL_NUMBER, 12.5), _Cell(xlrd.XL_CELL_NUMBER, 4.0)],
    ]

    class _Sheet:
        name = "Table S1"
        nrows = len(rows)
        ncols = 3
        merged_cells = [(0, 1, 0, 3)]
        def cell(self, r, c): return rows[r][c]

    class _Book:
        datemode = 0
        def sheets(self): return [_Sheet()]

    monkeypatch.setattr(xlrd, "open_workbook", lambda *a, **k: _Book())
    xls_md = mdc._convert_excel_multirow_headers_to_markdown("supp.xls", file_bytes=b"dummy")
    assert xls_md is not None
    assert "Unnamed:" not in xls_md
    assert "> Table S1. Trace elements" in xls_md
    assert "| Sample | Au / ppm | Sb / wt% |" in xls_md
    assert "| M-1 | 12.5 | 4 |" in xls_md


def test_scansci_preserved_done_line_printed_after_phase_timeout_warning(tmp_path, monkeypatch, capsys):
    args = SimpleNamespace(
        url_or_doi="10.1016/j.oregeorev.2024.106384",
        output_dir=str(tmp_path),
        headful=False,
        browser=False,
        no_api=True,
        skip_cdn=False,
        no_vpn=True,
        no_cookies=True,
        no_oa=True,
        oa=False,
        list_only=False,
        cdp=False,
        cdp_url=None,
        browserskill=False,
        bsk_browser=None,
        bsk_tab=None,
        proxy=None,
        max_mmc=5,
        max_misses=2,
        convert_markdown=False,
        phase_timeout=0.05,
        browser_goto_timeout=5,
        browser_fallback_budget=5,
        tunnel_fail_threshold=3,
    )

    def _fake_cdn(pii, output_dir, **kwargs):
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        f = Path(output_dir) / "mmc1.xlsx"
        f.write_bytes(b"PK\x03\x04dummy")
        return [str(f)]

    def _slow_empty_fetch(*a, **k):
        import time as _t
        _t.sleep(0.08)
        return ("", 0, "https://www.sciencedirect.com/science/article/pii/S0169136824005171")

    monkeypatch.setattr(ssd, "HAS_SCANSCI", False)
    monkeypatch.setattr(ssd, "HAS_SCRAPLING", False)
    monkeypatch.setattr(ssd, "load_config", lambda: {})
    monkeypatch.setattr(ssd, "resolve_doi_url", lambda d: "https://www.sciencedirect.com/science/article/pii/S0169136824005171")
    monkeypatch.setattr(ssd, "_quick_title_lookup", lambda *a, **k: "Awanda Au Sb Deposit")
    monkeypatch.setattr(ssd, "try_elsevier_cdn_brute_force", _fake_cdn)
    monkeypatch.setattr(ssd, "_setup_authenticated_session_and_cookies", lambda *a, **k: (MagicMock(), []))
    monkeypatch.setattr(ssd, "fetch_page_html", _slow_empty_fetch)

    ssd.run_download(args, bsk.BrowserSkillRuntime(enabled=False))
    out = capsys.readouterr().out
    warn_idx = out.find("[Warning] Phase timeout")
    done_idx = out.find("Done:")
    assert warn_idx != -1 and done_idx != -1 and done_idx > warn_idx


def test_journal_downloader_grants_download_window_for_already_found_candidates(tmp_path, monkeypatch, capsys):
    import time as _t
    html = (
        "<html><head><title>Zhaxikang Test</title></head><body>"
        "<section id='supplementary'>"
        "<a href='https://static-content.springer.com/esm/126_2025_1387_MOESM1_ESM.docx'>Supplementary file 1</a>"
        "</section></body></html>"
    )
    valid_docx = b"PK\x03\x04" + b"x" * 200

    def _slow_stealth(*a, **k):
        _t.sleep(0.12)
        return SimpleNamespace(status=200, html_content=html, text=html, url="https://link.springer.com/article/10.1007/s00126-025-01387-3")

    def _fake_download(cand, out_dir, **kw):
        dl = kw.get("deadline")
        assert dl is not None and dl > _t.monotonic()
        p = Path(out_dir) / cand.filename
        p.write_bytes(valid_docx)
        return str(p)

    import requests as _req

    class _FailSess:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass
        def get(self, *a, **k): raise RuntimeError("offline")

    monkeypatch.setattr(_req, "Session", _FailSess)
    monkeypatch.setattr(jd, "stealthy_fetch_bounded", _slow_stealth)
    monkeypatch.setattr(jd, "load_saved_cookies", lambda *a, **k: [])
    monkeypatch.setattr(jd, "download_file", _fake_download)

    args = SimpleNamespace(
        url="10.1007/s00126-025-01387-3",
        output_dir=str(tmp_path),
        headful=False,
        solve_cloudflare=True,
        no_cloudflare=False,
        real_chrome=False,
        proxy=None,
        wait_selector=None,
        timeout=30000,
        cdp=False,
        cdp_url=None,
        browserskill=False,
        no_oa=True,
        oa=False,
        list_only=False,
        convert_markdown=False,
        phase_timeout=0.08,
        browser_goto_timeout=5,
        browser_fallback_budget=5,
    )
    jd.run_download(args, bsk.BrowserSkillRuntime(enabled=False))
    out = capsys.readouterr().out
    assert "Done: 1/1 file transfers succeeded" in out


def test_markitdown_excel_does_not_absorb_col0_group_subheader_into_header(tmp_path):
    import openpyxl
    import markitdown_converter as mc

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Table S2"
    ws.append(["Table S2. Trace elements concentration (ppm) in pyrites"])
    ws.append([])
    ws.append(["PYRITE"])
    ws.append([])
    ws.append(["", "Type", "Ti", "Mn", "Co"])
    ws.append(["MAT6pir_1"])
    ws.append(["MAT6pir_1", "Py-II", 3.59, 484.8, 0.0])

    xlsx_path = tmp_path / "pyrite.xlsx"
    wb.save(xlsx_path)

    md = mc._convert_excel_multirow_headers_to_markdown(str(xlsx_path))
    assert md is not None
    assert "| Col 1 | Type | Ti | Mn | Co |" in md
    assert "| MAT6pir_1 | Type |" not in md
    assert "| MAT6pir_1 |  |  |  |  |" in md
    assert "| MAT6pir_1 | Py-II | 3.59 | 484.8 | 0 |" in md


def test_sciencedirect_bulk_post_endpoint_filtered():
    html = """
    <html><body>
      <section class="supplementary-material">
        <a href="https://ars.els-cdn.com/content/image/1-s2.0-S0169136824005171-mmc1.xlsx">Download spreadsheet (159KB)</a>
        <a href="https://www.sciencedirect.com/sdfe/pdf/download/S0169136824005171/attachments">Download all supplementary files included with this article</a>
      </section>
    </body></html>
    """
    diag = sf.find_all_candidates(
        html,
        "https://www.sciencedirect.com/science/article/pii/S0169136824005171",
    )
    urls = [c.url for c in diag.candidates]
    assert urls == ["https://ars.els-cdn.com/content/image/1-s2.0-S0169136824005171-mmc1.xlsx"]
    assert any(reason == "sciencedirect_bulk_post_endpoint" for _, reason in diag.junk_filtered)


def test_stealthy_fetch_bounded_defaults_retries_to_one(monkeypatch):
    from scrapling.fetchers import StealthyFetcher

    captured = {}

    class _FakeSession:
        def __init__(self, **kw):
            captured["session_kw"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, url, **kw):
            captured["fetch_kw"] = kw
            return SimpleNamespace(status=200, text="<html></html>")

    monkeypatch.setattr("scrapling.fetchers.StealthySession", _FakeSession)
    sf.stealthy_fetch_bounded(
        StealthyFetcher,
        "https://www.sciencedirect.com/science/article/pii/S0169136824005171",
        headless=True,
        solve_cloudflare=True,
        timeout=30000,
    )
    assert captured["session_kw"]["retries"] == 1


def test_scansci_tier0_manifest_url_skips_duplicate_and_avoids_double_count(tmp_path):
    import openpyxl
    import scansci_supp_downloader as ssd

    mmc_url = "https://ars.els-cdn.com/content/image/1-s2.0-S0169136824005171-mmc1.xlsx"
    tier0_file = tmp_path / "mmc1.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["sample", "val"])
    wb.save(tier0_file)
    manifest = {}
    sf.record_manifest_entry(manifest, "mmc1.xlsx", mmc_url)
    sf.save_download_manifest(str(tmp_path), manifest)

    cand = sf.Candidate(
        url=mmc_url,
        filename="1-s2.0-S0169136824005171-mmc1.xlsx",
        link_text="Download spreadsheet",
        section="supplementary_section",
    )
    dl_path = ssd.download_file(cand, str(tmp_path), session=None)
    assert dl_path == str(tier0_file)
    assert not (tmp_path / "1-s2.0-S0169136824005171-mmc1.xlsx").exists()

    # count_tier_files_on_disk must not double-count mmc1.xlsx once candidate cand succeeded
    assert ssd.count_tier_files_on_disk([], [str(tier0_file)], str(tmp_path), [cand]) == 0


def test_sciencedirect_pii_not_parsed_as_table_s2_0():
    from discovery_state import DiscoveryEvidence

    assert sf.supplementary_table_ids(
        "1-s2.0-S0169136824005171-mmc1.xlsx", in_supplementary_context=True
    ) == set()
    assert sf.supplementary_table_ids("S2.0", in_supplementary_context=True) == set()

    c = sf.Candidate(
        url="https://ars.els-cdn.com/content/image/1-s2.0-S0169136824005171-mmc1.xlsx",
        filename="1-s2.0-S0169136824005171-mmc1.xlsx",
        link_text="Download spreadsheet (21KB)",
        section="supplementary_section",
    )
    assert sf.has_opaque_bundle_candidate([c]) is True

    ev = DiscoveryEvidence()
    ev.observe(
        "publisher",
        "<html><body><p>Supplementary Table S1 is provided.</p></body></html>",
        "https://www.sciencedirect.com/science/article/pii/S0169136824005171",
    )
    assert ev.check([c]) == (False, "")


def test_stealthy_fetch_bounded_caps_networkidle_and_retries_nav_race(monkeypatch):
    from scrapling.fetchers import StealthyFetcher

    captured = {"calls": 0, "nav_timeouts": [], "networkidle_timeouts": []}

    class _FakePage:
        def set_default_timeout(self, ms):
            pass

        def set_default_navigation_timeout(self, ms):
            captured["nav_timeouts"].append(ms)

        def wait_for_load_state(self, state="load", timeout=None):
            if state == "networkidle":
                captured["networkidle_timeouts"].append(timeout)

        def on(self, event, fn):
            pass

    class _FakeSession:
        def __init__(self, **kw):
            captured["session_kw"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, url, **kw):
            captured["calls"] += 1
            page = _FakePage()
            kw["page_setup"](page)
            page.wait_for_load_state("networkidle")
            if captured["calls"] == 1:
                raise RuntimeError(
                    "Page.content: Unable to retrieve content because the page is navigating and changing the content."
                )
            return SimpleNamespace(status=200, text="<html>ok</html>")

    monkeypatch.setattr("scrapling.fetchers.StealthySession", _FakeSession)
    res = sf.stealthy_fetch_bounded(
        StealthyFetcher,
        "https://www.sciencedirect.com/science/article/pii/S0169136824005171",
        headless=True,
        solve_cloudflare=True,
        timeout=30000,
    )
    assert res.status == 200
    assert captured["session_kw"]["retries"] == 1
    assert captured["calls"] == 2
    assert captured["nav_timeouts"] and max(captured["nav_timeouts"]) <= 30000
    assert captured["networkidle_timeouts"] and all(
        t is not None and t <= 4000 for t in captured["networkidle_timeouts"]
    )


def test_walk_json_for_supp_objects_ignores_jats_xml_when_abstract_mentions_table():
    from bs4 import BeautifulSoup

    html = """
    <html><head>
    <script type="application/ld+json">
    {
      "@type": "ScholarlyArticle",
      "description": "Trace element concentrations are reported in Supplementary Table S1.",
      "encoding": {
        "@type": "MediaObject",
        "name": "Supplementary Table S1 XML mirror",
        "contentUrl": "https://www.mdpi.com/2075-163X/15/8/814/xml",
        "fileName": "minerals-15-00814.xml"
      }
    }
    </script>
    </head><body></body></html>
    """
    cands = []
    soup = BeautifulSoup(html, "html.parser")
    sf._extract_embedded_json_candidates(
        soup,
        "https://www.mdpi.com/2075-163X/15/8/814",
        lambda u, *a, **kw: cands.append(u),
    )
    assert all(not (getattr(u, "url", u)).endswith("xml") for u in cands)


def test_journal_downloader_manifest_url_skips_duplicate(tmp_path):
    import openpyxl
    import journal_downloader as jd

    mmc_url = "https://ars.els-cdn.com/content/image/1-s2.0-S0169136824005171-mmc1.xlsx"
    existing_file = tmp_path / "mmc1.xlsx"
    wb = openpyxl.Workbook()
    wb.active.append(["sample", "val"])
    wb.save(existing_file)
    manifest = {}
    sf.record_manifest_entry(manifest, "mmc1.xlsx", mmc_url)
    sf.save_download_manifest(str(tmp_path), manifest)

    cand = sf.Candidate(
        url=mmc_url,
        filename="1-s2.0-S0169136824005171-mmc1.xlsx",
        link_text="Download spreadsheet",
        section="supplementary_section",
    )
    dl_path = jd.download_file(cand, str(tmp_path))
    assert dl_path == str(existing_file)
    assert not (tmp_path / "1-s2.0-S0169136824005171-mmc1.xlsx").exists()


def test_should_match_candidate_rejects_nav_link_nearby_table_in_supp_or_html():
    from bs4 import BeautifulSoup

    html = """
    <div class="supplementary-materials">
      <p>See Table S2 for trace element analyses.</p>
      <a href="/2075-163X/15/8/813">... Previous</a>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("a")
    matched, reason = sf.should_match_candidate(
        "https://www.mdpi.com/2075-163X/15/8/813",
        text="... Previous",
        attrs={},
        is_supp_section=True,
        tag=tag,
    )
    assert matched is False, f"unexpected match: {reason}"


