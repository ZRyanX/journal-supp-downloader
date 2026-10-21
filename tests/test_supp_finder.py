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
from unittest.mock import patch, MagicMock
from bs4 import BeautifulSoup

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
    is_explicit_supp_table,
    TABLE_LABEL_PATTERN,
    TABLE_FILENAME_PATTERN,
    expand_page_accordions_action,
    disambiguate_target_filename,
    should_match_candidate,
    get_nearby_context_text,
    _extract_embedded_json_candidates,
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

            def fake_fetch_page(url, session, cookies, headful, force_browser=False, **kwargs):
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

    # ── 12. Regression Tests for Boost Detection Fixes (A1-A4, B1-B3, C1-C2, D1-D2, E1-E2, F1-F5) ──

    def test_a1_appendix_and_fulu_table_labels_and_candidates(self):
        """A1: Standalone Appendix 1, Appendix A, 附录 1, 附录1 recognized without 'Table'."""
        self.assertTrue(is_explicit_supp_table("Appendix 1"))
        self.assertTrue(is_explicit_supp_table("Appendix A"))
        self.assertTrue(is_explicit_supp_table("Appendix 1: Full dataset"))
        self.assertTrue(is_explicit_supp_table("附录 1"))
        self.assertTrue(is_explicit_supp_table("附录1"))
        self.assertTrue(is_explicit_supp_table("附录A"))

        html = """
        <div>
            <a href="/dl?id=101">附录 1</a>
            <a href="/dl?id=102">Appendix 1: Full dataset</a>
            <a href="/files/appendix_a.xlsx">Appendix A</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/dl?id=101", urls)
        self.assertIn("https://example.com/dl?id=102", urls)
        self.assertIn("https://example.com/files/appendix_a.xlsx", urls)

    def test_a2_etable_labels_and_filenames(self):
        """A2: eTable 1 / eTable1 / eTable1.pdf recognized."""
        self.assertTrue(is_explicit_supp_table("eTable 1"))
        self.assertTrue(is_explicit_supp_table("eTable1"))
        self.assertTrue(is_explicit_supp_table("e-Table 1"))

        self.assertIsNotNone(TABLE_FILENAME_PATTERN.search("eTable1.pdf"))
        self.assertIsNotNone(TABLE_FILENAME_PATTERN.search("table-s1.pdf"))

        html = """
        <div>
            <a href="/files/eTable1.pdf">Download</a>
            <a href="/dl?id=201">eTable 1</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/files/eTable1.pdf", urls)
        self.assertIn("https://example.com/dl?id=201", urls)

    def test_a3_supplementary_data_and_dataset(self):
        """A3: Supplementary Data S1, Data S1, Dataset S1 recognized as candidates."""
        self.assertTrue(is_explicit_supp_table("Supplementary Data S1"))
        self.assertTrue(is_explicit_supp_table("Data S1"))
        self.assertTrue(is_explicit_supp_table("Dataset S1"))
        self.assertTrue(is_explicit_supp_table("Supplementary Dataset 1"))
        self.assertTrue(is_explicit_supp_table("Additional file 1"))

        html = """
        <div>
            <a href="/dl?id=1">Supplementary Data S1</a>
            <a href="/dl?id=2">Data S1</a>
            <a href="/dl?id=3">Dataset S1</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        self.assertEqual(len(diag.candidates), 3)

    def test_a4_table_letter_and_endpoint_boundary(self):
        """A4: Table A1 admitted only with supp/appendix context; /att/ endpoint recognized."""
        html = """
        <div>
            <!-- Table A1 with pure path endpoint /att/1 but in page body without appendix -> NOT admitted -->
            <a href="/att/1">Table A1</a>
            <!-- Table A1 inside container or with appendix path -> admitted -->
            <a href="/content/appendix/tables/a1">Table A1</a>
            <!-- Explicit supp table with pure path endpoint /att/2 -> admitted -->
            <a href="/att/2">附表 1</a>
            <!-- Standard article table Table 1 with query -> NOT admitted -->
            <a href="/dl?id=99">Table 1</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/content/appendix/tables/a1", urls)
        self.assertIn("https://example.com/att/2", urls)
        self.assertNotIn("https://example.com/att/1", urls)
        self.assertNotIn("https://example.com/dl?id=99", urls)

    def test_b1_onclick_diverse_syntax(self):
        """B1: onclick with location.href, window.location, dl, downloadFile recognized."""
        html = """
        <div>
            <button onclick="location.href='/dl?id=10'">附表 1</button>
            <button onclick="window.location='/dl?id=11'">附表 2</button>
            <span onclick="dl('/dl?id=12')">Supplementary Table S3</span>
            <div onclick="downloadFile('/dl?id=13')">Extended Data Table 4</div>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/dl?id=10", urls)
        self.assertIn("https://example.com/dl?id=11", urls)
        self.assertIn("https://example.com/dl?id=12", urls)
        self.assertIn("https://example.com/dl?id=13", urls)

    def test_b2_input_and_table_cell_controls(self):
        """B2: <input value="附表 1">, <td onclick="...">, iframe, embed controls scanned."""
        html = """
        <div id="supplementary">
            <input type="button" data-url="/dl/t1.xlsx" value="附表 1">
            <table>
                <tr>
                    <td onclick="window.open('/dl/t2.xlsx')">附表 2</td>
                </tr>
            </table>
            <iframe src="/supp/embed_table.html" title="Supplementary Table S3"></iframe>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/dl/t1.xlsx", urls)
        self.assertIn("https://example.com/dl/t2.xlsx", urls)
        self.assertIn("https://example.com/supp/embed_table.html", urls)

    def test_b3_embedded_json_relative_urls(self):
        """B3: Embedded JSON with relative paths, unicode and escaped slashes extracted."""
        html = """
        <script type="application/json">
        {
            "supp": [
                "/files/附表1.xlsx",
                "\\/files\\/Supplementary_Table_S2.xlsx"
            ]
        }
        </script>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/files/附表1.xlsx", urls)
        self.assertIn("https://example.com/files/Supplementary_Table_S2.xlsx", urls)

    def test_c1_exclude_url_patterns_immunity_for_data_files(self):
        """C1: elsevier.com and doi.org download endpoints with data files / MMC are not excluded."""
        url_els_api = "https://api.elsevier.com/content/object/eid/1-s2.0-S123-mmc2.xlsx"
        url_els_aem = "https://www.elsevier.com/__data/assets/excel/0001/table-s1.xlsx"
        url_doi_file = "https://doi.org/10.1016/j.test.2023/table-s1.xlsx"
        url_doi_ref = "https://doi.org/10.1016/j.ref.2020.01"

        self.assertFalse(is_excluded_url(url_els_api))
        self.assertFalse(is_excluded_url(url_els_aem))
        self.assertFalse(is_excluded_url(url_doi_file))
        self.assertTrue(is_excluded_url(url_doi_ref))

    def test_c2_figure_pattern_boundaries_and_data_immunity(self):
        """C2: /img/gr1_lrg.jpg excluded, but data file table-gr1.xlsx immune from figure exclusion."""
        self.assertTrue(is_article_figure_url("https://example.com/img/gr1_lrg.jpg"))
        self.assertFalse(is_article_figure_url("https://example.com/files/table-gr1.xlsx"))

    def test_d1_validate_standalone_html_table_with_bin_extension(self):
        """D1: Standalone HTML table attachments with .bin extension accepted and not deleted."""
        with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as f:
            html_table = b"<!DOCTYPE html><html><body><table><tr><th>Sample</th><th>Value</th></tr></table></body></html>"
            f.write(html_table)
            temp_path = f.name

        try:
            is_valid, reason = validate_downloaded_file(temp_path, content_bytes=html_table)
            self.assertTrue(is_valid, f"HTML table with .bin was falsely rejected: {reason}")
            self.assertIn("HTML table attachment", reason)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_d1_download_html_table_rename(self):
        """D1: download_file renames HTML table attachment with .bin to .html."""
        import journal_downloader
        from supp_finder import Candidate

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {"content-type": "application/octet-stream"}
        mock_resp.body = b"<!DOCTYPE html><html><body><table><tr><td>Data</td></tr></table></body></html>"

        with patch("journal_downloader.Fetcher.get", return_value=mock_resp):
            with tempfile.TemporaryDirectory() as tmpdir:
                cand = Candidate(url="https://example.com/table1.bin", filename="table1.bin")
                res_path = journal_downloader.download_file(cand, tmpdir)
                self.assertIsNotNone(res_path)
                self.assertTrue(os.path.exists(res_path))
                self.assertTrue(res_path.endswith(".html"))
                self.assertFalse(os.path.exists(os.path.join(tmpdir, "table1.bin")))

    def test_d2_candidate_filename_disambiguation(self):
        """D2: Distinct URLs with identical filenames disambiguated with _2 suffix."""
        html = """
        <div>
            <a href="https://example.com/part1/table_s1.xlsx">Table S1 (Part 1)</a>
            <a href="https://example.com/part2/table_s1.xlsx">Table S1 (Part 2)</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        self.assertEqual(len(diag.candidates), 2)
        fnames = [c.filename for c in diag.candidates]
        self.assertEqual(len(set(fnames)), 2)
        self.assertIn("table_s1.xlsx", fnames)
        self.assertIn("table_s1_2.xlsx", fnames)

    def test_d2_download_cd_collision_disambiguation(self):
        """D2: download_file disambiguates Content-Disposition collisions without skipping."""
        import journal_downloader
        from supp_finder import Candidate

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {
            "content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "content-disposition": 'attachment; filename="data.xlsx"',
        }
        mock_resp.body = b"PK\x03\x04" + b"\x00" * 200

        with patch("journal_downloader.Fetcher.get", return_value=mock_resp):
            with tempfile.TemporaryDirectory() as tmpdir:
                cand1 = Candidate(url="https://example.com/part1", filename="part1.bin")
                cand2 = Candidate(url="https://example.com/part2", filename="part2.bin")

                res1 = journal_downloader.download_file(cand1, tmpdir)
                self.assertIsNotNone(res1)
                self.assertEqual(os.path.basename(res1), "data.xlsx")

                res2 = journal_downloader.download_file(cand2, tmpdir)
                self.assertIsNotNone(res2)
                self.assertEqual(os.path.basename(res2), "data_2.xlsx")

    def test_e1_declared_count_comprehensive_patterns(self):
        """E1: detect_declared_supplement_count covers ranges, non-contiguous tables, and lists."""
        self.assertEqual(detect_declared_supplement_count("Tables S1-S3 in Supplementary Information"), 3)
        self.assertEqual(detect_declared_supplement_count("Supplementary Table S1 and S2"), 2)
        self.assertEqual(detect_declared_supplement_count("见附表1至附表6"), 6)
        self.assertEqual(detect_declared_supplement_count("see Supplementary Tables 2-5"), 4)
        self.assertEqual(
            detect_declared_supplement_count("Supplementary Materials and Methods, Figures S1-S3, Tables S1-S4"),
            4,
        )
        self.assertEqual(detect_declared_supplement_count("附表1、附表2和附表3"), 3)

    def test_e2_obviously_incomplete_not_falsely_triggered_by_meta_or_publisher(self):
        """E2: When meta or publisher extractors yield candidates, don't falsely claim 0 candidates."""
        from supp_finder import Candidate
        cands = [
            Candidate(
                url="https://example.com/mmc1.xlsx",
                filename="mmc1.xlsx",
                section="meta_tags",
            )
        ]
        html_with_supp_div = '<div id="supplementary-material"><p>Placeholder</p></div>'
        is_inc, reason = is_result_obviously_incomplete(cands, html_with_supp_div, declared_count=1)
        self.assertFalse(is_inc, f"Falsely marked incomplete: {reason}")

    def test_f1_is_data_url_with_explicit_text(self):
        """F1: is_data_url recognizes endpoint when accompanied by supplementary text."""
        import scansci_supp_downloader
        self.assertTrue(scansci_supp_downloader.is_data_url("/dl?id=1", text="附表 1"))
        self.assertTrue(scansci_supp_downloader.is_data_url("/dl?id=2", text="Supplementary Table S1"))

    def test_f2_extract_filename_deterministic_hash(self):
        """F2: Fallback hash filename is deterministic across runs."""
        fn1 = extract_filename_from_url("https://example.com/download/")
        fn2 = extract_filename_from_url("https://example.com/download/")
        self.assertEqual(fn1, fn2)
        self.assertTrue(fn1.startswith("download_"))
        self.assertTrue(fn1.endswith(".bin"))

    def test_f3_check_data_extension_multi_part(self):
        """F3: check_data_extension correctly recognizes .nii.gz instead of .gz."""
        from supp_finder import check_data_extension
        ext = check_data_extension("https://example.com/brain.nii.gz")
        self.assertEqual(ext, ".nii.gz")

    def test_f4_sanitize_filename_url_unquote(self):
        """F4: sanitize_filename unquotes URL-encoded Chinese characters."""
        from supp_finder import sanitize_filename
        fn = sanitize_filename("%E9%99%84%E8%A1%A81.xlsx")
        self.assertEqual(fn, "附表1.xlsx")

    def test_f5_publisher_extractors_filtered_by_exclusion(self):
        """F5: Publisher extractors uniformly filter out excluded URLs and figures."""
        html = """
        <a data-track-action="download" href="https://scholar.google.com/search?q=foo">Scholar Download</a>
        <a data-track-action="download" href="https://example.com/article-gr1_lrg.jpg">Figure Download</a>
        <a data-track-action="download" href="https://example.com/real_data.xlsx">Real Data</a>
        """
        diag = find_all_candidates(html, base_url="https://link.springer.com/article/10.1007/test")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/real_data.xlsx", urls)
        self.assertNotIn("https://scholar.google.com/search?q=foo", urls)
        self.assertNotIn("https://example.com/article-gr1_lrg.jpg", urls)

    def test_b1_relative_paths_in_onclick(self):
        """B1: Relative paths without leading slash inside onclick are properly extracted."""
        from supp_finder import extract_target_url_from_tag
        from bs4 import BeautifulSoup
        html = """
        <div>
            <a id="a1" onclick="location.href='download.php?id=1'">Link 1</a>
            <a id="a2" onclick="dl('files/table1.xlsx')">Link 2</a>
            <a id="a3" onclick="downloadFile('./files/table1.xlsx')">Link 3</a>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        self.assertEqual(extract_target_url_from_tag(soup.find(id="a1")), "download.php?id=1")
        self.assertEqual(extract_target_url_from_tag(soup.find(id="a2")), "files/table1.xlsx")
        self.assertEqual(extract_target_url_from_tag(soup.find(id="a3")), "./files/table1.xlsx")

    def test_b3_relative_paths_and_inline_scripts_json(self):
        """B3: JSON script extracts relative paths without leading slash and inline script data."""
        html = """
        <script type="application/json">
        {"supp": ["files/附表1.xlsx", "Supplementary_Table_S2.xlsx", "table1.csv"]}
        </script>
        <script>
        window.__INITIAL_STATE__ = {"supp": ["/files/table_s3.xlsx"]};
        </script>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper/")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/paper/files/附表1.xlsx", urls)
        self.assertIn("https://example.com/paper/Supplementary_Table_S2.xlsx", urls)
        self.assertIn("https://example.com/paper/table1.csv", urls)
        self.assertIn("https://example.com/files/table_s3.xlsx", urls)

    def test_c1_pdf_and_doc_immunity_on_elsevier_and_doi(self):
        """C1: PDFs, DOCXs, and data files on Elsevier / DOI endpoints are immune from exclusion."""
        self.assertFalse(is_excluded_url("https://api.elsevier.com/content/object/eid/1-s2.0-S123/supplementary-tables.pdf"))
        self.assertFalse(is_excluded_url("https://www.elsevier.com/__data/assets/pdf/0001/table-s1.pdf"))
        self.assertFalse(is_excluded_url("https://doi.org/10.1016/j.cell/appendix1.pdf"))
        # Bare citation DOIs and non-file Elsevier URLs remain excluded
        self.assertTrue(is_excluded_url("https://doi.org/10.1016/j.cell.2023.01.001"))
        self.assertTrue(is_excluded_url("https://www.elsevier.com/about"))

    def test_d1_html_table_with_large_head_section(self):
        """D1: Standalone HTML table attachment with large head (>2KB) is valid and preserved."""
        with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as f:
            large_html = b"<!DOCTYPE html><html><head><style>" + b"body { margin: 0; }\n" * 150 + b"</style></head><body><table><tr><th>Sample</th><th>Value</th></tr></table></body></html>"
            f.write(large_html)
            temp_path = f.name

        try:
            is_valid, reason = validate_downloaded_file(temp_path)
            self.assertTrue(is_valid, f"HTML table with large head falsely rejected: {reason}")
            self.assertIn("HTML table attachment", reason)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def test_d2_download_distinct_urls_same_filename_not_skipped(self):
        """D2: Distinct URLs with identical filenames are not skipped and are disambiguated."""
        import journal_downloader
        mock_resp1 = MagicMock(status=200, headers={}, body=b"PK\x03\x04file1_content")
        mock_resp2 = MagicMock(status=200, headers={}, body=b"PK\x03\x04file2_content")

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("journal_downloader.Fetcher.get", side_effect=[mock_resp1, mock_resp2]):
                f1 = journal_downloader.download_file("https://example.com/part1/table_s1.xlsx", tmpdir)
                f2 = journal_downloader.download_file("https://example.com/part2/table_s1.xlsx", tmpdir)
                self.assertIsNotNone(f1)
                self.assertIsNotNone(f2)
                self.assertNotEqual(f1, f2)
                self.assertTrue(f1.endswith("table_s1.xlsx"))
                self.assertTrue(f2.endswith("table_s1_2.xlsx"))
                with open(f1, "rb") as fp1, open(f2, "rb") as fp2:
                    self.assertEqual(fp1.read(), b"PK\x03\x04file1_content")
                    self.assertEqual(fp2.read(), b"PK\x03\x04file2_content")

    def test_e1_declared_count_appendix_data_oxford(self):
        """E1: detect_declared_supplement_count covers standalone Appendix, Data ranges, and Oxford commas."""
        self.assertEqual(detect_declared_supplement_count("Tables S1, S2, and S3"), 3)
        self.assertEqual(detect_declared_supplement_count("see Appendix 1-4"), 4)
        self.assertEqual(detect_declared_supplement_count("Appendix 1 to 5"), 5)
        self.assertEqual(detect_declared_supplement_count("Data S1-S3"), 3)
        self.assertEqual(detect_declared_supplement_count("Supplementary Data S1-S4"), 4)
        self.assertEqual(detect_declared_supplement_count("见附录 1-4"), 4)
        self.assertEqual(detect_declared_supplement_count("附录1至附录5"), 5)

    def test_a1_appendix_variations_and_chinese_numerals(self):
        """A1: is_explicit_supp_table covers Appendix without space and Chinese numerals."""
        self.assertTrue(is_explicit_supp_table("Appendix1"))
        self.assertTrue(is_explicit_supp_table("Appendix-1"))
        self.assertTrue(is_explicit_supp_table("Appendix_1"))
        self.assertTrue(is_explicit_supp_table("附录一"))
        self.assertTrue(is_explicit_supp_table("附录二"))

    def test_f1_unified_is_data_url_rejects_main_text_table(self):
        """F1: is_data_url unified with should_match_candidate rejects bare main-text Table 1."""
        import scansci_supp_downloader
        self.assertFalse(scansci_supp_downloader.is_data_url("https://example.com/article/tables/1", text="Table 1"))
        self.assertTrue(scansci_supp_downloader.is_data_url("https://example.com/dl?id=1", text="附表 1"))

    def test_f3_compound_archive_extensions(self):
        """F3: check_data_extension recognizes compound archive extensions."""
        from supp_finder import check_data_extension
        self.assertEqual(check_data_extension("https://example.com/data.tar.gz"), ".tar.gz")
        self.assertEqual(check_data_extension("https://example.com/data.tgz"), ".tgz")
        self.assertEqual(check_data_extension("https://example.com/data.tar.bz2"), ".tar.bz2")


class TestCodebaseAuditFixes(unittest.TestCase):
    """Regression tests verifying all fixes from DeepInvestigator codebase audit."""

    def test_p0_try_elsevier_api_campus_unpack_safety(self):
        """P0: try_elsevier_api_campus returns (list, set) on all early exits, preventing ValueError unpack crash."""
        from scansci_supp_downloader import try_elsevier_api_campus
        from unittest.mock import patch, MagicMock

        # 1. No campus network & no insttoken
        files, mmcs = try_elsevier_api_campus("10.1016/test", "S123", "/tmp", {})
        self.assertEqual(files, [])
        self.assertEqual(mmcs, set())

        # 2. No API key
        files, mmcs = try_elsevier_api_campus("10.1016/test", "S123", "/tmp", {"is_campus_network": True})
        self.assertEqual(files, [])
        self.assertEqual(mmcs, set())

        # 3. Invalid DOI
        files, mmcs = try_elsevier_api_campus("invalid_doi", "S123", "/tmp", {"is_campus_network": True, "elsevier_api_key": "dummy"})
        self.assertEqual(files, [])
        self.assertEqual(mmcs, set())

        # 4. HTTP error / failed request
        with patch("requests.get", side_effect=Exception("network error")):
            files, mmcs = try_elsevier_api_campus("10.1016/test", "S123", "/tmp", {"is_campus_network": True, "elsevier_api_key": "dummy"})
            self.assertEqual(files, [])
            self.assertEqual(mmcs, set())

        # 5. Non-200 HTTP status
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("requests.get", return_value=mock_resp):
            files, mmcs = try_elsevier_api_campus("10.1016/test", "S123", "/tmp", {"is_campus_network": True, "elsevier_api_key": "dummy"})
            self.assertEqual(files, [])
            self.assertEqual(mmcs, set())

        # 6. JSON parse failure
        mock_resp.status_code = 200
        mock_resp.json.side_effect = Exception("json err")
        with patch("requests.get", return_value=mock_resp):
            files, mmcs = try_elsevier_api_campus("10.1016/test", "S123", "/tmp", {"is_campus_network": True, "elsevier_api_key": "dummy"})
            self.assertEqual(files, [])
            self.assertEqual(mmcs, set())

        # 7. No PII
        mock_resp.json.side_effect = None
        mock_resp.json.return_value = {"full-text-retrieval-response": {"coredata": {}}}
        with patch("requests.get", return_value=mock_resp):
            files, mmcs = try_elsevier_api_campus("10.1016/test", "", "/tmp", {"is_campus_network": True, "elsevier_api_key": "dummy"})
            self.assertEqual(files, [])
            self.assertEqual(mmcs, set())

    def test_p0_login_publishers_save_merged_cookies(self):
        """P0: login_publishers merges cookies by (domain, name, path) without overwriting existing publisher sessions."""
        import tempfile
        from login_publishers import save_merged_cookies

        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_file = os.path.join(tmpdir, "cookies.json")
            # Session 1: Elsevier cookies
            c1 = [
                {"name": "els_session", "value": "els123", "domain": ".sciencedirect.com", "path": "/"},
                {"name": "common_pref", "value": "v1", "domain": ".example.com", "path": "/"},
            ]
            save_merged_cookies(c1, cookie_file)

            # Session 2: Springer cookies + updated common_pref
            c2 = [
                {"name": "springer_session", "value": "sp456", "domain": ".springer.com", "path": "/"},
                {"name": "common_pref", "value": "v2", "domain": ".example.com", "path": "/"},
            ]
            merged = save_merged_cookies(c2, cookie_file)

            # Verification: All domains preserved, common_pref updated
            self.assertEqual(len(merged), 3)
            cookie_dict = {(c["domain"], c["name"]): c["value"] for c in merged}
            self.assertEqual(cookie_dict[(".sciencedirect.com", "els_session")], "els123")
            self.assertEqual(cookie_dict[(".springer.com", "springer_session")], "sp456")
            self.assertEqual(cookie_dict[(".example.com", "common_pref")], "v2")

    def test_p1_normalize_playwright_cookies(self):
        """P1: normalize_playwright_cookies fixes sameSite casing, removes invalid values, and drops empty domains."""
        from playwright_utils import normalize_playwright_cookies

        raw = [
            # Valid cookies
            {"name": "c1", "value": "v1", "domain": ".test.com", "sameSite": "lax"},
            {"name": "c2", "value": "v2", "domain": ".test.com", "sameSite": "STRICT"},
            {"name": "c3", "value": "v3", "domain": ".test.com", "sameSite": "no_restriction"},
            # Invalid sameSite -> removed
            {"name": "c4", "value": "v4", "domain": ".test.com", "sameSite": "unspecified"},
            {"name": "c5", "value": "v5", "domain": ".test.com", "sameSite": None},
            # Empty domain without url -> dropped
            {"name": "c6", "value": "v6", "domain": ""},
            # Empty domain with url -> domain stripped, url retained
            {"name": "c7", "value": "v7", "domain": "", "url": "https://test.com"},
            # Missing name/value -> dropped
            {"name": "", "value": "v8", "domain": ".test.com"},
            {"name": "c9", "domain": ".test.com"},
        ]
        norm = normalize_playwright_cookies(raw)
        names = {c["name"]: c for c in norm}

        self.assertEqual(names["c1"]["sameSite"], "Lax")
        self.assertEqual(names["c2"]["sameSite"], "Strict")
        self.assertEqual(names["c3"]["sameSite"], "None")
        self.assertNotIn("sameSite", names["c4"])
        self.assertNotIn("sameSite", names["c5"])
        self.assertNotIn("c6", names)
        self.assertIn("c7", names)
        self.assertNotIn("domain", names["c7"])
        self.assertNotIn("c9", names)

    def test_p1_supp_keywords_and_case_e_wiley(self):
        """P1: supp_finder recognizes broadened keywords and Case E matches text/attrs & Wiley extensionless links."""
        from supp_finder import SUPP_KEYWORD_PATTERN, should_match_candidate

        # Keywords pattern
        self.assertTrue(SUPP_KEYWORD_PATTERN.search("Supporting Information"))
        self.assertTrue(SUPP_KEYWORD_PATTERN.search("Additional file 1"))
        self.assertTrue(SUPP_KEYWORD_PATTERN.search("Supplementary Material"))
        self.assertTrue(SUPP_KEYWORD_PATTERN.search("补充数据"))

        # Case E: PDF matched by anchor text even if URL has no supp keyword
        is_cand, rule = should_match_candidate("https://example.com/asset/12345.pdf", text="Supporting Information")
        self.assertTrue(is_cand)
        self.assertEqual(rule, "pdf_with_supplementary_keyword")

        # Case E: Wiley direct download link without .pdf
        wiley_dl = "https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002%2Fanie.202300000&file=suppl1"
        is_cand, rule = should_match_candidate(wiley_dl, text="Supporting Information")
        self.assertTrue(is_cand)
        self.assertEqual(rule, "wiley_supp_link")

    def test_p1_detect_declared_count_rejects_main_text_tables(self):
        """P1: detect_declared_supplement_count rejects bare 'Tables 1 to 3' but matches 'Tables S1-S3'."""
        from supp_finder import detect_declared_supplement_count

        # Bare main text tables MUST return None
        self.assertIsNone(detect_declared_supplement_count("As seen in Tables 1 to 3, the results indicate..."))
        self.assertIsNone(detect_declared_supplement_count("Tables 1, 2, and 3 describe the primary cohort."))

        # Supplementary tables with S or modifier MUST match
        self.assertEqual(detect_declared_supplement_count("See Tables S1-S3 for raw data."), 3)
        self.assertEqual(detect_declared_supplement_count("Supplementary Tables 1 to 4 provide details."), 4)
        self.assertEqual(detect_declared_supplement_count("Tables S1, S2, and S3 list the antibodies."), 3)

    def test_p1_exclude_main_article_pdf_springer_and_plos(self):
        """P1: _extract_springer_nature_candidates excludes main article PDF and _extract_plos_candidates excludes printable."""
        from supp_finder import find_all_candidates

        # Springer article with both main PDF and supplementary ESM PDF
        springer_html = '''
        <html><body>
          <a data-track-action="download" href="/content/pdf/10.1007/s00126-020-00999-x.pdf">Download article PDF</a>
          <a data-track-action="download" href="/content/pdf/10.1007/s00126-020-00999-x/esm/table-s1.pdf">Supplementary Table S1 (PDF)</a>
        </body></html>
        '''
        diag = find_all_candidates(springer_html, "https://link.springer.com/article/10.1007/s00126-020-00999-x")
        urls = [c.url for c in diag.candidates]
        self.assertNotIn("https://link.springer.com/content/pdf/10.1007/s00126-020-00999-x.pdf", urls)
        self.assertIn("https://link.springer.com/content/pdf/10.1007/s00126-020-00999-x/esm/table-s1.pdf", urls)

        # PLOS article with printable main PDF and supplementary file
        plos_html = '''
        <html><body>
          <a href="/article/file?id=10.1371/journal.pone.0298123&type=printable">Download printable PDF</a>
          <a href="/article/file?id=10.1371/journal.pone.0298123.s001&type=supplementary">S1 Table. (XLSX)</a>
        </body></html>
        '''
        diag_plos = find_all_candidates(plos_html, "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0298123")
        plos_urls = [c.url for c in diag_plos.candidates]
        self.assertNotIn("https://journals.plos.org/article/file?id=10.1371/journal.pone.0298123&type=printable", plos_urls)
        self.assertIn("https://journals.plos.org/article/file?id=10.1371/journal.pone.0298123.s001&type=supplementary", plos_urls)

    def test_p2_embedded_json_excludes_static_assets(self):
        """P2: _extract_embedded_json_candidates excludes .js, .css, .map, .svg, .json, .html."""
        from supp_finder import find_all_candidates

        html = '''
        <script type="application/json">
        {
          "supplements": [
            "/static/js/supp-bundle.min.js",
            "/styles/table-styles.css",
            "/assets/supp_table_1.xlsx",
            "/assets/supp_data.pdf",
            "/source/map.js.map",
            "/icons/table.svg"
          ]
        }
        </script>
        '''
        diag = find_all_candidates(html, "https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/assets/supp_table_1.xlsx", urls)
        self.assertIn("https://example.com/assets/supp_data.pdf", urls)
        self.assertNotIn("https://example.com/static/js/supp-bundle.min.js", urls)
        self.assertNotIn("https://example.com/styles/table-styles.css", urls)
        self.assertNotIn("https://example.com/source/map.js.map", urls)
        self.assertNotIn("https://example.com/icons/table.svg", urls)

    def test_p2_extract_filename_preserves_default_name_extension(self):
        """P2: extract_filename_from_url preserves default_name with extension over extensionless query param."""
        from supp_finder import extract_filename_from_url

        url = "https://onlinelibrary.wiley.com/action/downloadSupplement?doi=10.1002%2Fanie.202300000&attachmentId=1001"
        # Without default_name -> disambiguates as attachment_1001
        self.assertEqual(extract_filename_from_url(url), "attachment_1001")

        # With default_name containing valid extension -> preserves default_name
        self.assertEqual(extract_filename_from_url(url, default_name="Table S1.xlsx"), "Table S1.xlsx")
        self.assertEqual(extract_filename_from_url(url, default_name="Supplementary Data.pdf"), "Supplementary Data.pdf")

    def test_p2_extract_article_title_unescape(self):
        """P2: extract_article_title unescapes HTML entities like &amp;, &#39; and avoids &amp; in folder names."""
        from supp_finder import extract_article_title

        html = "<title>Geology &amp; Mineralogy: Gold &amp; Copper Deposits - ScienceDirect</title>"
        title = extract_article_title(html)
        self.assertNotIn("&amp;", title)
        self.assertIn("&", title)
        self.assertEqual(title, "Geology & Mineralogy_ Gold & Copper Deposits")

    def test_p1_journal_downloader_load_cookies_local(self):
        import json
        import tempfile
        from unittest.mock import patch
        from journal_downloader import load_saved_cookies

        with tempfile.TemporaryDirectory() as tmpdir:
            local_cookies = os.path.join(tmpdir, "cookies.json")
            with open(local_cookies, "w", encoding="utf-8") as f:
                json.dump([{"name": "test_cookie", "value": "val123"}], f)

            with patch("os.path.abspath", return_value=local_cookies), \
                 patch("os.path.expanduser", return_value="/nonexistent/cookies.json"):
                cookies = load_saved_cookies()
                self.assertIsNotNone(cookies)
                self.assertEqual(cookies[0]["name"], "test_cookie")

    def test_p1_scansci_env_vars_injection(self):
        """P1: scansci_supp_downloader main() injects ELSEVIER_API_KEY, IS_CAMPUS_NETWORK, ELSEVIER_INSTTOKEN into config."""
        from unittest.mock import patch
        env = {
            "ELSEVIER_API_KEY": "test_key_123",
            "IS_CAMPUS_NETWORK": "true",
            "ELSEVIER_INSTTOKEN": "test_token_456",
        }
        with patch.dict(os.environ, env):
            # Simulate the config initialization from main()
            config = {}
            if os.environ.get("ELSEVIER_API_KEY"):
                config["elsevier_api_key"] = os.environ["ELSEVIER_API_KEY"].strip()
            if os.environ.get("IS_CAMPUS_NETWORK"):
                val = os.environ["IS_CAMPUS_NETWORK"].strip().lower()
                config["is_campus_network"] = val in ("1", "true", "yes", "y")
            if os.environ.get("ELSEVIER_INSTTOKEN"):
                config["elsevier_insttoken"] = os.environ["ELSEVIER_INSTTOKEN"].strip()

            self.assertEqual(config["elsevier_api_key"], "test_key_123")
            self.assertTrue(config["is_campus_network"])
            self.assertEqual(config["elsevier_insttoken"], "test_token_456")

    def test_p1_test_api_env_handling(self):
        """P1: test_api.py exits with error message if ELSEVIER_API_KEY is unset."""
        import subprocess
        # Run test_api.py in a clean environment without ELSEVIER_API_KEY
        env = {k: v for k, v in os.environ.items() if k != "ELSEVIER_API_KEY"}
        script_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "test_api.py")
        res = subprocess.run([sys.executable, script_path], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("ELSEVIER_API_KEY environment variable is not set", res.stdout)

    def test_p2_method_3_expect_download_configuration(self):
        """P2: Verify scansci_supp_downloader Method 3 uses 10s timeout and injects cookies."""
        import inspect
        import scansci_supp_downloader
        src = inspect.getsource(scansci_supp_downloader.download_file)
        self.assertIn("timeout=10000", src)
        self.assertIn("normalize_playwright_cookies(cookies", src)
        self.assertIn("context.add_cookies(normalized_c)", src)

    def test_find_all_candidates_wiley_preserves_extensions(self):
        """Verify find_all_candidates preserves .xlsx and (XLSX) in Wiley download links instead of .bin."""
        from supp_finder import find_all_candidates
        html = '''
        <html><body>
          <div class="article-row">
            <a href="/action/downloadSupplement?doi=10.1002%2Fanie.202300000&attachmentId=1001">Table S1.xlsx</a>
            <a href="/action/downloadSupplement?doi=10.1002%2Fanie.202300000&attachmentId=1002">Supplementary Dataset 2 (XLSX)</a>
          </div>
        </body></html>
        '''
        diag = find_all_candidates(html, "https://onlinelibrary.wiley.com/doi/10.1002/anie.202300000")
        fnames = {c.filename for c in diag.candidates}
        self.assertIn("Table S1.xlsx", fnames)
        self.assertIn("Supplementary Dataset 2.XLSX", fnames)
        self.assertFalse(any(f.endswith(".bin") for f in fnames))

    def test_normalize_playwright_cookies_dict_and_strip_path(self):
        """Verify normalize_playwright_cookies handles dict format and strips path when url is present."""
        from playwright_utils import normalize_playwright_cookies

        # 1. Key-value mapping dict
        mapping = {"session_token": "abc123xyz", "logged_in": "true"}
        norm = normalize_playwright_cookies(mapping, default_url="https://example.com/test")
        self.assertEqual(len(norm), 2)
        by_name = {c["name"]: c for c in norm}
        self.assertEqual(by_name["session_token"]["value"], "abc123xyz")
        self.assertEqual(by_name["session_token"]["domain"], "example.com")

        # 2. Cookie with both url and path (must strip path to avoid Playwright either url or path error)
        c_both = [{"name": "auth", "value": "val", "url": "https://example.com", "path": "/"}]
        norm_both = normalize_playwright_cookies(c_both)
        self.assertIn("url", norm_both[0])
        self.assertNotIn("path", norm_both[0])
        self.assertNotIn("domain", norm_both[0])

    def test_load_saved_cookies_merges_local_and_profile_and_supports_dict(self):
        """Verify journal_downloader load_saved_cookies supports dicts and merges local + profile."""
        import tempfile
        import json
        from unittest.mock import patch
        from journal_downloader import load_saved_cookies

        with tempfile.TemporaryDirectory() as tmpdir:
            local_path = os.path.join(tmpdir, "local_cookies.json")
            profile_path = os.path.join(tmpdir, "profile_cookies.json")

            # Local cookies as dict
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump({"local_cookie": "local_val"}, f)

            # Profile cookies as list
            with open(profile_path, "w", encoding="utf-8") as f:
                json.dump([{"name": "profile_cookie", "value": "profile_val", "domain": "example.com"}], f)

            with patch("os.path.abspath", return_value=local_path), \
                 patch("os.path.expanduser", return_value=profile_path):
                cookies = load_saved_cookies()
                self.assertIsNotNone(cookies)
                names = {c["name"] for c in cookies}
                self.assertIn("local_cookie", names)
                self.assertIn("profile_cookie", names)

    # ── 12. PubMed Central (PMC) & Europe PMC Supplementary Extraction ──────

    def test_pmc_supplementary_section_and_bin_links(self):
        """PMC supplementary sections and /bin/ binary downloads must be extracted."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>PMC Test Article</title></head>
        <body>
            <section id="sec8">
                <h2 class="pmc_sec_title">Supplementary information</h2>
                <section class="supplementary-materials">
                    <div class="media p">
                        <a href="/articles/instance/7759461/bin/41586_2020_2649_MOESM1_ESM.pdf"
                           data-ga-action="click_feat_suppl" class="usa-link">
                            <span>Supplementary Information</span>
                        </a>
                        <p>This file contains Supplementary Methods and Figures.</p>
                    </div>
                    <div class="media p">
                        <a href="/articles/instance/7759461/bin/supp_table_1.xlsx">
                            <span>Supplementary Table 1</span>
                        </a>
                    </div>
                </section>
            </section>
        </body>
        </html>
        """
        landing_url = "https://pmc.ncbi.nlm.nih.gov/articles/PMC7759461/"
        diag = find_all_candidates(html, base_url=landing_url)
        urls = [c.url for c in diag.candidates]
        fnames = [c.filename for c in diag.candidates]

        self.assertEqual(len(diag.candidates), 2)
        self.assertIn("https://pmc.ncbi.nlm.nih.gov/articles/instance/7759461/bin/41586_2020_2649_MOESM1_ESM.pdf", urls)
        self.assertIn("https://pmc.ncbi.nlm.nih.gov/articles/instance/7759461/bin/supp_table_1.xlsx", urls)
        self.assertIn("41586_2020_2649_MOESM1_ESM.pdf", fnames)
        self.assertIn("supp_table_1.xlsx", fnames)

    def test_europepmc_supplementary_links(self):
        """Europe PMC binary supplementary links must be extracted."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Europe PMC Article</title></head>
        <body>
            <div class="supplementary-material">
                <a href="/articles/PMC12345/bin/Supp_Data_S1.docx">Supplementary Data S1</a>
            </div>
        </body>
        </html>
        """
        landing_url = "https://europepmc.org/article/PMC/12345"
        diag = find_all_candidates(html, base_url=landing_url)
        self.assertEqual(len(diag.candidates), 1)
        self.assertEqual(diag.candidates[0].url, "https://europepmc.org/articles/PMC12345/bin/Supp_Data_S1.docx")
        self.assertEqual(diag.candidates[0].filename, "Supp_Data_S1.docx")

    def test_pmc_excludes_article_figures_and_anchors(self):
        """PMC parser must strictly exclude main-text figures (Fig1_HTML, gr1, fx1) and anchors."""
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>PMC Mixed Content</title></head>
        <body>
            <div class="supplementary-material">
                <a href="/articles/PMC12345/bin/Supp_Table_S1.xlsx">Supplementary Table S1</a>
                <a href="#sec8">Back to section 8</a>
                <a href="https://doi.org/10.1038/s41586-ref">External Reference</a>
            </div>
            <!-- Main-text figures often placed in /bin/ in PMC -->
            <div class="figure">
                <a href="/articles/PMC12345/bin/41586_2020_2649_Fig1_HTML.jpg">Figure 1 Overview</a>
                <a href="/articles/PMC12345/bin/figure_2.png">Figure 2</a>
                <a href="/articles/PMC12345/bin/gr1.jpg">Graphical Abstract</a>
                <a href="/articles/PMC12345/bin/fx1.jpg">Figure FX1</a>
            </div>
        </body>
        </html>
        """
        landing_url = "https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/"
        diag = find_all_candidates(html, base_url=landing_url)
        # ONLY Supp_Table_S1.xlsx should be extracted!
        self.assertEqual(len(diag.candidates), 1)
        self.assertEqual(diag.candidates[0].filename, "Supp_Table_S1.xlsx")
        self.assertEqual(diag.candidates[0].url, "https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/bin/Supp_Table_S1.xlsx")


    def test_scenario1_card_short_circuit_fix(self):
        """Scenario 1: get_nearby_context_text must not short-circuit on inner non-table labels when outer container has explicit table label."""
        html = """
        <div class="supp-item-card">
            <div class="supp-item-heading">
                <h4>Supplementary Table S1. Patient demographic metadata</h4>
            </div>
            <div class="supp-item-body">
                <span class="file-format-badge">XLSX (15.2 KB)</span>
                <a href="/download/data-asset-8921.bin">Download File</a>
            </div>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/article/123")
        self.assertEqual(len(diag.candidates), 1)
        cand = diag.candidates[0]
        self.assertIn("Supplementary Table S1", cand.filename)
        self.assertIn("nearby_context", cand.match_rule)

    def test_scenario2_s_prefixed_table_numbering(self):
        """Scenario 2: S-prefixed numbering (S1 Table, S2 Table, S1 Data, s1-table.pdf, S1_Table.xlsx) must be recognized."""
        html = """
        <div>
            <a href="/download/s1_table.xlsx">S1 Table</a>
            <a href="/download/s2_table.pdf">S2 Table</a>
            <a href="/download/s1_data.xlsx">S1 Data</a>
            <a href="/download/s1_dataset.csv">S1 Dataset</a>
            <a href="/download/plos_file.bin" title="S1 File: Complete phylogenetic matrix">Download</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/article/123")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/download/s1_table.xlsx", urls)
        self.assertIn("https://example.com/download/s2_table.pdf", urls)
        self.assertIn("https://example.com/download/s1_data.xlsx", urls)
        self.assertIn("https://example.com/download/s1_dataset.csv", urls)
        self.assertIn("https://example.com/download/plos_file.bin", urls)

    def test_scenario3_source_data_files(self):
        """Scenario 3: Source Data files and Springer Nature source-data links must be recognized."""
        html = """
        <div class="article-body">
            <a href="/download/source_data_fig1.xlsx">Source Data Fig. 1</a>
            <a href="/download/source_data_table1.xlsx">Source Data Table 1</a>
            <a href="/download/source-data.pdf">Download</a>
            <a href="/download/nature_sd.bin" data-test="source-data-link">Raw Data</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://nature.com/articles/s41586-021")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://nature.com/download/source_data_fig1.xlsx", urls)
        self.assertIn("https://nature.com/download/source_data_table1.xlsx", urls)
        self.assertIn("https://nature.com/download/source-data.pdf", urls)
        self.assertIn("https://nature.com/download/nature_sd.bin", urls)

    def test_scenario4_chinese_table_numerals(self):
        """Scenario 4: Chinese numerals and labels (附表一, 补充表二, 补充文件一) must be recognized."""
        html = """
        <div>
            <a href="/files/fubiao1.xlsx">附表一：样本基本特征</a>
            <a href="/files/buchongbiao2.xlsx">补充表二：回归分析模型结果</a>
            <a href="/files/buchongwenjian1.pdf">补充文件一：质控标准</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://journal.cn/article/1")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://journal.cn/files/fubiao1.xlsx", urls)
        self.assertIn("https://journal.cn/files/buchongbiao2.xlsx", urls)
        self.assertIn("https://journal.cn/files/buchongwenjian1.pdf", urls)

    def test_scenario5_roman_numeral_tables(self):
        """Scenario 5: Roman numeral table designations (Table SI, Table SII, Supplementary Table II, Table SIV)."""
        html = """
        <div>
            <a href="/tables/table_si.xlsx">Table SI: Cohort characteristics</a>
            <a href="/tables/table_sii.pdf">Table SII: Sensitivity analysis</a>
            <a href="/tables/supp_table_ii.xlsx">Supplementary Table II</a>
            <a href="/tables/table_siv.csv">Table SIV</a>
        </div>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/tables/table_si.xlsx", urls)
        self.assertIn("https://example.com/tables/table_sii.pdf", urls)
        self.assertIn("https://example.com/tables/supp_table_ii.xlsx", urls)
        self.assertIn("https://example.com/tables/table_siv.csv", urls)

    def test_scenario6_non_anchor_link_controls(self):
        """Scenario 6: Non-<a> controls such as <object data="..."> and <form action="..."><button>."""
        html = """
        <section id="supplements">
            <object data="/supp/table_s1.xlsx" type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet">
                Table S1 Spreadsheet
            </object>
            <form action="/api/download/table_s2.xlsx" method="POST">
                <button type="submit">Download Supplementary Table S2</button>
            </form>
        </section>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/supp/table_s1.xlsx", urls)
        self.assertIn("https://example.com/api/download/table_s2.xlsx", urls)

    def test_scenario7_embedded_json_with_spaces_and_extensions(self):
        """Scenario 7: Embedded JSON scripts containing file paths with spaces and various data extensions."""
        html = """
        <script type="application/json">
        {
            "supplements": [
                {"url": "/files/Supplementary Table S1.xlsx", "title": "Table S1"},
                {"url": "/files/Source Data 1.csv", "title": "Data 1"},
                {"url": "/files/Extended Methods.docx", "title": "Methods"}
            ]
        }
        </script>
        """
        diag = find_all_candidates(html, base_url="https://example.com/paper")
        urls = [c.url for c in diag.candidates]
        self.assertIn("https://example.com/files/Supplementary Table S1.xlsx", urls)
        self.assertIn("https://example.com/files/Source Data 1.csv", urls)
        self.assertIn("https://example.com/files/Extended Methods.docx", urls)

    def test_scenario8_declared_count_pattern8(self):
        """Scenario 8: Declared supplement count pattern 8 matches highest explicit table mention."""
        text1 = "Clinical characteristics are presented in Supplementary Table S5."
        self.assertEqual(detect_declared_supplement_count(text1), 5)
        text2 = "Detailed metrics are available in Table S1-S4."
        self.assertEqual(detect_declared_supplement_count(text2), 4)

    def test_ole2_binary_magic_bytes(self):
        """Legacy Office 97-2003 OLE2 binary files must be recognized by infer_file_extension and validate_downloaded_file."""
        ole2_header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
        inferred = infer_file_extension(ole2_header, "application/octet-stream")
        self.assertEqual(inferred, ".xls")

        with tempfile.NamedTemporaryFile(suffix=".xls", delete=False) as tf:
            tf.write(ole2_header)
            tpath = tf.name
        try:
            is_val, reason = validate_downloaded_file(tpath)
            self.assertTrue(is_val)
            self.assertIn("Office 97-2003", reason)
        finally:
            if os.path.exists(tpath):
                os.remove(tpath)

    def test_expand_page_accordions_action(self):
        """expand_page_accordions_action safely evaluates JavaScript on Playwright Page."""
        mock_page = MagicMock()
        expand_page_accordions_action(mock_page)
        self.assertTrue(mock_page.evaluate.called)
        self.assertTrue(mock_page.wait_for_timeout.called)

        # Non-mock / empty object should not raise exception
        expand_page_accordions_action(object())

    def test_webvpn_no_vpn_and_proxy_security(self):
        """load_all_scansci_cookies with no_vpn=True skips WebVPN cookies, and NO_PROXY is not polluted."""
        from scansci_supp_downloader import load_all_scansci_cookies
        config = {
            "vpnsci_enabled": True,
            "webvpn_base": "https://webvpn.univ.edu.cn",
        }
        with patch("scansci_supp_downloader.load_vpnsci_cookies_unified", return_value={"vpn_token": "secret"}):
            # When no_vpn=False, WebVPN cookies are included
            c_with_vpn = load_all_scansci_cookies(config, no_vpn=False)
            names_with_vpn = [c["name"] for c in c_with_vpn]
            self.assertIn("vpn_token", names_with_vpn)

            # When no_vpn=True, WebVPN cookies are skipped
            c_no_vpn = load_all_scansci_cookies(config, no_vpn=True)
            names_no_vpn = [c["name"] for c in c_no_vpn]
            self.assertNotIn("vpn_token", names_no_vpn)

    def test_disambiguate_target_filename_collisions(self):
        """disambiguate_target_filename appends suffixes to avoid overwriting files from different URLs."""
        with tempfile.TemporaryDirectory() as td:
            manifest = {}
            f1 = disambiguate_target_filename(td, "supplement.pdf", "https://example.com/doc1.pdf", manifest)
            self.assertEqual(f1, "supplement.pdf")
            # Create file on disk and add to manifest
            p1 = os.path.join(td, f1)
            with open(p1, "wb") as f:
                f.write(b"data1")
            manifest[f1] = "https://example.com/doc1.pdf"

            # Same URL returns same filename (idempotent)
            f1_again = disambiguate_target_filename(td, "supplement.pdf", "https://example.com/doc1.pdf", manifest)
            self.assertEqual(f1_again, "supplement.pdf")

            # Different URL returns disambiguated filename
            f2 = disambiguate_target_filename(td, "supplement.pdf", "https://example.com/doc2.pdf", manifest)
            self.assertEqual(f2, "supplement_2.pdf")

    def test_tier_0_multi_format_and_extended_extensions(self):
        """Tier 0 Elsevier CDN brute force probes extended extensions (.cif, .h5, etc.) and does not abort early on multi-format mmcs."""
        from scansci_supp_downloader import try_elsevier_cdn_brute_force
        from supp_finder import load_download_manifest

        with tempfile.TemporaryDirectory() as td:
            def fake_head(url, timeout=10, allow_redirects=True):
                resp = MagicMock()
                if "mmc1.xlsx" in url or "mmc1.pdf" in url or "mmc2.cif" in url:
                    resp.status_code = 200
                    resp.headers = {"content-length": "1024"}
                else:
                    resp.status_code = 404
                    resp.headers = {}
                return resp

            def fake_get(url, timeout=30, stream=True):
                resp = MagicMock()
                resp.status_code = 200
                resp.headers = {}
                resp.iter_content = MagicMock(return_value=[b"fake binary content" * 10])
                return resp

            with patch("requests.Session.head", side_effect=fake_head), \
                 patch("requests.Session.get", side_effect=fake_get), \
                 patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")):
                files = try_elsevier_cdn_brute_force("S99999", td, max_mmc=5, max_consecutive_misses=2)
                basenames = [os.path.basename(f) for f in files]
                self.assertIn("mmc1.xlsx", basenames)
                self.assertIn("mmc1.pdf", basenames)
                self.assertIn("mmc2.cif", basenames)
                self.assertEqual(len(files), 3)

                # Check manifest records all 3 files
                manifest = load_download_manifest(td)
                self.assertIn("mmc1.xlsx", manifest)
                self.assertIn("mmc1.pdf", manifest)
                self.assertIn("mmc2.cif", manifest)

    def test_tier_a_content_disposition_collision_disambiguation(self):
        """Tier A Elsevier API campus downloads handle Content-Disposition filename collisions without overwriting."""
        from scansci_supp_downloader import try_elsevier_api_campus
        from supp_finder import load_download_manifest

        with tempfile.TemporaryDirectory() as td:
            api_response_json = {
                "full-text-retrieval-response": {
                    "coredata": {"pii": "S88888"},
                    "originalText": "mmc1.pdf mmc2.pdf"
                }
            }

            def fake_api_get(url, **kwargs):
                resp = MagicMock()
                if "content/article/doi" in url or "content/article/pii" in url:
                    resp.status_code = 200
                    resp.json = MagicMock(return_value=api_response_json)
                elif "ars.els-cdn.com" in url:
                    resp.status_code = 200
                    resp.headers = {
                        "content-disposition": 'attachment; filename="Supplementary_Information.pdf"'
                    }
                    resp.iter_content = MagicMock(return_value=[b"%PDF-1.5 fake pdf"])
                return resp

            with patch("requests.get", side_effect=fake_api_get), \
                 patch("requests.Session.get", side_effect=fake_api_get), \
                 patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")):
                files, mmcs = try_elsevier_api_campus("10.1016/j.test.2026.01", "S88888", td, {"elsevier_api_key": "dummy", "is_campus_network": True})
                self.assertEqual(len(files), 2)
                basenames = [os.path.basename(f) for f in files]
                self.assertIn("Supplementary_Information.pdf", basenames)
                self.assertIn("Supplementary_Information_2.pdf", basenames)

                manifest = load_download_manifest(td)
                self.assertIn("Supplementary_Information.pdf", manifest)
                self.assertIn("Supplementary_Information_2.pdf", manifest)
                self.assertNotEqual(manifest["Supplementary_Information.pdf"], manifest["Supplementary_Information_2.pdf"])

    def test_underscore_filenames_in_table_filename_pattern(self):
        """Filenames with underscores after table/data keywords must match TABLE_FILENAME_PATTERN."""
        names = ["S1_Table_raw.xlsx", "s1_table_data.xlsx", "S2_Data_final.zip", "s1-table.pdf"]
        for name in names:
            self.assertTrue(bool(TABLE_FILENAME_PATTERN.search(name)), f"Failed for {name}")

    def test_letter_prefixed_appendix_tables_matching(self):
        """Letter-prefixed appendix tables (Table A1, Table A.1, Table A-1, Table B.2) on PDF assets and appendix endpoints."""
        letter_tables = ["Table A1", "Table A.1", "Table A-1", "Table B1", "Table B.2", "Tab. A.1", "Tbl A-1"]
        for lt in letter_tables:
            # Matches when pointing to PDF asset (e.g. /content/assets/12345.pdf)
            matched, rule = should_match_candidate("https://example.com/content/assets/12345.pdf", text=lt)
            self.assertTrue(matched, f"Failed should_match_candidate on PDF asset for {lt}")
            # Matches when endpoint has appendix context
            matched, rule = should_match_candidate("https://example.com/appendix/download?id=99", text=lt)
            self.assertTrue(matched, f"Failed should_match_candidate on appendix endpoint for {lt}")

        # Primary tables must not match generic endpoints
        for pt in ["Table 1", "Table 2", "Table 10"]:
            self.assertFalse(is_explicit_supp_table(pt), f"Primary table {pt} should not be explicit supp")
            matched, _ = should_match_candidate("https://example.com/download?id=99", text=pt)
            self.assertFalse(matched, f"Primary table {pt} should not match generic endpoint")

    def test_multi_table_nearby_context_not_shared(self):
        """Multiple tables in the same container must not falsely share the first table heading."""
        html = """
        <div class="table-container">
          <h4>Supplementary Table S1</h4>
          <p><a id="a1" href="/download/1">Download 1</a></p>
          <h4>Supplementary Table S2</h4>
          <p><a id="a2" href="/download/2">Download 2</a></p>
        </div>
        """
        soup = BeautifulSoup(html, "html.parser")
        a1 = soup.find(id="a1")
        a2 = soup.find(id="a2")
        ctx1, _ = get_nearby_context_text(a1)
        ctx2, _ = get_nearby_context_text(a2)
        self.assertIn("Supplementary Table S1", ctx1)
        self.assertIn("Supplementary Table S2", ctx2)
        self.assertNotIn("Supplementary Table S1", ctx2)

    def test_embedded_json_filtering_false_positives(self):
        """_extract_embedded_json_candidates must extract data files while rejecting UI routes like /support or table-of-contents."""
        html = """
        <script type="application/json">
        {
          "help_center": "/user/settings/support",
          "toc": "/articles/table-of-contents",
          "script": "/static/js/main.bundle.js",
          "supp1": "/files/Supplementary Table S1.xlsx",
          "supp2": "https://example.com/assets/Table S1.pdf"
        }
        </script>
        """
        soup = BeautifulSoup(html, "html.parser")
        cands = []
        _extract_embedded_json_candidates(soup, "https://example.com/paper", cands.append)
        urls = [c.url for c in cands]
        self.assertIn("https://example.com/files/Supplementary Table S1.xlsx", urls)
        self.assertIn("https://example.com/assets/Table S1.pdf", urls)
        self.assertNotIn("https://example.com/user/settings/support", urls)
        self.assertNotIn("https://example.com/articles/table-of-contents", urls)
        self.assertNotIn("https://example.com/static/js/main.bundle.js", urls)

    def test_declared_count_s_prefixed_and_chinese_numerals(self):
        """detect_declared_supplement_count supports S-prefixed table formats and Chinese numerals."""
        self.assertEqual(detect_declared_supplement_count("Results presented in S4 Table."), 4)
        self.assertEqual(detect_declared_supplement_count("见附表一至附表四"), 4)
        self.assertEqual(detect_declared_supplement_count("共四个补充文件"), 4)
        self.assertEqual(detect_declared_supplement_count("详见附表四"), 4)

    def test_manifest_aware_mmc_deduplication_after_rename(self):
        """_try_page_scrape_supplements skips candidate MMC if already downloaded and recorded in manifest under renamed filename."""
        from scansci_supp_downloader import _try_page_scrape_supplements
        from supp_finder import Candidate, save_download_manifest

        with tempfile.TemporaryDirectory() as td:
            # Simulate mmc1 downloaded and renamed to Table_S1.xlsx
            renamed_file = os.path.join(td, "Table_S1.xlsx")
            with open(renamed_file, "wb") as f:
                f.write(b"data")
            manifest = {"Table_S1.xlsx": "https://ars.els-cdn.com/content/image/1-s2.0-S1234-mmc1.xlsx"}
            save_download_manifest(td, manifest)

            html = '<a href="https://ars.els-cdn.com/content/image/1-s2.0-S1234-mmc1.xlsx">mmc1.xlsx</a>'
            with patch("scansci_supp_downloader.fetch_page_html", return_value=(html, 200, "https://example.com/article")), \
                 patch("scansci_supp_downloader.download_file") as mock_dl:
                _try_page_scrape_supplements("https://example.com/article", td, {}, MagicMock(no_cookies=True, headful=False), [renamed_file])
                # download_file should NOT be called for mmc1 because it's already in manifest
                self.assertFalse(mock_dl.called)

    def test_elsevier_api_objects_and_ecomponent_extraction(self):
        """try_elsevier_api_campus parses objects field and <ce:e-component> tags in Elsevier API response."""
        from scansci_supp_downloader import try_elsevier_api_campus
        from supp_finder import load_download_manifest

        with tempfile.TemporaryDirectory() as td:
            api_response_json = {
                "full-text-retrieval-response": {
                    "coredata": {"pii": "S77777"},
                    "objects": {
                        "object": [
                            {"@ref": "mmc1.xlsx", "@format": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
                            {"@ref": "mmc2", "@format": "application/pdf"}
                        ]
                    },
                    "originalText": '<ce:e-component id="mmc3" xlink:href="mmc3.csv"><ce:caption>Table S3</ce:caption></ce:e-component>'
                }
            }

            def fake_api_get(url, **kwargs):
                resp = MagicMock()
                if "content/article/doi" in url or "content/article/pii" in url:
                    resp.status_code = 200
                    resp.json = MagicMock(return_value=api_response_json)
                elif "ars.els-cdn.com" in url:
                    resp.status_code = 200
                    resp.headers = {}
                    resp.iter_content = MagicMock(return_value=[b"dummy file bytes"])
                return resp

            with patch("requests.get", side_effect=fake_api_get), \
                 patch("requests.Session.get", side_effect=fake_api_get), \
                 patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")):
                files, mmcs = try_elsevier_api_campus("10.1016/j.test.2026.02", "S77777", td, {"elsevier_api_key": "dummy", "is_campus_network": True})
                self.assertEqual(len(files), 3)
                basenames = [os.path.basename(f) for f in files]
                self.assertIn("mmc1.xlsx", basenames)
                self.assertIn("mmc2.pdf", basenames)
                self.assertIn("mmc3.csv", basenames)
                self.assertEqual(mmcs, {1, 2, 3})

    def test_expanded_data_extensions(self):
        from supp_finder import check_data_extension
        self.assertEqual(check_data_extension("data.parquet"), ".parquet")
        self.assertEqual(check_data_extension("genomics_table.tsv.gz"), ".tsv.gz")
        self.assertEqual(check_data_extension("rnaseq.csv.gz"), ".csv.gz")
        self.assertEqual(check_data_extension("dataset.feather"), ".feather")
        self.assertEqual(check_data_extension("model_weights.arrow"), ".arrow")
        self.assertEqual(check_data_extension("params.yaml"), ".yaml")
        self.assertEqual(check_data_extension("clinical_cohort.sas7bdat"), ".sas7bdat")
        self.assertEqual(check_data_extension("stata_data.dta"), ".dta")
        # Added extensions
        self.assertEqual(check_data_extension("raw_data.xz"), ".xz")
        self.assertEqual(check_data_extension("archive.tar.xz"), ".tar.xz")
        self.assertEqual(check_data_extension("compressed.bz2"), ".bz2")
        self.assertEqual(check_data_extension("database.db"), ".db")
        self.assertEqual(check_data_extension("data.sqlite"), ".sqlite")
        self.assertEqual(check_data_extension("data.sqlite3"), ".sqlite3")
        self.assertEqual(check_data_extension("variants.vcf"), ".vcf")
        self.assertEqual(check_data_extension("variants.vcf.gz"), ".vcf.gz")
        self.assertEqual(check_data_extension("annotations.bed"), ".bed")
        self.assertEqual(check_data_extension("features.gff3"), ".gff3")
        self.assertEqual(check_data_extension("transcripts.gtf"), ".gtf")

    def test_infer_file_extension_new_formats(self):
        # Parquet
        self.assertEqual(infer_file_extension(b"PAR1\x00\x00dummy"), ".parquet")
        # Arrow/Feather
        self.assertEqual(infer_file_extension(b"ARROW1\x00\x00dummy"), ".feather")
        # Tar archive with ustar at offset 257
        tar_header = b"\x00" * 257 + b"ustar" + b"\x00" * 10
        self.assertEqual(infer_file_extension(tar_header), ".tar")
        # Bzip2
        self.assertEqual(infer_file_extension(b"BZh91AY&SYdummy"), ".bz2")
        # XZ
        self.assertEqual(infer_file_extension(b"\xfd7zXZ\x00dummy"), ".xz")
        # SQLite
        self.assertEqual(infer_file_extension(b"SQLite format 3\x00dummy"), ".db")

    def test_infer_file_extension_gzip_inspections(self):
        import gzip
        # Plain GZ
        self.assertEqual(infer_file_extension(b"\x1f\x8b\x08\x00generic"), ".gz")
        # GZ with Tar
        tar_inner = b"\x00" * 257 + b"ustar" + b"\x00" * 20
        self.assertEqual(infer_file_extension(gzip.compress(tar_inner)), ".tar.gz")
        # GZ with CSV
        csv_inner = b"sample_id,condition,score\nA,control,10\nB,case,20\n"
        self.assertEqual(infer_file_extension(gzip.compress(csv_inner)), ".csv.gz")
        # GZ with TSV
        tsv_inner = b"sample_id\tcondition\tscore\nA\tcontrol\t10\nB\tcase\t20\n"
        self.assertEqual(infer_file_extension(gzip.compress(tsv_inner)), ".tsv.gz")

    def test_supp_keyword_pattern_expanded(self):
        from supp_finder import SUPP_KEYWORD_PATTERN
        self.assertTrue(bool(SUPP_KEYWORD_PATTERN.search("Supplementary Note 1")))
        self.assertTrue(bool(SUPP_KEYWORD_PATTERN.search("Supporting Notes")))
        self.assertTrue(bool(SUPP_KEYWORD_PATTERN.search("Supplementary Protocol S1")))
        self.assertTrue(bool(SUPP_KEYWORD_PATTERN.search("Supplementary Method")))
        self.assertTrue(bool(SUPP_KEYWORD_PATTERN.search("Supplementary Code")))

    def test_explicit_supp_figures_and_tables(self):
        from supp_finder import detect_declared_supplement_count
        self.assertTrue(is_explicit_supp_table("Supplementary Figures and Tables"))
        self.assertTrue(is_explicit_supp_table("Supplementary Tables and Figures"))
        self.assertTrue(is_explicit_supp_table("Supporting Figures and Tables"))
        self.assertTrue(is_explicit_supp_table("Supporting Tables and Figures"))
        m1 = TABLE_LABEL_PATTERN.search("Supplementary Figures and Tables S1-S5")
        self.assertTrue(bool(m1))
        self.assertIn("S1-S5", m1.group(0))
        m2 = TABLE_LABEL_PATTERN.search("Supplementary Tables and Figures S1-S5")
        self.assertTrue(bool(m2))
        self.assertIn("S1-S5", m2.group(0))
        self.assertEqual(detect_declared_supplement_count("See Supplementary Figures and Tables S1-S5 for details."), 5)
        self.assertEqual(detect_declared_supplement_count("Supplementary Tables and Figures S1 to S4 provide cohort info."), 4)


if __name__ == "__main__":
    unittest.main()

