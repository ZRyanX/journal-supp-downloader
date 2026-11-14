"""Tests for priority-ordered automatic live-browser & headful Chrome recovery."""

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import supp_finder as sf  # noqa: E402
import journal_downloader as jd  # noqa: E402
import scansci_supp_downloader as ssd  # noqa: E402


GOOD_SUPP_HTML = """
<html><head><title>Antimony Mineralization Study</title></head>
<body>
  <main>
    <p>Geological setting and analytical methods covering more than enough article text
    so the page is not classified as a stub shell. See Supplementary Table S1.</p>
    <section id="supplementary-data">
      <h2>Supplementary Data</h2>
      <a href="/files/Table_S1.xlsx">Table S1.xlsx</a>
    </section>
  </main>
</body></html>
""" + ("x" * 2600)

CF_BLOCKED_HTML = "<html><head><title>Just a moment...</title></head><body>Checking your browser before accessing...</body></html>"


def _jd_args(tmp_path, **overrides):
    defaults = dict(
        url="https://publisher.example/article/10.1000/test",
        output_dir=str(tmp_path),
        headful=False,
        proxy=None,
        timeout=20000,
        phase_timeout=60,
        browser_goto_timeout=15,
        browser_fallback_budget=40,
        solve_cloudflare=True,
        no_cloudflare=False,
        wait_selector=None,
        real_chrome=False,
        cdp=False,
        cdp_url=None,
        no_cdp=False,
        no_auto_headful=False,
        browserskill=False,
        bsk_auto=False,
        no_bsk=False,
        bsk_browser=None,
        bsk_tab=None,
        list_only=True,
        oa=False,
        no_oa=True,
        convert_markdown=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _ssd_args(tmp_path, **overrides):
    defaults = dict(
        url_or_doi="https://publisher.example/article/10.1000/test",
        output_dir=str(tmp_path),
        no_vpn=True,
        no_cookies=True,
        no_api=True,
        headful=False,
        skip_cdn=True,
        max_mmc=5,
        max_misses=2,
        proxy=None,
        tunnel_fail_threshold=3,
        phase_timeout=60,
        browser_goto_timeout=15,
        browser_fallback_budget=40,
        list_only=True,
        oa=False,
        no_oa=True,
        cdp=False,
        cdp_url=None,
        no_cdp=False,
        no_auto_headful=False,
        browserskill=False,
        bsk_auto=False,
        no_bsk=False,
        bsk_browser=None,
        bsk_tab=None,
        convert_markdown=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ── Shared helper & priority ordering ────────────────────────────────────────


def test_run_auto_browser_recovery_prioritizes_active_bsk_over_cdp_and_headful():
    called = []
    out = sf.run_auto_browser_recovery(
        need_recovery=True,
        retry_allowed=True,
        browser_allowed_fn=lambda: True,
        try_bsk_fn=lambda: (called.append("bsk") or ("<html>ok</html>", 200, "https://e.com", None)),
        try_cdp_fn=lambda: (called.append("cdp") or ("<html>cdp</html>", 200, "https://e.com", None)),
        try_headful_fn=lambda: (called.append("headful") or ("<html>hf</html>", 200, "https://e.com", None)),
        evaluate_fn=lambda *a: (True, True),
    )
    assert called == ["bsk"]
    assert out["mode"] == "auto_bsk"
    assert out["recovered"] is True


def test_run_auto_browser_recovery_falls_back_to_headful_when_live_browsers_absent_or_fail():
    called = []

    def _eval(html_str, status, final_url, extra_cookies, mode):
        if mode == "auto_cdp":
            return False, False
        return True, True

    out = sf.run_auto_browser_recovery(
        need_recovery=True,
        retry_allowed=True,
        browser_allowed_fn=lambda: True,
        try_bsk_fn=lambda: (called.append("bsk") and None),
        try_cdp_fn=lambda: (called.append("cdp") or ("<html>partial</html>", 200, "https://e.com", None)),
        try_headful_fn=lambda: (called.append("headful") or ("<html>full</html>", 200, "https://e.com", None)),
        evaluate_fn=_eval,
    )
    assert called == ["bsk", "cdp", "headful"]
    assert out["mode"] == "auto_headful"
    assert out["recovered"] is True


def test_run_auto_browser_recovery_stops_when_budget_exhausted():
    called = []
    out = sf.run_auto_browser_recovery(
        need_recovery=True,
        retry_allowed=True,
        browser_allowed_fn=lambda: False,
        try_bsk_fn=lambda: called.append("bsk"),
        try_cdp_fn=lambda: called.append("cdp"),
        try_headful_fn=lambda: called.append("headful"),
        evaluate_fn=lambda *a: (True, True),
    )
    assert called == []
    assert out["recovered"] is False


# ── journal_downloader.py ────────────────────────────────────────────────────


def test_journal_downloader_auto_headful_rescue_and_network_idle_false(monkeypatch, tmp_path):
    fetch_calls = []

    class _BlockedFirstThenHeadfulFetcher:
        @staticmethod
        def fetch(url, **kwargs):
            fetch_calls.append(dict(kwargs))
            if not kwargs.get("headless", True):
                return SimpleNamespace(
                    url=url,
                    status=200,
                    html_content=GOOD_SUPP_HTML,
                    text=GOOD_SUPP_HTML,
                    cookies=[{"name": "cf_clearance", "value": "ok", "domain": ".publisher.example", "path": "/"}],
                )
            return SimpleNamespace(url=url, status=403, html_content=CF_BLOCKED_HTML, text=CF_BLOCKED_HTML)

    monkeypatch.setattr(jd, "load_saved_cookies", lambda: [])
    monkeypatch.setattr(jd, "load_local_skill_config", lambda: {})
    monkeypatch.setattr(jd, "StealthyFetcher", _BlockedFirstThenHeadfulFetcher)
    monkeypatch.setattr(jd, "Fetcher", SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status=403, html_content=CF_BLOCKED_HTML, text=CF_BLOCKED_HTML)))
    monkeypatch.setattr(jd, "check_browserskill_status", lambda **k: {"available": False, "ready": False})
    monkeypatch.setattr(jd, "discover_active_cdp_port", lambda **k: None)
    monkeypatch.setattr(jd, "has_gui_display", lambda: True)
    monkeypatch.setattr(jd, "has_system_chrome", lambda: True)

    dl_kwargs = []
    monkeypatch.setattr(
        jd,
        "download_file",
        lambda cand, out_dir, **kw: dl_kwargs.append(dict(kw)) or str(Path(out_dir) / "Table_S1.xlsx"),
    )
    monkeypatch.setattr(jd, "download_with_recovery", lambda cands, fn, *a, **k: ([c for c in cands if fn(c)], len(cands)))

    args = _jd_args(tmp_path, list_only=False)
    bsk_rt = SimpleNamespace(enabled=False, browser=None, tab_id=None)
    jd.run_download(args, bsk_runtime=bsk_rt)

    assert len(fetch_calls) >= 2
    assert all(c["network_idle"] is False for c in fetch_calls)
    headful_calls = [c for c in fetch_calls if c.get("headless") is False]
    assert headful_calls, "Expected automatic headful Chrome fallback on CF block"
    assert headful_calls[0].get("real_chrome") is True
    assert headful_calls[0].get("solve_cloudflare") is True
    assert dl_kwargs and dl_kwargs[0]["headful"] is True and dl_kwargs[0]["real_chrome"] is True


def test_journal_downloader_auto_cdp_wins_over_headful_when_active(monkeypatch, tmp_path):
    fetch_calls = []

    monkeypatch.setattr(jd, "load_saved_cookies", lambda: [])
    monkeypatch.setattr(jd, "load_local_skill_config", lambda: {})
    monkeypatch.setattr(
        jd,
        "StealthyFetcher",
        SimpleNamespace(
            fetch=lambda url, **kw: fetch_calls.append(dict(kw))
            or SimpleNamespace(url=url, status=403, html_content=CF_BLOCKED_HTML, text=CF_BLOCKED_HTML)
        ),
    )
    monkeypatch.setattr(jd, "Fetcher", SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status=403, html_content=CF_BLOCKED_HTML, text=CF_BLOCKED_HTML)))
    monkeypatch.setattr(jd, "check_browserskill_status", lambda **k: {"available": False, "ready": False})
    monkeypatch.setattr(jd, "discover_active_cdp_port", lambda **k: ("http://127.0.0.1:9222", "ws://127.0.0.1:9222/devtools/browser/1"))
    monkeypatch.setattr(jd, "extract_cdp_cookies", lambda ep: [{"name": "sid", "value": "123"}])
    monkeypatch.setattr(jd, "fetch_page_via_cdp", lambda ep, u, **kw: (GOOD_SUPP_HTML, 200, u, []))
    monkeypatch.setattr(jd, "has_gui_display", lambda: True)

    args = _jd_args(tmp_path)
    jd.run_download(args, bsk_runtime=SimpleNamespace(enabled=False, browser=None, tab_id=None))

    assert not any(c.get("headless") is False for c in fetch_calls), "Active CDP should avoid spawning headful window"


def test_journal_downloader_respects_no_auto_headful_and_no_cdp(monkeypatch, tmp_path):
    fetch_calls = []
    cdp_called = []

    monkeypatch.setattr(jd, "load_saved_cookies", lambda: [])
    monkeypatch.setattr(jd, "load_local_skill_config", lambda: {})
    monkeypatch.setattr(
        jd,
        "StealthyFetcher",
        SimpleNamespace(
            fetch=lambda url, **kw: fetch_calls.append(dict(kw))
            or SimpleNamespace(url=url, status=403, html_content=CF_BLOCKED_HTML, text=CF_BLOCKED_HTML)
        ),
    )
    monkeypatch.setattr(jd, "Fetcher", SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status=403, html_content=CF_BLOCKED_HTML, text=CF_BLOCKED_HTML)))
    monkeypatch.setattr(jd, "check_browserskill_status", lambda **k: {"available": False, "ready": False})
    monkeypatch.setattr(jd, "discover_active_cdp_port", lambda **k: cdp_called.append(True) or ("http://127.0.0.1:9222", "ws://..."))
    monkeypatch.setattr(jd, "has_gui_display", lambda: True)

    args = _jd_args(tmp_path, no_cdp=True, no_auto_headful=True, no_bsk=True)
    jd.run_download(args, bsk_runtime=SimpleNamespace(enabled=False, browser=None, tab_id=None))

    assert not cdp_called
    assert not any(c.get("headless") is False for c in fetch_calls)


# ── scansci_supp_downloader.py ───────────────────────────────────────────────


def test_scansci_auto_headful_rescue_when_live_browsers_offline(monkeypatch, tmp_path):
    fetch_calls = []

    def _fake_fetch_page_html(url, session, play_cookies, headful, **kwargs):
        fetch_calls.append({"headful": headful, "real_chrome": kwargs.get("real_chrome", False), "cdp": kwargs.get("cdp_url")})
        if headful:
            return GOOD_SUPP_HTML, 200, url
        return "", 0, url

    monkeypatch.setattr(ssd, "load_config", lambda: {})
    monkeypatch.setattr(ssd, "_setup_authenticated_session_and_cookies", lambda *a, **k: (SimpleNamespace(), []))
    monkeypatch.setattr(ssd, "fetch_page_html", _fake_fetch_page_html)
    monkeypatch.setattr(ssd, "check_browserskill_status", lambda **k: {"available": False, "ready": False})
    monkeypatch.setattr(ssd, "discover_active_cdp_port", lambda **k: None)
    monkeypatch.setattr(ssd, "has_gui_display", lambda: True)
    monkeypatch.setattr(ssd, "has_system_chrome", lambda: True)

    dl_kwargs = []
    monkeypatch.setattr(
        ssd,
        "download_file",
        lambda cand, out_dir, *a, **kw: dl_kwargs.append(dict(kw)) or str(Path(out_dir) / "Table_S1.xlsx"),
    )
    monkeypatch.setattr(ssd, "download_with_recovery", lambda cands, fn, *a, **k: ([c for c in cands if fn(c)], len(cands)))

    args = _ssd_args(tmp_path, list_only=False)
    ssd.run_download(args, bsk_runtime=SimpleNamespace(enabled=False, browser=None, tab_id=None))

    assert any(c["headful"] is True and c["real_chrome"] is True for c in fetch_calls)
    assert dl_kwargs and dl_kwargs[0]["headful"] is True and dl_kwargs[0]["real_chrome"] is True


def test_scansci_skips_auto_recovery_when_tier_api_already_satisfied(monkeypatch, tmp_path):
    api_file = tmp_path / "supplements" / "mmc1.xlsx"
    api_file.parent.mkdir(parents=True, exist_ok=True)
    api_file.write_bytes(b"PK\x03\x04dummy-xlsx")

    fetch_calls = []

    def _fake_fetch_page_html(url, session, play_cookies, headful, **kwargs):
        fetch_calls.append({"headful": headful})
        return "", 0, url

    monkeypatch.setattr(ssd, "load_config", lambda: {})
    monkeypatch.setattr(ssd, "resolve_doi_url", lambda d: "https://www.sciencedirect.com/science/article/pii/S0000000000000001")
    monkeypatch.setattr(ssd, "try_elsevier_api_campus", lambda *a, **k: ([str(api_file)], {1}))
    monkeypatch.setattr(ssd, "_setup_authenticated_session_and_cookies", lambda *a, **k: (SimpleNamespace(), []))
    monkeypatch.setattr(ssd, "fetch_page_html", _fake_fetch_page_html)
    monkeypatch.setattr(ssd, "check_browserskill_status", lambda **k: (_ for _ in ()).throw(AssertionError("should not probe bsk")))
    monkeypatch.setattr(ssd, "discover_active_cdp_port", lambda **k: (_ for _ in ()).throw(AssertionError("should not probe cdp")))
    monkeypatch.setattr(ssd, "has_gui_display", lambda: True)

    args = _ssd_args(tmp_path, url_or_doi="10.1016/j.fake.2024.01.001", no_api=False, list_only=False)
    ssd.run_download(args, bsk_runtime=SimpleNamespace(enabled=False, browser=None, tab_id=None))

    assert not any(c["headful"] for c in fetch_calls)


def test_scansci_fetch_page_html_passes_network_idle_false_and_real_chrome(monkeypatch):
    captured = {}

    class _FakeStealth:
        @staticmethod
        def fetch(url, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(url=url, status=200, html_content=GOOD_SUPP_HTML)

    monkeypatch.setattr(ssd, "StealthyFetcher", _FakeStealth)
    html_s, st, _ = ssd.fetch_page_html(
        "https://publisher.example/article",
        None,
        [],
        headful=True,
        real_chrome=True,
    )
    assert st == 200 and "Table S1" in html_s
    assert captured["headless"] is False
    assert captured["real_chrome"] is True
    assert captured["network_idle"] is False
    assert captured["solve_cloudflare"] is True


def test_stealthy_fetch_bounded_caps_domcontentloaded_and_merges_slow_domains(monkeypatch):
    from scrapling.fetchers import StealthyFetcher

    captured = {"dcl_timeouts": [], "load_timeouts": []}

    class _FakePage:
        def set_default_timeout(self, ms):
            pass

        def set_default_navigation_timeout(self, ms):
            pass

        def wait_for_load_state(self, state="load", timeout=None):
            if state == "domcontentloaded":
                captured["dcl_timeouts"].append(timeout)
                raise TimeoutError("simulated slow GSW domcontentloaded")
            if state == "load":
                captured["load_timeouts"].append(timeout)
                raise TimeoutError("simulated slow GSW load")

        def on(self, event, fn):
            pass

    class _FakeSession:
        def __init__(self, **kw):
            captured["session_kw"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def fetch(self, url, **kw):
            p = _FakePage()
            kw["page_setup"](p)
            assert p.wait_for_load_state("load") is None
            assert p.wait_for_load_state("domcontentloaded") is None
            return SimpleNamespace(status=200, html_content=GOOD_SUPP_HTML, url=url)

    monkeypatch.setattr("scrapling.fetchers.StealthySession", _FakeSession)
    res = sf.stealthy_fetch_bounded(
        StealthyFetcher,
        "https://pubs.geoscienceworld.org/segweb/economicgeology/article/120/8/1953/638098",
        headless=False,
        solve_cloudflare=True,
        timeout=60000,
    )
    assert res.status == 200
    assert "ajax.googleapis.com" in captured["session_kw"]["blocked_domains"]
    assert "scholar.google.com" in captured["session_kw"]["blocked_domains"]
    assert "cookie-cdn.cookiepro.com" in captured["session_kw"]["blocked_domains"]
    assert captured["dcl_timeouts"] and captured["dcl_timeouts"][0] <= 8000
    assert captured["load_timeouts"] and captured["load_timeouts"][0] <= 8000


def test_normalize_bare_doi_url_and_candidate():
    assert sf.normalize_bare_doi_url("http://10.5066/P94MI7E5") == "https://doi.org/10.5066/P94MI7E5"
    assert sf.normalize_bare_doi_url("https://pubs.geoscienceworld.org/foo") == "https://pubs.geoscienceworld.org/foo"
    cand = sf.Candidate(url="http://10.5066/P94MI7E5", link_text="10.5066/P94MI7E5")
    assert cand.url == "https://doi.org/10.5066/P94MI7E5"


def test_get_alternate_chooser_urls_filters_seen_hosts():
    import repository_resolver as rr

    seed = [
        "https://www.lyellcollection.org/doi/10.1144/geochem2024-031",
        "https://pubs.geoscienceworld.org/geea/article/24/4/geochem2024-031/650104/title",
        "https://geoscienceworld.org/geea/article-lookup?doi=10.1144/geochem2024-031",
    ]
    alts = rr.get_alternate_chooser_urls(
        doi="10.1144/geochem2024-031",
        exclude_urls=[
            "https://doi.org/10.1144/geochem2024-031",
            "https://www.lyellcollection.org/doi/10.1144/geochem2024-031",
        ],
        seed_urls=seed,
        session=SimpleNamespace(get=lambda *a, **k: SimpleNamespace(json=lambda: {})),
    )
    assert alts == [
        "https://pubs.geoscienceworld.org/geea/article/24/4/geochem2024-031/650104/title",
        "https://geoscienceworld.org/geea/article-lookup?doi=10.1144/geochem2024-031",
    ]


def test_scansci_alternate_chooser_fallback_when_primary_has_zero_cands(monkeypatch, tmp_path):
    lyell_url = "https://www.lyellcollection.org/doi/10.1144/geochem2024-031"
    gsw_url = "https://pubs.geoscienceworld.org/geea/article/24/4/geochem2024-031/650104/test"
    chooser_html = (
        f'<div class="resource-line"><a href="{lyell_url}">Lyell</a>'
        f'<a href="{gsw_url}">GSW</a></div>'
    )
    lyell_paywall_html = "<html><head><title>Article Abstract</title></head><body><p>Purchase access.</p></body></html>"
    fetched_urls = []

    def _fake_fetch_page_html(url, session, play_cookies, headful, **kwargs):
        fetched_urls.append(url)
        if "doi.org" in url:
            return chooser_html, 200, "https://chooser.crossref.org/?doi=10.1144/geochem2024-031"
        if "lyellcollection.org" in url:
            return lyell_paywall_html, 200, lyell_url
        if "pubs.geoscienceworld.org" in url:
            return GOOD_SUPP_HTML, 200, gsw_url
        return "", 404, url

    monkeypatch.setattr(ssd, "load_config", lambda: {})
    monkeypatch.setattr(ssd, "resolve_doi_url", lambda d: "https://chooser.crossref.org/?doi=10.1144/geochem2024-031")
    monkeypatch.setattr(ssd, "_setup_authenticated_session_and_cookies", lambda *a, **k: (SimpleNamespace(), []))
    monkeypatch.setattr(ssd, "fetch_page_html", _fake_fetch_page_html)
    monkeypatch.setattr(
        ssd,
        "get_alternate_chooser_urls",
        lambda **kw: [gsw_url] if lyell_url in kw.get("exclude_urls", []) else [],
    )
    monkeypatch.setattr(ssd, "check_browserskill_status", lambda **k: {"available": False, "ready": False})
    monkeypatch.setattr(ssd, "discover_active_cdp_port", lambda **k: None)
    monkeypatch.setattr(ssd, "has_gui_display", lambda: False)

    saved = []
    monkeypatch.setattr(
        ssd,
        "download_file",
        lambda cand, out_dir, *a, **kw: saved.append(cand.url) or str(Path(out_dir) / "Table_S1.xlsx"),
    )
    monkeypatch.setattr(ssd, "download_with_recovery", lambda cands, fn, *a, **k: ([c for c in cands if fn(c)], len(cands)))

    args = _ssd_args(tmp_path, url_or_doi="10.1144/geochem2024-031", list_only=False)
    ssd.run_download(args, bsk_runtime=SimpleNamespace(enabled=False, browser=None, tab_id=None))

    assert any("lyellcollection.org" in u for u in fetched_urls)
    assert any("pubs.geoscienceworld.org" in u for u in fetched_urls)
    assert saved and "Table_S1.xlsx" in saved[0]


def test_expand_repository_candidates_browser_figshare_and_sciencebase():
    import repository_resolver as rr

    col_url = "https://doi.org/10.6084/m9.figshare.c.7537384"
    col_landing = "https://geolsoc.figshare.com/collections/Test/7537384"
    item_url = "https://geolsoc.figshare.com/articles/dataset/Supp_1_Test/27691690"
    col_html = f'<html><body><a href="{item_url}?backTo=/collections/7537384">Supp 1</a></body></html>'
    item_html = (
        '<html><script>window["__APOLLO_STATE__"] = {'
        '"File:50429260":{"__typename":"File","id":50429260,'
        '"downloadUrl":"https:\\u002F\\u002Fgeolsoc.figshare.com\\u002Fndownloader\\u002Ffiles\\u002F50429260",'
        '"extension":"xlsx"}};</script></html>'
    )
    sb_doi = "http://10.5066/P94MI7E5"
    sb_item = "https://www.sciencebase.gov/catalog/item/5e95f8a482ce172707f25376"
    sb_json = '{"files":[{"name":"ArgonData.csv","url":"https://www.sciencebase.gov/catalog/file/get/5e95f8a482ce172707f25376?f=a"}]}'

    class _BlockedHTTP:
        def get(self, u, **kw):
            if "api.figshare.com" in u or "figshare" in u or "sciencebase" in u:
                raise rr.requests.RequestException("403 blocked")
            if "10.5066/P94MI7E5" in u:
                return SimpleNamespace(
                    status_code=200,
                    url=sb_item,
                    headers={"Content-Type": "text/html"},
                    raise_for_status=lambda: None,
                    iter_content=lambda n: iter([b"<html>blocked</html>"]),
                    encoding="utf-8",
                    close=lambda: None,
                )
            return SimpleNamespace(status_code=403, headers={}, raise_for_status=lambda: None, close=lambda: None)

    def _browser_fetch(u):
        if "m9.figshare.c.7537384" in u:
            return col_html, 200, col_landing
        if "/articles/dataset/" in u:
            return item_html, 200, item_url
        if "format=json" in u:
            return f"<html><body>{sb_json}</body></html>", 200, u
        return "", 404, u

    c_fig = sf.Candidate(url=col_url, source_url="https://pubs.geoscienceworld.org/geea/1")
    c_sb = sf.Candidate(url=sb_doi, source_url="https://pubs.geoscienceworld.org/segweb/1")
    expanded = rr.expand_repository_candidates([c_fig, c_sb], session=_BlockedHTTP(), browser_fetch=_browser_fetch)
    urls = {c.url for c in expanded}
    assert "https://geolsoc.figshare.com/ndownloader/files/50429260" in urls
    assert any(c.filename.endswith(".xlsx") for c in expanded)
    assert "https://www.sciencebase.gov/catalog/file/get/5e95f8a482ce172707f25376?f=a" in urls
    assert any(c.filename == "ArgonData.csv" for c in expanded)


def test_extract_crossref_chooser_ignores_publisher_footer_links_and_sub_budget():
    import time
    import repository_resolver as rr
    import supp_finder as sf

    publisher_html = """
    <html><body>
      <footer>
        <a href="https://www.geolsoc.org.uk/privacy">Privacy</a>
        <a href="https://geolsoc.figshare.com/">Figshare portal</a>
      </footer>
    </body></html>
    """
    urls = rr.extract_crossref_chooser_urls(html_str=publisher_html)
    assert urls == []

    deadline = time.monotonic() + 120.0
    exhausted = sf.BrowserFallbackBudget(goto_timeout=30, total_budget=60, phase_deadline=deadline)
    exhausted._started_at = time.monotonic() - 62.0
    assert exhausted.exhausted is True

    pinned = sf.ensure_phase_browser_budget(exhausted, phase_deadline=deadline, explicit_budget=25)
    assert pinned is exhausted

    sub_default = sf.ensure_phase_browser_budget(exhausted, phase_deadline=deadline, explicit_budget=60)
    assert sub_default is not exhausted
    assert sub_default.exhausted is False

    sub = sf.ensure_phase_browser_budget(exhausted, phase_deadline=deadline, explicit_budget=None)
    assert sub is not exhausted
    assert sub.exhausted is False
    assert 20.0 <= sub.total_budget <= 60.0


def test_repo_browser_session_reuses_engine_and_merges_cookies(monkeypatch):
    import scrapling.fetchers as scrapling_fetchers
    import supp_finder as sf

    entered = []
    fetched = []

    class _FakeEngine:
        def __init__(self, **kw):
            entered.append(kw)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            entered.append("closed")

        def fetch(self, url, **kw):
            fetched.append((url, kw))
            return SimpleNamespace(
                html_content="<html><body>ok figshare __APOLLO_STATE__</body></html>",
                status=200,
                url=url,
                cookies=[{"name": "aws-waf-token", "value": "tok1", "domain": ".figshare.com", "path": "/"}],
            )

    monkeypatch.setattr(scrapling_fetchers, "StealthySession", _FakeEngine)
    cookies = []
    sess = sf.RepoBrowserSession(play_cookies=cookies, headful=True, real_chrome=True)
    try:
        h1, s1, _ = sess.fetch("https://geolsoc.figshare.com/collections/test/1")
        h2, s2, _ = sess.fetch("https://geolsoc.figshare.com/articles/dataset/item/2")
    finally:
        sess.close()

    assert s1 == 200 and s2 == 200
    assert "ok figshare" in h1 and "ok figshare" in h2
    assert len([x for x in entered if isinstance(x, dict)]) == 1
    assert "closed" in entered
    assert len(fetched) == 2
    assert any(c.get("name") == "aws-waf-token" for c in cookies)
