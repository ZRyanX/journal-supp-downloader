"""P1 C regression tests: the phase deadline must actually stop page loads.

Round-3 report (~/workspace/jsd-50-paper-test-p1fix-report.md, sec. 2(b),
sec. 2(h) row C, sec. 4 item 3): P1-CE's "budget reached, stop cleanly"
logic fired 0 times in the whole round. Phase B was not stopping
gracefully -- it was killed by the runner at 150s, dead inside a browser
page load (60-90s per page over the proxy; #1's Phase A died on the
"[1/3] Fetching article page" step alone), never reaching the
download loop where the deadline is checked. H-1 capped each single
browser attempt (30s goto / 60s total), but nothing checked the *phase*
deadline before starting the *next* load, and no load timeout was
clamped to the phase time still remaining -- so sequential loads each
took their full slice and cumulatively blew past the deadline.

The fix, on top of H-1's plumbing (no second budget system):
(a) BrowserFallbackBudget becomes phase-aware: one attempt runs at most
    min(goto, total remaining, phase remaining), and the budget counts
    as exhausted once the phase deadline passes, so the fallback quota
    can never exceed the remaining phase budget.
(b) fetch_page_html / the journal_downloader [1/3] chain check the
    deadline before starting each load and right after each load
    returns; a load that is already past the deadline never starts,
    and a load that returns late stops the phase instead of starting
    the next stage.
(c) the graceful-stop line ("Phase timeout reached; N candidate(s) not
    attempted", P1-CE wording) must appear when the phase dies inside
    a page load -- with candidates pending they are not attempted and
    not reported as failed, and the process returns normally instead
    of being killed.

No test here touches the network outside localhost; fetches are
stubbed and time is driven by a fake monotonic clock.
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
    BrowserFallbackBudget,
    Candidate,
    browser_budget_from_args,
)

NOT_ATTEMPTED_LINE = "Phase timeout reached; "
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


# ── (a) the browser budget is clamped by the remaining phase budget ────────

def test_attempt_timeout_clamped_to_remaining_phase_budget(fake_clock):
    """A 150s phase with 140s gone leaves 10s: the next goto may only
    run 10s, even though the goto quota is 30s and the total quota
    (600s here, so it is not the binding one) still has room."""
    budget = BrowserFallbackBudget(
        goto_timeout=30, total_budget=600,
        phase_deadline=fake_clock.now + 150)
    fake_clock.now += 140
    assert budget.attempt_timeout() == 10
    assert budget.attempt_timeout_ms() == 10000
    fake_clock.now += 5
    assert budget.attempt_timeout() == 5


def test_budget_exhausted_when_phase_deadline_passes(fake_clock, capsys):
    """The phase deadline passing exhausts the budget even with total
    quota left -- otherwise a 60s fallback quota could outlive a
    45s phase and keep launching loads after the deadline."""
    budget = BrowserFallbackBudget(
        goto_timeout=30, total_budget=600,
        phase_deadline=fake_clock.now + 150)
    fake_clock.now += 149
    assert not budget.exhausted
    fake_clock.now += 2
    assert budget.exhausted
    assert budget.attempt_timeout() == 0
    budget.announce_exhausted()
    out = capsys.readouterr().out
    assert BUDGET_EXHAUSTED_LINE in out
    assert "阶段预算已到" in out


def test_browser_budget_from_args_accepts_phase_deadline(fake_clock):
    budget = browser_budget_from_args(
        NS(browser_goto_timeout=30, browser_fallback_budget=60),
        phase_deadline=fake_clock.now + 20)
    assert budget.attempt_timeout() == 20
    assert budget.phase_remaining() == 20


def test_no_phase_deadline_keeps_old_behaviour(fake_clock):
    """A budget with no phase deadline behaves exactly as H-1 left it."""
    budget = BrowserFallbackBudget()
    assert budget.phase_remaining() is None
    assert budget.attempt_timeout() == 30
    assert not budget.exhausted


def test_set_phase_deadline_attaches_after_construction(fake_clock):
    budget = BrowserFallbackBudget(goto_timeout=30, total_budget=600)
    budget.set_phase_deadline(fake_clock.now + 12)
    assert budget.attempt_timeout() == 12


# ── (b) fetch_page_html honours the deadline on the load path ──────────────

class _RecordingSession:
    """requests-like session recording get() timeouts."""

    def __init__(self, clock=None, advance=0.0, exc=None, html=None):
        self.calls = []
        self.clock = clock
        self.advance = advance
        self.exc = exc
        self.html = html

    def get(self, url, **kwargs):
        self.calls.append(dict(kwargs))
        if self.clock is not None:
            self.clock.now += self.advance
        if self.exc is not None:
            raise self.exc
        return NS(status_code=200, url=url,
                  text=self.html or "<html><body>ok</body></html>")


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


def test_fetch_page_html_deadline_passed_starts_no_load(monkeypatch, fake_clock):
    """Deadline already gone: neither the requests path nor the browser
    may start a load at all."""
    session = _RecordingSession()
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    html, status, final = ssd.fetch_page_html(
        "https://example.com/article", session=session,
        deadline=fake_clock.now - 1)
    assert (html, status) == ("", 0)
    assert final == "https://example.com/article"
    assert session.calls == []
    assert _RecordingFetcher.calls == []


def test_fetch_page_html_requests_timeout_clamped_to_phase_remaining(
        monkeypatch, fake_clock):
    """The requests path had a hardcoded 30s timeout: with 8s of phase
    left it must run at most 8s, or one cheap load overshoots the
    deadline on its own."""
    session = _RecordingSession()
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    html, status, _ = ssd.fetch_page_html(
        "https://example.com/article", session=session,
        deadline=fake_clock.now + 8)
    assert html and status == 200
    assert session.calls, "requests path was never tried"
    assert session.calls[0]["timeout"] == 8
    assert _RecordingFetcher.calls == []


def test_fetch_page_html_stealthy_timeout_clamped_to_phase_remaining(
        monkeypatch, fake_clock):
    """With 10s of phase left the StealthyFetcher goto must be capped
    at 10s, not the 30s goto quota."""
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    ssd.fetch_page_html("https://example.com/article", session=None,
                        deadline=fake_clock.now + 10)
    assert _RecordingFetcher.calls, "StealthyFetcher.fetch was never called"
    assert _RecordingFetcher.calls[0]["timeout"] == 10000


def test_fetch_page_html_deadline_passed_during_requests_skips_browser(
        monkeypatch, fake_clock):
    """The requests attempt burns the remaining budget (R3: one load
    eats the phase). The browser must not launch afterwards -- the
    load that returns late stops the phase instead of starting the
    next stage."""
    session = _RecordingSession(
        clock=fake_clock, advance=200.0, exc=RuntimeError("blocked"))
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    html, status, _ = ssd.fetch_page_html(
        "https://example.com/article", session=session,
        deadline=fake_clock.now + 150)
    assert (html, status) == ("", 0)
    assert _RecordingFetcher.calls == []


def test_fetch_page_html_explicit_deadline_wins_over_budget_deadline(
        monkeypatch, fake_clock):
    """A caller passing both a budget and a deadline means the
    deadline: the freshest phase instruction caps the attempt."""
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    _RecordingFetcher.calls.clear()
    budget = BrowserFallbackBudget(
        goto_timeout=30, total_budget=600,
        phase_deadline=fake_clock.now + 100)
    ssd.fetch_page_html("https://example.com/article", session=None,
                        browser_budget=budget,
                        deadline=fake_clock.now + 12)
    assert _RecordingFetcher.calls[0]["timeout"] == 12000


# ── (b)+(c) Phase B end to end: dying inside a page load ───────────────────

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


class _FailingSession:
    def get(self, *a, **k):
        raise RuntimeError("requests blocked")


class _RecordingMirrors:
    """MirrorDiscovery stand-in recording whether the round ran."""

    def __init__(self, *a, **k):
        self.discover_calls = 0
        self.info = None

    def discover(self, force=False, on_candidate=None):
        self.discover_calls += 1
        return []


DECLARING_HTML = (
    "<html><head><title>Declaring Article</title></head><body>"
    "<p>The results are shown in Supplementary Table S1.</p>"
    "</body></html>"
)

# Declares S1 and S2 in prose but links only S1: one candidate exists
# and the evidence check still reports the set incomplete, so the OA
# mirror round would run next if the deadline did not stop it.
INCOMPLETE_HTML = (
    "<html><head><title>Incomplete Article</title></head><body>"
    "<p>The results are shown in Supplementary Tables S1 and S2.</p>"
    "<section class='supplementary-materials'>"
    '<a href="https://example.com/S1.xlsx">Supplementary Table S1</a>'
    "</section></body></html>"
)


def test_scansci_page_load_hangs_past_deadline_no_mirror_round(
        tmp_path, fake_clock, monkeypatch, capsys):
    """C repro, Phase B, no-candidate case (#1 R3: '[1/3] Fetching
    article page' alone ate the 150s): the publisher page load hangs
    90s past a 45s phase deadline. The run must return normally with
    an explicit phase-timeout line and must not start the OA mirror
    round afterwards -- pre-fix it ran the mirrors with a dead budget
    and printed no timeout line at all."""
    mirrors_seen = []

    class _HangingFetcher:
        calls = 0

        @staticmethod
        def fetch(url, **kwargs):
            _HangingFetcher.calls += 1
            fake_clock.now += 90.0  # one page load hangs 90s (R3: 120s+)
            return _page(DECLARING_HTML)

    orig_mirror_cls = ssd.MirrorDiscovery

    def _mirror_factory(*a, **k):
        m = _RecordingMirrors(*a, **k)
        mirrors_seen.append(m)
        return m

    monkeypatch.setattr(ssd, "StealthyFetcher", _HangingFetcher)
    monkeypatch.setattr(
        ssd, "_setup_authenticated_session_and_cookies",
        lambda *a, **k: (_FailingSession(), []))
    monkeypatch.setattr(ssd, "MirrorDiscovery", _mirror_factory)
    ssd.run_download(_ssd_args(tmp_path, phase_timeout=45),
                     _DisabledRuntime())
    out = capsys.readouterr().out
    assert _HangingFetcher.calls == 1
    assert mirrors_seen, "MirrorDiscovery was never constructed"
    assert mirrors_seen[0].discover_calls == 0, (
        "the OA mirror round must not start once the phase deadline "
        "has passed inside the page load")
    assert "Phase timeout (45s budget) reached" in out
    assert "No supplementary links found in HTML." in out


def test_scansci_page_load_hangs_past_deadline_candidates_not_attempted(
        tmp_path, fake_clock, monkeypatch, capsys):
    """C repro, Phase B, candidate-pending case: the hanging page load
    returns late with a real supplement link in hand. The P1-CE
    graceful-stop line must appear, the pending candidate must not be
    attempted (and so must not be reported as failed), and the phase
    must return normally -- not be killed mid-load with no line, as
    in R3 (0 occurrences in all logs)."""
    mirrors_seen = []
    download_calls = []

    class _HangingFetcher:
        calls = 0

        @staticmethod
        def fetch(url, **kwargs):
            _HangingFetcher.calls += 1
            fake_clock.now += 90.0
            return _page(INCOMPLETE_HTML)

    def _mirror_factory(*a, **k):
        m = _RecordingMirrors(*a, **k)
        mirrors_seen.append(m)
        return m

    def _fake_download(cand, *a, **k):
        download_calls.append(cand.url)
        return os.path.join(str(tmp_path), cand.filename)

    monkeypatch.setattr(ssd, "StealthyFetcher", _HangingFetcher)
    monkeypatch.setattr(
        ssd, "_setup_authenticated_session_and_cookies",
        lambda *a, **k: (_FailingSession(), []))
    monkeypatch.setattr(ssd, "MirrorDiscovery", _mirror_factory)
    monkeypatch.setattr(ssd, "download_file", _fake_download)
    ssd.run_download(_ssd_args(tmp_path, phase_timeout=45),
                     _DisabledRuntime())
    out = capsys.readouterr().out
    assert _HangingFetcher.calls == 1, (
        "no second browser load may start after the deadline")
    assert mirrors_seen[0].discover_calls == 0
    assert f"{NOT_ATTEMPTED_LINE}1 candidate(s) not attempted" in out, (
        "the P1-CE graceful-stop line must appear when the phase dies "
        "inside a page load, not only inside the download loop")
    assert download_calls == [], (
        "the pending candidate must not be attempted after the deadline")
    assert "FAIL" not in out
    assert "Unresolved downloads" not in out
    assert "Done: 0/0" in out


# ── (b)+(c) Phase A end to end: the [1/3] chain respects the deadline ──────

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


def test_journal_page_fetch_timeout_clamped_to_phase_remaining(
        tmp_path, fake_clock, monkeypatch):
    """A 20s phase leaves 20s: the [1/3] page fetch must be capped at
    20s, not the 30s goto quota -- otherwise the first load alone
    overshoots the phase (R3 #1)."""
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

    monkeypatch.setattr(jd, "StealthyFetcher", _RecordingFetcher)
    monkeypatch.setattr(jd, "load_saved_cookies", lambda: None)
    jd.run_download(
        _jd_args(tmp_path, list_only=True, phase_timeout=20),
        _DisabledRuntime())
    assert seen["timeout"] == 20000


def test_journal_hanging_fetch_skips_http_fallback(
        tmp_path, fake_clock, monkeypatch, capsys):
    """C repro, Phase A chain: the StealthyFetcher load hangs 90s past
    a 45s deadline and raises. The HTTP direct fallback is another
    page load -- it must not start once the deadline has passed, and
    the run must still end with the explicit phase-timeout line."""
    fetch_calls = []

    class _HangingFetcher:
        @staticmethod
        def fetch(url, **kwargs):
            fetch_calls.append(dict(kwargs))
            fake_clock.now += 90.0
            raise RuntimeError("Timeout 60000ms exceeded")

    class _RecordingFetcher:
        calls = []

        @staticmethod
        def get(url, **kwargs):
            _RecordingFetcher.calls.append(dict(kwargs))
            raise RuntimeError("http fallback must not run")

    monkeypatch.setattr(jd, "StealthyFetcher", _HangingFetcher)
    monkeypatch.setattr(jd, "Fetcher", _RecordingFetcher)
    monkeypatch.setattr(jd, "load_saved_cookies", lambda: None)
    jd.run_download(_jd_args(tmp_path, phase_timeout=45),
                    _DisabledRuntime())
    out = capsys.readouterr().out
    assert len(fetch_calls) == 1
    assert _RecordingFetcher.calls == [], (
        "the HTTP direct fallback must not start a new load after "
        "the phase deadline passed inside the browser fetch")
    assert "Phase timeout (45s budget) reached with no candidates" in out


def test_journal_mirror_fetch_timeout_clamped_to_phase_remaining(
        tmp_path, fake_clock, monkeypatch):
    """The OA mirror's Fetcher.get had a hardcoded 20s timeout: with
    8s of phase left it must run at most 8s."""
    fetch_calls = []

    class _SlowFetcher:
        @staticmethod
        def fetch(url, **kwargs):
            fake_clock.now += 142.0  # publisher fetch eats almost all 150s
            return _page(DECLARING_HTML)

    class _RecordingFetcher:
        calls = []

        @staticmethod
        def get(url, **kwargs):
            _RecordingFetcher.calls.append(dict(kwargs))
            raise RuntimeError("mirror unreachable")

    def _fake_resolve_oa(doi, **kwargs):
        return NS(oa_landing_urls=["https://pmc.example.org/article"],
                  title=None)

    monkeypatch.setattr(jd, "StealthyFetcher", _SlowFetcher)
    monkeypatch.setattr(jd, "Fetcher", _RecordingFetcher)
    monkeypatch.setattr(jd, "resolve_oa_info", _fake_resolve_oa)
    monkeypatch.setattr(jd, "load_saved_cookies", lambda: None)
    # A DOI input (not a bare publisher URL) so the mirror round has a
    # DOI to resolve; without one MirrorDiscovery returns early.
    jd.run_download(
        _jd_args(tmp_path, url="https://doi.org/10.1038/s41467-025-61134-0",
                 phase_timeout=150, no_oa=False),
        _DisabledRuntime())
    assert _RecordingFetcher.calls, "mirror Fetcher.get was never called"
    assert _RecordingFetcher.calls[0]["timeout"] == 8


# ── (a) the two budgets document the same semantics ─────────────────────────

def test_phase_and_browser_budget_help_documents_coordination():
    """--phase-timeout / --browser-goto-timeout / --browser-fallback-
    budget must document one shared rule: a load never runs past the
    remaining phase budget."""
    for script in ("journal_downloader.py", "scansci_supp_downloader.py"):
        out = os.popen(
            f"{sys.executable} {Path(__file__).resolve().parents[1] / 'scripts' / script} --help"
        ).read()
        assert "remaining phase budget" in out, script
