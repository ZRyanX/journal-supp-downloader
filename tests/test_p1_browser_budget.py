"""P1 H-1 regression tests: browser fallback has no independent budget.

Round-3 report (~/workspace/jsd-50-paper-test-p1fix-report.md, sec. 2(h)
H-1, sec. 4 item 1): after --proxy fixed the tunnel failures, the
browser fallback stopped failing fast and started hanging -- a single
page load over the proxy takes 60-90s, so the 150s phase budget dies
inside one page load and the OA mirror round / download round never
run. Iron-clad case: #1 (10.1038/s41467-025-61134-0) -- R2: browser
fallback failed fast, OA mirrors ran, 13 MOESM files downloaded via
requests; R3: fallback hung 120s+ on nature.com, the runner killed the
phase at 150s, mirrors never ran, 0 files. Phase A timed out 46/50,
Phase B 48/50.

The fix, in two parts (either alone would help; both together are the
belt and braces):
(a) the browser fallback gets its own quota -- one page goto at most
    30s by default (--browser-goto-timeout), 60s total per phase by
    default (--browser-fallback-budget). Whichever quota runs out first
    degrades to the OA mirror round (requests path) instead of killing
    the phase, with an explicit log line.
(b) the cheap requests-based OA mirror round runs *before* the browser
    fallback, so files reachable without a browser are already in hand
    when the browser hangs.

No test here touches the network outside localhost; fetches and mirrors
are stubbed and time is driven by a fake monotonic clock.
"""
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
    DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS,
    DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS,
    BrowserFallbackBudget,
    Candidate,
    browser_budget_from_args,
    resolve_browser_fallback_budget,
    resolve_browser_goto_timeout,
)

BUDGET_EXHAUSTED_LINE = "[Browser] 回退预算耗尽，降级到 OA 镜像轮次"


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


class _DisabledRuntime:
    enabled = False
    browser = None
    tab_id = None


def _page(html, url="https://example.com/article"):
    return NS(html_content=html, text=html, url=url, status=200)


# ── (a) defaults + resolvers ────────────────────────────────────────────────

def test_browser_budget_defaults_are_30s_goto_and_60s_total():
    assert DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS == 30
    assert DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS == 60
    budget = BrowserFallbackBudget()
    assert budget.goto_timeout == 30
    assert budget.total_budget == 60


def test_browser_timeout_resolvers_accept_custom_and_reject_bad_values():
    assert resolve_browser_goto_timeout(10) == 10
    assert resolve_browser_goto_timeout("45") == 45
    assert resolve_browser_fallback_budget(120) == 120
    assert resolve_browser_goto_timeout(0) == 30
    assert resolve_browser_goto_timeout(-5) == 30
    assert resolve_browser_goto_timeout("not-a-number") == 30
    assert resolve_browser_fallback_budget(None) == 60
    assert resolve_browser_fallback_budget(0) == 60
    assert resolve_browser_fallback_budget("junk") == 60


def test_browser_budget_from_args_reads_cli_values_and_defaults():
    assert browser_budget_from_args(NS()).goto_timeout == 30
    assert browser_budget_from_args(NS()).total_budget == 60
    budget = browser_budget_from_args(
        NS(browser_goto_timeout=10, browser_fallback_budget=25))
    assert budget.goto_timeout == 10
    assert budget.total_budget == 25


def test_browser_budget_cli_options_exist_on_both_entry_points():
    for script in ("journal_downloader.py", "scansci_supp_downloader.py"):
        out = os.popen(
            f"{sys.executable} {Path(__file__).resolve().parents[1] / 'scripts' / script} --help"
        ).read()
        assert "--browser-goto-timeout" in out, script
        assert "--browser-fallback-budget" in out, script


def test_browser_budget_elapsed_and_remaining(fake_clock):
    budget = BrowserFallbackBudget(goto_timeout=30, total_budget=60)
    assert budget.elapsed() == 0
    assert budget.remaining() == 60
    assert not budget.exhausted
    fake_clock.now += 45
    assert budget.elapsed() == 45
    assert budget.remaining() == 15
    assert not budget.exhausted
    fake_clock.now += 20
    assert budget.exhausted


def test_browser_budget_announce_logs_elapsed_and_quota_once(fake_clock, capsys):
    budget = BrowserFallbackBudget(goto_timeout=30, total_budget=60)
    fake_clock.now += 61
    assert budget.announce_exhausted() is True
    assert budget.announce_exhausted() is False  # logged once
    out = capsys.readouterr().out
    assert BUDGET_EXHAUSTED_LINE in out
    assert "61" in out  # elapsed seconds spent
    assert "60" in out  # total quota


# ── (a) fetch_page_html honours the goto timeout + total budget ─────────────

class _RecordingFetcher:
    calls = []

    @staticmethod
    def fetch(url, **kwargs):
        _RecordingFetcher.calls.append(dict(kwargs))
        return NS(
            html_content="<html><head><title>t</title></head><body>ok</body></html>",
            status=200,
            url=url,
        )


def test_fetch_page_html_default_goto_timeout_is_30_seconds(monkeypatch):
    """The old hardcoded 60000ms StealthyFetcher timeout is what let one
    page load eat 60s+ of the phase budget (Wiley: two 60s gotos)."""
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    html, status, _ = ssd.fetch_page_html(
        "https://example.com/article", session=None)
    assert status == 200 and html
    assert _RecordingFetcher.calls, "StealthyFetcher.fetch was never called"
    assert _RecordingFetcher.calls[0]["timeout"] == 30000


def test_fetch_page_html_custom_goto_timeout_is_respected(monkeypatch):
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    ssd.fetch_page_html("https://example.com/article", session=None,
                        browser_goto_timeout=10)
    assert _RecordingFetcher.calls[0]["timeout"] == 10000


def test_fetch_page_html_caps_attempt_timeout_at_remaining_budget(
        monkeypatch, fake_clock):
    """45s of the 60s budget already spent: the next goto may only run
    15s, so the total browser time can never overshoot the budget by
    more than one (capped) attempt."""
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    budget = BrowserFallbackBudget(goto_timeout=30, total_budget=60)
    fake_clock.now += 45
    ssd.fetch_page_html("https://example.com/article", session=None,
                        browser_budget=budget)
    assert _RecordingFetcher.calls[0]["timeout"] == 15000


def test_fetch_page_html_skips_browser_once_budget_exhausted(
        monkeypatch, fake_clock, capsys):
    """Reproduces H-1 at the fetch layer: after 61s of browser time the
    fallback must not launch another browser at all -- it degrades to
    the (requests) mirror round instead."""
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    budget = BrowserFallbackBudget(goto_timeout=30, total_budget=60)
    fake_clock.now += 61
    html, status, final = ssd.fetch_page_html(
        "https://example.com/article", session=None, force_browser=True,
        browser_budget=budget)
    assert _RecordingFetcher.calls == []
    assert (html, status) == ("", 0)
    assert final == "https://example.com/article"
    assert BUDGET_EXHAUSTED_LINE in capsys.readouterr().out


def test_fetch_page_html_browserskill_timeout_is_capped_at_goto_timeout(
        monkeypatch):
    """BrowserSkill/CDP attach to a live browser and cannot take a proxy,
    but they do take a timeout -- it must be the 30s goto timeout, not
    the old hardcoded 60s."""
    seen = {}

    def _fake_bsk(url, **kwargs):
        seen.update(kwargs)
        return "<html><body>ok</body></html>", None, url

    monkeypatch.setattr(ssd, "fetch_page_via_browserskill", _fake_bsk)
    html, _status, _ = ssd.fetch_page_html(
        "https://example.com/article", use_bsk=True)
    assert html
    assert seen["timeout"] == 30.0


# ── (a)+(b) Phase B end to end: hanging browser, mirrors still run ──────────

def _ssd_args(tmp_path, **overrides):
    base = dict(
        url_or_doi="https://example.com/article",
        output_dir=str(tmp_path),
        no_vpn=True, no_cookies=True, no_api=True, headful=False,
        skip_cdn=True, max_mmc=25, max_misses=4, proxy=None,
        tunnel_fail_threshold=3, list_only=False, oa=False, no_oa=True,
        cdp=False, cdp_url=None, browserskill=False, bsk_browser=None,
        bsk_tab=None, convert_markdown=False, phase_timeout=150,
        browser_goto_timeout=30, browser_fallback_budget=60,
    )
    base.update(overrides)
    return NS(**base)


def test_scansci_hanging_browser_fallback_still_runs_oa_mirrors(
        tmp_path, fake_clock, monkeypatch, capsys):
    """H-1 repro, Phase B (#1 R3): the publisher page load hangs 90s in
    the browser. The run must still reach the OA mirror round, download
    the mirror file, and finish -- not burn the whole phase in the
    browser and never run the mirrors. The browser must be attempted
    exactly once (no second attempt after the budget is gone)."""
    order = []

    class _HangingFetcher:
        calls = 0

        @staticmethod
        def fetch(url, **kwargs):
            _HangingFetcher.calls += 1
            order.append("stealthy")
            fake_clock.now += 90.0  # one page load hangs 90s (R3: 120s+)
            raise RuntimeError("Timeout 60000ms exceeded")

    class _FailingSession:
        def get(self, *a, **k):
            raise RuntimeError("requests blocked")

    class _Mirrors:
        info = None

        def __init__(self, *a, **k):
            pass

        def discover(self, force=False, on_candidate=None):
            order.append("mirror")
            return [Candidate(
                url="https://pmc.ncbi.nlm.nih.gov/articles/PMC1/bin/MOESM1.xlsx",
                filename="MOESM1.xlsx", link_text="MOESM1",
                section="supplementary_section",
                match_rule="supp_section_link")]

    attempted = []

    def _fake_download_with_recovery(cands, *a, **k):
        attempted.extend(c.url for c in cands)
        return [], len(cands)

    monkeypatch.setattr(ssd, "StealthyFetcher", _HangingFetcher)
    monkeypatch.setattr(
        ssd, "_setup_authenticated_session_and_cookies",
        lambda *a, **k: (_FailingSession(), []))
    monkeypatch.setattr(ssd, "MirrorDiscovery", _Mirrors)
    monkeypatch.setattr(ssd, "download_with_recovery", _fake_download_with_recovery)
    ssd.run_download(_ssd_args(tmp_path), _DisabledRuntime())
    out = capsys.readouterr().out
    assert _HangingFetcher.calls == 1, (
        "browser fallback must be attempted once, not retried after the "
        "budget is gone")
    assert order == ["stealthy", "mirror"], (
        "OA mirror round must run after the (single) browser attempt, "
        "not after a second browser retry")
    assert attempted == [
        "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/bin/MOESM1.xlsx"], (
        "the mirror candidate must reach the download loop")
    assert BUDGET_EXHAUSTED_LINE in out


# ── (a)+(b) Phase A end to end: same hang, mirrors first, retry skipped ─────

def _jd_args(tmp_path, **overrides):
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
        list_only=False,
        oa=False,
        no_oa=True,
        convert_markdown=False,
        phase_timeout=150,
        browser_goto_timeout=30,
        browser_fallback_budget=60,
    )
    base.update(overrides)
    return NS(**base)


# The publisher page declares Supplementary Table S1 in prose but links
# nothing, so the evidence check stays incomplete even after the mirror
# round -- the browser retry below is only skipped because of the budget.
DECLARING_HTML = (
    "<html><head><title>Declaring Article</title></head><body>"
    "<p>The results are shown in Supplementary Table S1.</p>"
    "</body></html>"
)


def test_journal_hanging_browser_fetch_still_runs_oa_mirrors_first(
        tmp_path, fake_clock, monkeypatch, capsys):
    """H-1 repro, Phase A (#1: '[1/3] Fetching article page' alone ate
    the 150s): the first browser fetch hangs 90s. The OA mirror round
    (requests) must still run before any browser retry, the retry must
    be skipped once the 60s fallback budget is gone, and the mirror
    candidate must still be downloaded."""
    order = []
    fetch_kwargs_seen = []

    class _HangingFetcher:
        calls = 0

        @staticmethod
        def fetch(url, **kwargs):
            _HangingFetcher.calls += 1
            order.append("stealthy")
            fetch_kwargs_seen.append(dict(kwargs))
            fake_clock.now += 90.0
            return _page(DECLARING_HTML)

    class _Mirrors:
        info = None

        def __init__(self, *a, **k):
            pass

        def discover(self, force=False, on_candidate=None):
            order.append("mirror")
            return [Candidate(
                url="https://example.com/other.pdf", filename="other.pdf",
                link_text="data", section="page_body",
                match_rule="generic_link")]

    attempted = []
    orig_download = jd.download_file
    jd.download_file = lambda cand, *a, **k: attempted.append(cand.url) or os.path.join(
        str(tmp_path), cand.filename)
    orig_cookies = jd.load_saved_cookies
    jd.load_saved_cookies = lambda: None
    try:
        monkeypatch.setattr(jd, "StealthyFetcher", _HangingFetcher)
        monkeypatch.setattr(jd, "MirrorDiscovery", _Mirrors)
        jd.run_download(_jd_args(tmp_path), _DisabledRuntime())
    finally:
        jd.download_file = orig_download
        jd.load_saved_cookies = orig_cookies
    out = capsys.readouterr().out
    assert fetch_kwargs_seen, "StealthyFetcher.fetch was never called"
    assert fetch_kwargs_seen[0]["timeout"] == 30000, (
        "Phase A page fetch must use the 30s goto timeout, not --timeout 60000")
    assert _HangingFetcher.calls == 1, (
        "browser retry must not run a second 90s page load after the "
        "fallback budget is gone")
    assert order == ["stealthy", "mirror"], (
        "OA mirror round must run before the browser retry, not after it")
    assert attempted == ["https://example.com/other.pdf"]
    assert BUDGET_EXHAUSTED_LINE in out


def test_journal_custom_goto_timeout_caps_page_fetch(monkeypatch, tmp_path):
    """--browser-goto-timeout 10 caps the Phase A StealthyFetcher fetch
    at 10s even though --timeout stays 60000."""
    seen = {}

    class _RecordingFetcher:
        @staticmethod
        def fetch(url, **kwargs):
            seen.update(kwargs)
            return _page(
                "<html><head><title>T</title></head><body>"
                "<section class='supplementary-materials'>"
                '<a href="https://example.com/S1.xlsx">Supplementary Table S1</a>'
                "</section></body></html>")

    orig_cookies = jd.load_saved_cookies
    jd.load_saved_cookies = lambda: None
    try:
        monkeypatch.setattr(jd, "StealthyFetcher", _RecordingFetcher)
        jd.run_download(
            _jd_args(tmp_path, list_only=True, browser_goto_timeout=10),
            _DisabledRuntime())
    finally:
        jd.load_saved_cookies = orig_cookies
    assert seen["timeout"] == 10000
