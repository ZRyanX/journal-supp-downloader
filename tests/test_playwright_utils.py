"""
Unit tests for playwright_utils.py:
Tests browser discovery, CDP detection, TCP port probing, and cookie normalization.
"""

import json
import os
import sys
import socket
import tempfile
import unittest
from unittest.mock import patch, MagicMock

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scripts_path = os.path.join(repo_root, "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

import playwright_utils as pu


class TestPlaywrightUtils(unittest.TestCase):

    # ── Cookie Normalization Tests ───────────────────────────────────────────

    def test_normalize_empty_cookies(self):
        self.assertEqual(pu.normalize_playwright_cookies([]), [])
        self.assertEqual(pu.normalize_playwright_cookies(None), [])
        self.assertEqual(pu.normalize_playwright_cookies({}), [])

    def test_normalize_dict_mapping(self):
        cookies = {"session_id": "xyz123", "theme": "dark"}
        res = pu.normalize_playwright_cookies(cookies, default_url="https://example.com/test")
        self.assertEqual(len(res), 2)
        names = {c["name"] for c in res}
        self.assertIn("session_id", names)
        self.assertIn("theme", names)
        for c in res:
            self.assertEqual(c["domain"], "example.com")
            self.assertEqual(c["path"], "/")

    def test_normalize_empty_domain_with_default_url(self):
        cookies = [{"name": "auth", "value": "token", "domain": ""}]
        res = pu.normalize_playwright_cookies(cookies, default_url="https://sciencedirect.com/article/123")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["domain"], "sciencedirect.com")
        self.assertEqual(res[0]["path"], "/")

    def test_normalize_empty_domain_without_default_url(self):
        cookies = [{"name": "auth", "value": "token", "domain": ""}]
        res = pu.normalize_playwright_cookies(cookies, default_url=None)
        # Without default_url or domain, cookie cannot be located and is dropped
        self.assertEqual(len(res), 0)

    def test_normalize_url_removes_domain_and_path(self):
        cookies = [{"name": "auth", "value": "token", "url": "https://example.com", "domain": "example.com", "path": "/"}]
        res = pu.normalize_playwright_cookies(cookies)
        self.assertEqual(len(res), 1)
        self.assertIn("url", res[0])
        self.assertNotIn("domain", res[0])
        self.assertNotIn("path", res[0])

    def test_normalize_samesite_values(self):
        cases = [
            ("strict", "Strict"),
            ("STRICT", "Strict"),
            ("lax", "Lax"),
            ("none", "None"),
            ("no_restriction", "None"),
        ]
        for raw, expected in cases:
            c = [{"name": "foo", "value": "bar", "domain": "example.com", "sameSite": raw}]
            res = pu.normalize_playwright_cookies(c)
            self.assertEqual(res[0]["sameSite"], expected)

        # Invalid sameSite removed
        c_invalid = [{"name": "foo", "value": "bar", "domain": "example.com", "sameSite": "invalid_value"}]
        res_inv = pu.normalize_playwright_cookies(c_invalid)
        self.assertNotIn("sameSite", res_inv[0])

    # ── Browser Discovery Tests ──────────────────────────────────────────────

    def test_find_playwright_chromium_env_override(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            fake_browser = f.name
        try:
            with patch.dict(os.environ, {"PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": fake_browser}):
                found = pu.find_playwright_chromium()
                self.assertEqual(found, fake_browser)
        finally:
            if os.path.exists(fake_browser):
                os.remove(fake_browser)

    def test_find_system_chromium(self):
        # Should return a string or None, but never throw an unhandled exception
        result = pu.find_system_chromium()
        if result is not None:
            self.assertTrue(os.path.isfile(result))
            self.assertTrue(os.access(result, os.X_OK))

    def test_find_any_chromium(self):
        # find_any_chromium should prioritize playwright or fall back to system
        res = pu.find_any_chromium()
        # On our dev machine with Playwright and Chrome installed, it should find one
        if res is not None:
            self.assertTrue(os.path.isfile(res))

    # ── TCP Port and CDP Probing Tests ───────────────────────────────────────

    def test_is_tcp_port_open(self):
        # Port 0 or negative should be False
        self.assertFalse(pu.is_tcp_port_open("127.0.0.1", -1))
        self.assertFalse(pu.is_tcp_port_open("127.0.0.1", 70000))

        # Mocking socket connect
        with patch("socket.socket") as mock_sock:
            mock_inst = MagicMock()
            mock_sock.return_value.__enter__.return_value = mock_inst
            mock_inst.connect_ex.return_value = 0
            self.assertTrue(pu.is_tcp_port_open("127.0.0.1", 9222))

            mock_inst.connect_ex.return_value = 111  # ECONNREFUSED
            self.assertFalse(pu.is_tcp_port_open("127.0.0.1", 9222))

    def test_discover_active_cdp_port_from_file(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            port_file = os.path.join(tmp_dir, "DevToolsActivePort")
            with open(port_file, "w", encoding="utf-8") as f:
                f.write("54321\n/devtools/browser/abc-123-guid\n")

            with patch("playwright_utils.is_tcp_port_open", return_value=True):
                endpoint = pu.discover_active_cdp_port(profile_paths=[tmp_dir])
                self.assertIsNotNone(endpoint)
                http_url, ws_url = endpoint
                self.assertEqual(http_url, "http://127.0.0.1:54321")
                self.assertEqual(ws_url, "ws://127.0.0.1:54321/devtools/browser/abc-123-guid")

    def test_discover_active_cdp_port_from_env(self):
        with patch.dict(os.environ, {"CDP_PORT": "9222"}):
            with patch("playwright_utils.is_tcp_port_open", return_value=True):
                with patch("playwright_utils.resolve_cdp_ws_url", return_value="ws://127.0.0.1:9222/devtools/browser/xyz"):
                    endpoint = pu.discover_active_cdp_port(profile_paths=[])
                    self.assertIsNotNone(endpoint)
                    self.assertEqual(endpoint[0], "http://127.0.0.1:9222")
                    self.assertEqual(endpoint[1], "ws://127.0.0.1:9222/devtools/browser/xyz")

    def test_resolve_cdp_ws_url_passthrough_ws(self):
        ws = "ws://127.0.0.1:9222/devtools/browser/test-id"
        self.assertEqual(pu.resolve_cdp_ws_url(ws), ws)

        wss = "wss://remote-host:9222/devtools/browser/test-id"
        self.assertEqual(pu.resolve_cdp_ws_url(wss), wss)

    def test_resolve_cdp_ws_url_http_endpoint(self):
        fake_json = json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/resolved"}).encode("utf-8")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = fake_json
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            resolved = pu.resolve_cdp_ws_url("http://127.0.0.1:9222")
            self.assertEqual(resolved, "ws://127.0.0.1:9222/devtools/browser/resolved")

    def test_resolve_cdp_ws_url_none_or_empty(self):
        self.assertIsNone(pu.resolve_cdp_ws_url(None))
        self.assertIsNone(pu.resolve_cdp_ws_url(""))

    def test_resolve_cdp_ws_url_http_error_handling(self):
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            self.assertIsNone(pu.resolve_cdp_ws_url("http://127.0.0.1:9222"))
            self.assertIsNone(pu.resolve_cdp_ws_url(9222))

    def test_fetch_page_html_passes_cdp_url(self):
        from scansci_supp_downloader import fetch_page_html
        with patch("scrapling.fetchers.StealthyFetcher.fetch") as mock_fetch:
            fake_page = MagicMock()
            fake_page.html_content = "<html><body>Article content</body></html>"
            fake_page.status = 200
            fake_page.url = "https://example.com/article"
            mock_fetch.return_value = fake_page

            html_text, status, final_url = fetch_page_html(
                "https://example.com/article",
                cdp_url="ws://127.0.0.1:9222/devtools/browser/abc",
            )
            self.assertEqual(status, 200)
            self.assertIn("Article content", html_text)
            self.assertEqual(mock_fetch.call_args[1].get("cdp_url"), "ws://127.0.0.1:9222/devtools/browser/abc")

    def test_login_publishers_save_merged_cookies(self):
        from login_publishers import save_merged_cookies
        with tempfile.TemporaryDirectory() as td:
            out_file = os.path.join(td, "cookies.json")
            new_cookies = [
                {"name": "sess", "value": "123", "domain": ".sciencedirect.com", "path": "/"},
                {"name": "token", "value": "abc", "domain": ".nature.com", "path": "/"},
            ]
            merged = save_merged_cookies(new_cookies, out_file)
            self.assertEqual(len(merged), 2)
            self.assertTrue(os.path.exists(out_file))

            # Merge update existing cookie
            updated_cookies = [
                {"name": "sess", "value": "456", "domain": ".sciencedirect.com", "path": "/"},
                {"name": "new_c", "value": "xyz", "domain": ".springer.com", "path": "/"},
            ]
            merged2 = save_merged_cookies(updated_cookies, out_file)
            self.assertEqual(len(merged2), 3)
            sess_cookie = next(c for c in merged2 if c["name"] == "sess")
            self.assertEqual(sess_cookie["value"], "456")


    def test_resolve_cdp_ws_url_json_list(self):
        """Verify resolve_cdp_ws_url handles /json or /json/list returning a JSON list without crashing."""
        fake_tab_list = [
            {"description": "", "devtoolsFrontendUrl": "", "id": "1", "title": "Tab 1", "type": "page", "url": "https://example.com", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/tab1"},
            {"description": "", "devtoolsFrontendUrl": "", "id": "2", "title": "Tab 2", "type": "page", "url": "about:blank", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/tab2"},
        ]
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(fake_tab_list).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            # Test with /json and /json/list
            resolved = pu.resolve_cdp_ws_url("http://127.0.0.1:9222/json")
            self.assertEqual(resolved, "ws://127.0.0.1:9222/devtools/page/tab1")
            resolved_list = pu.resolve_cdp_ws_url("http://127.0.0.1:9222/json/list")
            self.assertEqual(resolved_list, "ws://127.0.0.1:9222/devtools/page/tab1")

    def test_discover_active_cdp_port_custom_port_in_env(self):
        """Verify discover_active_cdp_port preserves custom port in CDP_URL rather than hardcoding 9222."""
        with patch.dict(os.environ, {"CDP_URL": "9444"}):
            with patch("playwright_utils.resolve_cdp_ws_url", return_value="ws://127.0.0.1:9444/devtools/browser/abc"):
                endpoint = pu.discover_active_cdp_port(profile_paths=[])
                self.assertIsNotNone(endpoint)
                self.assertEqual(endpoint[0], "http://127.0.0.1:9444")
                self.assertEqual(endpoint[1], "ws://127.0.0.1:9444/devtools/browser/abc")

        with patch.dict(os.environ, {"CDP_URL": "ws://127.0.0.1:9555/devtools/browser/xyz"}):
            with patch("playwright_utils.resolve_cdp_ws_url", return_value="ws://127.0.0.1:9555/devtools/browser/xyz"):
                endpoint = pu.discover_active_cdp_port(profile_paths=[])
                self.assertIsNotNone(endpoint)
                self.assertEqual(endpoint[0], "http://127.0.0.1:9555")

    def test_merge_cookies_helper(self):
        """Verify merge_cookies deduplicates on (domain, name, path) and updates newer values."""
        existing = [
            {"name": "session", "value": "old_val", "domain": ".example.com", "path": "/"},
            {"name": "theme", "value": "light", "domain": "example.com", "path": "/"},
        ]
        new_c = [
            {"name": "session", "value": "new_val", "domain": "example.com", "path": "/"},
            {"name": "token", "value": "tok123", "domain": ".sciencedirect.com", "path": "/"},
        ]
        merged = pu.merge_cookies(existing, new_c)
        self.assertEqual(len(merged), 3)
        sess = next(c for c in merged if c["name"] == "session")
        self.assertEqual(sess["value"], "new_val")

    def test_fetch_page_via_cdp_reuse_open_tab(self):
        """Verify fetch_page_via_cdp reuses an already open tab in browser.contexts[0]."""
        mock_page = MagicMock()
        mock_page.url = "https://www.sciencedirect.com/science/article/pii/S0169136822002578"
        mock_page.content.return_value = "<html><body>Paper text in open tab</body></html>"
        mock_page.evaluate.return_value = "10.1016/j.oregeorev.2022.104949"

        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        mock_context.cookies.return_value = [{"name": "AuthToken", "value": "secret", "domain": ".sciencedirect.com"}]

        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]

        with patch("playwright.sync_api.sync_playwright") as mock_pw:
            mock_inst = MagicMock()
            mock_pw.return_value.__enter__.return_value = mock_inst
            mock_inst.chromium.connect_over_cdp.return_value = mock_browser

            html, status, final_url, live_c = pu.fetch_page_via_cdp(
                "http://127.0.0.1:9222",
                "https://doi.org/10.1016/j.oregeorev.2022.104949",
            )
            self.assertEqual(status, 200)
            self.assertIn("Paper text in open tab", html)
            self.assertEqual(len(live_c), 1)
            self.assertEqual(live_c[0]["name"], "AuthToken")
            # Should not create a new page when tab already exists
            mock_context.new_page.assert_not_called()

    def test_fetch_page_via_cdp_new_page(self):
        """Verify fetch_page_via_cdp opens new page and closes it when not already open."""
        mock_page = MagicMock()
        mock_page.url = "https://link.springer.com/article/10.1007/s00126-020-00999-x"
        mock_page.content.return_value = "<html><body>Springer paper text</body></html>"

        mock_context = MagicMock()
        mock_context.pages = []  # No open tabs
        mock_context.new_page.return_value = mock_page
        mock_context.cookies.return_value = [{"name": "idp_session", "value": "shib123"}]

        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]

        with patch("playwright.sync_api.sync_playwright") as mock_pw:
            mock_inst = MagicMock()
            mock_pw.return_value.__enter__.return_value = mock_inst
            mock_inst.chromium.connect_over_cdp.return_value = mock_browser

            html, status, final_url, live_c = pu.fetch_page_via_cdp(
                "http://127.0.0.1:9222",
                "https://link.springer.com/article/10.1007/s00126-020-00999-x",
                wait_selector="#supplementary-material",
            )
            self.assertEqual(status, 200)
            self.assertIn("Springer paper text", html)
            self.assertEqual(len(live_c), 1)
            mock_context.new_page.assert_called_once()
            mock_page.wait_for_selector.assert_called_once()
            mock_page.close.assert_called_once()

    def test_live_local_cdp_extraction_and_navigation(self):
        """End-to-end integration test with a real local headless Chromium process."""
        import subprocess
        import time
        exe = pu.find_any_chromium()
        if not exe:
            self.skipTest("No local Chromium browser executable available.")

        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.Popen([
                exe,
                "--remote-debugging-port=0",
                f"--user-data-dir={td}",
                "--headless=new",
            ])
            try:
                # Wait for DevToolsActivePort file to be written
                port_file = os.path.join(td, "DevToolsActivePort")
                ready = False
                for _ in range(30):
                    time.sleep(0.15)
                    if os.path.isfile(port_file):
                        ready = True
                        break
                if not ready:
                    self.skipTest("Chromium did not write DevToolsActivePort in time.")

                # 1. Test discover_active_cdp_port with user-data-dir
                endpoint = pu.discover_active_cdp_port(profile_paths=[td])
                self.assertIsNotNone(endpoint)
                self.assertTrue(endpoint[0].startswith("http://127.0.0.1:"))
                self.assertTrue(endpoint[1].startswith("ws://127.0.0.1:"))

                # 2. Test extract_cdp_cookies
                cookies = pu.extract_cdp_cookies(endpoint[0])
                self.assertIsInstance(cookies, list)

                # 3. Test fetch_page_via_cdp
                html, status, final_url, cdp_c = pu.fetch_page_via_cdp(
                    endpoint[0],
                    "about:blank",
                )
                self.assertEqual(status, 200)
                self.assertIsInstance(html, str)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        proc.kill()


if __name__ == "__main__":
    unittest.main()
