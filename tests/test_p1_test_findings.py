"""P1 regression tests from the v1.8.0 50-paper live test (2026-11-12).

Cause A in ~/workspace/jsd-50-paper-test-report.md: supplementary_table_ids
read DOI path segments (s41467-025-61134-0) as S41467/S25/S61134/S0, which
polluted the declared-table set and made completeness warnings meaningless
(every Nature paper reported hundreds of "missing" tables).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from supp_finder import detect_declared_table_range, supplementary_table_ids


def test_doi_url_yields_no_table_ids():
    assert supplementary_table_ids("https://doi.org/10.1038/s41467-025-61134-0") == set()


def test_bare_doi_yields_no_table_ids():
    assert supplementary_table_ids("10.1038/s41467-025-61134-0") == set()
    assert supplementary_table_ids("10.1002/advs.202500798") == set()


def test_doi_in_sentence_does_not_block_real_table():
    text = "See https://doi.org/10.1038/s41467-025-61134-0. Supplementary Table S1 is provided."
    assert supplementary_table_ids(text) == {"S1"}


def test_year_range_does_not_inherit_table_namespace():
    assert supplementary_table_ids("Table S1, 2020-2021 data") == {"S1"}


def test_zero_is_not_a_table_id():
    assert supplementary_table_ids("Table S0") == set()


def test_oversized_numbers_are_not_table_ids():
    # Article numbers / accession-like numbers must not become table ids.
    assert supplementary_table_ids("Table S202500798") == set()
    assert supplementary_table_ids("see Table E36269 for details") == set()


def test_legitimate_ranges_and_lists_still_work():
    assert supplementary_table_ids("Tables S1-S3") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("S1-S3") == {"S1", "S2", "S3"}
    assert supplementary_table_ids("Supplementary Tables 2-5") == {"S2", "S3", "S4", "S5"}
    assert supplementary_table_ids("Appendix A1-A3") == {"A1", "A2", "A3"}
    assert supplementary_table_ids("Extended Data Tables 1-3") == {"E1", "E2", "E3"}
    assert supplementary_table_ids("Table S1.1 and Table S1.2") == {"S1.1", "S1.2"}
    assert supplementary_table_ids("Tables S1, S2, and S3") == {"S1", "S2", "S3"}


def test_declared_range_ignores_doi_link_in_html():
    html = (
        '<a href="https://doi.org/10.1038/s41467-025-61134-0">doi</a>'
        "<p>Tables S1-S3 in Supplementary Information</p>"
    )
    assert detect_declared_table_range(html) == {"S1", "S2", "S3"}


def test_download_failure_prints_reason(tmp_path, capsys, monkeypatch):
    import scansci_supp_downloader as a

    monkeypatch.setattr(a, "HAS_SCRAPLING", False)

    class BoomSession:
        def get(self, *args, **kwargs):
            raise ConnectionError("boom-503")

    class FakeRun:
        returncode = 7

    monkeypatch.setattr(a.subprocess, "run", lambda *args, **kwargs: FakeRun())
    result = a.download_file(
        "http://127.0.0.1:9/nope.xlsx", str(tmp_path), session=BoomSession()
    )
    out = capsys.readouterr().out
    assert result is None
    assert "FAIL (requests: boom-503)" in out
