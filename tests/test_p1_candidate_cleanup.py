"""P1-B regression tests: candidate cleanup and URL/filename dedup.

Cause B in ~/workspace/jsd-50-paper-test-p1-report.md (and the first-round
report): PLOS Biology papers yielded 104-160 candidates each; the same real
file was downloaded 2-3 times in one run (#26's 18 s-files each downloaded
twice as ``_2`` copies, #46's MOESM x3, #36's Appendix 7 x18); and junk
candidates (GEO acc.cgi, CC 4.0 licence pages, CRAN package pages,
Table/Appendix .html navigation pages, bioRxiv preprint attachments,
Client Challenge pages) burned the already-tight time budget.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from supp_finder import (  # noqa: E402
    Candidate,
    dedupe_candidates,
    filename_dedup_key,
    find_all_candidates,
    _normalize_url_for_dedup,
)

PMC46 = "https://pmc.ncbi.nlm.nih.gov/articles/PMC12185701/"
PMC36 = "https://pmc.ncbi.nlm.nih.gov/articles/PMC12208662/"


def _supp_section(*links):
    return "<html><body><section class='supplementary-materials'>" + "".join(links) + "</section></body></html>"


# ── Dedup: real cases from the 50-paper test ────────────────────────────────

def test_p46_same_moesm_via_three_urls_merges_to_one():
    """#46: MOESM1 via Springer static-content, PMC :443 /bin/ and PMC /bin/."""
    html = _supp_section(
        '<a href="https://static-content.springer.com/esm/art%3A10.1038%2Fs42003-025-08387-z/MediaObjects/42003_2025_8387_MOESM1_ESM.pdf">Table S1</a>',
        '<a href="https://pmc.ncbi.nlm.nih.gov:443/articles/instance/12185701/bin/42003_2025_8387_MOESM1_ESM.pdf">Table S2</a>',
        '<a href="https://pmc.ncbi.nlm.nih.gov/articles/instance/12185701/bin/42003_2025_8387_MOESM1_ESM.pdf">42003_2025_8387_MOESM1_ESM.pdf</a>',
    )
    diag = find_all_candidates(html, PMC46)
    assert len(diag.candidates) == 1
    cand = diag.candidates[0]
    assert cand.filename == "42003_2025_8387_MOESM1_ESM.pdf"
    assert cand.url.startswith("https://static-content.springer.com/")
    # H6: the merged candidate keeps every table id the mirrors named.
    assert {"S1", "S2"} <= cand.covered_table_ids


def test_p36_appendix7_eighteen_links_merge_to_one():
    """#36: 18 /table/app7tableN/ links all labelled 'Appendix 7' -> 1."""
    rows = "".join(
        f'<div class="suppl"><p>Appendix 7</p>'
        f'<a href="/articles/PMC12208662/table/app7table{i}/">Open in a new tab</a></div>'
        for i in range(1, 19)
    )
    html = f"<html><body><section id='supplementary-materials'>{rows}</section></body></html>"
    diag = find_all_candidates(html, PMC36)
    assert len(diag.candidates) == 1
    assert diag.candidates[0].filename == "Appendix 7.bin"


def test_p26_duplicate_suffix_filename_merges():
    """#26: same s001 file under two mirror URLs, one already _2-suffixed."""
    html = _supp_section(
        '<a href="https://example.com/mirror1/journal.pone.0327011.s001.docx" download="journal.pone.0327011.s001.docx">S1 Table</a>',
        '<a href="https://example.com/mirror2/journal.pone.0327011.s001.docx" download="journal.pone.0327011.s001_2.docx">S1 Table</a>',
    )
    diag = find_all_candidates(html, "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0327011")
    assert len(diag.candidates) == 1
    assert diag.candidates[0].filename == "journal.pone.0327011.s001.docx"


def test_filename_dedup_key_strips_dup_suffix():
    assert filename_dedup_key("MOESM1_ESM_2.pdf") == filename_dedup_key("MOESM1_ESM.pdf")
    assert filename_dedup_key("Appendix 7_18.bin") == filename_dedup_key("Appendix 7.bin")
    assert filename_dedup_key("Table 1_2.HTML") == filename_dedup_key("table 1.html")
    assert filename_dedup_key("") is None


def test_filename_dedup_key_keeps_distinct_numbered_files():
    assert filename_dedup_key("journal.pone.0327011.s001.docx") != filename_dedup_key("journal.pone.0327011.s002.docx")
    assert filename_dedup_key("mmc1.xlsx") != filename_dedup_key("mmc2.xlsx")
    assert filename_dedup_key("S1.xlsx") != filename_dedup_key("S1.pdf")
    # Wiley-style attachment ids are real identifiers, not dup suffixes.
    assert filename_dedup_key("attachment_1001") != filename_dedup_key("attachment_1002")


def test_url_normalization_strips_default_port():
    assert _normalize_url_for_dedup(
        "https://pmc.ncbi.nlm.nih.gov:443/articles/instance/12185701/bin/MOESM1_ESM.pdf"
    ) == _normalize_url_for_dedup(
        "https://pmc.ncbi.nlm.nih.gov/articles/instance/12185701/bin/MOESM1_ESM.pdf"
    )


def test_dedupe_candidates_merges_across_sources():
    cands = [
        Candidate(url="https://static-content.springer.com/esm/MOESM1_ESM.pdf",
                  filename="MOESM1_ESM.pdf", covered_table_ids={"S1"}),
        Candidate(url="https://pmc.ncbi.nlm.nih.gov/articles/instance/1/bin/MOESM1_ESM.pdf",
                  filename="MOESM1_ESM.pdf", covered_table_ids={"S2"}),
        Candidate(url="https://pmc.ncbi.nlm.nih.gov/articles/instance/1/bin/MOESM2_ESM.pdf",
                  filename="MOESM2_ESM.pdf"),
    ]
    out = dedupe_candidates(cands)
    assert len(out) == 2
    assert out[0].covered_table_ids == {"S1", "S2"}
    assert [c.filename for c in out] == ["MOESM1_ESM.pdf", "MOESM2_ESM.pdf"]


# ── Junk filters: one case per confirmed pattern ────────────────────────────

def test_geo_acc_cgi_filtered():
    html = _supp_section(
        '<a href="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE268522">acc.cgi</a>',
        '<a href="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE182233">acc.cgi</a>',
        '<a href="https://example.com/S1.xlsx">Table S1</a>',
    )
    diag = find_all_candidates(html, PMC46)
    assert [c.url for c in diag.candidates] == ["https://example.com/S1.xlsx"]
    assert any(reason == "geo_acc_cgi" for _, reason in diag.junk_filtered)


def test_cc_license_pages_filtered():
    html = _supp_section(
        '<a href="https://creativecommons.org/licenses/by/4.0/">Creative Commons Attribution 4.0 International License</a>',
        '<a href="https://creativecommons.org/licenses/by-nc-nd/4.0/">http://creativecommons.org/licenses/by-nc-nd/4.0/</a>',
        '<a href="https://example.com/S1.xlsx">Table S1</a>',
    )
    diag = find_all_candidates(html, PMC46)
    assert [c.url for c in diag.candidates] == ["https://example.com/S1.xlsx"]
    assert any(reason == "cc_license" for _, reason in diag.junk_filtered)


def test_cran_package_pages_filtered():
    html = _supp_section(
        '<a href="https://cran.r-project.org/web/packages/MASS/index.html">https://cran.r-project.org/web/packages/MASS/index.html</a>',
        '<a href="https://cran.r-project.org/web/packages/pheatmap/index.html">pheatmap</a>',
        '<a href="https://cran.r-project.org/">https://cran.r-project.org/</a>',
        '<a href="https://example.com/S1.xlsx">Table S1</a>',
    )
    diag = find_all_candidates(html, PMC46)
    assert [c.url for c in diag.candidates] == ["https://example.com/S1.xlsx"]
    assert any(reason == "cran_package_page" for _, reason in diag.junk_filtered)


def test_table_appendix_html_nav_pages_filtered():
    html = _supp_section(
        '<a href="https://example.com/files/Table%201.html">Table 1</a>',
        '<a href="https://example.com/files/Appendix%207.html">Appendix 7</a>',
        '<a href="https://example.com/S1.xlsx">Table S1</a>',
    )
    diag = find_all_candidates(html, PMC46)
    assert [c.url for c in diag.candidates] == ["https://example.com/S1.xlsx"]
    assert any(reason == "table_appendix_html_nav" for _, reason in diag.junk_filtered)


def test_biorxiv_preprint_attachments_filtered():
    html = _supp_section(
        '<a href="https://www.biorxiv.org/content/10.1101/2024.09.24.614807v1.supplementary-material">supplement</a>',
        '<a href="https://pmc.ncbi.nlm.nih.gov/articles/instance/11463620/bin/NIHPP2024.09.24.614807v1-supplement-1.pdf">NIHPP2024.09.24.614807v1-supplement-1.pdf</a>',
        '<a href="https://example.com/S1.xlsx">Table S1</a>',
    )
    diag = find_all_candidates(html, PMC46)
    assert [c.url for c in diag.candidates] == ["https://example.com/S1.xlsx"]
    assert any(reason == "biorxiv_preprint_attachment" for _, reason in diag.junk_filtered)


def test_client_challenge_page_filtered():
    html = _supp_section(
        '<a href="https://elifesciences.org/articles/101157">Client Challenge</a>',
        '<a href="https://example.com/S1.xlsx">Table S1</a>',
    )
    diag = find_all_candidates(html, PMC36)
    assert [c.url for c in diag.candidates] == ["https://example.com/S1.xlsx"]
    assert any(reason == "client_challenge_page" for _, reason in diag.junk_filtered)


def test_filtered_candidates_are_logged_with_reason(capsys):
    html = _supp_section(
        '<a href="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE268522">acc.cgi</a>',
        '<a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a>',
    )
    diag = find_all_candidates(html, PMC46)
    assert diag.candidates == []
    out = capsys.readouterr().out
    assert "[Filtered]" in out
    assert "geo_acc_cgi" in out
    assert "cc_license" in out
    assert "acc.cgi?acc=GSE268522" in out


def test_real_supplement_files_not_filtered():
    html = _supp_section(
        '<a href="https://example.com/S1.xlsx">Table S1</a>',
        '<a href="https://example.com/data.csv">Supplementary data</a>',
        '<a href="https://example.com/supp.docx">Supplementary Table S1</a>',
        '<a href="https://example.com/supp.pdf">Supplementary Information</a>',
        '<a href="https://example.com/archive.zip">Supplementary data</a>',
        # A CRAN tarball is a real file, not a package page.
        '<a href="https://cran.r-project.org/src/contrib/MASS_7.3-60.tar.gz">MASS package</a>',
        # An explicit supplementary table served as HTML is a real file.
        '<a href="https://example.com/files/Table%20S1.html">Supplementary Table S1</a>',
    )
    diag = find_all_candidates(html, PMC46)
    assert len(diag.candidates) == 7
    assert diag.junk_filtered == []


# ── Download loop: same file downloaded once per run ────────────────────────

class _Mirrors:
    def __init__(self, items=()):
        self.items = list(items)

    def discover(self):
        return list(self.items)


def test_download_loop_downloads_same_filename_once():
    from discovery_state import download_with_recovery

    calls = []

    def download(cand):
        calls.append(cand.url)
        return "/tmp/" + cand.filename

    cands = [
        Candidate(url="https://static-content.springer.com/esm/MOESM1_ESM.pdf", filename="MOESM1_ESM.pdf"),
        Candidate(url="https://pmc.ncbi.nlm.nih.gov/articles/instance/1/bin/MOESM1_ESM.pdf", filename="MOESM1_ESM.pdf"),
        Candidate(url="https://pmc.ncbi.nlm.nih.gov/articles/instance/1/bin/MOESM2_ESM.pdf", filename="MOESM2_ESM.pdf"),
    ]
    ok, attempted = download_with_recovery(cands, download, _Mirrors(), list, sleep=lambda s: None)
    assert calls == [
        "https://static-content.springer.com/esm/MOESM1_ESM.pdf",
        "https://pmc.ncbi.nlm.nih.gov/articles/instance/1/bin/MOESM2_ESM.pdf",
    ]
    assert len(ok) == 2
    assert attempted == 2


def test_download_loop_failed_same_filename_is_reattempted_in_mirror_round():
    """H8 still holds: a *failed* file gets a fresh attempt via its mirror."""
    from discovery_state import download_with_recovery

    calls = []

    def download(cand):
        calls.append(cand.url)
        return (False, "HTTP 429 Too Many Requests")

    cand = Candidate(url="https://example.com/a/S1.xlsx", filename="S1.xlsx")
    mirror = Candidate(url="https://mirror.example/S1.xlsx", filename="S1.xlsx")
    ok, _ = download_with_recovery(
        [cand], download, _Mirrors([mirror]), list, sleep=lambda s: None)
    assert ok == []
    # Round 1 (initial + 1 retry) and mirror round (initial + 1 retry).
    assert calls == [
        "https://example.com/a/S1.xlsx",
        "https://example.com/a/S1.xlsx",
        "https://mirror.example/S1.xlsx",
        "https://mirror.example/S1.xlsx",
    ]


def test_page_scrape_resume_skips_already_downloaded_filename(tmp_path):
    """Resume: a _2 copy on disk + in the manifest counts as already having
    the file, so the mirror URL for the same filename is not re-downloaded."""
    import scansci_supp_downloader as ssd
    from supp_finder import record_manifest_entry, save_download_manifest
    from unittest.mock import MagicMock, patch

    article_dir = str(tmp_path)
    springer_url = "https://static-content.springer.com/esm/MOESM1_ESM.pdf"
    pmc_url = "https://pmc.ncbi.nlm.nih.gov/articles/instance/12185701/bin/42003_2025_8387_MOESM1_ESM.pdf"
    dup_name = "42003_2025_8387_MOESM1_ESM_2.pdf"
    with open(os.path.join(article_dir, dup_name), "wb") as f:
        f.write(b"fake pdf")
    manifest = {}
    record_manifest_entry(manifest, dup_name, springer_url)
    save_download_manifest(article_dir, manifest)
    existing = [os.path.join(article_dir, dup_name)]

    html = _supp_section(
        f'<a href="{pmc_url}">42003_2025_8387_MOESM1_ESM.pdf</a>',
        '<a href="https://pmc.ncbi.nlm.nih.gov/articles/instance/12185701/bin/42003_2025_8387_MOESM2_ESM.pdf">42003_2025_8387_MOESM2_ESM.pdf</a>',
    )
    args = MagicMock()
    args.no_cookies = True
    args.headful = False
    args.skip_cdn = False
    args.max_misses = 4
    args.max_mmc = 25
    args.output_dir = article_dir

    with patch("scansci_supp_downloader.fetch_page_html",
               return_value=(html, 200, PMC46)), \
         patch("scansci_supp_downloader.download_file") as mock_dl, \
         patch("scansci_supp_downloader.try_elsevier_cdn_brute_force", return_value=[]):
        mock_dl.return_value = "/tmp/x"
        ssd._try_page_scrape_supplements(PMC46, article_dir, {}, args, existing)
    assert mock_dl.call_count == 1
    assert mock_dl.call_args[0][0].filename == "42003_2025_8387_MOESM2_ESM.pdf"
