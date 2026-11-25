"""Unit & integration tests for browser-layer engineering optimizations:
1. Patchright-first CDP & login wizard sync_playwright loader (with Playwright fallback)
2. Persistent stealth browser profile (user_data_dir) with channel split & lock fallback
3. Cross-platform browser process-tree reaper (atexit + Windows taskkill /F /T)
"""

import os
import sys
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import playwright_utils as pu  # noqa: E402
import supp_finder as sf  # noqa: E402


class TestPatchrightFirstCDP(unittest.TestCase):
    def test_get_sync_playwright_prefers_patchright_when_available(self):
        import patchright.sync_api as pr_sync

        fn = pu.get_sync_playwright()
        self.assertIs(fn, pr_sync.sync_playwright)

    def test_get_sync_playwright_falls_back_to_playwright_when_patchright_missing(self):
        import playwright.sync_api as pw_sync

        with patch.dict(sys.modules, {"patchright": None, "patchright.sync_api": None}):
            fn = pu.get_sync_playwright()
            self.assertIs(fn, pw_sync.sync_playwright)

    def test_get_sync_playwright_honors_monkeypatched_playwright(self):
        fake_pw = MagicMock(name="patched_sync_playwright")
        with patch("playwright.sync_api.sync_playwright", fake_pw):
            fn = pu.get_sync_playwright()
            self.assertIs(fn, fake_pw)

    def test_fetch_page_via_cdp_uses_patchright_by_default(self):
        mock_page = MagicMock()
        mock_page.url = "https://example.org/article"
        mock_page.content.return_value = "<html><body>Patchright CDP content</body></html>"
        mock_page.evaluate.return_value = 0

        mock_context = MagicMock()
        mock_context.pages = [mock_page]
        mock_context.cookies.return_value = [{"name": "cf_clearance", "value": "ok"}]

        mock_browser = MagicMock()
        mock_browser.contexts = [mock_context]

        with patch("patchright.sync_api.sync_playwright") as mock_pr:
            mock_inst = MagicMock()
            mock_pr.return_value.__enter__.return_value = mock_inst
            mock_inst.chromium.connect_over_cdp.return_value = mock_browser

            html, status, final_url, cookies = pu.fetch_page_via_cdp(
                "http://127.0.0.1:9222",
                "https://example.org/article",
            )
            mock_pr.assert_called_once()
            self.assertIn("Patchright CDP content", html)
            self.assertEqual(cookies[0]["name"], "cf_clearance")

    def test_login_publishers_uses_patchright_loader(self):
        import login_publishers
        import patchright.sync_api as pr_sync

        self.assertIs(login_publishers.sync_playwright, pr_sync.sync_playwright)


class TestPersistentBrowserProfile(unittest.TestCase):
    def setUp(self):
        self._prev_tls = pu.set_thread_persistent_profile(profile_dir=None, enabled=None)

    def tearDown(self):
        pu.set_thread_persistent_profile(profile_dir=self._prev_tls[0], enabled=self._prev_tls[1])

    def test_resolve_splits_chrome_and_chromium_channels(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOURNAL_SUPP_BROWSER_PROFILE_DIR", None)
            os.environ.pop("JOURNAL_SUPP_NO_PERSISTENT_PROFILE", None)
            p_chromium = pu.resolve_persistent_profile_dir(real_chrome=False)
            p_chrome = pu.resolve_persistent_profile_dir(real_chrome=True)
            self.assertIsNotNone(p_chromium)
            self.assertIsNotNone(p_chrome)
            self.assertTrue(p_chromium.endswith(os.path.join("browser_profile", "chromium")))
            self.assertTrue(p_chrome.endswith(os.path.join("browser_profile", "chrome")))
            self.assertNotEqual(p_chromium, p_chrome)

    def test_resolve_precedence_and_disable_flags(self):
        with tempfile.TemporaryDirectory() as td:
            env_dir = os.path.join(td, "from_env")
            cli_dir = os.path.join(td, "from_cli")
            cfg_dir = os.path.join(td, "from_cfg")

            self.assertEqual(
                pu.resolve_persistent_profile_dir(config={"browser_profile_dir": cfg_dir}),
                os.path.abspath(cfg_dir),
            )
            with patch.dict(os.environ, {"JOURNAL_SUPP_BROWSER_PROFILE_DIR": env_dir}):
                self.assertEqual(
                    pu.resolve_persistent_profile_dir(config={"browser_profile_dir": cfg_dir}),
                    os.path.abspath(env_dir),
                )
                self.assertEqual(
                    pu.resolve_persistent_profile_dir(profile_dir=cli_dir, config={"browser_profile_dir": cfg_dir}),
                    os.path.abspath(cli_dir),
                )
            with patch.dict(os.environ, {"JOURNAL_SUPP_NO_PERSISTENT_PROFILE": "1"}):
                self.assertIsNone(pu.resolve_persistent_profile_dir(profile_dir=cli_dir))
            self.assertIsNone(
                pu.resolve_persistent_profile_dir(
                    profile_dir=cli_dir,
                    config={"persistent_browser_profile": False},
                )
            )
            self.assertIsNone(pu.resolve_persistent_profile_dir(profile_dir=cli_dir, enabled=False))

    def test_in_process_profile_lock_and_release(self):
        with tempfile.TemporaryDirectory() as td:
            prof = os.path.join(td, "prof")
            first = pu.acquire_profile_dir(prof)
            self.assertEqual(first, os.path.abspath(prof))
            self.assertTrue(os.path.isdir(first))
            # Second concurrent claim must gracefully return None (ephemeral fallback)
            second = pu.acquire_profile_dir(prof)
            self.assertIsNone(second)
            pu.release_profile_dir(first)
            # Re-acquirable after release
            third = pu.acquire_profile_dir(prof)
            self.assertEqual(third, os.path.abspath(prof))
            pu.release_profile_dir(third)

    def test_start_managed_session_sets_profile_and_retries_only_on_lock_error(self):
        with tempfile.TemporaryDirectory() as td:
            prof = os.path.join(td, "my_prof")
            seen = []

            class _LockThenOkSession:
                def __init__(self, **kw):
                    seen.append(dict(kw))
                    self.closed = False

                def start(self):
                    if seen[-1].get("user_data_dir"):
                        raise RuntimeError("Failed to create a ProcessSingleton for your profile directory")

                def close(self):
                    self.closed = True

            eng, acquired = sf.start_managed_stealthy_session(
                _LockThenOkSession,
                {"headless": True},
                profile_dir=prof,
                persistent_profile=True,
                use_context_manager=False,
            )
            self.assertIsNone(acquired)
            self.assertEqual(len(seen), 2)
            self.assertEqual(seen[0].get("user_data_dir"), os.path.abspath(prof))
            self.assertNotIn("user_data_dir", seen[1])
            sf.close_managed_stealthy_session(eng, profile_dir=acquired)
            self.assertTrue(eng.closed)
            # Lock must not remain stuck after fallback
            reclaimed = pu.acquire_profile_dir(prof)
            self.assertEqual(reclaimed, os.path.abspath(prof))
            pu.release_profile_dir(reclaimed)

    def test_start_managed_session_does_not_retry_on_tunnel_error(self):
        with tempfile.TemporaryDirectory() as td:
            prof = os.path.join(td, "tunnel_prof")
            attempts = []

            class _TunnelFailSession:
                def __init__(self, **kw):
                    attempts.append(dict(kw))
                    raise RuntimeError("net::ERR_TUNNEL_CONNECTION_FAILED")

            with self.assertRaises(RuntimeError):
                sf.start_managed_stealthy_session(
                    _TunnelFailSession,
                    {"headless": True},
                    profile_dir=prof,
                    persistent_profile=True,
                )
            self.assertEqual(len(attempts), 1)
            # Lock released on failure
            reclaimed = pu.acquire_profile_dir(prof)
            self.assertEqual(reclaimed, os.path.abspath(prof))
            pu.release_profile_dir(reclaimed)

    def test_start_managed_session_skips_profile_for_cdp_or_explicit_empty(self):
        seen = []

        class _OkSession:
            def __init__(self, **kw):
                seen.append(dict(kw))

            def start(self):
                pass

            def close(self):
                pass

        eng1, acq1 = sf.start_managed_stealthy_session(
            _OkSession,
            {"cdp_url": "ws://127.0.0.1:9222/devtools/browser/1"},
        )
        self.assertIsNone(acq1)
        self.assertNotIn("user_data_dir", seen[-1])
        sf.close_managed_stealthy_session(eng1, profile_dir=acq1)

        eng2, acq2 = sf.start_managed_stealthy_session(
            _OkSession,
            {"headless": True, "user_data_dir": ""},
        )
        self.assertIsNone(acq2)
        self.assertNotIn("user_data_dir", seen[-1])
        sf.close_managed_stealthy_session(eng2, profile_dir=acq2)

    def test_cli_exposes_profile_flags_on_both_downloaders(self):
        for script in ("journal_downloader.py", "scansci_supp_downloader.py"):
            res = subprocess.run(
                [sys.executable, os.path.join(SCRIPTS_DIR, script), "--help"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            self.assertIn("--profile-dir", res.stdout)
            self.assertIn("--no-persistent-profile", res.stdout)


class TestBrowserProcessTreeReaper(unittest.TestCase):
    def test_extract_driver_process_handles_session_and_direct_browser(self):
        fake_proc = SimpleNamespace(pid=42424, returncode=None)
        fake_transport = SimpleNamespace(_proc=fake_proc)
        fake_conn = SimpleNamespace(_transport=fake_transport)
        fake_impl = SimpleNamespace(_connection=fake_conn)
        fake_pw = SimpleNamespace(_impl_obj=fake_impl)
        fake_session = SimpleNamespace(playwright=fake_pw)

        self.assertIs(pu.extract_driver_process(fake_session), fake_proc)
        self.assertIs(pu.extract_driver_process(fake_pw), fake_proc)
        self.assertIsNone(pu.extract_driver_process(SimpleNamespace()))

    def test_tree_kill_process_windows_and_already_exited_skip(self):
        dead_asyncio_proc = SimpleNamespace(pid=11111, returncode=0)
        alive_asyncio_proc = SimpleNamespace(pid=22222, returncode=None)

        with patch("playwright_utils.subprocess.run") as mock_run:
            self.assertFalse(pu.tree_kill_process(dead_asyncio_proc))
            mock_run.assert_not_called()

            with patch.object(os, "name", "nt"):
                self.assertTrue(pu.tree_kill_process(alive_asyncio_proc))
                mock_run.assert_called_once()
                cmd = mock_run.call_args[0][0]
                self.assertEqual(cmd, ["taskkill", "/F", "/T", "/PID", "22222"])

    def test_close_and_unregister_reaps_surviving_driver_and_releases_profile(self):
        with tempfile.TemporaryDirectory() as td:
            prof = pu.acquire_profile_dir(os.path.join(td, "reap_prof"))
            fake_proc = SimpleNamespace(pid=33333, returncode=None)
            fake_pw = SimpleNamespace(
                _impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=SimpleNamespace(_proc=fake_proc)))
            )

            class _StubbornSession:
                def __init__(self):
                    self.playwright = fake_pw
                    self.closed = False

                def close(self):
                    # Mirrors Scrapling SyncSession.close(): clears self.playwright
                    self.playwright = None
                    self.closed = True

            sess = _StubbornSession()
            pu.register_live_browser(sess, profile_dir=prof, use_context_manager=False)

            with patch("playwright_utils.tree_kill_process", return_value=True) as mock_kill:
                pu.close_and_unregister_browser(sess)
                self.assertTrue(sess.closed)
                mock_kill.assert_called_once_with(fake_proc)

            # Profile lock released
            reclaimed = pu.acquire_profile_dir(prof)
            self.assertEqual(reclaimed, prof)
            pu.release_profile_dir(reclaimed)

    def test_reap_live_browsers_atexit_cleans_unclosed_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            prof = pu.acquire_profile_dir(os.path.join(td, "exit_prof"))
            fake_proc = SimpleNamespace(pid=44444, returncode=None)
            fake_pw = SimpleNamespace(
                _impl_obj=SimpleNamespace(_connection=SimpleNamespace(_transport=SimpleNamespace(_proc=fake_proc)))
            )

            class _LeakedSession:
                def __init__(self):
                    self.playwright = fake_pw
                    self.exited = False

                def __exit__(self, *a):
                    self.exited = True
                    self.playwright = None

            sess = _LeakedSession()
            pu.register_live_browser(sess, profile_dir=prof, use_context_manager=True)

            with patch("playwright_utils.tree_kill_process", return_value=True) as mock_kill:
                count = pu.reap_live_browsers()
                self.assertGreaterEqual(count, 1)
                self.assertTrue(sess.exited)
                mock_kill.assert_called_once_with(fake_proc)

            reclaimed = pu.acquire_profile_dir(prof)
            self.assertEqual(reclaimed, prof)
            pu.release_profile_dir(reclaimed)


if __name__ == "__main__":
    unittest.main()
