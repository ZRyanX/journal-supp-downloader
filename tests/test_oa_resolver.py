"""
Unit and Integration Tests for Open Access Resolver (Unpaywall & OpenAlex)
==========================================================================
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure scripts directory is on sys.path
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scripts_path = os.path.join(repo_root, "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

from oa_resolver import (
    extract_doi,
    is_doi,
    _score_oa_url,
    query_unpaywall,
    query_openalex,
    resolve_oa_info,
    OAResult,
)


class TestOAResolver(unittest.TestCase):

    def test_extract_doi(self):
        """Test extraction of DOI from various URL formats and messy strings."""
        self.assertEqual(extract_doi("10.1016/j.oregeorev.2022.104949"), "10.1016/j.oregeorev.2022.104949")
        self.assertEqual(extract_doi("https://doi.org/10.1038/s41586-020-2649-2"), "10.1038/s41586-020-2649-2")
        self.assertEqual(extract_doi("http://dx.doi.org/10.1126/science.1234567"), "10.1126/science.1234567")
        self.assertEqual(extract_doi("DOI: 10.1002/anie.202300123."), "10.1002/anie.202300123")
        self.assertEqual(extract_doi("(10.1038/nature12345)"), "10.1038/nature12345")
        # Edge cases: URL encoded slashes and trailing slashes
        self.assertEqual(extract_doi("https://doi.org/10.1016%2Fj.cell.2020.08.012"), "10.1016/j.cell.2020.08.012")
        self.assertEqual(extract_doi("https://doi.org/10.1038/s41586-020-2649-2/"), "10.1038/s41586-020-2649-2")
        self.assertIsNone(extract_doi("https://example.com/not-a-doi"))
        self.assertIsNone(extract_doi(""))

    def test_extract_doi_from_html(self):
        """Test DOI extraction from HTML meta tags or header metadata."""
        from oa_resolver import extract_doi_from_html
        html = '<meta name="citation_doi" content="10.1038/s41586-020-2649-2">'
        self.assertEqual(extract_doi_from_html(html), "10.1038/s41586-020-2649-2")
        html_dc = '<meta property="dc.identifier" content="doi:10.1016/j.cell.2020.08.012">'
        self.assertEqual(extract_doi_from_html(html_dc), "10.1016/j.cell.2020.08.012")
        self.assertIsNone(extract_doi_from_html("<html><body>No doi here</body></html>"))

    def test_is_doi(self):
        """Test DOI detection boolean."""
        self.assertTrue(is_doi("10.1016/j.oregeorev.2022.104949"))
        self.assertTrue(is_doi("https://doi.org/10.1038/s41586-020-2649-2"))
        self.assertTrue(is_doi("https://doi.org/10.1016%2Fj.cell.2020.08.012"))
        self.assertFalse(is_doi("https://www.nature.com/articles/s41586-020-2649-2"))
        self.assertFalse(is_doi("random string"))

    def test_score_oa_url(self):
        """Verify repository ranking: PMC > Europe PMC > Zenodo > others."""
        pmc_score = _score_oa_url("https://pmc.ncbi.nlm.nih.gov/articles/PMC12345/")
        ncbi_score = _score_oa_url("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC12345/")
        eur_score = _score_oa_url("https://europepmc.org/article/PMC/12345")
        zenodo_score = _score_oa_url("https://zenodo.org/record/12345")
        arxiv_score = _score_oa_url("https://arxiv.org/abs/2006.10256")
        ir_score = _score_oa_url("https://eprints.university.edu/paper")
        generic_score = _score_oa_url("https://example.com/paper")

        self.assertEqual(pmc_score, 100)
        self.assertEqual(ncbi_score, 100)
        self.assertEqual(eur_score, 80)
        self.assertEqual(zenodo_score, 60)
        self.assertEqual(arxiv_score, 40)
        self.assertEqual(ir_score, 30)
        self.assertEqual(generic_score, 0)

    @patch("oa_resolver.requests.get")
    def test_query_unpaywall_success(self, mock_get):
        """Test Unpaywall API parsing with PMC repository location."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "doi": "10.1038/s41586-020-2649-2",
            "title": "Array programming with NumPy",
            "is_oa": True,
            "oa_status": "hybrid",
            "best_oa_location": {
                "url_for_landing_page": "https://www.nature.com/articles/s41586-020-2649-2",
                "host_type": "publisher",
            },
            "oa_locations": [
                {
                    "url_for_landing_page": "https://doi.org/10.1038/s41586-020-2649-2",
                    "host_type": "publisher",
                },
                {
                    "url_for_landing_page": "https://pmc.ncbi.nlm.nih.gov/articles/PMC7759461/",
                    "host_type": "repository",
                },
                {
                    "url_for_landing_page": "https://europepmc.org/articles/PMC7759461",
                    "host_type": "repository",
                },
            ],
            "publisher": "Springer Nature",
        }
        mock_get.return_value = mock_resp

        result = query_unpaywall("10.1038/s41586-020-2649-2")
        self.assertIsNotNone(result)
        self.assertEqual(result.doi, "10.1038/s41586-020-2649-2")
        self.assertEqual(result.title, "Array programming with NumPy")
        self.assertTrue(result.is_oa)
        self.assertEqual(result.publisher_url, "https://www.nature.com/articles/s41586-020-2649-2")
        self.assertTrue(result.has_oa_mirror)
        # PMC should be prioritized first
        self.assertEqual(result.oa_landing_urls[0], "https://pmc.ncbi.nlm.nih.gov/articles/PMC7759461/")
        self.assertIn("https://europepmc.org/articles/PMC7759461", result.oa_landing_urls)

    @patch("oa_resolver.requests.get")
    def test_query_unpaywall_error(self, mock_get):
        """Test Unpaywall API handling of HTTP errors or closed access."""
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_get.return_value = mock_resp

        result = query_unpaywall("10.1000/nonexistent")
        self.assertIsNone(result)

    @patch("oa_resolver.requests.get")
    def test_query_openalex_success(self, mock_get):
        """Test OpenAlex API parsing."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "https://openalex.org/W3083651234",
            "doi": "https://doi.org/10.1038/s41586-020-2649-2",
            "title": "Array programming with NumPy",
            "open_access": {
                "is_oa": True,
                "oa_status": "hybrid",
            },
            "primary_location": {
                "landing_page_url": "https://www.nature.com/articles/s41586-020-2649-2",
            },
            "locations": [
                {"landing_page_url": "https://doi.org/10.1038/s41586-020-2649-2"},
                {"landing_page_url": "https://europepmc.org/pmc/articles/PMC7759461"},
                {"landing_page_url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC7759461"},
            ],
        }
        mock_get.return_value = mock_resp

        result = query_openalex("10.1038/s41586-020-2649-2")
        self.assertIsNotNone(result)
        self.assertEqual(result.doi, "10.1038/s41586-020-2649-2")
        self.assertEqual(result.title, "Array programming with NumPy")
        self.assertTrue(result.is_oa)
        self.assertEqual(result.source, "openalex")
        # PMC should be ranked first
        self.assertEqual(result.oa_landing_urls[0], "https://pmc.ncbi.nlm.nih.gov/articles/PMC7759461")

    @patch("oa_resolver.query_unpaywall")
    @patch("oa_resolver.query_openalex")
    def test_resolve_oa_info_fallback(self, mock_openalex, mock_unpaywall):
        """Verify fallback from Unpaywall to OpenAlex."""
        mock_unpaywall.return_value = None
        mock_openalex.return_value = OAResult(
            doi="10.1016/j.test.2023",
            title="Fallback Title",
            is_oa=True,
            source="openalex",
            oa_landing_urls=["https://europepmc.org/article/PMC/999"],
        )

        res = resolve_oa_info("10.1016/j.test.2023")
        self.assertEqual(res.source, "openalex")
        self.assertEqual(res.title, "Fallback Title")
        self.assertEqual(len(res.oa_landing_urls), 1)

    @patch("oa_resolver.requests.get")
    def test_query_unpaywall_url_fallback(self, mock_get):
        """When url_for_landing_page is missing/null, query_unpaywall should fall back to url."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "doi": "10.1000/182",
            "title": "Test Paper",
            "is_oa": True,
            "oa_locations": [
                {
                    "url_for_landing_page": None,
                    "url": "https://europepmc.org/articles/PMC12345",
                    "host_type": "repository",
                }
            ]
        }
        mock_get.return_value = mock_resp
        res = query_unpaywall("10.1000/182")
        self.assertIsNotNone(res)
        self.assertIn("https://europepmc.org/articles/PMC12345", res.oa_landing_urls)

    @patch("oa_resolver.query_unpaywall")
    @patch("oa_resolver.query_openalex")
    def test_resolve_oa_info_cross_rescue_merge(self, mock_openalex, mock_unpaywall):
        """When primary resolver finds no OA mirrors, fallback resolver should rescue and merge."""
        # Unpaywall returns metadata but 0 OA mirrors
        mock_unpaywall.return_value = OAResult(
            doi="10.1038/s41586-020-2649-2",
            title="Array programming with NumPy",
            is_oa=False,
            oa_landing_urls=[],
            source="unpaywall",
        )
        # OpenAlex discovers PMC mirror
        mock_openalex.return_value = OAResult(
            doi="10.1038/s41586-020-2649-2",
            title="Array programming with NumPy",
            is_oa=True,
            oa_landing_urls=["https://pmc.ncbi.nlm.nih.gov/articles/PMC7759461/"],
            source="openalex",
        )

        res = resolve_oa_info("10.1038/s41586-020-2649-2")
        self.assertTrue(res.is_oa)
        self.assertEqual(len(res.oa_landing_urls), 1)
        self.assertEqual(res.oa_landing_urls[0], "https://pmc.ncbi.nlm.nih.gov/articles/PMC7759461/")


if __name__ == "__main__":
    unittest.main()
