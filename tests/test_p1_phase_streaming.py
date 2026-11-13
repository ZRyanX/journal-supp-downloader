"""P1-CE regression tests: 150s phase budget + Phase A streaming output.

Causes C+E in ~/workspace/jsd-50-paper-test-p1-report.md (sec. 4 item 3):
Phase A timed out on 49/50 papers and the candidate list was discarded
with the timeout, because ``Found N potential ...`` is only printed after
*every* mirror has been probed (journal_downloader.py), and the download
loop follows discovery order -- so on a PLOS paper with 127-160 candidates
the junk/external candidates burn the 150s budget before any real
supplement file is attempted. Phase B timed out on 41/50.

(a) Phase A streams partial results: every batch of newly discovered
candidates is printed immediately and written to a partial-results file
(marked incomplete) in the article directory, so a timeout/interrupt
leaves the already-found candidates readable on disk.
(b) Downloads are ordered by candidate priority: real supplement files
(data extensions incl. pdf, or explicitly supplementary-marked) first,
external/repository/other candidates last -- without dropping any.
(c) The single-phase timeout is a CLI option (--phase-timeout) on both
entry points, default 150s, and both phases honour it.

No test here touches the network outside localhost; fetches and mirrors
are stubbed and time is driven by a fake monotonic clock.
"""
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import journal_downloader as jd  # noqa: E402
import scansci_supp_downloader as ssd  # noqa: E402
from supp_finder import (  # noqa: E402
    DEFAULT_PHASE_TIMEOUT_SECONDS,
    Candidate,
    find_all_candidates,
    resolve_phase_timeout,
    sort_candidates_by_priority,
)

PUB_HTML = (
    "<html><head><title>Test Article Title</title></head><body>"
    "<section class='supplementary-materials'>"
    '<a href="https://example.com/S1.xlsx">Supplementary Table S1</a>'
    '<a href="https://example.com/S2.csv">Supplementary Table S2</a>'
    "</section></body></html>"
)


class FakeClock:
    """Controllable time.monotonic for deadline tests."""

    def __init__(self, now=1000.0):
        self.now = now

    def monotonic(self):
        return self.now


@pytest.fixture()
def fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    return clock


def _page(html=PUB_HTML, url="https://example.com/article"):
    return NS(html_content=html, text=html, url=url, status=200)


def _args(tmp_path, **overrides):
    base = dict(
        url="https://example.com/article",
        output_dir=str(tmp_path),
        headful=False,
        proxy=None,
        timeout=60000,
        solve_cloudflare=True,
        no_cloudflare=False,
        wait_selector=None,
        real_chrome=False,
        cdp=False,
        cdp_url=None,
        browserskill=False,
        bsk_browser=None,
        bsk_tab=None,
        list_only=True,
        oa=False,
        no_oa=True,
        convert_markdown=False,
        phase_timeout=150,
    )
    base.update(overrides)
    return NS(**base)


class _DisabledRuntime:
    enabled = False
    browser = None
    tab_id = None


# ── (c) timeout is configurable, default 150s ───────────────────────────────

def test_default_phase_timeout_is_150_seconds():
    assert DEFAULT_PHASE_TIMEOUT_SECONDS == 150
    assert resolve_phase_timeout(None) == 150
    assert resolve_phase_timeout(150) == 150


def test_phase_timeout_accepts_custom_value():
    assert resolve_phase_timeout(30) == 30
    assert resolve_phase_timeout(600) == 600
    assert resolve_phase_timeout("45") == 45


def test_phase_timeout_invalid_value_falls_back_to_default():
    assert resolve_phase_timeout(0) == 150
    assert resolve_phase_timeout(-5) == 150
    assert resolve_phase_timeout("not-a-number") == 150


def test_phase_timeout_cli_option_defaults_to_150_on_both_entry_points():
    for script in ("journal_downloader.py", "scansci_supp_downloader.py"):
        out = os.popen(
            f"{sys.executable} {Path(__file__).resolve().parents[1] / 'scripts' / script} --help"
        ).read()
        assert "--phase-timeout" in out, script
        assert "150" in out, script


# ── (b) download priority order ─────────────────────────────────────────────

def test_sort_puts_real_supplement_files_first_and_keeps_everything():
    junk_page = Candidate(
        url="https://example.com/external-page", filename="external-page",
        link_text="data", section="page_body", match_rule="generic_link")
    supp_html = Candidate(
        url="https://example.com/supplement.html", filename="supplement.html",
        link_text="Supplementary Information", section="supplementary_section",
        match_rule="supp_section_link")
    real_xlsx = Candidate(
        url="https://example.com/S1.xlsx", filename="S1.xlsx",
        link_text="Supplementary Table S1", section="supplementary_section",
        match_rule="supp_section_link", covered_table_ids={"S1"})
    real_pdf = Candidate(
        url="https://example.com/S1.pdf", filename="S1.pdf",
        link_text="Supplementary Table S1", section="supplementary_section",
        match_rule="supp_section_link")
    repo_page = Candidate(
        url="https://zenodo.org/records/12345", filename="12345",
        link_text="Supplementary data", section="supplementary_section",
        match_rule="repository_link")
    ordered = sort_candidates_by_priority(
        [junk_page, supp_html, real_pdf, repo_page, real_xlsx])
    # Nothing is dropped, only reordered.
    assert len(ordered) == 5
    assert {c.url for c in ordered} == {
        c.url for c in (junk_page, supp_html, real_pdf, repo_page, real_xlsx)}
    # Real data files first (stable: pdf before xlsx as discovered), then
    # explicitly supplementary pages, then external/other last.
    assert ordered[0] is real_pdf
    assert ordered[1] is real_xlsx
    assert ordered[2] is supp_html
    assert ordered[3] is repo_page
    assert ordered[4] is junk_page


def test_sort_is_stable_within_priority_tier():
    a = Candidate(url="https://example.com/a.csv", filename="a.csv")
    b = Candidate(url="https://example.com/b.zip", filename="b.zip")
    c = Candidate(url="https://example.com/c.docx", filename="c.docx")
    assert sort_candidates_by_priority([c, a, b]) == [c, a, b]


def test_sort_does_not_drop_low_priority_candidates():
    cands = [Candidate(url=f"https://example.com/page{i}", filename=f"page{i}")
             for i in range(5)]
    assert sort_candidates_by_priority(cands) == cands


def test_download_loop_attempts_real_files_before_external_pages(tmp_path, fake_clock):
    """End to end through journal_downloader's download loop: the .xlsx is
    attempted before the supplementary .html page, even though the page
    was discovered first."""
    html = (
        "<html><head><title>Order Article</title></head><body>"
        "<section class='supplementary-materials'>"
        '<a href="https://example.com/files/supplement">Supplementary Information</a>'
        '<a href="https://example.com/S1.xlsx">Supplementary Table S1</a>'
        "</section></body></html>"
    )
    attempted = []
    orig_fetch = jd.StealthyFetcher.fetch
    jd.StealthyFetcher.fetch = staticmethod(lambda *a, **k: _page(html))
    orig_download = jd.download_file
    jd.download_file = lambda cand, *a, **k: attempted.append(cand.url) or os.path.join(
        str(tmp_path), cand.filename)
    orig_cookies = jd.load_saved_cookies
    jd.load_saved_cookies = lambda: None
    try:
        jd.run_download(_args(tmp_path, list_only=False), _DisabledRuntime())
    finally:
        jd.StealthyFetcher.fetch = orig_fetch
        jd.download_file = orig_download
        jd.load_saved_cookies = orig_cookies
    assert attempted == ["https://example.com/S1.xlsx",
                         "https://example.com/files/supplement"]


# ── (a) Phase A streams partial results and survives a timeout ──────────────

def test_partial_candidates_file_marks_incomplete_results(tmp_path):
    cands = list(find_all_candidates(PUB_HTML, "https://example.com/article").candidates)
    assert len(cands) == 2
    jd.write_partial_candidates(str(tmp_path), cands, complete=False,
                                sources_completed=["publisher"])
    partial = tmp_path / jd.PARTIAL_CANDIDATES_FILENAME
    assert partial.exists()
    data = json.loads(partial.read_text(encoding="utf-8"))
    assert data["complete"] is False
    assert data["partial"] is True
    assert data["candidate_count"] == 2
    assert [c["url"] for c in data["candidates"]] == [
        "https://example.com/S1.xlsx", "https://example.com/S2.csv"]
    assert data["sources_completed"] == ["publisher"]


def test_completed_discovery_writes_final_list_not_partial(tmp_path):
    cands = list(find_all_candidates(PUB_HTML, "https://example.com/article").candidates)
    jd.write_partial_candidates(str(tmp_path), cands, complete=False,
                                sources_completed=["publisher"])
    jd.finalize_candidates(str(tmp_path), cands, sources_completed=["publisher", "oa_mirrors"])
    assert not (tmp_path / jd.PARTIAL_CANDIDATES_FILENAME).exists()
    final = tmp_path / jd.FINAL_CANDIDATES_FILENAME
    assert final.exists()
    data = json.loads(final.read_text(encoding="utf-8"))
    assert data["complete"] is True
    assert data["partial"] is False
    assert data["candidate_count"] == 2


def test_phase_a_timeout_keeps_publisher_candidates_in_partial_file(
        tmp_path, fake_clock, capsys):
    """Reproduces C+E: the publisher page is fetched (2 candidates found)
    but the phase budget expires before the OA mirrors are probed. The
    run must still exit normally with the 2 candidates readable from the
    partial file -- not discard them with the timeout."""
    def slow_fetch(*args, **kwargs):
        fake_clock.now += 200.0  # fetch alone burns the whole 150s budget
        return _page()

    orig_fetch = jd.StealthyFetcher.fetch
    jd.StealthyFetcher.fetch = staticmethod(slow_fetch)
    orig_cookies = jd.load_saved_cookies
    jd.load_saved_cookies = lambda: None
    try:
        jd.run_download(_args(tmp_path, phase_timeout=150), _DisabledRuntime())
    finally:
        jd.StealthyFetcher.fetch = orig_fetch
        jd.load_saved_cookies = orig_cookies
    out = capsys.readouterr().out
    assert "S1.xlsx" in out  # streamed to stdout as soon as discovered
    assert "timed out" in out.lower() or "timeout" in out.lower()
    partial = tmp_path / "Test Article Title" / jd.PARTIAL_CANDIDATES_FILENAME
    assert partial.exists(), "partial file must survive the Phase A timeout"
    data = json.loads(partial.read_text(encoding="utf-8"))
    assert data["complete"] is False
    assert data["timed_out"] is True
    assert [c["url"] for c in data["candidates"]] == [
        "https://example.com/S1.xlsx", "https://example.com/S2.csv"]
    # No final (complete) list may be claimed after a timeout.
    assert not (tmp_path / "Test Article Title" / jd.FINAL_CANDIDATES_FILENAME).exists()


def test_phase_timeout_value_is_honoured_not_just_default(
        tmp_path, fake_clock, capsys):
    """Same slow fetch (+200s), but with --phase-timeout 600 the mirrors
    must still be probed: the budget, not a hardcoded 150s, decides."""
    def slow_fetch(*args, **kwargs):
        fake_clock.now += 200.0
        return _page()

    mirror_calls = []

    class _Mirrors:
        info = None

        def discover(self, force=False, on_candidate=None):
            mirror_calls.append(True)
            return []

    orig_fetch = jd.StealthyFetcher.fetch
    jd.StealthyFetcher.fetch = staticmethod(slow_fetch)
    orig_cookies = jd.load_saved_cookies
    jd.load_saved_cookies = lambda: None
    orig_mirrors = jd.MirrorDiscovery
    jd.MirrorDiscovery = lambda *a, **k: _Mirrors()
    try:
        # oa=True so the mirror round is actually expected to run: with a
        # complete publisher candidate set it would otherwise be skipped.
        jd.run_download(_args(tmp_path, phase_timeout=600, oa=True, no_oa=False),
                        _DisabledRuntime())
    finally:
        jd.StealthyFetcher.fetch = orig_fetch
        jd.load_saved_cookies = orig_cookies
        jd.MirrorDiscovery = orig_mirrors
    assert mirror_calls, "with a 600s budget the mirror round must still run"
    final = tmp_path / "Test Article Title" / jd.FINAL_CANDIDATES_FILENAME
    assert final.exists()
    assert json.loads(final.read_text(encoding="utf-8"))["complete"] is True


def test_mirror_discovery_streams_each_mirror_batch_via_callback(tmp_path):
    """MirrorDiscovery calls on_candidate per newly found candidate, so a
    slow mirror round can stream instead of buffering to the end."""
    from discovery_state import MirrorDiscovery
    from oa_resolver import OAResult

    seen = []
    html = (
        "<html><body><section class='supplementary-materials'>"
        '<a href="https://example.com/M1.xlsx">Supplementary Table S1</a>'
        "</section></body></html>"
    )

    def resolve(doi):
        return OAResult(oa_landing_urls=["https://pmc.example/articles/PMC1/"])

    def fetch(url):
        return html, 200, url

    from discovery_state import DiscoveryEvidence
    mirrors = MirrorDiscovery("10.1234/test", True, resolve, fetch, DiscoveryEvidence())
    found = mirrors.discover(on_candidate=lambda c: seen.append(c.url))
    assert [c.url for c in found] == ["https://example.com/M1.xlsx"]
    assert seen == ["https://example.com/M1.xlsx"]


# ── Phase B: the download loop itself respects the phase deadline ───────────

def test_download_with_recovery_stops_at_phase_deadline(fake_clock):
    from discovery_state import download_with_recovery

    calls = []

    def download(cand):
        calls.append(cand.url)
        fake_clock.now += 160.0  # each attempt burns 160s of the 150s budget
        return "/tmp/" + cand.filename

    class _Mirrors:
        def discover(self):
            return []

    cands = [Candidate(url=f"https://example.com/S{i}.xlsx", filename=f"S{i}.xlsx")
             for i in (1, 2, 3)]
    ok, attempted = download_with_recovery(
        cands, download, _Mirrors(), list, sleep=lambda s: None,
        deadline=fake_clock.now + 150)
    # First attempt (160s) exhausts the budget; the second never starts.
    assert calls == ["https://example.com/S1.xlsx"]
    assert attempted == 1
    assert len(ok) == 1


def test_download_with_recovery_no_deadline_keeps_old_behaviour():
    from discovery_state import download_with_recovery

    calls = []

    def download(cand):
        calls.append(cand.url)
        return "/tmp/" + cand.filename

    class _Mirrors:
        def discover(self):
            return []

    cands = [Candidate(url=f"https://example.com/S{i}.xlsx", filename=f"S{i}.xlsx")
             for i in (1, 2)]
    ok, attempted = download_with_recovery(
        cands, download, _Mirrors(), list, sleep=lambda s: None)
    assert calls == ["https://example.com/S1.xlsx", "https://example.com/S2.xlsx"]
    assert attempted == 2


def test_scansci_run_download_sorts_candidates_before_downloading(tmp_path):
    """scansci Phase B: the download loop receives priority order, not
    discovery order (same rule as journal_downloader)."""
    html = (
        "<html><head><title>Scansci Order</title></head><body>"
        "<section class='supplementary-materials'>"
        '<a href="https://example.com/files/supplement">Supplementary Information</a>'
        '<a href="https://example.com/S1.xlsx">Supplementary Table S1</a>'
        "</section></body></html>"
    )
    attempted = []
    args = NS(
        url_or_doi="https://example.com/article",
        output_dir=str(tmp_path),
        no_vpn=True, no_cookies=True, no_api=True, headful=False,
        skip_cdn=True, max_mmc=25, max_misses=4, proxy=None,
        tunnel_fail_threshold=3, list_only=False, oa=False, no_oa=True,
        cdp=False, cdp_url=None, browserskill=False, bsk_browser=None,
        bsk_tab=None, convert_markdown=False, phase_timeout=150,
    )
    orig_fetch = ssd.fetch_page_html
    ssd.fetch_page_html = lambda *a, **k: (html, 200, "https://example.com/article")
    orig_dl = ssd.download_with_recovery
    ssd.download_with_recovery = lambda cands, *a, **k: (
        attempted.extend(c.url for c in cands), (cands, len(cands)))[1]
    try:
        ssd.run_download(args, _DisabledRuntime())
    finally:
        ssd.fetch_page_html = orig_fetch
        ssd.download_with_recovery = orig_dl
    assert attempted == ["https://example.com/S1.xlsx",
                         "https://example.com/files/supplement"]
