"""P1-G remainder: the final Done line must count Tier A / Tier 0 files.

Round-3 follow-up commit ee81cac fixed the *no-candidates* path: when
Tier A/Tier 0 files were preserved but no candidate list was found, the
run now prints a countable ``✓ Done: N file(s) downloaded via Tier A/CDN
(page completeness unverified)`` line. The *candidates-exist* path was
left behind: the final ``✓ Done: {success_count}/{total_target}`` line
(``scansci_supp_downloader.py``, end of ``run_download``) still counts
only the candidate download loop plus the extra CDN probe --
``success_count = len(successful)`` and
``total_target = attempted + len(extra_cdn_files)`` -- while the Tier A
(``api_files``) / Tier 0 (``cdn_files``) files downloaded before the
loop never enter either side.

Repro (round-3 #13 shape): Tier 0 downloads 11/11 mmc files, the page
scrape then finds candidates, the candidate loop downloads 0 of them --
the final line reads ``✓ Done: 0/3`` and the runner scores the paper
failed(-timeout) with ``dl_done`` 0, even though 11 real files are on
disk. Fix: fold the on-disk-verified Tier A/Tier 0 files into both
sides of the fraction (the same convention the extra CDN probe already
uses), excluding any file the candidate loop already counted (same file
reached via both paths, e.g. a same-URL resume) so nothing is counted
twice. Files are verified on disk by basename in the final article dir
(the dir may have migrated since Tier A/Tier 0 ran), mirroring
``print_tier_preserved_files``.

No network use; everything runs against tmp_path fixtures.
"""
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import scansci_supp_downloader as ssd  # noqa: E402
from supp_finder import Candidate  # noqa: E402


def _cand(url, filename="S1.xlsx", **kw):
    return Candidate(url=url, filename=filename, **kw)


def _write_mmc_files(article_dir, count=11):
    article_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for n in range(1, count + 1):
        p = article_dir / f"mmc{n}.xlsx"
        p.write_bytes(b"fake-xlsx")
        paths.append(str(p))
    return paths


# ── unit: count_tier_files_on_disk ────────────────────────────────────────

def test_count_tier_files_on_disk_counts_verified_files(tmp_path):
    on_disk = _write_mmc_files(tmp_path, 2)
    missing = str(tmp_path / "mmc3.xlsx")  # listed but not on disk
    assert ssd.count_tier_files_on_disk([], on_disk + [missing], str(tmp_path)) == 2


def test_count_tier_files_on_disk_dedupes_shared_basename(tmp_path):
    """The same file listed under both Tier A and Tier 0 counts once."""
    on_disk = _write_mmc_files(tmp_path, 1)
    assert ssd.count_tier_files_on_disk(on_disk, on_disk, str(tmp_path)) == 1


def test_count_tier_files_on_disk_excludes_files_already_counted(tmp_path):
    """A tier file the candidate loop already downloaded (same file
    reached via both paths, e.g. a same-URL resume) must not be
    counted a second time; an unrelated tier file still counts."""
    tier = _write_mmc_files(tmp_path, 2)
    successful = [_cand("https://example.com/mmc1.xlsx", "mmc1.xlsx")]
    assert ssd.count_tier_files_on_disk([], tier, str(tmp_path), successful) == 1


def test_count_tier_files_on_disk_resolves_moved_article_dir(tmp_path):
    """Tier A/Tier 0 ran before the article dir migrated to the title
    dir, so the recorded paths are stale -- the file still counts when
    it resolves by basename under the final article dir."""
    stale_dir = tmp_path / "supplements"
    stale_paths = _write_mmc_files(stale_dir, 1)
    final_dir = tmp_path / "Real Title"
    final_dir.mkdir()
    (final_dir / "mmc1.xlsx").write_bytes(b"fake-xlsx")
    assert ssd.count_tier_files_on_disk([], stale_paths, str(final_dir)) == 1


def test_count_tier_files_on_disk_empty_when_nothing_on_disk(tmp_path):
    assert ssd.count_tier_files_on_disk([], [], str(tmp_path)) == 0
    assert ssd.count_tier_files_on_disk(
        [], [str(tmp_path / "mmc1.xlsx")], str(tmp_path)) == 0


# ── end-to-end: final Done line in run_download ───────────────────────────

def _run_main_with_tier0(tmp_path, tier_paths, cand_urls, download_side_effect, capsys):
    """Run main() with Tier 0 returning tier_paths and a stubbed page
    scrape yielding cand_urls; download_file behaves per
    download_side_effect(candidate) -> truthy path / None. Returns
    the captured stdout."""
    from types import SimpleNamespace as NS  # noqa: F811 - local clarity

    base = "https://www.sciencedirect.com/science/article/pii/S1234567890123456"
    argv = ["scansci_supp_downloader.py", "10.1016/j.fake",
            "-o", str(tmp_path), "--no-bsk", "--no-vpn"]
    cands = [_cand(u, f"S{i + 1}.xlsx") for i, u in enumerate(cand_urls)]
    diag = NS(candidates=list(cands), needs_browser=False)
    with patch.object(sys, "argv", argv), \
         patch.object(ssd, "load_config", return_value={}), \
         patch.object(ssd, "resolve_doi_url", return_value=base), \
         patch.object(ssd, "try_elsevier_api_campus",
                      return_value=([], set())), \
         patch.object(ssd, "try_elsevier_cdn_brute_force",
                      return_value=list(tier_paths)), \
         patch.object(ssd, "_setup_authenticated_session_and_cookies",
                      return_value=(NS(), [])), \
         patch.object(ssd, "fetch_page_html", return_value=("", 0, base)), \
         patch.object(ssd, "find_all_candidates", return_value=diag), \
         patch.object(ssd, "resolve_oa_info", return_value=None), \
         patch.object(ssd, "download_file",
                      side_effect=download_side_effect):
        ssd.main()
    return capsys.readouterr().out


def test_final_done_line_counts_tier0_files_when_candidates_fail(tmp_path, capsys):
    """Repro: Tier 0 downloads 11/11, page scrape finds 3 candidates,
    the candidate loop downloads 0 -- the old line read Done: 0/3 and
    the runner scored the paper failed despite 11 files on disk."""
    tier = _write_mmc_files(tmp_path / "supplements", 11)
    out = _run_main_with_tier0(
        tmp_path, tier,
        ["https://example.com/S1.xlsx", "https://example.com/S2.xlsx",
         "https://example.com/S3.xlsx"],
        lambda cand, *a, **k: None, capsys)
    assert re.search(r"✓ Done: 11/14 files downloaded", out), out


def test_final_done_line_counts_tier0_files_alongside_candidate_success(tmp_path, capsys):
    """Tier 0 11/11 plus 1 of 3 candidates downloaded -> Done: 12/14
    (old line: 1/3)."""
    tier = _write_mmc_files(tmp_path / "supplements", 11)
    good = str(tmp_path / "supplements" / "S1.xlsx")
    Path(good).write_bytes(b"fake-xlsx")

    def _dl(cand, *a, **k):
        return good if cand.url.endswith("S1.xlsx") else None

    out = _run_main_with_tier0(
        tmp_path, tier,
        ["https://example.com/S1.xlsx", "https://example.com/S2.xlsx",
         "https://example.com/S3.xlsx"],
        _dl, capsys)
    assert re.search(r"✓ Done: 12/14 files downloaded", out), out


def test_final_done_line_unchanged_without_tier_files(tmp_path, capsys):
    """No Tier A/Tier 0 files: the old counting is untouched."""
    out = _run_main_with_tier0(
        tmp_path, [],
        ["https://example.com/S1.xlsx", "https://example.com/S2.xlsx",
         "https://example.com/S3.xlsx"],
        lambda cand, *a, **k: None, capsys)
    assert re.search(r"✓ Done: 0/3 files downloaded", out), out
