"""Round-3 follow-up regression tests: E (Phase B seeds from Phase A
candidate list) and G (Tier A/Tier 0 preserved files get a countable
Done line).

Round-3 report (~/workspace/jsd-50-paper-test-p1fix-report.md, sec. 4):

E -- partial candidates were preserved but nobody used them: 15 papers,
348 candidates landed in supplementary_candidates.partial.json, yet
Phase B (scansci_supp_downloader.py, a separate process) re-discovered
from scratch and never read the file Phase A had written into the same
article directory (#26's 21 candidates included real s-links).
Fix: Phase B loads the final/partial candidate list from article_dir
and merges it (URL-deduped) into its candidate set.

G -- Tier 0 success still could not reach the tool status: #13 (11/11)
and #47 (3/3) downloaded via Tier 0 yet were scored failed-timeout
with an empty dl_done, because the "no candidate list, Tier A/Tier 0
files preserved" path printed no "✓ Done:" line at all.
Fix: print_tier_preserved_files returns the on-disk count and the
caller prints an honest countable line:
"✓ Done: N file(s) downloaded via Tier A/CDN (page completeness
unverified)" -- N real files, no invented denominator.

No network use; everything runs against tmp_path fixtures.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import scansci_supp_downloader as ssd  # noqa: E402
from supp_finder import Candidate  # noqa: E402


def _cand(url, filename="S1.xlsx", **kw):
    return Candidate(url=url, filename=filename, **kw)


def _write_seed_file(article_dir, name, candidates, *, complete=False):
    article_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "complete": complete,
        "partial": not complete,
        "timed_out": not complete,
        "candidate_count": len(candidates),
        "sources_completed": ["publisher"],
        "candidates": [c.to_dict() for c in candidates],
    }
    (article_dir / name).write_text(json.dumps(payload), encoding="utf-8")


# ── E: Candidate.from_dict ────────────────────────────────────────────────

def test_candidate_from_dict_roundtrips_to_dict():
    c = Candidate(
        url="https://example.com/mmc1.xlsx", filename="mmc1.xlsx",
        link_text="Supplementary Table 1", section="supplementary",
        match_rule="table_label", source_tag="a",
        extra_info={"k": "v"}, source_url="https://example.com/article",
        covered_table_ids={"S1", "S2"},
    )
    back = Candidate.from_dict(c.to_dict())
    assert back is not None
    assert back.url == c.url
    assert back.filename == c.filename
    assert back.link_text == c.link_text
    assert back.section == c.section
    assert back.match_rule == c.match_rule
    assert back.source_tag == c.source_tag
    assert back.extra_info == c.extra_info
    assert back.source_url == c.source_url
    assert back.covered_table_ids == {"S1", "S2"}


def test_candidate_from_dict_rejects_urless_entries():
    assert Candidate.from_dict({}) is None
    assert Candidate.from_dict({"filename": "S1.xlsx"}) is None
    assert Candidate.from_dict("not-a-dict") is None
    assert Candidate.from_dict(None) is None


# ── E: load_seed_candidates ───────────────────────────────────────────────

def test_load_seed_candidates_missing_files(tmp_path):
    cands, source = ssd.load_seed_candidates(str(tmp_path))
    assert cands == []
    assert source is None


def test_load_seed_candidates_reads_partial(tmp_path):
    _write_seed_file(
        tmp_path, "supplementary_candidates.partial.json",
        [_cand("https://example.com/S1.xlsx"),
         _cand("https://example.com/S2.csv", "S2.csv")])
    cands, source = ssd.load_seed_candidates(str(tmp_path))
    assert source == "supplementary_candidates.partial.json"
    assert [c.url for c in cands] == [
        "https://example.com/S1.xlsx", "https://example.com/S2.csv"]


def test_load_seed_candidates_prefers_final_over_partial(tmp_path):
    _write_seed_file(tmp_path, "supplementary_candidates.partial.json",
                     [_cand("https://example.com/old.xlsx", "old.xlsx")])
    _write_seed_file(tmp_path, "supplementary_candidates.json",
                     [_cand("https://example.com/new.xlsx", "new.xlsx")],
                     complete=True)
    cands, source = ssd.load_seed_candidates(str(tmp_path))
    assert source == "supplementary_candidates.json"
    assert [c.url for c in cands] == ["https://example.com/new.xlsx"]


def test_load_seed_candidates_corrupt_final_falls_back_to_partial(tmp_path):
    (tmp_path / "supplementary_candidates.json").write_text(
        "{not json", encoding="utf-8")
    _write_seed_file(tmp_path, "supplementary_candidates.partial.json",
                     [_cand("https://example.com/S1.xlsx")])
    cands, source = ssd.load_seed_candidates(str(tmp_path))
    assert source == "supplementary_candidates.partial.json"
    assert len(cands) == 1


def test_load_seed_candidates_all_corrupt_returns_empty(tmp_path):
    (tmp_path / "supplementary_candidates.json").write_text(
        "{not json", encoding="utf-8")
    (tmp_path / "supplementary_candidates.partial.json").write_text(
        "[also not json", encoding="utf-8")
    cands, source = ssd.load_seed_candidates(str(tmp_path))
    assert cands == []
    assert source is None


def test_load_seed_candidates_skips_urless_entries(tmp_path):
    article_dir = tmp_path / "art"
    article_dir.mkdir()
    payload = {"candidates": [
        {"filename": "no-url.xlsx"},
        _cand("https://example.com/S1.xlsx").to_dict(),
    ]}
    (article_dir / "supplementary_candidates.partial.json").write_text(
        json.dumps(payload), encoding="utf-8")
    cands, source = ssd.load_seed_candidates(str(article_dir))
    assert [c.url for c in cands] == ["https://example.com/S1.xlsx"]


# ── E: merge_seed_candidates ──────────────────────────────────────────────

def test_merge_seed_candidates_appends_new_skips_known():
    existing = [_cand("https://example.com/S1.xlsx")]
    seeds = [_cand("https://example.com/S1.xlsx"),
             _cand("https://example.com/S2.csv", "S2.csv")]
    merged, added = ssd.merge_seed_candidates(existing, seeds)
    assert added == 1
    assert [c.url for c in merged] == [
        "https://example.com/S1.xlsx", "https://example.com/S2.csv"]
    # input list untouched
    assert len(existing) == 1


def test_merge_seed_candidates_empty_seeds():
    existing = [_cand("https://example.com/S1.xlsx")]
    merged, added = ssd.merge_seed_candidates(existing, [])
    assert added == 0
    assert merged == existing


# ── G: preserved Tier A/Tier 0 files get a countable Done line ───────────

def test_preserved_done_line_format(tmp_path):
    line = ssd.preserved_done_line(3, str(tmp_path))
    assert line.startswith("✓ Done: 3 file(s) downloaded via Tier A/CDN")
    assert "page completeness unverified" in line
    assert os.path.abspath(str(tmp_path)) in line
    # no invented denominator: must not claim N/N completeness
    assert not re.search(r"✓ Done: \d+/\d+", line)


def test_print_tier_preserved_files_returns_on_disk_count(tmp_path, capsys):
    on_disk = tmp_path / "mmc1.xlsx"
    on_disk.write_bytes(b"fake-xlsx")
    missing = str(tmp_path / "mmc2.xlsx")  # listed but not on disk
    count = ssd.print_tier_preserved_files(
        [], [str(on_disk), missing], str(tmp_path))
    out = capsys.readouterr().out
    assert count == 1
    assert "mmc1.xlsx" in out
    assert "not found on disk" in out  # the missing one is flagged, not counted


def test_no_candidate_branch_prints_done_line_for_tier0_files(tmp_path, capsys):
    """End-to-end (mocked): Tier A files with no candidates must produce
    the countable Done line, so downstream status parsing sees the
    download instead of scoring it failed-timeout (#13/#47)."""
    from types import SimpleNamespace as NS
    from unittest.mock import patch

    mmc = tmp_path / "mmc1.xlsx"
    mmc.write_bytes(b"fake-xlsx")
    base = "https://www.sciencedirect.com/science/article/pii/S1234567890123456"
    argv = ["scansci_supp_downloader.py", "10.1016/j.fake",
            "-o", str(tmp_path), "--no-bsk", "--no-vpn"]
    with patch.object(sys, "argv", argv), \
         patch.object(ssd, "load_config", return_value={}), \
         patch.object(ssd, "resolve_doi_url", return_value=base), \
         patch.object(ssd, "try_elsevier_api_campus",
                      return_value=([str(mmc)], {1})), \
         patch.object(ssd, "try_elsevier_cdn_brute_force", return_value=[]), \
         patch.object(ssd, "_setup_authenticated_session_and_cookies",
                      return_value=(NS(), [])), \
         patch.object(ssd, "fetch_page_html", return_value=("", 0, base)), \
         patch.object(ssd, "resolve_oa_info", return_value=None):
        ssd.main()
    out = capsys.readouterr().out
    assert re.search(
        r"✓ Done: 1 file\(s\) downloaded via Tier A/CDN", out), out
