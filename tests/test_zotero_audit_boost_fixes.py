"""Regression tests for Zotero full-library audit findings across all publisher groups."""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import supp_finder as sf  # noqa: E402
from archive_fixture import office_archive  # noqa: E402


# ── 1. Blocked-page & challenge detection ────────────────────────────────────


def test_hidden_login_modals_on_full_articles_not_blocked():
    filler = " ".join(["Sedimentary basin geochemical analysis and trace element ratios."] * 140)
    assert len(filler) > 5000
    for phrase in ("Log in to view", "Log in to access", "Sign in to continue", "Institutional Login"):
        html = (
            "<html><head><title>Geochemistry of Ore Deposits | Science</title></head>"
            f"<body><main><article><h1>Main Study</h1><p>{filler}</p></article></main>"
            f"<div class='login-modal hidden'><form><input type='password' name='pw'>"
            f"<p>{phrase} full institutional subscription options</p></form></div></body></html>"
        )
        assert sf.is_blocked_page(html) is False, f"False block on modal phrase: {phrase}"


def test_real_login_wall_and_bot_challenges_are_blocked():
    # Small real login wall must still be blocked
    login_wall = (
        "<html><head><title>Subscription Access</title></head>"
        "<body><div><p>Log in to view this article.</p><form><input type='password'></form></div></body></html>"
    )
    assert sf.is_blocked_page(login_wall) is True

    # Silverchair /crawlprevention/governor
    sc_gov = (
        "<html><head><title>Validate User</title></head>"
        "<body><form action='/crawlprevention/governor?content=%2Fdoi%2F10.1'></form></body></html>"
    )
    assert sf.is_blocked_page(sc_gov) is True
    assert sf.is_placeholder_or_blocked_title("Validate User") is True

    # IOP Radware Bot Manager Captcha
    radware = (
        "<html><head><title>Radware Bot Manager Captcha</title></head>"
        "<body><script src='https://validate.perfdrive.com/captcha.js'></script>"
        "<p>We apologize for the inconvenience. Please complete the verification.</p></body></html>"
    )
    assert sf.is_blocked_page(radware) is True
    assert sf.is_placeholder_or_blocked_title("Radware Bot Manager Captcha") is True

    # Localized Cloudflare
    cf_cn = "<html><head><title>请稍候...</title></head><body><p>正在检查站点连接安全性，请稍候</p></body></html>"
    assert sf.is_blocked_page(cf_cn) is True
    assert sf.is_placeholder_or_blocked_title("请稍候...") is True


def test_academic_paper_about_captcha_not_falsely_blocked():
    filler = " ".join(["Convolutional neural networks classify distorted character glyphs accurately."] * 140)
    html = (
        "<html><head><title>Solving Text CAPTCHA with Deep Learning</title></head>"
        f"<body><article><h1>Solving Text CAPTCHA</h1><p>{filler}</p></article></body></html>"
    )
    assert sf.is_blocked_page(html) is False


# ── 2. Elsevier / ScienceDirect ──────────────────────────────────────────────


def test_elsevier_appendices_download_link_and_preloaded_state_needs_browser():
    html = """
    <html><head><title>Ore Geology Reviews Article</title></head>
    <body>
      <section class="Appendices">
        <h2>Appendix A. Supplementary data</h2>
        <div>
          <a class="anchor download-link" href="https://ars.els-cdn.com/content/image/1-s2.0-S0169136824001111-mmc1.xlsx">Download spreadsheet (42KB)</a>
          <a class="anchor download-link" href="/cms/attachment/12345/mmc2.pdf">Download document</a>
        </div>
      </section>
      <script>window.__PRELOADED_STATE__ = {"article": {"title": "OGR"}};</script>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.sciencedirect.com/science/article/pii/S0169136824001111")
    urls = [c.url for c in diag.candidates]
    assert "https://ars.els-cdn.com/content/image/1-s2.0-S0169136824001111-mmc1.xlsx" in urls
    assert "https://www.sciencedirect.com/cms/attachment/12345/mmc2.pdf" in urls
    assert diag.needs_browser is False


# ── 3. SpringerLink & Nature Portfolio ───────────────────────────────────────


def test_nature_extended_data_figures_and_full_size_tables_excluded():
    html = """
    <html><body>
      <section aria-labelledby="Extended-data-figures-and-tables">
        <div data-container-type="supplementary-info">
          <div class="c-article-supplementary__item">
            <a data-test="supp-info-link" data-track-context="Extended data figures and tables"
               data-track-label="extended data fig. 1 performance comparison across additional dataset characteristics"
               href="/articles/s41586-024-08328-6/figures/7">
               Extended Data Fig. 1 Performance comparison across additional dataset characteristics, extending Fig. 3.
            </a>
          </div>
          <div class="c-article-supplementary__item">
            <a data-test="supp-info-link" data-track-context="Extended data figures and tables"
               href="/articles/s41586-024-08328-6/figures/8">
               Extended Data Fig. 2 Performance comparisons of TabPFN
            </a>
          </div>
          <div class="c-article-supplementary__item">
            <div id="table-1" class="c-article-table" data-test="inline-table">
              <div class="u-hide-print">
                <a aria-label="Full size table 1" class="c-article__pill-button" data-test="table-link"
                   href="/articles/s41586-024-08328-6/tables/1"><span>Full size table</span></a>
              </div>
            </div>
          </div>
          <div class="c-article-supplementary__item">
            <a data-test="supp-info-link"
               href="https://static-content.springer.com/esm/art%3A10.1038%2Fs41586-024-08328-6/MediaObjects/41586_2024_8328_MOESM1_ESM.pdf">
               Supplementary Tables 1–4
            </a>
          </div>
        </div>
      </section>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.nature.com/articles/s41586-024-08328-6")
    urls = [c.url for c in diag.candidates]
    assert urls == [
        "https://static-content.springer.com/esm/art%3A10.1038%2Fs41586-024-08328-6/MediaObjects/41586_2024_8328_MOESM1_ESM.pdf"
    ]
    assert diag.is_incomplete is False
    assert diag.needs_browser is False


def test_springer_additional_information_heading_and_page_range_s221_s231():
    html = """
    <html><body>
      <article>
        <p>Volume 65, pages S221–S231 (2023). (Suppl 2), S221–S231 (2023).</p>
        <h2 id="additional-information">Additional information</h2>
        <p>Publisher's note: Springer Nature remains neutral with regard to jurisdictional claims.</p>
      </article>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://link.springer.com/article/10.1134/S1075701523090039")
    assert diag.candidates == []
    assert diag.needs_browser is False
    assert sf.supplementary_table_ids("Volume 65, pages S221–S231 (2023)") == set()
    assert sf.supplementary_table_ids("(Suppl 2), S221–S231 (2023)") == set()
    assert "Missing supplementary tables" not in diag.incomplete_reason
    # Explicit table declarations remain intact
    assert sf.supplementary_table_ids("Tables S1-S3") == {"S1", "S2", "S3"}
    assert sf.supplementary_table_ids("Tables S221-S223") == {"S221", "S222", "S223"}


# ── 4. Wiley ─────────────────────────────────────────────────────────────────


def test_wiley_linkout_mat_slug_and_si_s01_bundle():
    html = """
    <html><body>
      <div class="supporting-information">
        <a href="/servlet/linkout?suffix=null&amp;dbid=32&amp;doi=10.1029%2F2021JB023170&amp;key=123">CAS</a>
        <a href="/topic/subject/mat-nano-quantum-sensing/.mat-nano-quantum-sensing">Quantum Sensing</a>
        <a href="/action/downloadSupplement?doi=10.1029%2F2021JB023170&amp;file=jgrb12345-sup-0001-SI-S01.pdf">SI-S01.pdf</a>
      </div>
      <p>Additional geochemical data are given in Supplementary Tables S1–S10.</p>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://agupubs.onlinelibrary.wiley.com/doi/full/10.1029/2021JB023170")
    urls = [c.url for c in diag.candidates]
    assert len(urls) == 1
    assert "downloadSupplement" in urls[0]
    assert sf.check_data_extension("/materials/sample.mat-nano-quantum-sensing") is None
    assert sf.check_data_extension("/materials/data_matrix.mat") == ".mat"
    assert sf.has_opaque_bundle_candidate(diag.candidates) is True
    assert diag.is_incomplete is False


# ── 5. Silverchair Ecosystem (ACS / RSC) ─────────────────────────────────────


def test_silverchair_acs_rsc_article_supplement_and_exclusions():
    html = """
    <html><body>
      <div class="widget-ArticleDataSupplements">
        <div class="dataSuppLink">
          <a href="/doi/suppl/10.1021/acsmacrolett.1c00553/article-supplement/998877/pdf/mz1c00553_si_001/">Supporting Information</a>
        </div>
      </div>
      <a href="/doi/10.1021/acsmacrolett.1c00553?goto=supporting-info">Supporting Information</a>
      <a href="https://pubs.rsc.org/en/journals/advanceAccess_2025.xml">RSS Feed</a>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://pubs.acs.org/doi/10.1021/acsmacrolett.1c00553")
    assert len(diag.candidates) == 1
    cand = diag.candidates[0]
    assert cand.url == "https://pubs.acs.org/doi/suppl/10.1021/acsmacrolett.1c00553/article-supplement/998877/pdf/mz1c00553_si_001/"
    assert cand.filename == "mz1c00553_si_001.pdf"
    assert cand.section in ("silverchair_supplement", "supplementary_section")

    # Also verify RSC / Silverchair widget extractor handles generic link text via Strategy 3
    rsc_html = """
    <div class="widget-ArticleDataSupplements">
      <div class="dataSuppLink">
        <a href="/doi/suppl/10.1039/D4PY00001A/article-supplement/1122/pdf/d4py00001a_suppl/">File</a>
      </div>
    </div>
    """
    rsc_diag = sf.find_all_candidates(rsc_html, "https://pubs.rsc.org/en/content/articlelanding/2024/py/d4py00001a")
    assert len(rsc_diag.candidates) == 1
    assert rsc_diag.candidates[0].filename == "d4py00001a_suppl.pdf"
    assert rsc_diag.candidates[0].section in ("silverchair_supplement", "supplementary_section")


# ── 6. Atypon, Cambridge, Ingenta/Annual Reviews, APS ────────────────────────


def test_atypon_tf_lyell_science_sm_bundle():
    html = """
    <html><body>
      <div class="supplementary-material">
        <a href="/doi/suppl/10.1080/01431161.2018.1433343?scroll=top">Supplemental Material</a>
        <a href="/action/downloadSupplement?doi=10.1080%2F01431161.2018.1433343&amp;file=tres_a_1433343_sm5998.zip">Download Zip</a>
        <a href="data:text/csv;charset=utf-8,col1,col2%0A1,2">Download CSV</a>
      </div>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.tandfonline.com/doi/full/10.1080/01431161.2018.1433343")
    assert [c.url for c in diag.candidates] == [
        "https://www.tandfonline.com/action/downloadSupplement?doi=10.1080%2F01431161.2018.1433343&file=tres_a_1433343_sm5998.zip"
    ]

    sci_html = """
    <html><body>
      <section class="supplementary-material">
        <a href="/doi/suppl/10.1126/science.aab2633/suppl_file/kuzmina-sm.pdf">Supplementary Materials</a>
      </section>
      <p>See Supplementary Table S1 for specimen localities.</p>
    </body></html>
    """
    sci_diag = sf.find_all_candidates(sci_html, "https://www.science.org/doi/10.1126/science.aab2633")
    assert len(sci_diag.candidates) == 1
    assert sf.has_opaque_bundle_candidate(sci_diag.candidates) is True
    assert sci_diag.is_incomplete is False


def test_degruyter_cambridge_nav_and_nuxt_not_matched():
    html = """
    <html><body>
      <nav>
        <a href="/publishing/services/for-booksellers-library-suppliers">For Booksellers &amp; Library Suppliers</a>
        <a href="/core/services/authors/publishing-supplementary-material">Publishing supplementary material</a>
      </nav>
      <article><h1>Geoscience Study</h1><p>No supplements exist.</p></article>
      <script>window.__NUXT__ = {"data": [{}]};</script>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.cambridge.org/core/journals/test/article/123")
    assert diag.candidates == []
    assert diag.needs_browser is False


def test_annual_reviews_figure_ppt_excluded_and_aps_supplement_supported():
    ar_html = """
    <html><body>
      <div class="supplementary">
        <a href="/docserver/fulltext/earth/39/1/ea390001.f1.ppt">Download Figure 1 PPT</a>
        <a href="/docserver/fulltext/earth/39/1/ea390001.f2.ppt">Figure 2</a>
        <a href="/docserver/fulltext/earth/39/1/supp_dataset_1.xlsx">Supplementary Dataset 1</a>
      </div>
    </body></html>
    """
    ar_diag = sf.find_all_candidates(ar_html, "https://www.annualreviews.org/doi/10.1146/annurev-earth-040809-152521")
    assert [c.url for c in ar_diag.candidates] == [
        "https://www.annualreviews.org/docserver/fulltext/earth/39/1/supp_dataset_1.xlsx"
    ]

    aps_html = """
    <html><body>
      <div class="article-actions">
        <a href="/prl/pdf/10.1103/PhysRevLett.120.010101">Subscription Required</a>
      </div>
      <section class="article-page-section article supplemental" data-magellan-destination="supplemental">
        <h2>Supplemental Material</h2>
        <a href="/prl/supplemental/10.1103/PhysRevLett.120.010101/supp_info.pdf">Supplemental Material (PDF)</a>
      </section>
    </body></html>
    """
    aps_diag = sf.find_all_candidates(aps_html, "https://journals.aps.org/prl/abstract/10.1103/PhysRevLett.120.010101")
    assert [c.url for c in aps_diag.candidates] == [
        "https://journals.aps.org/prl/supplemental/10.1103/PhysRevLett.120.010101/supp_info.pdf"
    ]


# ── 7. Frontiers, SciEngine, RHHZ1, RHHZ2 & Misc ────────────────────────────


def test_frontiers_rendered_with_statement_and_nuxt_data_complete():
    html = """
    <html><body>
      <div class="Statement__Supplementarymaterial">
        <p>The Supplementary Material for this article can be found online at:
          <a href="#supplementary-material">https://www.frontiersin.org/articles/10.3389/feart.2024.111/full#supplementary-material</a>
        </p>
      </div>
      <div id="supplementaryMaterial-content">
        <a href="https://public-pages-files-2025.frontiersin.org/articles/111/file/Data_Sheet_1.docx/111_ds1/1">Data Sheet 1</a>
        <a href="https://public-pages-files-2025.frontiersin.org/articles/111/file/Table_1.docx/111_tbl1/1">Table 1</a>
      </div>
      <script id="__NUXT_DATA__" type="application/json">[["ShallowReactive", 1], {"@_xlink:href": "Table_1.docx"}]</script>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.frontiersin.org/journals/earth-science/articles/10.3389/feart.2024.111/full")
    assert len(diag.candidates) == 2
    assert diag.is_incomplete is False
    assert diag.needs_browser is False


def test_sciengine_rendered_dom_and_open_api(monkeypatch):
    spa_shell = "<html><body><div id='app'></div><script src='/assets/index.js'></script></body></html>"
    spa_diag = sf.find_all_candidates(spa_shell, "https://www.sciengine.com/SSTe/doi/10.1360/N072021-0099")
    assert spa_diag.candidates == []
    assert spa_diag.needs_browser is True

    rendered_html = """
    <html><body>
      <a href="/plat/search?queryField_a=geology">Search A1</a>
      <div class="detail-wrapper">
        <div class="title">附件 (1)</div>
        <div id="moop-inner">
          <div class="icon-text-line cursor-pointer">
            <div class="text">附录_1.xls</div>
          </div>
        </div>
      </div>
    </body></html>
    """
    ren_diag = sf.find_all_candidates(rendered_html, "https://www.sciengine.com/SSTe/doi/10.1360/N072021-0099")
    assert len(ren_diag.candidates) == 1
    assert ren_diag.candidates[0].filename == "附录_1.xls"
    assert ren_diag.candidates[0].extra_info.get("sciengine_attachment") is True
    assert "A1" not in sf.detect_declared_table_range("See sample results on page")

    fake_basic = MagicMock(status_code=200)
    fake_basic.json.return_value = {
        "data": {"article": {"baseId": "64292B7DB29D4C2CBB2ACB77C3008CE1", "titleEn": "Tibetan Plateau Tectonics"}}
    }
    fake_moop = MagicMock(status_code=200)
    fake_moop.json.return_value = {
        "data": [{"titleZh": "附录_1.xls", "fileName": "N072021-0099Supplementary.xls", "filePath": "61dfc3c8ffde8a05359e4615"}]
    }

    with patch("requests.get", side_effect=[fake_basic, fake_moop]):
        api_cands, title = sf.fetch_sciengine_api_candidates("https://www.sciengine.com/SSTe/doi/10.1360/N072021-0099")
    assert title == "Tibetan Plateau Tectonics"
    assert len(api_cands) == 1
    assert api_cands[0].url == "https://www.sciengine.com/sci-open/api/v1/open/file/61dfc3c8ffde8a05359e4615"
    assert api_cands[0].filename == "N072021-0099Supplementary.xls"


def test_rhhz1_fileup_and_sample_ids_17d8s2_802_1s1():
    html = """
    <html><body>
      <p>Sample IDs 17D8S2 and 802-1S1 and 17D8S1–17D8S2 were analyzed by LA-ICP-MS.</p>
      <div id="Supplementary">
        <a href="/fileup/1000-2383/SUPP/2020/supp_table.docx">Supplementary Table 1.docx</a>
      </div>
    </body></html>
    """
    assert sf.supplementary_table_ids("Sample IDs 17D8S2 and 802-1S1 and 17D8S1-17D8S2 were analyzed.") == set()
    diag = sf.find_all_candidates(html, "https://www.earth-science.net/cn/article/doi/10.3799/dqkx.2019.207")
    assert [c.url for c in diag.candidates] == [
        "https://www.earth-science.net/fileup/1000-2383/SUPP/2020/supp_table.docx"
    ]
    assert "Missing supplementary tables" not in diag.incomplete_reason


def test_rhhz2_export_supplementary_post_and_exclusions(tmp_path):
    import journal_downloader as jd

    html = """
    <html><body>
      <ul><li id="Supplements"></li></ul>
      <a href="/sitemap.txt">站点地图</a>
      <a href="/soft/Reader.rar">下载PDF阅读器</a>
      <a href="/rss.xml">RSS</a>
      <a href="/export/7866.txt">RefWorks</a>
      <div class="supp-list">
        <a href="javascript:void(0)" onclick="exportSupplementary('9876')">附表1.xlsx</a>
      </div>
      <form id="suppForm" action="/article/exportSupplementary" method="post">
        <input type="hidden" name="id" value="">
        <input type="hidden" name="pageType" value="1">
      </form>
      <script>
        var other = {"related": "/doi/10.1086/999999/plutons.zip"};
      </script>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.ygxb.ac.cn/article/doi/10.1086/123456")
    assert len(diag.candidates) == 1
    cand = diag.candidates[0]
    assert cand.match_rule == "rhhz2_export_supplementary"
    assert cand.extra_info["http_method"] == "POST"
    assert cand.extra_info["post_data"] == {"id": "9876", "pageType": "1"}
    assert diag.needs_browser is False
    assert diag.is_incomplete is False

    # Verify POST download path in journal_downloader.download_file
    fake_post_resp = MagicMock(status_code=200, content=office_archive(), headers={"Content-Disposition": 'attachment; filename="Appendix_S1.xlsx"'})
    fake_check_resp = MagicMock(status_code=200, content=b'{"hasSupp":true}', headers={})
    fake_session = MagicMock()
    fake_session.post.side_effect = [fake_check_resp, fake_post_resp]
    with patch("requests.Session", return_value=fake_session):
        saved = jd.download_file(cand, str(tmp_path))
    assert saved is not None
    assert saved.endswith("Appendix_S1.xlsx")
    assert os.path.exists(saved)


def test_round2_blocked_page_client_challenge_radware_script_and_chinese_article():
    client_challenge = """
    <html><head><title>Client Challenge</title></head>
    <body><script src="/_fs-ch-1T1wmsGaOgGaSxcX/script.js"></script><noscript>Please enable JS</noscript></body></html>
    """
    assert sf.is_blocked_page(client_challenge) is True
    assert sf.is_placeholder_or_blocked_title("Client Challenge") is True

    radware_script = """
    <html><head><title>Loading...</title>
    <script src="https://validate.perfdrive.com/captcha.js"></script></head>
    <body><p>Security check in progress</p></body></html>
    """
    assert sf.is_blocked_page(radware_script) is True

    short_zh_cf = "<html><head><title>请稍候...</title></head><body>正在检查站点连接安全性，请稍候...</body></html>"
    assert sf.is_blocked_page(short_zh_cf) is True

    long_zh_article = (
        "<html><head><title>青藏高原东缘构造演化特征研究</title></head><body>"
        "<div id='loading-spinner'>数据加载中，请稍候...</div>"
        + ("<p>本文通过对青藏高原东缘典型剖面的岩相学、年代学和地球化学分析，揭示了晚中生代以来的多期构造变形过程。</p>" * 120)
        + "</body></html>"
    )
    assert sf.is_blocked_page(long_zh_article) is False


def test_round2_nuxt3_ssr_dual_markers_and_springer_anchor_additional_info():
    nuxt3_html = (
        "<html><head><title>遥感学报文章页</title></head><body>"
        "<main>" + ("<p>本文提出了一种基于多源遥感数据的地表温度反演方法并进行了野外验证。</p>" * 80) + "</main>"
        "<script>window.__NUXT__={config:{}}</script>"
        "<script id='__NUXT_DATA__' type='application/json'>[]</script>"
        "</body></html>"
    )
    diag = sf.find_all_candidates(nuxt3_html, "https://www.ygxb.ac.cn/cn/article/doi/10.11834/jrs.20209297")
    assert diag.needs_browser is False

    springer_html = """
    <html><body>
      <main>
        <p>Abstract text without any supplementary files.</p>
        <a href="#additional-information">Additional information</a>
        <h2 id="additional-information">Additional information</h2>
        <p>Publisher's note: Springer Nature remains neutral.</p>
      </main>
    </body></html>
    """
    s_diag = sf.find_all_candidates(springer_html, "https://link.springer.com/article/10.1007/s00126-024-01333-9")
    assert s_diag.needs_browser is False


def test_round2_elsevier_appendices_without_heading_upgrades_section_and_empty_div():
    html = """
    <html><body>
      <script>window.__PRELOADED_STATE__ = {"article": {"pii": "S0169136824000010"}};</script>
      <div id="Supplements"></div>
      <div class="Appendices">
        <a class="anchor download-link" href="https://ars.els-cdn.com/content/image/1-s2.0-S0169136824000010-mmc1.xlsx">Download</a>
      </div>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.sciencedirect.com/science/article/pii/S0169136824000010")
    assert len(diag.candidates) == 1
    assert diag.candidates[0].section in sf._DOM_SUPP_SECTIONS
    assert diag.needs_browser is False
    assert diag.is_incomplete is False


def test_round2_sciengine_opaque_bundle_fulu_label_selector_and_dict_api():
    from discovery_state import DiscoveryEvidence

    rendered_html = """
    <html><body>
      <p>详见网络版附表S1。</p>
      <div class="detail-wrapper">
        <div class="title">附件 (1)</div>
        <div class="icon-text-line cursor-pointer"><div class="text">附录_1.xls</div></div>
      </div>
    </body></html>
    """
    ren_diag = sf.find_all_candidates(rendered_html, "https://www.sciengine.com/SSTe/doi/10.1360/N072021-0099")
    assert len(ren_diag.candidates) == 1
    assert ren_diag.candidates[0].extra_info.get("browser_selector") == "div.detail-wrapper div.icon-text-line:nth-of-type(1)"

    fake_basic = MagicMock(status_code=200)
    fake_basic.json.return_value = {
        "data": {"article": {"baseId": "BASE123", "title": "华南陆块中生代构造岩浆作用"}}
    }
    fake_moop = MagicMock(status_code=200)
    fake_moop.json.return_value = {
        "data": {
            "list": [
                {"titleZh": "附录_1.xls", "fileName": "N072021-0099Supplementary.xls", "filePath": "61dfc3c8ffde8a05359e4615"},
                {"titleEn": "Dataset mirror", "fileName": "extra_data", "linkUrl": "/downloads/extra.zip", "fileType": "application/zip"},
            ]
        }
    }
    with patch("requests.get", side_effect=[fake_basic, fake_moop]):
        se_cands, se_title = sf.fetch_sciengine_api_candidates("https://www.sciengine.com/SSTe/doi/10.1360/N072021-0099")
    assert se_title == "华南陆块中生代构造岩浆作用"
    assert len(se_cands) == 2
    assert se_cands[1].url == "https://www.sciengine.com/downloads/extra.zip"
    assert se_cands[1].filename.endswith(".zip")

    ev = DiscoveryEvidence()
    ev.observe("publisher", rendered_html, "https://www.sciengine.com/SSTe/doi/10.1360/N072021-0099")
    inc_single, reason_single = ev.check([se_cands[0]])
    assert inc_single is False, reason_single
    inc, reason = ev.check(se_cands)
    assert inc is False, reason


def test_round2_rhhz2_without_form_and_scansci_post_download(tmp_path):
    import scansci_supp_downloader as sd

    html = """
    <html><body>
      <input type="hidden" id="basePath" value="/app/">
      <div class="supp-list">
        <a href="javascript:void(0)" onclick="exportSupplementary('42')">附表S1.xlsx</a>
      </div>
    </body></html>
    """
    diag = sf.find_all_candidates(html, "https://www.ygxb.ac.cn/app/article/doi/10.11834/jrs.20209297")
    assert len(diag.candidates) == 1
    cand = diag.candidates[0]
    assert cand.extra_info["post_url"] == "https://www.ygxb.ac.cn/app/article/exportSupplementary"
    assert cand.extra_info["check_url"] == "https://www.ygxb.ac.cn/app/article/checkSupplement?id=42"

    fake_check_resp = MagicMock(status_code=200, content=b'{"hasSupp":true}', headers={})
    fake_post_resp = MagicMock(
        status_code=200,
        headers={"Content-Disposition": 'attachment; filename="Appendix_S1.xlsx"'},
    )
    fake_post_resp.iter_content.return_value = [office_archive()]
    fake_session = MagicMock()
    fake_session.post.side_effect = [fake_check_resp, fake_post_resp]
    with patch("requests.Session", return_value=fake_session):
        saved = sd.download_file(cand, str(tmp_path))
    assert saved is not None
    assert saved.endswith("Appendix_S1.xlsx")
    assert os.path.exists(saved)
    assert "User-Agent" in fake_session.post.call_args_list[1].kwargs.get("headers", {})


def test_round2_edge_filters_silverchair_aps_refworks_reader_and_colon_range():
    assert sf.extract_filename_from_url(
        "https://pubs.rsc.org/en/content/article-supplement/2024/py/d4py00001a.si1/pdf/d4py00001a.si1/"
    ).endswith(".pdf")
    assert sf.junk_candidate_reason(
        "https://journals.aps.org/physrevb/pdf/10.1103/PhysRevB.100.014101", "PDF"
    ) == "article_pdf_viewer"
    assert sf.junk_candidate_reason(
        "https://www.persee.fr/export/7866.txt", "Export", attrs={"title": "Export to RefWorks"}
    ) == "citation_export"
    assert sf.junk_candidate_reason(
        "https://www.cagsbulletin.com/soft/foxit-reader.rar", "PDF阅读器"
    ) == "reader_utility_archive"
    assert sf.detect_declared_table_range("Geol. Ore Deposits, 65 (Suppl 2: S221-S231)") == set()
    assert sf.is_article_figure_url(
        "https://www.annualreviews.org/docserver/fulltext/earth/39/1/annurev.ea390001_fig2.pptx"
    ) is True

