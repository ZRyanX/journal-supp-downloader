"""P1-D regression tests: browser-layer --proxy plumbing + tunnel fast-fail.

Cause D in ~/workspace/jsd-50-paper-test-p1-report.md: 521/584 FAILs were
``browser: Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED`` because
``scansci_supp_downloader.py`` had no ``--proxy`` option and its
``StealthyFetcher.fetch`` / ``StealthySession`` calls were all ``proxy=None``,
while ``journal_downloader.py`` already had ``--proxy``. Each candidate then
burned its own 3 browser retries against a dead tunnel until the 150s
budget ran out.

(a) proxy plumbing: the ``--proxy`` value (same
``http://[user:pass@]host:port`` syntax as journal_downloader) must actually
reach the browser layer -- StealthyFetcher.fetch kwargs and the
StealthySession (Playwright) proxy config -- verified with a recording fake
and with a real in-process HTTP proxy (no external network, no browser).
(b) tunnel fast-fail: consecutive ERR_TUNNEL_CONNECTION_FAILED failures for
one host trip a breaker (default threshold 3, configurable); later attempts
for that host skip the browser entirely instead of retrying per candidate.

No test here touches the network outside localhost.
"""
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import scansci_supp_downloader as ssd  # noqa: E402

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
TUNNEL_MSG = "Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://blocked.example/article"
PROXY = "http://user:pass@127.0.0.1:18080"


# ── local tiny HTTP proxy (records absolute-form requests, serves canned HTML) ──

class _ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append(self.path)
        body = b"<html><head><title>proxy test</title></head><body>via-proxy-ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def tiny_proxy():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
    server.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class _ProxyHonouringFetcher:
    """Stand-in for StealthyFetcher.fetch: performs a plain HTTP GET, but
    only through the proxy it was given -- exactly the contract the real
    browser fetch must honour. A target host that only the proxy can serve
    proves the proxy kwarg was actually used, not merely stored."""

    calls = []

    @staticmethod
    def fetch(url, **kwargs):
        _ProxyHonouringFetcher.calls.append(dict(kwargs))
        proxies = {}
        if kwargs.get("proxy"):
            proxies = {"http": kwargs["proxy"], "https": kwargs["proxy"]}
        resp = requests.get(url, proxies=proxies, timeout=5)
        return NS(html_content=resp.text, status=resp.status_code, url=url)


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


class _TunnelFailingFetcher:
    calls = 0

    @staticmethod
    def fetch(url, **kwargs):
        _TunnelFailingFetcher.calls += 1
        raise RuntimeError(TUNNEL_MSG)


class _OtherFailingFetcher:
    calls = 0

    @staticmethod
    def fetch(url, **kwargs):
        _OtherFailingFetcher.calls += 1
        raise RuntimeError("boom: something else went wrong")


def _fresh_tracker(threshold=3):
    return ssd.TunnelFailureTracker(threshold=threshold)


# ── (a) proxy plumbing ──────────────────────────────────────────────────────

def test_playwright_proxy_config_parses_auth():
    assert ssd.playwright_proxy_config(PROXY) == {
        "server": "http://127.0.0.1:18080",
        "username": "user",
        "password": "pass",
    }


def test_playwright_proxy_config_no_auth_and_none():
    assert ssd.playwright_proxy_config("http://127.0.0.1:18080") == {
        "server": "http://127.0.0.1:18080",
    }
    assert ssd.playwright_proxy_config(None) is None
    assert ssd.playwright_proxy_config("") is None


def test_playwright_proxy_config_rejects_hostless_url():
    with pytest.raises(ValueError):
        ssd.playwright_proxy_config("not-a-proxy-url")


def test_fetch_page_html_passes_proxy_to_stealthy_fetcher(monkeypatch):
    _RecordingFetcher.calls = []
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    html, status, _ = ssd.fetch_page_html(
        "https://blocked.example/article", session=None, proxy=PROXY,
    )
    assert status == 200 and "ok" in html
    assert _RecordingFetcher.calls, "StealthyFetcher.fetch was never called"
    assert _RecordingFetcher.calls[0].get("proxy") == PROXY


def test_fetch_page_html_without_proxy_passes_no_proxy(monkeypatch):
    _RecordingFetcher.calls = []
    monkeypatch.setattr(ssd, "StealthyFetcher", _RecordingFetcher)
    html, status, _ = ssd.fetch_page_html("https://blocked.example/article", session=None)
    assert status == 200
    assert "proxy" not in _RecordingFetcher.calls[0]


def test_fetch_page_html_request_goes_through_local_proxy(monkeypatch, tiny_proxy):
    """End-to-end at the fetch layer: the target host is unresolvable
    directly, so the page can only come back if the proxy config is
    actually used for the request."""
    _ProxyHonouringFetcher.calls = []
    monkeypatch.setattr(ssd, "StealthyFetcher", _ProxyHonouringFetcher)
    proxy_url = f"http://127.0.0.1:{tiny_proxy.server_address[1]}"
    html, status, _ = ssd.fetch_page_html(
        "http://proxy-test.invalid/article", session=None, proxy=proxy_url,
    )
    assert status == 200
    assert "via-proxy-ok" in html
    assert _ProxyHonouringFetcher.calls[0].get("proxy") == proxy_url
    assert tiny_proxy.seen == ["http://proxy-test.invalid/article"]


def test_download_file_passes_proxy_to_stealthy_session(monkeypatch, tmp_path):
    captured = {}

    class _CapturingSession:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            raise RuntimeError(TUNNEL_MSG)

    class _FailingFetcher:
        @staticmethod
        def get(url, **kwargs):
            raise RuntimeError("no plain http in this test")

    monkeypatch.setattr(ssd, "Fetcher", _FailingFetcher)
    monkeypatch.setattr("scrapling.fetchers.StealthySession", _CapturingSession)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: NS(returncode=7))
    result = ssd.download_file(
        "https://blocked.example/mmc1.xlsx", str(tmp_path),
        session=None, proxy=PROXY, tunnel_tracker=_fresh_tracker(),
    )
    assert result is None
    assert captured.get("proxy") == {
        "server": "http://127.0.0.1:18080",
        "username": "user",
        "password": "pass",
    }


def test_cli_exposes_proxy_and_tunnel_threshold():
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "scansci_supp_downloader.py"), "--help"],
        capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0
    assert "--proxy" in out.stdout
    assert "http://user:pass@host:port" in out.stdout
    assert "--tunnel-fail-threshold" in out.stdout


def test_cli_proxy_and_threshold_reach_run_download(monkeypatch, tmp_path):
    from unittest.mock import patch

    seen = {}
    with patch.object(sys, "argv", [
        "scansci_supp_downloader.py", "10.1038/s41467-025-61134-0",
        "-o", str(tmp_path), "--no-bsk", "--no-vpn",
        "--proxy", PROXY, "--tunnel-fail-threshold", "2",
    ]), patch.object(ssd, "run_download", lambda args, rt: seen.update(vars(args))):
        ssd.main()
    assert seen.get("proxy") == PROXY
    assert seen.get("tunnel_fail_threshold") == 2


# ── (b) tunnel fast-fail ────────────────────────────────────────────────────

def test_tunnel_tracker_trips_at_threshold_and_is_per_host():
    tracker = _fresh_tracker(threshold=3)
    host = "https://blocked.example/article"
    other = "https://other.example/article"
    assert tracker.record_failure(host, TUNNEL_MSG) is False
    assert tracker.record_failure(host, TUNNEL_MSG) is False
    assert tracker.is_tripped(host) is False
    assert tracker.record_failure(host, TUNNEL_MSG) is True
    assert tracker.is_tripped(host) is True
    assert tracker.is_tripped(other) is False
    assert tracker.consecutive_failures(host) == 3


def test_tunnel_tracker_ignores_non_tunnel_errors_and_resets_on_success():
    tracker = _fresh_tracker(threshold=3)
    host = "https://blocked.example/article"
    assert tracker.record_failure(host, "boom: something else") is False
    assert tracker.consecutive_failures(host) == 0
    tracker.record_failure(host, TUNNEL_MSG)
    tracker.record_failure(host, TUNNEL_MSG)
    tracker.record_success(host)
    assert tracker.consecutive_failures(host) == 0
    assert tracker.is_tripped(host) is False


def test_tunnel_tracker_threshold_is_configurable():
    tracker = _fresh_tracker(threshold=2)
    host = "https://blocked.example/article"
    assert tracker.record_failure(host, TUNNEL_MSG) is False
    assert tracker.record_failure(host, TUNNEL_MSG) is True
    assert tracker.is_tripped(host) is True


def test_fetch_page_html_fast_fails_after_repeated_tunnel_errors(monkeypatch, capsys):
    _TunnelFailingFetcher.calls = 0
    monkeypatch.setattr(ssd, "StealthyFetcher", _TunnelFailingFetcher)
    tracker = _fresh_tracker(threshold=3)
    t0 = time.monotonic()
    for _ in range(5):
        html, status, _ = ssd.fetch_page_html(
            "https://blocked.example/article", session=None, tunnel_tracker=tracker,
        )
        assert html == "" and status == 0
    elapsed = time.monotonic() - t0
    # Only 3 browser launches for 5 attempts: the 4th/5th short-circuit.
    assert _TunnelFailingFetcher.calls == 3
    assert elapsed < 5
    assert "隧道连续失败、提前放弃该站点" in capsys.readouterr().out


def test_fetch_page_html_non_tunnel_errors_do_not_trip(monkeypatch, capsys):
    _OtherFailingFetcher.calls = 0
    monkeypatch.setattr(ssd, "StealthyFetcher", _OtherFailingFetcher)
    tracker = _fresh_tracker(threshold=3)
    for _ in range(5):
        html, status, _ = ssd.fetch_page_html(
            "https://blocked.example/article", session=None, tunnel_tracker=tracker,
        )
        assert html == "" and status == 0
    assert _OtherFailingFetcher.calls == 5
    assert tracker.is_tripped("https://blocked.example/article") is False
    assert "隧道连续失败、提前放弃该站点" not in capsys.readouterr().out


def test_fetch_page_html_success_resets_tunnel_count(monkeypatch):
    calls = {"n": 0}

    class _FlakyFetcher:
        @staticmethod
        def fetch(url, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RuntimeError(TUNNEL_MSG)
            return NS(
                html_content="<html><head><title>t</title></head><body>ok</body></html>",
                status=200,
                url=url,
            )

    monkeypatch.setattr(ssd, "StealthyFetcher", _FlakyFetcher)
    tracker = _fresh_tracker(threshold=3)
    for _ in range(2):
        assert ssd.fetch_page_html(
            "https://blocked.example/article", session=None, tunnel_tracker=tracker,
        )[0] == ""
    html, status, _ = ssd.fetch_page_html(
        "https://blocked.example/article", session=None, tunnel_tracker=tracker,
    )
    assert status == 200 and "ok" in html
    assert tracker.consecutive_failures("https://blocked.example/article") == 0


def test_download_file_skips_browser_after_tunnel_trip(monkeypatch, tmp_path, capsys):
    constructions = {"n": 0}

    class _TunnelSession:
        def __init__(self, **kwargs):
            constructions["n"] += 1
            raise RuntimeError(TUNNEL_MSG)

    class _FailingFetcher:
        @staticmethod
        def get(url, **kwargs):
            raise RuntimeError("no plain http in this test")

    monkeypatch.setattr(ssd, "Fetcher", _FailingFetcher)
    monkeypatch.setattr("scrapling.fetchers.StealthySession", _TunnelSession)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: NS(returncode=7))
    tracker = _fresh_tracker(threshold=3)
    url = "https://blocked.example/mmc1.xlsx"
    t0 = time.monotonic()
    for _ in range(5):
        assert ssd.download_file(url, str(tmp_path), session=None, tunnel_tracker=tracker) is None
    elapsed = time.monotonic() - t0
    # 3 browser launches for 5 candidates, then fast-fail: no per-candidate retry.
    assert constructions["n"] == 3
    assert elapsed < 10
    assert "隧道连续失败、提前放弃该站点" in capsys.readouterr().out


def test_download_file_pre_tripped_host_never_launches_browser(monkeypatch, tmp_path):
    constructions = {"n": 0}

    class _TunnelSession:
        def __init__(self, **kwargs):
            constructions["n"] += 1
            raise RuntimeError(TUNNEL_MSG)

    class _FailingFetcher:
        @staticmethod
        def get(url, **kwargs):
            raise RuntimeError("no plain http in this test")

    monkeypatch.setattr(ssd, "Fetcher", _FailingFetcher)
    monkeypatch.setattr("scrapling.fetchers.StealthySession", _TunnelSession)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: NS(returncode=7))
    tracker = _fresh_tracker(threshold=3)
    url = "https://blocked.example/mmc1.xlsx"
    for _ in range(3):
        assert tracker.record_failure(url, TUNNEL_MSG) in (False, True)
    assert tracker.is_tripped(url) is True
    assert ssd.download_file(url, str(tmp_path), session=None, tunnel_tracker=tracker) is None
    assert constructions["n"] == 0
