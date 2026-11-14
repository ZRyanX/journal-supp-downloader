"""Regression and unit tests for mainstream publisher supplementary structures,
hyperlinks/controls, and embedded JSON extraction in scripts/supp_finder.py.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

import supp_finder as sf  # noqa: E402
import repository_resolver as rr  # noqa: E402


class TestGeoScienceWorldAndSilverchair(unittest.TestCase):
    def test_geoscienceworld_silverchair_widget_and_cdn_links(self):
        html = """
        <html><body>
          <article>
            <div class="widget-article-supplementary-data" id="supplementary-data">
              <h2>Supplementary Data</h2>
              <div class="data-supplement">
                <span class="supplement-description">Table S1. Geochemical analyses of stibnite</span>
                <a class="al-link" href="https://silverchair-cdn.com/gsw/content_public/journal/econgeo/119/4/10.5382_econgeo.5012/2/econgeo-5012-s1.xlsx?Signature=abc&amp;Expires=999999&amp;Key-Pair-Id=XYZ">
                  econgeo-5012-s1.xlsx
                </a>
              </div>
              <div class="supplement-item">
                <a href="/gsw/content_public/journal/econgeo/119/4/10.5382_econgeo.5012/2/appendix_a.pdf">Appendix A (PDF)</a>
              </div>
              <div class="supplement-item">
                <a href="/econgeo/article-pdf/119/4/801/main.pdf">Download Article PDF</a>
              </div>
            </div>
          </article>
        </body></html>
        """
        diag = sf.find_all_candidates(
            html,
            "https://pubs.geoscienceworld.org/segweb/economicgeology/article/119/4/801/123456",
        )
        urls = [c.url for c in diag.candidates]
        self.assertTrue(any("econgeo-5012-s1.xlsx" in u for u in urls))
        self.assertIn(
            "https://pubs.geoscienceworld.org/gsw/content_public/journal/econgeo/119/4/10.5382_econgeo.5012/2/appendix_a.pdf",
            urls,
        )
        self.assertFalse(any("/article-pdf/" in u for u in urls))

    def test_oup_silverchair_content_public_and_gsa_figshare_doi(self):
        html = """
        <html><body>
          <section class="widget-SupplementaryData">
            <h3>Supplementary Data</h3>
            <p><a href="https://oup.silverchair-cdn.com/oup/backfile/Content_public/Journal/petrology/65/1/10.1093_petrology_egae001/1/egae001_supplementary_data.zip">Supplementary Data File</a></p>
          </section>
          <section id="data-availability">
            <p>Supplemental tables are hosted at <a href="https://doi.org/10.1130/geos.s.21957038.v3">https://doi.org/10.1130/geos.s.21957038.v3</a>.</p>
            <p>Unrelated citation: <a href="https://doi.org/10.1130/B36000.1">Smith et al., 2022</a>.</p>
          </section>
        </body></html>
        """
        diag = sf.find_all_candidates(
            html,
            "https://academic.oup.com/petrology/article/65/1/egae001/7500001",
        )
        urls = [c.url for c in diag.candidates]
        self.assertIn(
            "https://oup.silverchair-cdn.com/oup/backfile/Content_public/Journal/petrology/65/1/10.1093_petrology_egae001/1/egae001_supplementary_data.zip",
            urls,
        )
        self.assertIn("https://doi.org/10.1130/geos.s.21957038.v3", urls)
        self.assertNotIn("https://doi.org/10.1130/B36000.1", urls)
        self.assertTrue(rr.is_repository_landing("https://doi.org/10.1130/geos.s.21957038.v3"))
        self.assertTrue(rr.is_repository_landing("https://geosociety.figshare.com/articles/dataset/21957038"))


class TestAtyponAndOtherPublishers(unittest.TestCase):
    def test_atypon_tandf_sage_acs_science_pnas_structures(self):
        html = """
        <html><body>
          <div class="supplemental-material-container">
            <a data-testid="supplementary-material-link" href="/doi/suppl/10.1080/00206814.2024.12345/suppl_file/tigr_a_12345_sm01.xlsx">Table S1</a>
            <a class="supplementary-material-link" href="/doi/suppl/10.1177/0000000024/suppl_file/supp_appendix.pdf">Online Appendix</a>
            <a href="/doi/epdf/10.1080/00206814.2024.12345">Read ePDF</a>
          </div>
          <div class="core-collateral">
            <a href="/doi/suppl/10.1126/science.abc1234/suppl_file/science.abc1234_sm.pdf">Supplementary Materials</a>
          </div>
          <div class="supInfoList">
            <a href="/doi/suppl/10.1021/acs.est.4c00001/suppl_file/es4c00001_si_001.pdf">Supporting Information (PDF)</a>
          </div>
        </body></html>
        """
        diag = sf.find_all_candidates(html, "https://www.tandfonline.com/doi/full/10.1080/00206814.2024.12345")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://www.tandfonline.com/doi/suppl/10.1080/00206814.2024.12345/suppl_file/tigr_a_12345_sm01.xlsx", urls)
        self.assertIn("https://www.tandfonline.com/doi/suppl/10.1177/0000000024/suppl_file/supp_appendix.pdf", urls)
        self.assertIn("https://www.tandfonline.com/doi/suppl/10.1126/science.abc1234/suppl_file/science.abc1234_sm.pdf", urls)
        self.assertIn("https://www.tandfonline.com/doi/suppl/10.1021/acs.est.4c00001/suppl_file/es4c00001_si_001.pdf", urls)
        self.assertFalse(any("/doi/epdf/" in u for u in urls))

    def test_rsc_cambridge_copernicus_springer_and_plos_filtering(self):
        rsc_html = """
        <html><body>
          <div class="ESIright_highlight_txt_red">
            <a href="https://www.rsc.org/suppdata/d4/ja/d4ja00001a/d4ja00001a1.pdf">Supplementary Information</a>
          </div>
          <div class="ESIright_highlight_txt_red_hidden">
            <a href="https://www.rsc.org/suppdata/d4/ja/d4ja00001a/d4ja00001a2.xlsx">Dataset S1</a>
          </div>
        </body></html>
        """
        rsc_diag = sf.find_all_candidates(rsc_html, "https://pubs.rsc.org/en/content/articlelanding/2024/ja/d4ja00001a")
        rsc_urls = [c.url for c in rsc_diag.candidates]
        self.assertIn("https://www.rsc.org/suppdata/d4/ja/d4ja00001a/d4ja00001a1.pdf", rsc_urls)
        self.assertIn("https://www.rsc.org/suppdata/d4/ja/d4ja00001a/d4ja00001a2.xlsx", rsc_urls)

        cambridge_html = """
        <html><body>
          <div class="supplementary-material">
            <a class="access-supplementary" href="https://static.cambridge.org/content/id/urn:cambridge.org:id:article:S001675682400001X/resource/name/S001675682400001Xsup001">Download Supplementary Table S1</a>
          </div>
        </body></html>
        """
        cam_diag = sf.find_all_candidates(cambridge_html, "https://www.cambridge.org/core/journals/geological-magazine/article/abs/test")
        cam_urls = [c.url for c in cam_diag.candidates]
        self.assertIn(
            "https://static.cambridge.org/content/id/urn:cambridge.org:id:article:S001675682400001X/resource/name/S001675682400001Xsup001",
            cam_urls,
        )

        # Springer live supp-item / supp-info-link (including extensionless / non-table-titled link)
        springer_html = """
        <html><body>
          <div class="c-article-supplementary__item" data-test="supp-item" id="MOESM1">
            <h3 class="c-article-supplementary__title">Supplementary file 1</h3>
            <a data-test="supp-info-link" data-track-action="view supplementary info"
               href="https://static-content.springer.com/esm/art%3A10.1007%2Fs00126-025-01387-3/MediaObjects/126_2025_1387_MOESM1_ESM.pdf">
               Supplementary file1 (PDF 450 KB)
            </a>
          </div>
          <div class="c-article-supplementary__item" data-test="supp-item" id="MOESM2">
            <a data-test="supp-info-link" data-track-action="view supplementary info"
               href="https://static-content.springer.com/media/item/42">
               Inspect
            </a>
          </div>
        </body></html>
        """
        sp_diag = sf.find_all_candidates(springer_html, "https://link.springer.com/article/10.1007/s00126-025-01387-3")
        sp_urls = [c.url for c in sp_diag.candidates]
        self.assertEqual(len(sp_urls), 2)
        self.assertEqual(sp_diag.candidates[0].filename, "126_2025_1387_MOESM1_ESM.pdf")
        self.assertEqual(sp_diag.candidates[1].match_rule, "nature_data_test_supp_link")

        # PLOS excludes editorial /s/ info pages and type=manuscript XML while keeping type=supplementary
        plos_html = """
        <html><body>
          <div class="supplementary-material">
            <a href="/plosone/s/supporting-information">Supporting Information</a>
            <a href="/plosone/s/data-availability">Data Availability</a>
            <a href="/plosone/article/file?id=10.1371/journal.pone.0298123&amp;type=manuscript">XML</a>
            <a href="/plosone/article/file?id=10.1371/journal.pone.0298123.s001&amp;type=supplementary">S1 Appendix.</a>
          </div>
        </body></html>
        """
        plos_diag = sf.find_all_candidates(plos_html, "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0298123")
        plos_urls = [c.url for c in plos_diag.candidates]
        self.assertEqual(plos_urls, ["https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0298123.s001&type=supplementary"])


class TestFrontiersNuxtAndEmbeddedJson(unittest.TestCase):
    def test_frontiers_static_nuxt_data_ignores_bare_jats_filename_and_anchor_triggers_needs_browser(self):
        html = """
        <html><body>
          <div class="Statement__Supplementarymaterial">
            <a href="#supplementary-material">https://www.frontiersin.org/articles/10.3389/fpls.2026.1988876/full#supplementary-material</a>
            <span class="SupplementaryMaterial" id="SM1"></span>
          </div>
          <script id="__NUXT_DATA__" type="application/json">
          [
            ["ShallowReactive", 1],
            {"article": 2},
            {"supp": 3, "link": 6},
            {"@_xlink:href": 4, "@_id": 5},
            "Table1.xlsx",
            "SM1",
            "https://www.frontiersin.org/articles/10.3389/fpls.2026.1988876/full#supplementary-material"
          ]
          </script>
        </body></html>
        """
        diag = sf.find_all_candidates(
            html,
            "https://www.frontiersin.org/journals/plant-science/articles/10.3389/fpls.2026.1988876/full",
        )
        self.assertEqual(diag.candidates, [])
        self.assertTrue(diag.needs_browser)

    def test_frontiers_rendered_button_icon_extracts_segment_filename(self):
        html = """
        <html><body>
          <div class="SidePanelID__section" id="supplementaryMaterial-content">
            <ul>
              <li>
                <span>Table 1.xlsx</span>
                <a href="https://public-pages-files-2025.frontiersin.org/articles/1988876/file/Table_1.xlsx/1988876_table_1/1"
                   class="ButtonIcon ButtonIcon--black ButtonIcon--small"></a>
              </li>
            </ul>
          </div>
        </body></html>
        """
        url = "https://public-pages-files-2025.frontiersin.org/articles/1988876/file/Table_1.xlsx/1988876_table_1/1"
        self.assertEqual(sf.extract_filename_from_url(url), "Table_1.xlsx")
        diag = sf.find_all_candidates(
            html,
            "https://www.frontiersin.org/journals/plant-science/articles/10.3389/fpls.2026.1988876/full",
        )
        self.assertEqual(len(diag.candidates), 1)
        self.assertEqual(diag.candidates[0].url, url)
        self.assertEqual(diag.candidates[0].filename, "Table_1.xlsx")

    def test_embedded_json_ieee_multimedia_sciencedirect_attachments_and_spa_shell(self):
        ieee_html = """
        <html><body>
          <script>
            xplGlobal.document.metadata = {
              "articleNumber": "10456789",
              "pdfPath": "/stamp/stamp.jsp?tp=&arnumber=10456789",
              "multimedia": [
                {"name": "Supplementary Dataset S1", "mediaPath": "/ielx7/12/34/10456789/supp_dataset.zip", "type": "zip"}
              ]
            };
          </script>
        </body></html>
        """
        ieee_diag = sf.find_all_candidates(ieee_html, "https://ieeexplore.ieee.org/document/10456789")
        ieee_urls = [c.url for c in ieee_diag.candidates]
        self.assertIn("https://ieeexplore.ieee.org/ielx7/12/34/10456789/supp_dataset.zip", ieee_urls)
        self.assertFalse(any("stamp.jsp" in u for u in ieee_urls))

        sd_html = """
        <html><body>
          <script>
            window.__PRELOADED_STATE__ = {
              "attachments": {
                "items": [
                  {
                    "attachment-eid": "1-s2.0-S0169136824005171-mmc1.xlsx",
                    "file-name": "Table S1.xlsx",
                    "downloadUrl": "https://ars.els-cdn.com/content/image/1-s2.0-S0169136824005171-mmc1.xlsx",
                    "type": "A"
                  }
                ]
              }
            };
          </script>
        </body></html>
        """
        sd_diag = sf.find_all_candidates(sd_html, "https://www.sciencedirect.com/science/article/pii/S0169136824005171")
        sd_urls = [c.url for c in sd_diag.candidates]
        self.assertEqual(sd_urls, ["https://ars.els-cdn.com/content/image/1-s2.0-S0169136824005171-mmc1.xlsx"])

        # Empty Vue SPA shell (e.g. SciEngine) should set needs_browser=True
        spa_html = '<!doctype html><html><head><title>SciEngine</title></head><body><div id="app"></div><script type="module" src="/assets/index.js"></script></body></html>'
        spa_diag = sf.find_all_candidates(spa_html, "https://www.sciengine.com/SCES/doi/10.1007/s11430-023-1288-1")
        self.assertTrue(spa_diag.needs_browser)


class TestReviewerAuditRegressionsAndNewPublishers(unittest.TestCase):
    def test_jstage_tab_routes_and_copernicus_xml_doi_filename_regressions(self):
        jstage_html = """
        <html><body>
          <nav>
            <a href="/static/pages/News/TAB1/Current/Page1/-char/en">News Tab</a>
            <a href="/static/pages/Help/TAB2/Current/Page1/-char/en">Help Tab</a>
          </nav>
          <div class="related-file-list">
            <a href="/article/geochemj/57/1/57_GJ23001/_supplement/57_GJ23001_s1.xlsx">Table S1</a>
          </div>
        </body></html>
        """
        j_diag = sf.find_all_candidates(jstage_html, "https://www.jstage.jst.go.jp/article/geochemj/57/1/57_GJ23001/_article/-char/en")
        j_urls = [c.url for c in j_diag.candidates]
        self.assertFalse(any("/TAB1/" in u or "/TAB2/" in u for u in j_urls))
        self.assertIn("https://www.jstage.jst.go.jp/article/geochemj/57/1/57_GJ23001/_supplement/57_GJ23001_s1.xlsx", j_urls)

        cop_html = """
        <html><body>
          <a href="/articles/24/6071/2024/acp-24-6071-2024.xml">Full-text XML</a>
          <a href="/articles/24/6071/2024/acp-24-6071-2024-supplement.pdf">Supplement (PDF)</a>
          <a href="https://doi.org/10.5194/acp-24-6071-2024-supplement">https://doi.org/10.5194/acp-24-6071-2024</a>
        </body></html>
        """
        c_diag = sf.find_all_candidates(cop_html, "https://acp.copernicus.org/articles/24/6071/2024/")
        c_by_url = {c.url: c for c in c_diag.candidates}
        self.assertNotIn("https://acp.copernicus.org/articles/24/6071/2024/acp-24-6071-2024.xml", c_by_url)
        self.assertIn("https://acp.copernicus.org/articles/24/6071/2024/acp-24-6071-2024-supplement.pdf", c_by_url)
        doi_cand = c_by_url.get("https://doi.org/10.5194/acp-24-6071-2024-supplement")
        self.assertIsNotNone(doi_cand)
        self.assertEqual(doi_cand.filename, "acp-24-6071-2024-supplement")

    def test_atypon_data_supp_file_completeness_and_citation_pdf_doi_noise(self):
        html = """
        <html><body>
          <div class="supplemental-material-container">
            <a data-supp-file="/doi/suppl/10.1080/1234567/suppl_file/table_s1.xlsx">Download Supplement</a>
            <a href="/doi/pdfdirect/10.1080/1234567">Article PDF Direct</a>
            <a href="/doi/reader/10.1080/1234567">Online Reader</a>
          </div>
          <a href="/action/downloadCitation?doi=10.1080/1234567&amp;format=.txt">Citation TXT</a>
          <a href="/citation/download.txt">Download Citation</a>
          <a href="https://onlinelibrary.wiley.com/doi/full/10.1002/ggge.2024.r/abstract">Related Article</a>
        </body></html>
        """
        diag = sf.find_all_candidates(html, "https://www.tandfonline.com/doi/full/10.1080/1234567")
        urls = [c.url for c in diag.candidates]
        self.assertEqual(urls, ["https://www.tandfonline.com/doi/suppl/10.1080/1234567/suppl_file/table_s1.xlsx"])
        self.assertFalse(diag.is_incomplete, diag.incomplete_reason)

    def test_sciencedirect_pdfft_rejected_and_eid_synthesized_plus_nuxt_memo(self):
        sd_html = """
        <html><body>
          <script>
            window.__PRELOADED_STATE__ = {
              "attachments": {
                "items": [
                  {
                    "attachment-eid": "1-s2.0-S0169136824005171-mmc1.xlsx",
                    "file-name": "Table S1.xlsx",
                    "url": "/science/article/pii/S0169136824005171/pdfft?isDTMRedir=true",
                    "type": "A"
                  }
                ]
              }
            };
          </script>
        </body></html>
        """
        sd_diag = sf.find_all_candidates(sd_html, "https://www.sciencedirect.com/science/article/pii/S0169136824005171")
        sd_urls = [c.url for c in sd_diag.candidates]
        self.assertEqual(sd_urls, ["https://ars.els-cdn.com/content/image/1-s2.0-S0169136824005171-mmc1.xlsx"])

        # Shared DAG nodes in __NUXT_DATA__ must resolve via memoization without exhausting node budget
        shared = {"downloadUrl": 3, "fileName": 4}
        payload = [{"items": 1}, [2] * 300, shared, "https://cdn.example.org/files/s1.xlsx", "Table S1.xlsx"]
        decoded = sf._decode_nuxt_data_payload(payload, max_depth=12, max_nodes=50)
        self.assertEqual(len(decoded["items"]), 200)
        self.assertEqual(decoded["items"][0]["downloadUrl"], "https://cdn.example.org/files/s1.xlsx")

    def test_sibling_label_isolation_and_repository_landing_edge_cases(self):
        html = """
        <html><body>
          <div id="supplementaryMaterial-content">
            <ul>
              <li><span>Item Alpha</span><a href="https://cdn.example.org/a/1"></a></li>
              <li><span>Item Beta</span><a href="https://cdn.example.org/b/2"></a></li>
            </ul>
            <ul id="flat-list">
              <a href="https://cdn.example.org/c/3"></a>
              <a href="https://cdn.example.org/d/4"></a>
            </ul>
          </div>
        </body></html>
        """
        soup = sf.BeautifulSoup(html, "html.parser")
        flat_links = soup.select("#flat-list a")
        # Multi-control flat parent must NOT steal combined label text across sibling links
        self.assertEqual(sf._sibling_or_container_label(flat_links[0]), "")
        # Immediate sibling label works inside structured <li>
        li_links = soup.select("li a")
        self.assertEqual(sf._sibling_or_container_label(li_links[0]), "Item Alpha")
        self.assertEqual(sf._sibling_or_container_label(li_links[1]), "Item Beta")

        self.assertFalse(rr.is_repository_landing("https://ndownloader.figshare.com/files/12345678"))
        self.assertTrue(rr.is_repository_landing("https://pangaea.de/?q=stibnite"))
        self.assertTrue(rr.is_repository_landing("https://www.pangaea.de/ddi/10.1594/PANGAEA.900001"))
        self.assertTrue(rr.is_repository_landing("https://scidb.cn/detail?dataSetId=abc"))

    def test_new_publisher_extractors_silverchair_peerj_mdpi_ojs_cell_nature_magtech(self):
        # Silverchair widget-instance + data-supplement-url + /Citation/Download skip
        sc_html = """
        <html><body>
          <div class="widget-instance-ArticleSupplementaryData">
            <a data-supplement-url="/gsw/DownloadFile/table_s1.xlsx">Table S1</a>
            <a href="/gsw/Citation/Download?resourceId=1">Download RIS</a>
          </div>
        </body></html>
        """
        sc_diag = sf.find_all_candidates(sc_html, "https://pubs.geoscienceworld.org/econgeo/article/119/1/1")
        self.assertEqual([c.url for c in sc_diag.candidates], ["https://pubs.geoscienceworld.org/gsw/DownloadFile/table_s1.xlsx"])

        # PeerJ + MDPI + OJS
        peerj_html = '<html><body><div id="supp-materials"><a href="https://dfzljdn9uc3pi.cloudfront.net/2024/17000/1/Supp_Table_S1.xlsx">Table S1</a></div></body></html>'
        peerj_diag = sf.find_all_candidates(peerj_html, "https://peerj.com/articles/17000/")
        self.assertEqual([c.url for c in peerj_diag.candidates], ["https://dfzljdn9uc3pi.cloudfront.net/2024/17000/1/Supp_Table_S1.xlsx"])

        mdpi_html = '<html><body><div id="supplementary-material-sec"><a class="UD_Article_Supplementary" href="/2075-163X/14/2/123/data/suppl/minerals-14-00123-s001.zip">Supplementary Data</a></div></body></html>'
        mdpi_diag = sf.find_all_candidates(mdpi_html, "https://www.mdpi.com/2075-163X/14/2/123")
        self.assertEqual([c.url for c in mdpi_diag.candidates], ["https://www.mdpi.com/2075-163X/14/2/123/data/suppl/minerals-14-00123-s001.zip"])

        ojs_html = '<html><body><ul class="supplementary_files"><li><a class="obj_galley_link_supplementary" href="https://journal.example.org/article/download/42/88">Appendix S1</a></li></ul></body></html>'
        ojs_diag = sf.find_all_candidates(ojs_html, "https://journal.example.org/article/view/42")
        self.assertEqual([c.url for c in ojs_diag.candidates], ["https://journal.example.org/article/download/42/88"])

        # Cell Press /cms/attachment/
        cell_html = '<html><body><div class="article-sec-supplements"><a href="/cms/attachment/abcd-1234/mmc1.xlsx">Table S1</a></div></body></html>'
        cell_diag = sf.find_all_candidates(cell_html, "https://www.cell.com/matter/fulltext/S2590-2385(24)00001-2")
        self.assertEqual([c.url for c in cell_diag.candidates], ["https://www.cell.com/cms/attachment/abcd-1234/mmc1.xlsx"])

        # Nature Extended Data keeps Table S1 pdf/xlsx, skips Extended Data Fig. 1 png
        nat_html = """
        <html><body>
          <section id="ExtendedData" data-title="Extended Data">
            <a data-test="extended-data-link" href="/articles/s41586-024-0001-1_MOESM1_ESM.xlsx">Extended Data Table 1</a>
            <a data-test="extended-data-link" href="https://media.springernature.com/full/springer-static/image/art%3A10.1038/MediaObjects/fig1.png">Extended Data Fig. 1</a>
          </section>
        </body></html>
        """
        nat_diag = sf.find_all_candidates(nat_html, "https://www.nature.com/articles/s41586-024-0001-1")
        self.assertEqual([c.url for c in nat_diag.candidates], ["https://www.nature.com/articles/s41586-024-0001-1_MOESM1_ESM.xlsx"])

        # Cross-path BMC/Springer #MOESM7 anchor link must be filtered as article_section_fragment_url
        bmc_html = """
        <html><body>
          <a href="/article/10.1186/s12864-024-10100-z#MOESM7">Table 1</a>
          <a data-test="supp-info-link" href="https://static-content.springer.com/esm/art%3A10.1186%2Fs12864-024-10100-z/MediaObjects/12864_2024_10100_MOESM7_ESM.xlsx">Additional file 7</a>
        </body></html>
        """
        bmc_diag = sf.find_all_candidates(bmc_html, "https://bmcgenomics.biomedcentral.com/articles/10.1186/s12864-024-10100-z")
        self.assertEqual(
            [c.url for c in bmc_diag.candidates],
            ["https://static-content.springer.com/esm/art%3A10.1186%2Fs12864-024-10100-z/MediaObjects/12864_2024_10100_MOESM7_ESM.xlsx"],
        )

        # Chinese Magtech downloadArticleFile.do
        cn_html = """
        <html><body>
          <a href="/CN/article/downloadArticleFile.do?attachType=PDF&amp;id=1001">Full PDF</a>
          <a href="/CN/article/downloadArticleFile.do?attachType=SUPPLEMENT&amp;id=2002">附表1 地球化学数据</a>
        </body></html>
        """
        cn_diag = sf.find_all_candidates(cn_html, "https://www.ysxb.ac.cn/article/doi/10.18654/1000-0569/2024.01.01")
        self.assertEqual([c.url for c in cn_diag.candidates], ["https://www.ysxb.ac.cn/CN/article/downloadArticleFile.do?attachType=SUPPLEMENT&id=2002"])


if __name__ == "__main__":
    unittest.main()
