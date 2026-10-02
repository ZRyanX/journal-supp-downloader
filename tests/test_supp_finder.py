"""
Unit and Regression Tests for Supplementary Material & Table Candidate Finder
=============================================================================
Tests all core link identification functions, HTML fixtures, edge cases,
and file validation rules:
  1. DOI redirection & relative URL resolution against effective base
  2. Hyphenated filenames (table-s1.pdf, supp-table-1.xlsx, etc.)
  3. Broad table naming rules (Supplementary Table S1, 补充表 S1, 附表 1, etc.)
  4. Attribute scanning (aria-label, title, download, data-url, onclick)
  5. PDF & HTML table attachments vs webpage exclusion
  6. Query parameters in download URLs
  7. File validation (< 1 KB valid CSV/table vs HTML error pages)
  8. Declared count & completeness detection
  9. False positive exclusion (figures, references, navigation)
"""

import os
import sys
import tempfile
import unittest

# Ensure scripts directory is on sys.path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scripts_path = os.path.join(repo_root, "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

from supp_finder import (
    find_all_candidates,
    validate_downloaded_file,
    extract_effective_base_url,
    extract_filename_from_url,
    extract_filename_from_content_disposition,
    infer_file_extension,
    cookies_to_dict,
    detect_declared_supplement_count,
    is_result_obviously_incomplete,
    is_excluded_url,
    is_article_figure_url,
    TABLE_LABEL_PATTERN,
    TABLE_FILENAME_PATTERN,
)


class TestSuppFinder(unittest.TestCase):

    # ── 1. DOI Redirection & Effective Base URL Resolution ───────────────────

    def test_doi_redirection_relative_url_resolution(self):
        """Relative URLs must resolve against final page URL or <base>, NOT doi.org."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Test Article</title></head>
        <body>
            <div id="supplementary-material">
                <a href="/science/article/pii/S0169136822002578/mmc1.xlsx">Supplementary Data 1</a>
                <a href="supplements/table-s1.pdf">Table S1 PDF</a>
            </div>
        </body>
        </html>
        """
        original_doi = "https://doi.org/10.1016/j.oregeorev.2022.104949"
        final_landing_url = "https://www.sciencedirect.com/science/article/pii/S0169136822002578"

        diag = find_all_candidates(html, base_url=final_landing_url, original_url=original_doi)
        urls = [c.url for c in diag.candidates]

        self.assertEqual(len(urls), 2)
        # Verify resolution to sciencedirect.com, NOT doi.org
        self.assertIn("https://www.sciencedirect.com/science/article/pii/S0169136822002578/mmc1.xlsx", urls)
        self.assertIn("https://www.sciencedirect.com/science/article/pii/supplements/table-s1.pdf", urls)
        for u in urls:
            self.assertFalse(u.startswith("https://doi.org/"))

    def test_base_tag_overrides_response_url(self):
        """<base href="..."> in HTML overrides default response URL."""
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <base href="https://assets.publisher.org/files/">
        </head>
        <body>
            <a href="table_s1.xlsx">Supplementary Table 1</a>
        </body>
        </html>
        """
        response_url = "https://www.publisher.org/articles/12345"
        diag = find_all_candidates(html, base_url=response_url)
        self.assertEqual(len(diag.candidates), 1)
        self.assertEqual(diag.candidates[0].url, "https://assets.publisher.org/files/table_s1.xlsx")

    # ── 2. Broadened Table Name & Hyphenated Filename Rules ──────────────────

    def test_hyphenated_filenames(self):
        """Filenames with hyphens like table-s1.pdf and supp-table-1.xlsx must be recognized."""
        html = """
        <div>
            <a href="https://example.com/downloads/table-s1.pdf">Download</a>
            <a href="https://example.com/downloads/supp-table-1.xlsx">Data</a>
            <a href="https://example.com/downloads/extended-data-table-2.pdf">Ext Table 2</a>
            <a href="https://example.com/downloads/tbl-s3.pdf">Tbl S3</a>
            <a href="https://example.com/downloads/附表1.pdf">附表 1</a>
            <a href="https://example.com/downloads/补充表_S1.xlsx">补充表 S1</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/article")
        fnames = [c.filename for c in diag.candidates]

        self.assertIn("table-s1.pdf", fnames)
        self.assertIn("supp-table-1.xlsx", fnames)
        self.assertIn("extended-data-table-2.pdf", fnames)
        self.assertIn("tbl-s3.pdf", fnames)
        self.assertIn("附表1.pdf", fnames)
        self.assertIn("补充表_S1.xlsx", fnames)

    def test_diverse_table_labels_and_languages(self):
        """Check recognition of various English and Chinese table prefixes and formats."""
        labels = [
            "Supplementary Table S1",
            "Extended Data Table 1",
            "Table S1",
            "Tab. S1",
            "Tab 1",
            "Supporting Table S2",
            "Appendix Table A1",
            "Additional Table 3",
            "Online Table S1",
            "补充表 S1",
            "补充表 1",
            "附表 1",
            "附表 S1",
            "附录表 1",
            "附加表 2",
        ]
        for label in labels:
            m = TABLE_LABEL_PATTERN.search(label)
            self.assertIsNotNone(m, f"Label failed to match: '{label}'")

    # ── 3. Attribute Scanning (aria-label, title, download, data-*) ──────────

    def test_controls_and_attributes_scanning(self):
        """Inspect aria-label, title, download, data-* attributes, and buttons."""
        html = """
        <div>
            <!-- aria-label -->
            <a href="/data/dataset_01.pdf" aria-label="Supplementary Table S1: Summary">Download</a>

            <!-- title -->
            <a href="/data/dataset_02.pdf" title="Extended Data Table 1">Click here</a>

            <!-- download attribute with table filename -->
            <a href="/download_action?id=500" download="table-s1.pdf">File</a>

            <!-- button with data-url -->
            <button data-url="/downloads/suppl_data.xlsx" class="btn">Download XLSX</button>

            <!-- button with data-download-url -->
            <button data-download-url="/downloads/table_2.csv" aria-label="Table 2">Table 2</button>

            <!-- onclick handler -->
            <button onclick="window.open('/files/table_s3.xlsx')">View Table</button>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]

        self.assertIn("https://example.com/data/dataset_01.pdf", urls)
        self.assertIn("https://example.com/data/dataset_02.pdf", urls)
        self.assertIn("https://example.com/download_action?id=500", urls)
        self.assertIn("https://example.com/downloads/suppl_data.xlsx", urls)
        self.assertIn("https://example.com/downloads/table_2.csv", urls)
        self.assertIn("https://example.com/files/table_s3.xlsx", urls)

    # ── 4. PDF & HTML Table Attachments vs Webpage Exclusions ─────────────────

    def test_pdf_in_supplementary_section_and_html_table(self):
        """PDF named S1.pdf in supplementary section and standalone HTML tables are recognized."""
        html = """
        <div id="supplementary-material">
            <h3>Supplementary Information</h3>
            <p>Additional files for this paper:</p>
            <!-- Button with 'Download PDF' and S1.pdf in supplementary section -->
            <a href="/media/S1.pdf">Download PDF</a>
            <!-- Standalone HTML supplementary table -->
            <a href="/supplements/Table_S1.html">Supplementary Table S1</a>
            <!-- Regular webpage link (should NOT be included as supplementary) -->
            <a href="/about/terms.html">Terms and Conditions</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]

        self.assertIn("https://example.com/media/S1.pdf", urls)
        self.assertIn("https://example.com/supplements/Table_S1.html", urls)
        self.assertNotIn("https://example.com/about/terms.html", urls)

    def test_meta_tag_and_embedded_json(self):
        """Extract candidates from Highwire citation meta tags and embedded JSON."""
        html = """
        <head>
            <meta name="citation_supplementary_material" content="https://example.com/supp/suppl_data.zip">
            <script type="application/json">
                {"attachments": ["https://example.com/api/files/table_s1.xlsx"]}
            </script>
        </head>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]

        self.assertIn("https://example.com/supp/suppl_data.zip", urls)
        self.assertIn("https://example.com/api/files/table_s1.xlsx", urls)

    # ── 5. Query Parameter Download Links ────────────────────────────────────

    def test_query_parameter_download_urls(self):
        """URLs with file parameter like /download?file=table1.xlsx should be recognized."""
        html = """
        <a href="/download.action?doi=10.1002/anie.202100000&fileName=mmc1.xlsx">Download File</a>
        <a href="/serve?attachment=Supplementary_Table_S1.csv&token=xyz">Table S1</a>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        self.assertEqual(len(diag.candidates), 2)

        cand_map = {c.url: c.filename for c in diag.candidates}
        self.assertIn("mmc1.xlsx", cand_map["https://example.com/download.action?doi=10.1002/anie.202100000&fileName=mmc1.xlsx"])
        self.assertIn("Supplementary_Table_S1.csv", cand_map["https://example.com/serve?attachment=Supplementary_Table_S1.csv&token=xyz"])

    # ── 6. False Positive Filtering (Figures, References, Navigation) ────────

    def test_exclusion_filters(self):
        """Article figures, Google Scholar, and reference DOIs must be excluded."""
        html = """
        <div>
            <!-- Article figures -->
            <a href="https://example.com/article-gr1_lrg.jpg">Figure 1</a>
            <a href="https://example.com/article-ga1.jpg">Graphical Abstract</a>
            <a href="https://example.com/article-fx1.jpg">Figure FX1</a>
            <!-- Reference links -->
            <a href="https://scholar.google.com/scholar_lookup?title=Foo">Google Scholar</a>
            <a href="https://doi.org/10.1016/j.ref.2020.01">Reference 1</a>
            <a href="#m0001">Go to section</a>
            <!-- Valid supplement -->
            <a href="https://example.com/mmc1.xlsx">Table S1</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]

        self.assertEqual(len(urls), 1)
        self.assertEqual(urls[0], "https://example.com/mmc1.xlsx")

    # ── 7. File Validation (< 1 KB valid CSV/tables vs HTML error pages) ──────

    def test_validate_small_csv_file(self):
        """Valid small CSV files (< 1 KB) must NOT be deleted or reported as failure."""
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as f:
            csv_content = b"Gene,FoldChange,PValue\nTP53,2.4,0.001\nBRCA1,-1.8,0.02\n"
            f.write(csv_content)
            temp_path = f.name

        try:
            self.assertLess(os.path.getsize(temp_path), 1024)
            is_valid, reason = validate_downloaded_file(temp_path, content_bytes=csv_content)
            self.assertTrue(is_valid, f"Small CSV was rejected: {reason}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_validate_binary_office_signature(self):
        """Files with PK zip header (XLSX, DOCX) should be accepted even if small."""
        with tempfile.NamedTemporaryFile("wb", suffix=".xlsx", delete=False) as f:
            # Fake mini PK header
            xlsx_content = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"A" * 200
            f.write(xlsx_content)
            temp_path = f.name

        try:
            self.assertLess(os.path.getsize(temp_path), 1024)
            is_valid, reason = validate_downloaded_file(temp_path, content_bytes=xlsx_content)
            self.assertTrue(is_valid, f"Mini XLSX was rejected: {reason}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_validate_html_error_page_rejected(self):
        """HTML error pages disguised as data downloads must be rejected."""
        with tempfile.NamedTemporaryFile("wb", suffix=".xlsx", delete=False) as f:
            error_html = b"<!DOCTYPE html><html><body><h1>403 Forbidden - Access Denied</h1></body></html>"
            f.write(error_html)
            temp_path = f.name

        try:
            is_valid, reason = validate_downloaded_file(temp_path, content_bytes=error_html)
            self.assertFalse(is_valid, "HTML error page was mistakenly accepted as valid file!")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_validate_standalone_html_table_accepted(self):
        """Legitimate standalone HTML table attachments should be accepted."""
        with tempfile.NamedTemporaryFile("wb", suffix=".html", delete=False) as f:
            table_html = b"<!DOCTYPE html><html><body><table><tr><th>Sample</th><th>Value</th></tr><tr><td>A</td><td>1.2</td></tr></table></body></html>"
            f.write(table_html)
            temp_path = f.name

        try:
            is_valid, reason = validate_downloaded_file(temp_path, content_bytes=table_html)
            self.assertTrue(is_valid, f"Valid HTML table was rejected: {reason}")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    # ── 8. Declared Count & Completeness Detection ───────────────────────────

    def test_declared_count_and_completeness_check(self):
        """Detect declared counts and flag obviously incomplete extractions."""
        html_with_count = """
        <html>
        <body>
            <h2>Supplementary Material (4 files)</h2>
            <div id="supplementary-material">
                <a href="mmc1.xlsx">Supplementary Table 1</a>
            </div>
        </body>
        </html>
        """
        count = detect_declared_supplement_count(html_with_count)
        self.assertEqual(count, 4)

        diag = find_all_candidates(html_with_count, base_url="https://example.com/paper")
        self.assertEqual(len(diag.candidates), 1)
        self.assertTrue(diag.is_incomplete)
        self.assertIn("less than declared count", diag.incomplete_reason)

    def test_empty_and_malformed_inputs(self):
        """Finder should handle empty string, None, or broken HTML safely."""
        diag_empty = find_all_candidates("", base_url="https://example.com/test")
        self.assertEqual(len(diag_empty.candidates), 0)

        diag_none = find_all_candidates(None, base_url="https://example.com/test")
        self.assertEqual(len(diag_none.candidates), 0)

        diag_broken = find_all_candidates("<div><a href='test.xlsx'>Link</div>", base_url="https://example.com/test")
        self.assertEqual(len(diag_broken.candidates), 1)

    def test_container_without_candidates_triggers_incomplete(self):
        """If a supplementary container exists but yields 0 candidates, mark incomplete."""
        html = """
        <html>
        <body>
            <div id="supplementary-material">
                <p>Loading supplementary information via client-side javascript...</p>
            </div>
            <div>
                <a href="/unrelated/terms.pdf">Terms PDF</a>
            </div>
        </body>
        </html>
        """
        diag = find_all_candidates(html, base_url="https://example.com/article")
        self.assertTrue(diag.is_incomplete)
        self.assertIn("0 candidates extracted from it", diag.incomplete_reason)

    def test_zero_byte_file_and_http_errors(self):
        """Zero byte files and HTTP status >= 400 must be marked invalid."""
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            temp_path = f.name

        try:
            # 0 bytes
            is_val, reason = validate_downloaded_file(temp_path)
            self.assertFalse(is_val)
            self.assertIn("empty", reason)

            # HTTP 403 / 404
            with open(temp_path, "wb") as f:
                f.write(b"data")
            is_val, reason = validate_downloaded_file(temp_path, response_status=403)
            self.assertFalse(is_val)
            self.assertIn("403", reason)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_extra_binary_signatures_and_small_text(self):
        """Validate GZIP, 7z, RAR, TSV, and JSON formats."""
        # GZIP signature
        with tempfile.NamedTemporaryFile("wb", suffix=".gz", delete=False) as f:
            f.write(b"\x1f\x8b\x08\x00" + b"\x00" * 50)
            p_gz = f.name
        # TSV 50 bytes
        with tempfile.NamedTemporaryFile("wb", suffix=".tsv", delete=False) as f:
            f.write(b"id\tval\n1\tA\n2\tB\n")
            p_tsv = f.name
        # JSON 40 bytes
        with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as f:
            f.write(b'{"table": [1, 2, 3]}')
            p_json = f.name

        try:
            is_val, r = validate_downloaded_file(p_gz)
            self.assertTrue(is_val, r)
            is_val, r = validate_downloaded_file(p_tsv)
            self.assertTrue(is_val, r)
            is_val, r = validate_downloaded_file(p_json)
            self.assertTrue(is_val, r)
        finally:
            for p in [p_gz, p_tsv, p_json]:
                if os.path.exists(p):
                    os.remove(p)

    def test_backward_compatibility_wrappers(self):
        """Ensure both scripts provide functional backward-compatible wrappers."""
        import journal_downloader
        import scansci_supp_downloader

        sample_html = '<a href="mmc1.xlsx">MMC 1</a>'
        links1 = journal_downloader.find_supplementary_links(sample_html, "https://example.com/")
        links2 = scansci_supp_downloader.find_supplementary_links(sample_html, "https://example.com/")

        self.assertEqual(links1, ["https://example.com/mmc1.xlsx"])
        self.assertEqual(links2, ["https://example.com/mmc1.xlsx"])
        self.assertTrue(scansci_supp_downloader.is_data_url("https://example.com/table-s1.pdf"))

    # ── 10. Additional Robustness Regression Tests ────────────────────────────

    def test_validate_html_with_table_tags_rejected_if_data_extension(self):
        """Even if an HTML page contains <table>, it must be rejected if named .xlsx or .pdf."""
        with tempfile.NamedTemporaryFile("wb", suffix=".xlsx", delete=False) as f:
            f.write(b"<!DOCTYPE html><html><body><table><tr><td>Login to Institutional Account</td></tr></table></body></html>")
            p_xlsx = f.name
        with tempfile.NamedTemporaryFile("wb", suffix=".pdf", delete=False) as f:
            f.write(b"<!DOCTYPE html><html><body><table><tr><td>Paywall</td></tr></table></body></html>")
            p_pdf = f.name
        try:
            is_val1, msg1 = validate_downloaded_file(p_xlsx)
            self.assertFalse(is_val1)
            self.assertIn("Expected .xlsx data file but received HTML document", msg1)

            is_val2, msg2 = validate_downloaded_file(p_pdf)
            self.assertFalse(is_val2)
            self.assertIn("Expected .pdf data file but received HTML document", msg2)
        finally:
            if os.path.exists(p_xlsx):
                os.remove(p_xlsx)
            if os.path.exists(p_pdf):
                os.remove(p_pdf)

    def test_in_page_anchor_exclusion(self):
        """In-page anchors like #tbl1 or full URL with fragment on same page must be excluded."""
        html = """
        <html>
        <body>
            <a href="#tbl1">Table 1 (jump)</a>
            <a href="https://example.com/paper/123#sec2">Jump to Section 2</a>
            <a href="/paper/123#table-s1">Jump to Supp Table S1</a>
            <!-- Genuine downloadable attachment -->
            <a href="/downloads/table_s1.xlsx">Supplementary Table S1</a>
        </body>
        </html>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper/123")
        urls = [c.url for c in diag.candidates]
        self.assertEqual(len(urls), 1)
        self.assertEqual(urls[0], "https://example.com/downloads/table_s1.xlsx")

    def test_plural_table_labels_and_ranges(self):
        """Check plural 'Tables' and ranges like S1-S4, 1 to 5, 附表 1-5."""
        labels = [
            "Supplementary Tables S1-S4",
            "Supplementary Tables S1–S4",
            "Extended Data Tables 1-5",
            "Tables S1 to S5",
            "Tables 1-4",
            "Supplementary Tables",
            "附表 1-5",
            "附表 1 至 5",
            "补充表格 1-3",
        ]
        for l in labels:
            m = TABLE_LABEL_PATTERN.search(l)
            self.assertIsNotNone(m, f"Failed to match plural/range table label: '{l}'")

    def test_chinese_declared_counts(self):
        """Detect Chinese declared supplementary counts and ranges."""
        self.assertEqual(detect_declared_supplement_count("本文共 4 个补充文件。"), 4)
        self.assertEqual(detect_declared_supplement_count("包含 5 个附表及相关数据。"), 5)
        self.assertEqual(detect_declared_supplement_count("提供 3个附件。"), 3)
        self.assertEqual(detect_declared_supplement_count("详见附表 1-5。"), 5)
        self.assertEqual(detect_declared_supplement_count("见附表 1 至 6。"), 6)

    def test_nearby_heading_context_recognition(self):
        """Table labels in enclosing or preceding headings are attributed to download links."""
        html = """
        <div class="table-card">
            <h4>Supplementary Table S1. Primary kinetic parameters</h4>
            <div class="actions">
                <a href="/data/endpoint?id=101">Download</a>
            </div>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        self.assertEqual(len(diag.candidates), 1)
        cand = diag.candidates[0]
        self.assertIn("nearby_context", cand.match_rule)
        self.assertEqual(cand.filename, "Supplementary Table S1.bin")

    def test_content_disposition_rfc6266_and_infer_ext(self):
        """Parse RFC 6266 / 5987 headers and infer extensions from magic bytes."""
        # RFC 5987 UTF-8 encoding
        h1 = "attachment; filename*=UTF-8''Supplementary_Table_S1.xlsx"
        self.assertEqual(extract_filename_from_content_disposition(h1), "Supplementary_Table_S1.xlsx")

        # Standard double-quoted filename
        h2 = 'attachment; filename="data_table_1.csv"'
        self.assertEqual(extract_filename_from_content_disposition(h2), "data_table_1.csv")

        # Extension inference
        self.assertEqual(infer_file_extension(b"%PDF-1.4 ..."), ".pdf")
        self.assertEqual(infer_file_extension(b"PK\x03\x04...", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"), ".xlsx")
        self.assertEqual(infer_file_extension(b"id,name\n1,a\n", "text/csv"), ".csv")

    def test_endpoint_filename_disambiguation(self):
        """Wiley and PLOS generic endpoints should not collide into duplicate filenames."""
        wiley_url1 = "https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002%2Fanie.202300000&attachmentId=1001"
        wiley_url2 = "https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002%2Fanie.202300000&attachmentId=1002"
        f1 = extract_filename_from_url(wiley_url1)
        f2 = extract_filename_from_url(wiley_url2)
        self.assertNotEqual(f1, f2)
        self.assertIn("1001", f1)
        self.assertIn("1002", f2)

        plos_url1 = "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0298123.s001&type=supplementary"
        plos_url2 = "https://journals.plos.org/plosone/article/file?id=10.1371/journal.pone.0298123.s002&type=supplementary"
        p1 = extract_filename_from_url(plos_url1)
        p2 = extract_filename_from_url(plos_url2)
        self.assertNotEqual(p1, p2)
        self.assertIn("s001", p1)
        self.assertIn("s002", p2)

    def test_cookies_to_dict_conversion(self):
        """Convert list of browser cookies to dict, filtering by domain if specified."""
        raw_cookies = [
            {"name": "session_id", "value": "xyz123", "domain": ".sciencedirect.com", "path": "/"},
            {"name": "tracking", "value": "abc", "domain": ".google.com", "path": "/"},
        ]
        cdict = cookies_to_dict(raw_cookies, target_url="https://www.sciencedirect.com/science/article/pii/123")
        self.assertIn("session_id", cdict)
        self.assertEqual(cdict["session_id"], "xyz123")
        self.assertNotIn("tracking", cdict)

    def test_resolve_doi_url_handling(self):
        """resolve_doi_url returns direct non-doi URLs without network calls."""
        from scansci_supp_downloader import resolve_doi_url
        direct = "https://www.sciencedirect.com/science/article/pii/S12345"
        self.assertEqual(resolve_doi_url(direct), direct)

    # ── 11. Regression Tests for Boost Gaps (Table Endpoints, Cookies, CDN) ─

    def test_table_label_with_generic_endpoint(self):
        """Extended Data Table 1 pointing to /assets/fetch?id=7 must be discovered as a candidate."""
        html = """
        <div>
            <a href="/assets/fetch?id=7">Extended Data Table 1</a>
            <a href="/api/v1/content?item_id=99" aria-label="Supplementary Table S2">Download Dataset</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper/12345")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/assets/fetch?id=7", urls)
        self.assertIn("https://example.com/api/v1/content?item_id=99", urls)

        cand_map = {c.url: c for c in diag.candidates}
        self.assertEqual(cand_map["https://example.com/assets/fetch?id=7"].filename, "Extended Data Table 1.bin")
        self.assertEqual(cand_map["https://example.com/api/v1/content?item_id=99"].filename, "Supplementary Table S2.bin")

    def test_extended_data_containers_and_headings(self):
        """Elements within extended data sections and headings must be marked as supplementary_section."""
        html = """
        <section id="extended-data">
            <h2>Extended Data</h2>
            <div class="table-entry">
                <a href="/files/stream?id=42">Extended Data Table 1: Kinetic Measurements</a>
            </div>
        </section>
        <div class="c-article-section" data-section="supp-table">
            <button data-url="/export/table_3">附表 3</button>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        self.assertEqual(len(diag.candidates), 2)
        for c in diag.candidates:
            self.assertEqual(c.section, "supplementary_section")

    def test_skill_markdown_cookie_description_accurate(self):
        """SKILL.md must accurately describe reading saved cookies and not overclaim live Chrome/Edge cloning."""
        skill_path = os.path.join(repo_root, "SKILL.md")
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("自动克隆本地 Chrome/Edge 的登录态", content)
        self.assertIn("已保存", content)

    def test_elsevier_cdn_brute_force_discontinuous_and_known_mmcs(self):
        """Elsevier CDN probe handles discontinuous numbers (gap of 3) and known MMCs beyond default limit."""
        from scansci_supp_downloader import try_elsevier_cdn_brute_force
        from unittest.mock import MagicMock, patch

        with tempfile.TemporaryDirectory() as tmpdir:
            # Case 1: Discontinuous numbering (mmc1 exists, mmc2..4 miss, mmc5 exists)
            # Default max_misses=4 must NOT abort before mmc5!
            def fake_head(url, timeout=10, allow_redirects=True):
                resp = MagicMock()
                if "mmc1.xlsx" in url or "mmc5.xlsx" in url:
                    resp.status_code = 200
                    resp.headers = {"content-length": "2048"}
                else:
                    resp.status_code = 404
                    resp.headers = {}
                return resp

            def fake_get(url, timeout=30, stream=True):
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {"content-disposition": ""}
                resp.iter_content = MagicMock(return_value=[b"PK\x03\x04" + b"x" * 200])
                return resp

            with patch("requests.Session.head", side_effect=fake_head), \
                 patch("requests.Session.get", side_effect=fake_get), \
                 patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")):
                files = try_elsevier_cdn_brute_force("S12345", tmpdir, max_mmc=10, max_consecutive_misses=4)
                basenames = [os.path.basename(f) for f in files]
                self.assertIn("mmc1.xlsx", basenames)
                self.assertIn("mmc5.xlsx", basenames)
                self.assertEqual(len(files), 2)

            # Case 2: Known MMC beyond max_mmc limit (e.g. known_mmcs={30}, max_mmc=15)
            def fake_head_known(url, timeout=10, allow_redirects=True):
                resp = MagicMock()
                if "mmc30.xlsx" in url:
                    resp.status_code = 200
                    resp.headers = {"content-length": "4096"}
                else:
                    resp.status_code = 404
                    resp.headers = {}
                return resp

            with patch("requests.Session.head", side_effect=fake_head_known), \
                 patch("requests.Session.get", side_effect=fake_get), \
                 patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")):
                files = try_elsevier_cdn_brute_force("S12345", tmpdir, max_mmc=15, max_consecutive_misses=4, known_mmcs={30})
                basenames = [os.path.basename(f) for f in files]
                self.assertIn("mmc30.xlsx", basenames)

    def test_table_endpoint_edge_cases(self):
        """Button controls, onclick, fragments, and Chinese table terms with endpoints."""
        html = """
        <div>
            <!-- button with data-url -->
            <button data-url="/assets/fetch?id=7" aria-label="Extended Data Table 1">Download</button>
            <!-- link with hash fragment on endpoint -->
            <a href="/assets/fetch?id=8#section-data">Extended Data Table 2</a>
            <!-- button with onclick -->
            <button onclick="window.open('/assets/fetch?id=9')">Supplementary Table S3</button>
            <!-- Chinese label with generic API endpoint -->
            <a href="/data/export?item=1">附表 1</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper/test")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/assets/fetch?id=7", urls)
        self.assertIn("https://example.com/assets/fetch?id=8#section-data", urls)
        self.assertIn("https://example.com/assets/fetch?id=9", urls)
        self.assertIn("https://example.com/data/export?item=1", urls)
        self.assertEqual(len(diag.candidates), 4)

    def test_page_scrape_supplements_recovers_missing_mmcs(self):
        """_try_page_scrape_supplements probes CDN for unlinked MMC mentions on the page."""
        from scansci_supp_downloader import _try_page_scrape_supplements
        from unittest.mock import MagicMock, patch

        html_with_unlinked_mmc = """
        <html>
        <body>
            <p>Raw kinetic data are provided in mmc7 (see supplementary materials).</p>
        </body>
        </html>
        """
        args = MagicMock()
        args.no_cookies = True
        args.headful = False
        args.skip_cdn = False
        args.max_misses = 4

        with tempfile.TemporaryDirectory() as tmpdir:
            existing_files = [os.path.join(tmpdir, "mmc1.xlsx")]
            with open(existing_files[0], "wb") as f:
                f.write(b"data")

            def fake_fetch_page(url, session, cookies, headful, force_browser=False):
                return html_with_unlinked_mmc, 200, url

            with patch("scansci_supp_downloader.fetch_page_html", side_effect=fake_fetch_page), \
                 patch("scansci_supp_downloader.try_elsevier_cdn_brute_force") as mock_cdn:
                mock_cdn.return_value = [os.path.join(tmpdir, "mmc7.xlsx")]
                _try_page_scrape_supplements(
                    "https://www.sciencedirect.com/science/article/pii/S12345",
                    tmpdir,
                    {},
                    args,
                    existing_files,
                )
                self.assertTrue(mock_cdn.called)
                call_args = mock_cdn.call_args
                self.assertEqual(call_args[0][0], "S12345")
                self.assertIn(7, call_args[1]["known_mmcs"])

    def test_main_text_table_not_falsely_matched(self):
        """Primary article inline tables (e.g. Table 1, Table 2) must not be treated as supplementary downloads."""
        html = """
        <html>
        <body>
            <p>Demographic characteristics are summarized in <a href="/articles/s41586-021-03819-2/tables/1">Table 1</a>.</p>
            <p>Outcomes are reported in <a href="/content/100/2/tables/2">Table 2</a>.</p>
            <p>Supplementary kinetic data are listed in <a href="/assets/fetch?id=7">Extended Data Table 1</a>.</p>
        </body>
        </html>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/assets/fetch?id=7", urls)
        self.assertNotIn("https://example.com/articles/s41586-021-03819-2/tables/1", urls)
        self.assertNotIn("https://example.com/content/100/2/tables/2", urls)
        self.assertEqual(len(diag.candidates), 1)

    def test_bin_extension_inferred_during_download(self):
        """Files with .bin candidate filename must have their real extension inferred upon download."""
        from scansci_supp_downloader import download_file
        from supp_finder import Candidate
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmpdir:
            cand = Candidate(
                url="https://example.com/assets/fetch?id=7",
                filename="Extended Data Table 1.bin",
                link_text="Extended Data Table 1",
            )
            # Fake requests session returning an XLSX file
            fake_session = MagicMock()
            fake_resp = MagicMock()
            fake_resp.status_code = 200
            fake_resp.headers = {"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
            fake_resp.iter_content = MagicMock(return_value=[b"PK\x03\x04" + b"xl/worksheets" + b"0" * 200])
            fake_session.get.return_value = fake_resp

            res = download_file(cand, tmpdir, session=fake_session)
            self.assertIsNotNone(res)
            self.assertTrue(res.endswith("Extended Data Table 1.xlsx"))
            self.assertTrue(os.path.exists(res))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Extended Data Table 1.bin")))

    def test_page_scrape_spaced_and_hyphenated_mmcs(self):
        """Regex recovers 'MMC 2', 'MMC-3', and 'mmc_4' from article text."""
        from scansci_supp_downloader import _try_page_scrape_supplements
        from unittest.mock import MagicMock, patch

        html_content = "<p>Refer to MMC 2, MMC-3, and mmc_4 for details.</p>"
        args = MagicMock()
        args.no_cookies = True
        args.headful = False
        args.skip_cdn = False
        args.max_misses = 4
        args.max_mmc = 25

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("scansci_supp_downloader.fetch_page_html", return_value=(html_content, 200, "https://example.com/pii/S99999")), \
                 patch("scansci_supp_downloader.try_elsevier_cdn_brute_force") as mock_cdn:
                mock_cdn.return_value = []
                _try_page_scrape_supplements("https://example.com/pii/S99999", tmpdir, {}, args, [])
                self.assertTrue(mock_cdn.called)
                known = mock_cdn.call_args[1]["known_mmcs"]
                self.assertIn(2, known)
                self.assertIn(3, known)
                self.assertIn(4, known)

    def test_cdn_targeted_known_mmc_skips_intermediate_gap(self):
        """When known_mmcs has a high number (e.g. mmc30), intermediate numbers (mmc6..29) are not polled after consecutive misses."""
        from scansci_supp_downloader import try_elsevier_cdn_brute_force
        from unittest.mock import MagicMock, patch

        polled_urls = []

        def track_head(url, timeout=10, allow_redirects=True):
            polled_urls.append(url)
            resp = MagicMock()
            if "mmc1.xlsx" in url or "mmc30.xlsx" in url:
                resp.status_code = 200
                resp.headers = {"content-length": "2048"}
            else:
                resp.status_code = 404
                resp.headers = {}
            return resp

        def fake_get(url, timeout=30, stream=True):
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-disposition": ""}
            resp.iter_content = MagicMock(return_value=[b"PK\x03\x04" + b"x" * 200])
            return resp

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("requests.Session.head", side_effect=track_head), \
                 patch("requests.Session.get", side_effect=fake_get), \
                 patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")):
                files = try_elsevier_cdn_brute_force("S12345", tmpdir, max_mmc=10, max_consecutive_misses=4, known_mmcs={30})
                basenames = [os.path.basename(f) for f in files]
                self.assertIn("mmc1.xlsx", basenames)
                self.assertIn("mmc30.xlsx", basenames)
                
                # Verify that mmc10..mmc29 were NEVER polled!
                for unpolled_num in range(10, 30):
                    self.assertFalse(any(f"mmc{unpolled_num}." in u for u in polled_urls),
                                     f"mmc{unpolled_num} was unnecessarily polled!")


if __name__ == "__main__":
    unittest.main()
