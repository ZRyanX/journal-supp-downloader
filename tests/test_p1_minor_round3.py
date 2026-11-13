"""P1FIX round-3 minor issues (report sec. 4, "次要问题").

Three small items left over from the third 50-paper run
(~/workspace/jsd-50-paper-test-p1fix-report.md):

(a) Wiley ``ERR_CERT_AUTHORITY_INVALID`` on retry: the same page's first
    goto timed out and the retry failed with a certificate error, i.e.
    the MITM proxy's CA trust did not hold across the fresh browser
    context / retry path. No call site passes any explicit certificate
    trust config today -- trust is entirely implicit in scrapling's
    default -- so a fresh context or retry can run without the same
    trust config as the first context. The fix is one shared helper,
    ``proxy_browser_trust_kwargs(proxy)``: empty with no proxy (cert
    validation untouched), and -- only in proxy mode -- the context
    level ``additional_args={"ignore_https_errors": True}`` plus the
    browser-launch level ``extra_flags=["--ignore-certificate-errors"]``,
    merged into every StealthyFetcher.fetch / StealthySession call so
    first call and every retry carry identical proxy + trust config.
    CDP / BrowserSkill paths attach to a live browser and cannot take a
    per-request proxy or trust config; they are intentionally untouched.

(b) Twingate false candidate (round 3, #50): PMC puts
    ``data-ga-action="click_feat_suppl"`` on *every* external link
    (verified live on PMC12434933: the DOI link itself and the
    Twingate reference link both carry it), so the
    ``pmc_feat_suppl_link`` rule minted the Twingate blog glossary
    link as a supplement. The rule must require the link to actually
    point at supplementary content (a /bin/ attachment, a data
    extension, a supplementary keyword / table filename / MMC id /
    repository DOI in the URL, link text or filename).

(c) Zenodo data packages crowd out real s files (round 3, #31): the
    72 candidates mixed Zenodo .csv/.zip packages with the 17 true
    PMC /bin/ s files, and on Phase B timeout none of the s files had
    downloaded. ``sort_candidates_by_priority`` now orders PMC /bin/
    s files before Zenodo / Data Availability repository packages
    *within* each tier; the tier values from ``candidate_priority``
    (0 real file / 1 explicitly supplementary / 2 other) are unchanged.

No test here touches the network; fetches and browser sessions are stubbed.
"""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import journal_downloader as jd  # noqa: E402
import scansci_supp_downloader as ssd  # noqa: E402
from supp_finder import (  # noqa: E402
    Candidate,
    candidate_priority,
    find_all_candidates,
    proxy_browser_trust_kwargs,
    sort_candidates_by_priority,
)

PROXY = "http://127.0.0.1:18080"
PROXY_AUTH = "http://user:pass@127.0.0.1:18080"
WILEY_URL = "https://advanced.onlinelibrary.wiley.com/doi/10.1002/advs.202500895"


# ── (a) proxy CA / certificate trust follows first call and retries ────────

def test_proxy_trust_kwargs_empty_without_proxy():
    """No proxy means no trust relaxation at all: cert validation is
    untouched for direct (non-proxied) runs."""
    assert proxy_browser_trust_kwargs(None) == {}
    assert proxy_browser_trust_kwargs("") == {}


def test_proxy_trust_kwargs_present_and_identical_every_call():
    """In proxy mode the helper returns the same trust config on every
    call -- first context and retry contexts get identical values --
    and each call returns a fresh dict (mutating one must not leak
    into the next)."""
    first = proxy_browser_trust_kwargs(PROXY)
    retry = proxy_browser_trust_kwargs(PROXY)
    assert first == retry
    assert first["additional_args"] == {"ignore_https_errors": True}
    assert first["extra_flags"] == ["--ignore-certificate-errors"]
    first["additional_args"]["ignore_https_errors"] = False
    assert retry["additional_args"] == {"ignore_https_errors": True}
    # An authenticated proxy gets the same trust config.
    assert proxy_browser_trust_kwargs(PROXY_AUTH) == retry


class _CertErrorFetcher:
    """Records StealthyFetcher.fetch kwargs; first call dies with the
    round-3 Wiley certificate error, the retry succeeds."""

    calls = []
    fail_first_with = "Page.goto: net::ERR_CERT_AUTHORITY_INVALID at " + WILEY_URL

    @classmethod
    def fetch(cls, url, **kwargs):
        cls.calls.append(dict(kwargs))
        if len(cls.calls) == 1:
            raise RuntimeError(cls.fail_first_with)
        return NS(html_content="<html>ok</html>", text="<html>ok</html>",
                  url=url, status=200)


def test_fetch_page_html_retry_carries_same_proxy_trust_as_first(monkeypatch):
    """Reproduces round-3 (a): the no-CDP retry after a cert error must
    carry the same proxy + trust config as the first attempt, not a
    bare context."""
    _CertErrorFetcher.calls = []
    monkeypatch.setattr(ssd, "StealthyFetcher", _CertErrorFetcher)
    monkeypatch.setattr(ssd, "fetch_page_via_cdp",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no live CDP browser")))
    html, status, _ = ssd.fetch_page_html(
        WILEY_URL, session=None, proxy=PROXY,
        cdp_url="http://127.0.0.1:9222",
    )
    assert status == 200 and "ok" in html
    assert len(_CertErrorFetcher.calls) == 2, _CertErrorFetcher.calls
    first, retry = _CertErrorFetcher.calls
    assert first.get("proxy") == PROXY
    assert retry.get("proxy") == PROXY
    assert retry.get("additional_args") == first.get("additional_args") == {
        "ignore_https_errors": True}
    assert retry.get("extra_flags") == first.get("extra_flags") == [
        "--ignore-certificate-errors"]
    assert "cdp_url" in first and "cdp_url" not in retry


def test_fetch_page_html_without_proxy_passes_no_trust_kwargs(monkeypatch):
    _CertErrorFetcher.calls = []
    _CertErrorFetcher.fail_first_with = "boom"
    monkeypatch.setattr(ssd, "StealthyFetcher", _CertErrorFetcher)
    monkeypatch.setattr(ssd, "fetch_page_via_cdp",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no live CDP browser")))
    try:
        html, status, _ = ssd.fetch_page_html(
            WILEY_URL, session=None, cdp_url="http://127.0.0.1:9222")
    finally:
        _CertErrorFetcher.fail_first_with = (
            "Page.goto: net::ERR_CERT_AUTHORITY_INVALID at " + WILEY_URL)
    assert status == 200
    assert len(_CertErrorFetcher.calls) == 2
    for call in _CertErrorFetcher.calls:
        assert "proxy" not in call
        assert "additional_args" not in call
        assert "extra_flags" not in call


def test_download_file_stealthy_session_gets_proxy_trust(monkeypatch, tmp_path):
    """Method 3 (StealthySession, one fresh context per candidate) gets
    the Playwright proxy dict AND the same trust config as the fetch
    path."""
    captured = {}

    class _CapturingSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError("no browser in this test")

    class _FailingFetcher:
        @staticmethod
        def get(url, **kwargs):
            raise RuntimeError("no plain http in this test")

    monkeypatch.setattr(ssd, "Fetcher", _FailingFetcher)
    monkeypatch.setattr("scrapling.fetchers.StealthySession", _CapturingSession)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: NS(returncode=7))
    result = ssd.download_file(
        "https://blocked.example/mmc1.xlsx", str(tmp_path),
        session=None, proxy=PROXY_AUTH,
        tunnel_tracker=ssd.TunnelFailureTracker(),
    )
    assert result is None
    assert captured.get("proxy") == {
        "server": "http://127.0.0.1:18080",
        "username": "user",
        "password": "pass",
    }
    assert captured.get("additional_args") == {"ignore_https_errors": True}
    assert captured.get("extra_flags") == ["--ignore-certificate-errors"]


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


_PUB_HTML = (
    "<html><head><title>Order Article</title></head><body>"
    "<section class='supplementary-materials'>"
    '<a href="https://example.com/S1.xlsx">Supplementary Table S1</a>'
    '<a href="https://example.com/S2.csv">Supplementary Table S2</a>'
    "</section></body></html>"
)


def test_journal_downloader_fetch_carries_proxy_trust_and_none_means_none(
        monkeypatch, tmp_path):
    """journal_downloader builds fetch_kwargs once and every later
    fetch (first / CDP retry / mirror / browser retry) copies it, so
    the trust config rides along everywhere -- and with no proxy no
    trust key is set at all."""
    seen = []

    def _recording_fetch(url, **kwargs):
        seen.append(dict(kwargs))
        return NS(html_content=_PUB_HTML, text=_PUB_HTML,
                  url="https://example.com/article", status=200)

    orig_fetch = jd.StealthyFetcher.fetch
    orig_cookies = jd.load_saved_cookies
    jd.StealthyFetcher.fetch = staticmethod(_recording_fetch)
    jd.load_saved_cookies = lambda: None
    try:
        jd.run_download(_jd_args(tmp_path, proxy=PROXY), _DisabledRuntime())
    finally:
        jd.StealthyFetcher.fetch = orig_fetch
        jd.load_saved_cookies = orig_cookies
    assert seen, "no browser fetch happened"
    for call in seen:
        assert call.get("proxy") == PROXY
        assert call.get("additional_args") == {"ignore_https_errors": True}
        assert call.get("extra_flags") == ["--ignore-certificate-errors"]

    seen.clear()
    jd.StealthyFetcher.fetch = staticmethod(_recording_fetch)
    jd.load_saved_cookies = lambda: None
    try:
        jd.run_download(_jd_args(tmp_path, proxy=None), _DisabledRuntime())
    finally:
        jd.StealthyFetcher.fetch = orig_fetch
        jd.load_saved_cookies = orig_cookies
    assert seen, "no browser fetch happened without proxy either"
    for call in seen:
        assert "additional_args" not in call
        assert "extra_flags" not in call


# ── (b) pmc_feat_suppl_link must point at real supplementary content ──────

_PMC50_HTML = (
    "<html><head><title>Corrigendum</title></head><body>"
    "<article>"
    # The DOI link itself carries click_feat_suppl on PMC (verified live
    # on PMC12434933) -- it is not a supplement either.
    '<a href="https://doi.org/10.1016/j.heliyon.2025.e43506" '
    'class="usa-link usa-link--external" '
    'data-ga-action="click_feat_suppl">10.1016/j.heliyon.2025.e43506</a>'
    "<p><em>[55] Twingate. (n.d.). Birthday attack.</em> "
    '<a href="https://www.twingate.com/blog/glossary/birthday%20attack" '
    'class="usa-link usa-link--external" '
    'data-ga-action="click_feat_suppl">'
    "https://www.twingate.com/blog/glossary/birthday attack</a></p>"
    "</article></body></html>"
)
_PMC50_BASE = "https://pmc.ncbi.nlm.nih.gov/articles/PMC12434933/"


def test_pmc_feat_suppl_link_rejects_twingate_blog_link():
    """Reproduces round-3 #50: the Twingate blog glossary link (a
    reference link that merely carries PMC's generic external-link
    tracker) must not become a supplement candidate."""
    diag = find_all_candidates(_PMC50_HTML, base_url=_PMC50_BASE)
    urls = [c.url for c in diag.candidates]
    assert not any("twingate.com" in u for u in urls), urls
    assert not any(c.match_rule == "pmc_feat_suppl_link"
                   for c in diag.candidates), [
                       (c.url, c.match_rule) for c in diag.candidates]


def test_pmc_feat_suppl_link_rejects_doi_link():
    """Same mechanism, second witness: the article's own DOI link also
    carries click_feat_suppl on PMC and is not a supplement."""
    diag = find_all_candidates(_PMC50_HTML, base_url=_PMC50_BASE)
    urls = [c.url for c in diag.candidates]
    assert not any("doi.org/10.1016" in u for u in urls), urls


_PMC_REAL_HTML = (
    "<html><head><title>Real PMC article</title></head><body>"
    "<article>"
    # Genuine featured supplement: a /bin/ s file with a supplement label.
    '<a href="https://pmc.ncbi.nlm.nih.gov/articles/PMC12186953/bin/'
    'pbio.3003193.s011.xlsx" data-ga-action="click_feat_suppl">'
    "S1 Data</a>"
    # Genuine featured supplement: an off-site supplementary file with an
    # explicit supplementary label (no /bin/ in the URL).
    '<a href="https://static-content.springer.com/esm/art%3A10.1038%2F'
    's41467-025-61134-0/MediaObjects/41467_2025_61134_MOESM1_ESM.pdf" '
    'data-ga-action="click_feat_suppl">Supplementary Information</a>'
    # Genuine featured supplement: a repository DOI with a supplementary
    # label.
    '<a href="https://doi.org/10.5281/zenodo.15306735" '
    'data-ga-action="click_feat_suppl">Supplementary data on Zenodo</a>'
    "</article></body></html>"
)
_PMC_REAL_BASE = "https://pmc.ncbi.nlm.nih.gov/articles/PMC12186953/"


def test_pmc_feat_suppl_link_keeps_real_supplement_links():
    """The tightening must not kill real featured supplement links:
    /bin/ s files, explicitly labelled supplementary files, and
    repository-DOI supplement links all still become candidates."""
    diag = find_all_candidates(_PMC_REAL_HTML, base_url=_PMC_REAL_BASE)
    urls = [c.url for c in diag.candidates]
    assert any("pbio.3003193.s011.xlsx" in u for u in urls), urls
    assert any("41467_2025_61134_MOESM1_ESM.pdf" in u for u in urls), urls
    assert any("10.5281/zenodo.15306735" in u for u in urls), urls


# ── (c) PMC /bin/ s files before Zenodo packages, tiers unchanged ──────────

def _s_file(name, ext):
    return Candidate(
        url=f"https://pmc.ncbi.nlm.nih.gov/articles/PMC12186953/bin/"
            f"pbio.3003193.{name}.{ext}",
        filename=f"pbio.3003193.{name}.{ext}",
        link_text="Download", section="pmc_bin_attachment",
        match_rule="pmc_bin_link")


def _zenodo_file(name):
    return Candidate(
        url=f"https://zenodo.org/records/15306735/files/{name}",
        filename=name, link_text="data file", section="page_body",
        match_rule="data_extension:.csv")


def test_sort_puts_pmc_bin_s_files_before_zenodo_packages():
    """Reproduces round-3 #31: in a mixed list the PMC /bin/ s files
    sort before the Zenodo data packages -- within each tier, so the
    0/1/2 tier values do not move."""
    s_xlsx = _s_file("s011", "xlsx")          # tier 0 (data extension)
    s_tiff = _s_file("s001", "tiff")          # tier 2 (no data extension)
    zen_csv = _zenodo_file("data.csv")        # tier 0 (data extension)
    zen_zip = _zenodo_file("package.zip")     # tier 0 (data extension)
    zen_landing = Candidate(                  # tier 2 (repository page)
        url="https://zenodo.org/records/15306735", filename="15306735",
        link_text="Data Availability", section="page_body",
        match_rule="repository_link")
    plain_xlsx = Candidate(                   # tier 0 control (not s, not Zenodo)
        url="https://example.com/S1.xlsx", filename="S1.xlsx",
        link_text="Supplementary Table S1", section="supplementary_section",
        match_rule="supp_section_link")
    plain_page = Candidate(                   # tier 2 control
        url="https://example.com/other-page", filename="other-page",
        link_text="data", section="page_body", match_rule="generic_link")

    cands = [zen_csv, s_tiff, plain_page, zen_zip, zen_landing,
             plain_xlsx, s_xlsx]
    # Tiers are unchanged by this fix (lock the 0/1/2 grading in place).
    assert candidate_priority(s_xlsx) == 0
    assert candidate_priority(zen_csv) == 0
    assert candidate_priority(zen_zip) == 0
    assert candidate_priority(plain_xlsx) == 0
    assert candidate_priority(s_tiff) == 2
    assert candidate_priority(zen_landing) == 2
    assert candidate_priority(plain_page) == 2

    ordered = sort_candidates_by_priority(cands)
    assert len(ordered) == len(cands)
    assert {c.url for c in ordered} == {c.url for c in cands}
    # Tier 0: both s files... only s_xlsx is tier 0 here; it and the
    # plain tier-0 file come before every Zenodo package.
    tier0 = [c for c in ordered if candidate_priority(c) == 0]
    assert [c.url for c in tier0] == [
        s_xlsx.url, plain_xlsx.url, zen_csv.url, zen_zip.url], tier0
    # Tier 2: the s tiff comes before the Zenodo / Data Availability
    # package (and the neutral page sits between them: only s files
    # are promoted and repository packages demoted within a tier).
    tier2 = [c for c in ordered if candidate_priority(c) == 2]
    assert [c.url for c in tier2] == [
        s_tiff.url, plain_page.url, zen_landing.url], tier2


def test_sort_still_stable_and_lossless_for_non_zenodo_lists():
    """The sub-ranking only separates s files from repository packages;
    ordinary lists keep their previous order."""
    a = Candidate(url="https://example.com/a.csv", filename="a.csv")
    b = Candidate(url="https://example.com/b.zip", filename="b.zip")
    assert sort_candidates_by_priority([b, a]) == [b, a]
    assert sort_candidates_by_priority([]) == []
