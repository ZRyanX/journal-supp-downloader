"""
Unit Tests for Microsoft MarkItDown Integration
================================================
Tests file convertibility, graceful degradation when markitdown is absent,
and simulated file conversion.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure scripts directory is on sys.path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scripts_path = os.path.join(repo_root, "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

from markitdown_converter import (
    is_convertible_file,
    is_markitdown_available,
    convert_file_to_markdown,
    convert_directory_supplements,
    CONVERTIBLE_EXTENSIONS,
)


class TestMarkItDownConverter(unittest.TestCase):

    def test_is_convertible_file(self):
        """Check supported file extensions for supplementary Markdown conversion."""
        self.assertTrue(is_convertible_file("supplement_s1.docx"))
        self.assertTrue(is_convertible_file("table_s2.xlsx"))
        self.assertTrue(is_convertible_file("dataset.csv"))
        self.assertTrue(is_convertible_file("slides.pptx"))
        self.assertTrue(is_convertible_file("appendix.pdf"))
        self.assertTrue(is_convertible_file("raw_data.zip"))
        self.assertFalse(is_convertible_file("structure.cif"))
        self.assertFalse(is_convertible_file("sequence.fasta"))
        self.assertFalse(is_convertible_file("already_converted.md"))
        self.assertFalse(is_convertible_file(""))

    def test_graceful_degradation_when_not_installed(self):
        """When markitdown is not installed, converter logs notice and returns None without crash."""
        with patch("markitdown_converter.is_markitdown_available", return_value=False):
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tf:
                tf.write(b"fake xlsx data")
                temp_path = tf.name

            try:
                res = convert_file_to_markdown(temp_path)
                self.assertIsNone(res)
                converted_list = convert_directory_supplements(os.path.dirname(temp_path))
                self.assertEqual(converted_list, [])
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

    def test_mock_conversion_creates_markdown_sidecar(self):
        """When markitdown is available, convert_file_to_markdown writes sidecar .md without modifying original."""
        with tempfile.TemporaryDirectory() as td:
            dummy_file = os.path.join(td, "Table_S1.xlsx")
            with open(dummy_file, "wb") as f:
                f.write(b"fake spreadsheet bytes")

            mock_result = MagicMock()
            mock_result.text_content = "| Col1 | Col2 |\n|---|---|\n| Val1 | Val2 |"

            mock_md_instance = MagicMock()
            mock_md_instance.convert.return_value = mock_result

            mock_md_class = MagicMock(return_value=mock_md_instance)

            with patch("markitdown_converter.is_markitdown_available", return_value=True):
                with patch.dict("sys.modules", {"markitdown": MagicMock(MarkItDown=mock_md_class)}):
                    res = convert_file_to_markdown(dummy_file)
                    self.assertIsNotNone(res)
                    self.assertIn("| Col1 | Col2 |", res)

                    # Original file must be 100% intact!
                    self.assertTrue(os.path.exists(dummy_file))
                    with open(dummy_file, "rb") as f:
                        self.assertEqual(f.read(), b"fake spreadsheet bytes")

                    # Sidecar .md file must exist
                    sidecar = f"{dummy_file}.md"
                    self.assertTrue(os.path.exists(sidecar))
                    with open(sidecar, "r", encoding="utf-8") as f:
                        self.assertIn("Val1", f.read())

    def test_batch_convert_directory_and_summary_index(self):
        """Test batch conversion of a folder and creation of SUPPLEMENTS_MARKDOWN_SUMMARY.md."""
        with tempfile.TemporaryDirectory() as td:
            file1 = os.path.join(td, "Table_S1.xlsx")
            file2 = os.path.join(td, "Supp_Info.docx")
            file3 = os.path.join(td, "ignored.txt")
            with open(file1, "wb") as f:
                f.write(b"data1")
            with open(file2, "wb") as f:
                f.write(b"data2")
            with open(file3, "wb") as f:
                f.write(b"plain text")

            mock_result = MagicMock()
            mock_result.text_content = "# Converted Markdown Content"

            mock_md_instance = MagicMock()
            mock_md_instance.convert.return_value = mock_result

            with patch("markitdown_converter.is_markitdown_available", return_value=True):
                with patch.dict("sys.modules", {"markitdown": MagicMock(MarkItDown=MagicMock(return_value=mock_md_instance))}):
                    converted = convert_directory_supplements(td)
                    self.assertEqual(len(converted), 2)

                    summary_index = os.path.join(td, "SUPPLEMENTS_MARKDOWN_SUMMARY.md")
                    self.assertTrue(os.path.exists(summary_index))
                    with open(summary_index, "r", encoding="utf-8") as f:
                        content = f.read()
                        self.assertIn("Table_S1.xlsx", content)
                        self.assertIn("Supp_Info.docx", content)

    def test_internal_manifests_are_never_converted(self):
        """Tool internal files like download_manifest.json must NOT be converted to markdown."""
        with tempfile.TemporaryDirectory() as td:
            manifest_file = os.path.join(td, "download_manifest.json")
            cookies_file = os.path.join(td, "cookies.json")
            real_file = os.path.join(td, "Table_S1.xlsx")
            with open(manifest_file, "w") as f:
                f.write('{"Table_S1.xlsx": "https://example.com/Table_S1.xlsx"}')
            with open(cookies_file, "w") as f:
                f.write('[]')
            with open(real_file, "wb") as f:
                f.write(b"data")

            self.assertFalse(is_convertible_file("download_manifest.json"))
            self.assertFalse(is_convertible_file("cookies.json"))

            mock_result = MagicMock()
            mock_result.text_content = "| Col | Val |"
            mock_md_instance = MagicMock()
            mock_md_instance.convert.return_value = mock_result

            with patch("markitdown_converter.is_markitdown_available", return_value=True):
                with patch.dict("sys.modules", {"markitdown": MagicMock(MarkItDown=MagicMock(return_value=mock_md_instance))}):
                    converted = convert_directory_supplements(td)
                    # Only the real file should be converted
                    self.assertEqual(len(converted), 1)
                    self.assertEqual(os.path.basename(converted[0][0]), "Table_S1.xlsx")
                    # Manifests must not have sidecars
                    self.assertFalse(os.path.exists(os.path.join(td, "download_manifest.json.md")))
                    self.assertFalse(os.path.exists(os.path.join(td, "cookies.json.md")))

    def test_reuse_existing_markdown_sidecar_without_reconversion(self):
        """Existing sidecars should be reused unless overwrite=True."""
        with tempfile.TemporaryDirectory() as td:
            real_file = os.path.join(td, "Table_S1.xlsx")
            sidecar = f"{real_file}.md"
            with open(real_file, "wb") as f:
                f.write(b"data")
            with open(sidecar, "w", encoding="utf-8") as f:
                f.write("# Existing Content")

            mock_md = MagicMock()
            with patch("markitdown_converter.is_markitdown_available", return_value=True):
                with patch("markitdown_converter.convert_file_to_markdown") as mock_conv:
                    converted = convert_directory_supplements(td, overwrite=False)
                    self.assertEqual(len(converted), 1)
                    # convert_file_to_markdown should NOT have been called
                    mock_conv.assert_not_called()

    def test_html_and_htm_convertibility(self):
        """HTML files (.html, .htm) must be supported by markitdown converter."""
        self.assertTrue(is_convertible_file("table_s1.html"))
        self.assertTrue(is_convertible_file("table_s2.htm"))

    def test_large_file_size_limit_guard(self):
        """Files exceeding MAX_CONVERTIBLE_FILE_SIZE (25MB) should be skipped to prevent OOM."""
        with tempfile.TemporaryDirectory() as td:
            big_file = os.path.join(td, "huge_dataset.csv")
            with open(big_file, "wb") as f:
                f.write(b"small placeholder")

            with patch("os.path.getsize", return_value=26 * 1024 * 1024):
                with patch("markitdown_converter.is_markitdown_available", return_value=True):
                    res = convert_file_to_markdown(big_file)
                    self.assertIsNone(res)

    def test_converter_instance_reused_in_directory_conversion(self):
        """Batch conversion should initialize MarkItDown once, not per file."""
        with tempfile.TemporaryDirectory() as td:
            f1 = os.path.join(td, "Table1.xlsx")
            f2 = os.path.join(td, "Table2.xlsx")
            with open(f1, "wb") as f:
                f.write(b"d1")
            with open(f2, "wb") as f:
                f.write(b"d2")

            mock_md_instance = MagicMock()
            mock_res = MagicMock()
            mock_res.text_content = "# Converted"
            mock_md_instance.convert.return_value = mock_res

            mock_class = MagicMock(return_value=mock_md_instance)
            with patch("markitdown_converter.is_markitdown_available", return_value=True):
                with patch.dict("sys.modules", {"markitdown": MagicMock(MarkItDown=mock_class)}):
                    converted = convert_directory_supplements(td)
                    self.assertEqual(len(converted), 2)
                    # Class constructor should be invoked only ONCE across all files
                    self.assertEqual(mock_class.call_count, 1)


if __name__ == "__main__":
    unittest.main()

