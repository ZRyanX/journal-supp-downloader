"""
Unit tests for Tencent BrowserSkill (bsk) adapter and downloader integration.
Tests discovery, status checking, session lifecycle, DOM extraction, CLI, and fallbacks.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest
from unittest.mock import MagicMock, call, patch

repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
scripts_path = os.path.join(repo_root, "scripts")
if scripts_path not in sys.path:
    sys.path.insert(0, scripts_path)

import browserskill_adapter as bsk
from browserskill_adapter import (
    BrowserSkillDaemonError,
    BrowserSkillError,
    BrowserSkillExecutionError,
    BrowserSkillSession,
    BrowserSkillSessionError,
    BrowserSkillUnavailableError,
    _parse_json_safely,
    check_browserskill_status,
    download_file_via_browserskill,
    fetch_page_via_browserskill,
    find_bsk_executable,
    is_browserskill_available,
)
import journal_downloader
import scansci_supp_downloader


@pytest.fixture(autouse=True)
def isolate_exit_handlers():
    # Test-created fake session IDs must never reach a real process at interpreter exit.
    with patch("browserskill_adapter.atexit.register"), patch("browserskill_adapter.atexit.unregister"):
        yield


def write_native_download(command, **kwargs):
    Path(command[command.index("--out") + 1]).write_bytes(b"x,y\n1,2\n")
    return subprocess.CompletedProcess(command, 0, "Downloaded", "")


class TestBrowserSkillDiscovery(unittest.TestCase):

    def test_find_bsk_executable_env_var_override(self):
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            fake_bsk = tf.name
        try:
            os.chmod(fake_bsk, 0o755)
            with patch.dict(os.environ, {"BSK_BIN": fake_bsk}):
                self.assertEqual(find_bsk_executable(), os.path.abspath(fake_bsk))
            with patch.dict(os.environ, {"BSK_BIN": "", "BSK_PATH": fake_bsk}):
                self.assertEqual(find_bsk_executable(), os.path.abspath(fake_bsk))
        finally:
            if os.path.exists(fake_bsk):
                os.remove(fake_bsk)

    def test_find_bsk_executable_bsk_install_dir(self):
        with tempfile.TemporaryDirectory() as td:
            bin_path = os.path.join(td, "bsk")
            with open(bin_path, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(bin_path, 0o755)
            with patch.dict(os.environ, {"BSK_BIN": "", "BSK_PATH": "", "BSK_INSTALL_DIR": td}):
                self.assertEqual(find_bsk_executable(), os.path.abspath(bin_path))

    def test_find_bsk_executable_bsk_home(self):
        with tempfile.TemporaryDirectory() as td:
            bin_dir = os.path.join(td, "bin")
            os.makedirs(bin_dir, exist_ok=True)
            bin_path = os.path.join(bin_dir, "bsk")
            with open(bin_path, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(bin_path, 0o755)
            with patch.dict(os.environ, {"BSK_BIN": "", "BSK_PATH": "", "BSK_INSTALL_DIR": "", "BSK_HOME": td}):
                self.assertEqual(find_bsk_executable(), os.path.abspath(bin_path))

    def test_find_bsk_executable_which(self):
        with patch.dict(os.environ, {"BSK_BIN": "", "BSK_PATH": "", "BSK_INSTALL_DIR": "", "BSK_HOME": ""}):
            with patch("shutil.which", return_value="/custom/bin/bsk"):
                with patch("os.path.isfile", return_value=True):
                    with patch("os.access", return_value=True):
                        self.assertEqual(find_bsk_executable(), "/custom/bin/bsk")

    def test_find_bsk_executable_not_found(self):
        with patch.dict(os.environ, {"BSK_BIN": "", "BSK_PATH": "", "BSK_INSTALL_DIR": "", "BSK_HOME": ""}):
            with patch("shutil.which", return_value=None):
                with patch("os.path.isfile", return_value=False):
                    self.assertIsNone(find_bsk_executable())

    def test_is_browserskill_available_true(self):
        mock_proc = MagicMock(returncode=0, stdout="bsk 0.3.3")
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", return_value=mock_proc):
                self.assertTrue(is_browserskill_available())

    def test_is_browserskill_available_false_when_missing(self):
        with patch("browserskill_adapter.find_bsk_executable", return_value=None):
            self.assertFalse(is_browserskill_available())

    def test_is_browserskill_available_false_on_timeout(self):
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="bsk", timeout=2.0)):
                self.assertFalse(is_browserskill_available())


class TestSafeJsonParsing(unittest.TestCase):

    def test_parse_json_safely_valid(self):
        self.assertEqual(_parse_json_safely('{"key": "value"}'), {"key": "value"})
        self.assertEqual(_parse_json_safely('[1, 2, 3]'), [1, 2, 3])

    def test_parse_json_safely_embedded_in_logs(self):
        text = "[INFO] Loading configuration...\n{\"session_id\": \"abc1234\"}\n[INFO] Done."
        parsed = _parse_json_safely(text)
        self.assertEqual(parsed, {"session_id": "abc1234"})

    def test_parse_json_safely_json_lines(self):
        text = "{\"event\": \"init\"}\n{\"event\": \"ready\", \"ok\": true}"
        parsed = _parse_json_safely(text)
        self.assertTrue(parsed.get("ok"))

    def test_parse_json_safely_none_on_invalid(self):
        self.assertIsNone(_parse_json_safely(""))
        self.assertIsNone(_parse_json_safely("not a json string"))


class TestBrowserSkillStatus(unittest.TestCase):

    def test_status_when_not_installed(self):
        with patch("browserskill_adapter.find_bsk_executable", return_value=None):
            status = check_browserskill_status()
            self.assertFalse(status["available"])
            self.assertFalse(status["daemon_running"])
            self.assertIn("not found", status["message"].lower())

    def test_status_when_daemon_and_browser_running(self):
        v_proc = MagicMock(returncode=0, stdout="bsk 0.3.3\n")
        s_proc = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "daemon": {"running": True},
                "extension_connected": True,
                "browsers": [{"id": "chrome-1", "name": "Google Chrome"}],
            }),
        )
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", side_effect=[v_proc, s_proc]):
                status = check_browserskill_status()
                self.assertTrue(status["available"])
                self.assertEqual(status["version"], "bsk 0.3.3")
                self.assertTrue(status["daemon_running"])
                self.assertTrue(status["browser_connected"])
                self.assertEqual(len(status["browsers"]), 1)
                self.assertIn("ready", status["message"].lower())

    def test_status_when_daemon_stopped(self):
        v_proc = MagicMock(returncode=0, stdout="bsk 0.3.3\n")
        s_proc = MagicMock(
            returncode=1,
            stdout=json.dumps({"daemon": False, "ok": False}),
        )
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", side_effect=[v_proc, s_proc]):
                status = check_browserskill_status()
                self.assertTrue(status["available"])
                self.assertFalse(status["daemon_running"])
                self.assertIn("stopped", status["message"].lower())


class TestBrowserSkillSession(unittest.TestCase):

    def test_session_init_fails_if_no_binary(self):
        with patch("browserskill_adapter.find_bsk_executable", return_value=None):
            with self.assertRaises(BrowserSkillUnavailableError):
                BrowserSkillSession()

    def test_session_start_flat_json(self):
        mock_proc = MagicMock(
            returncode=0,
            stdout=json.dumps({"session_id": "sess_1234", "ok": True}),
        )
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", return_value=mock_proc) as mock_run:
                sess = BrowserSkillSession(no_focus=True)
                sid = sess.start()
                self.assertEqual(sid, "sess_1234")
                self.assertEqual(sess.session_id, "sess_1234")
                mock_run.assert_called_once_with(
                    ["/bin/bsk", "session", "start", "--json", "--no-focus"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=45.0,
                )

    def test_session_start_nested_reply_json(self):
        mock_proc = MagicMock(
            returncode=0,
            stdout=json.dumps({"reply": {"session_id": "nested_sess_99"}}),
        )
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", return_value=mock_proc):
                sess = BrowserSkillSession()
                self.assertEqual(sess.start(), "nested_sess_99")

    def test_session_start_regex_text_fallback(self):
        mock_proc = MagicMock(
            returncode=0,
            stdout="[INFO] Daemon connected\nSession started: regex_sess_42\nReady for input.",
        )
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", return_value=mock_proc):
                sess = BrowserSkillSession()
                self.assertEqual(sess.start(), "regex_sess_42")

    def test_session_start_failure_raises(self):
        mock_proc = MagicMock(returncode=1, stderr="Extension not connected", stdout="")
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", return_value=mock_proc):
                sess = BrowserSkillSession()
                with self.assertRaises(BrowserSkillSessionError) as ctx:
                    sess.start()
                self.assertIn("Extension not connected", str(ctx.exception))

    def test_session_stop_cleanup_in_context_manager(self):
        start_proc = MagicMock(returncode=0, stdout=json.dumps({"session_id": "clean_sess"}))
        stop_proc = MagicMock(returncode=0, stdout="Session stopped")
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", side_effect=[start_proc, stop_proc]) as mock_run:
                with BrowserSkillSession() as sess:
                    self.assertEqual(sess.session_id, "clean_sess")
                self.assertIsNone(sess.session_id)
                self.assertEqual(mock_run.call_count, 2)
                stop_call = mock_run.call_args_list[1]
                self.assertEqual(stop_call[0][0], ["/bin/bsk", "session", "stop", "clean_sess"])

    def test_session_stop_called_even_on_exception(self):
        start_proc = MagicMock(returncode=0, stdout=json.dumps({"session_id": "err_sess"}))
        stop_proc = MagicMock(returncode=0, stdout="Session stopped")
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", side_effect=[start_proc, stop_proc]) as mock_run:
                try:
                    with BrowserSkillSession() as sess:
                        self.assertEqual(sess.session_id, "err_sess")
                        raise ValueError("User error in session")
                except ValueError:
                    pass
                self.assertIsNone(sess.session_id)
                self.assertEqual(mock_run.call_count, 2)

    def test_session_stop_all(self):
        mock_proc = MagicMock(returncode=0, stdout="All sessions stopped")
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", return_value=mock_proc) as mock_run:
                res = BrowserSkillSession.stop_all()
                self.assertTrue(res)
                mock_run.assert_called_once_with(
                    ["/bin/bsk", "session", "stop", "--all"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10.0,
                )

    def test_list_sessions(self):
        mock_proc = MagicMock(
            returncode=0,
            stdout=json.dumps([{"id": "sess_1", "browser": "chrome"}]),
        )
        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch("subprocess.run", return_value=mock_proc):
                sessions = BrowserSkillSession.list_sessions()
                self.assertEqual(len(sessions), 1)
                self.assertEqual(sessions[0]["id"], "sess_1")

    def test_navigate(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_nav"
        mock_proc = MagicMock(returncode=0, stdout="Navigated to https://example.com")
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            self.assertTrue(sess.navigate("https://example.com"))
            mock_run.assert_called_once_with(
                ["/bin/bsk", "navigate", "https://example.com", "--session", "sess_nav"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=45.0,
            )

    def test_get_html_native(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_html"
        mock_proc = MagicMock(returncode=0, stdout="<html><head><title>Test</title></head></html>")
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            html = sess.get_html()
            self.assertIn("<title>Test</title>", html)
            mock_run.assert_called_once_with(
                ["/bin/bsk", "get-html", "--session", "sess_html", "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30.0,
            )

    def test_get_html_fallback_to_evaluate(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_html_fallback"
        # get-html fails, evaluate succeeds
        proc_fail = MagicMock(returncode=1, stderr="Unknown command")
        proc_eval = MagicMock(returncode=0, stdout=json.dumps({"ok": True, "result": "<html><body>Evaluated</body></html>"}))
        with patch("subprocess.run", side_effect=[proc_fail, proc_eval]):
            html = sess.get_html()
            self.assertIn("Evaluated", html)

    def test_observe(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_obs"
        mock_proc = MagicMock(returncode=0, stdout=json.dumps({"elements": [{"ref": "@e1", "role": "link"}]}))
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            obs = sess.observe()
            self.assertIn("elements", obs)
            mock_run.assert_called_once_with(
                ["/bin/bsk", "observe", "--session", "sess_obs", "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15.0,
            )

    def test_click(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_clk"
        mock_proc = MagicMock(returncode=0, stdout="Clicked @e3")
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = sess.click("@e3")
            self.assertTrue(res)
            mock_run.assert_called_once_with(
                ["/bin/bsk", "click", "@e3", "--session", "sess_clk"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15.0,
            )

    def test_tab_list(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_tab"
        mock_proc = MagicMock(returncode=0, stdout=json.dumps([{"id": "tab_10", "title": "Nature"}]))
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            tabs = sess.tab_list(scope="user")
            self.assertEqual(len(tabs), 1)
            self.assertEqual(tabs[0]["id"], "tab_10")
            mock_run.assert_called_once_with(
                ["/bin/bsk", "tab", "list", "--session", "sess_tab", "--json", "--scope", "user"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10.0,
            )

    def test_tab_borrow_and_return(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_tab_borrow"
        borrow_proc = MagicMock(returncode=0, stdout="Tab borrowed")
        return_proc = MagicMock(returncode=0, stdout="Tab returned")
        with patch("subprocess.run", side_effect=[borrow_proc, return_proc]) as mock_run:
            self.assertTrue(sess.tab_borrow("tab_42"))
            self.assertTrue(sess.tab_return("tab_42"))
            self.assertEqual(mock_run.call_count, 2)
            self.assertEqual(mock_run.call_args_list[0][0][0], ["/bin/bsk", "tab", "borrow", "tab_42", "--session", "sess_tab_borrow"])
            self.assertEqual(mock_run.call_args_list[1][0][0], ["/bin/bsk", "tab", "return", "tab_42", "--session", "sess_tab_borrow"])

    def test_evaluate_json(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_eval"
        mock_proc = MagicMock(
            returncode=0,
            stdout=json.dumps({"ok": True, "result": "<h1>Journal Title</h1>"}),
        )
        with patch("subprocess.run", return_value=mock_proc) as mock_run:
            res = sess.evaluate("document.body.innerHTML")
            self.assertEqual(res, "<h1>Journal Title</h1>")
            mock_run.assert_called_once_with(
                ["/bin/bsk", "evaluate", "document.body.innerHTML", "--session", "sess_eval", "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15.0,
            )

    def test_download_ref(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_dl"
        with tempfile.TemporaryDirectory() as td:
            output = os.path.join(td, "table.csv")
            with patch("subprocess.run", side_effect=write_native_download) as run:
                self.assertEqual(sess.download("@e1", output), output)
            self.assertEqual(Path(output).read_bytes(), b"x,y\n1,2\n")
            command = run.call_args.args[0]
            self.assertEqual(command[2], "@e1")
            self.assertNotEqual(command[command.index("--out") + 1], output)
            self.assertEqual(os.listdir(td), ["table.csv"])

    def test_download_url_authenticated_success(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "sess_dl_url"
        sample_data = b"PK\x03\x04sample_xlsx_data"
        b64_str = base64.b64encode(sample_data).decode("ascii")
        eval_proc = MagicMock(
            returncode=0,
            stdout=json.dumps({"ok": True, "base64": b64_str, "size": len(sample_data)}),
        )
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            target_path = tf.name
        try:
            with patch("subprocess.run", return_value=eval_proc):
                res = sess.download("https://example.com/s1.xlsx", target_path)
                self.assertEqual(res, os.path.abspath(target_path))
                with open(target_path, "rb") as f:
                    self.assertEqual(f.read(), sample_data)
        finally:
            if os.path.exists(target_path):
                os.remove(target_path)


class TestFetchPageViaBrowserSkill(unittest.TestCase):

    def test_fetch_page_via_browserskill_doi_normalized_and_dom_extracted(self):
        start_proc = MagicMock(returncode=0, stdout=json.dumps({"session_id": "fetch_sess"}))
        nav_proc = MagicMock(returncode=0, stdout="ok")
        eval_expand_proc = MagicMock(returncode=0, stdout=json.dumps({"ok": True, "result": None}))
        eval_dom_proc = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "result": json.dumps({
                    "html": "<html><body><h1>Sample Article</h1><a href='/suppl/s1.xlsx'>Table S1</a></body></html>",
                    "url": "https://www.sciencedirect.com/science/article/pii/S12345678",
                    "title": "Sample Article Title",
                }),
            }),
        )
        stop_proc = MagicMock(returncode=0, stdout="ok")

        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch(
                "subprocess.run",
                side_effect=[start_proc, nav_proc, eval_expand_proc, eval_dom_proc, stop_proc],
            ) as mock_run:
                html, status, final_url = fetch_page_via_browserskill(
                    "10.1016/j.sample.2026.01",
                    expand_accordions=True,
                )
                self.assertEqual(status, 200)
                self.assertIn("Sample Article", html)
                self.assertEqual(final_url, "https://www.sciencedirect.com/science/article/pii/S12345678")

                # Verify navigation was called with normalized https://doi.org/...
                nav_call = mock_run.call_args_list[1]
                self.assertIn("https://doi.org/10.1016/j.sample.2026.01", nav_call[0][0])

    def test_fetch_page_via_browserskill_with_tab_borrow(self):
        start_proc = MagicMock(returncode=0, stdout=json.dumps({"session_id": "tab_fetch_sess"}))
        borrow_proc = MagicMock(returncode=0, stdout="Borrowed")
        eval_dom_proc = MagicMock(
            returncode=0,
            stdout=json.dumps({
                "ok": True,
                "result": json.dumps({
                    "html": "<html><body><h1>Borrowed Tab Article</h1></body></html>",
                    "url": "https://nature.com/articles/sample",
                    "title": "Borrowed Title",
                }),
            }),
        )
        return_proc = MagicMock(returncode=0, stdout="Returned")
        stop_proc = MagicMock(returncode=0, stdout="Stopped")

        with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"):
            with patch(
                "subprocess.run",
                side_effect=[start_proc, borrow_proc, eval_dom_proc, return_proc, stop_proc],
            ):
                html, status, final_url = fetch_page_via_browserskill(
                    url_or_doi="https://nature.com/articles/sample",
                    tab_id="tab_88",
                    expand_accordions=False,
                )
                self.assertEqual(status, 200)
                self.assertIn("Borrowed Tab Article", html)
                self.assertEqual(final_url, "https://nature.com/articles/sample")

    def test_fetch_page_via_browserskill_missing_raises_unavailable(self):
        with patch("browserskill_adapter.find_bsk_executable", return_value=None):
            with self.assertRaises(BrowserSkillUnavailableError):
                fetch_page_via_browserskill("https://example.com")


class TestDownloadFileViaBrowserSkill(unittest.TestCase):

    def test_download_file_via_browserskill_success(self):
        def cli(command, **kwargs):
            if command[1:3] == ["session", "start"]:
                return subprocess.CompletedProcess(command, 0, '{"session_id":"dl_sess"}', "")
            if command[1] == "download":
                return write_native_download(command, **kwargs)
            return subprocess.CompletedProcess(command, 0, "Stopped", "")
        with tempfile.TemporaryDirectory() as td:
            output = os.path.join(td, "table.csv")
            with patch("browserskill_adapter.find_bsk_executable", return_value="/bin/bsk"), patch("subprocess.run", side_effect=cli) as run:
                self.assertEqual(download_file_via_browserskill("@e1", output), output)
            self.assertEqual(Path(output).read_bytes(), b"x,y\n1,2\n")
            self.assertEqual(run.call_args.args[0], ["/bin/bsk", "session", "stop", "dl_sess"])

    def test_download_file_via_browserskill_with_active_session(self):
        sess = BrowserSkillSession(bsk_bin="/bin/bsk")
        sess.session_id = "active_sess_1"
        with tempfile.TemporaryDirectory() as td:
            output = os.path.join(td, "table.csv")
            with patch("subprocess.run", side_effect=write_native_download) as run:
                self.assertEqual(download_file_via_browserskill("@e2", output, session=sess), output)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(sess.session_id, "active_sess_1")


class TestDownloaderIntegrations(unittest.TestCase):

    def test_journal_downloader_cli_accepts_bsk_flags(self):
        with patch("sys.argv", ["journal_downloader.py", "https://example.com/article/1", "--bsk", "--bsk-browser", "edge-1", "--bsk-tab", "tab_2", "--list-only", "--no-oa"]):
            with patch("os.makedirs"):
                with patch("journal_downloader.fetch_page_via_browserskill", return_value=("<html></html>", 200, "https://target")) as mock_bsk_fetch:
                    cand_mock = MagicMock(url="https://example.com/s1.xlsx", filename="s1.xlsx")
                    diag_mock = MagicMock()
                    diag_mock.candidates = [cand_mock]
                    diag_mock.is_incomplete = False
                    diag_mock.incomplete_reason = None
                    diag_mock.declared_count = 1
                    diag_mock.found_count = 1
                    with patch("journal_downloader.find_all_candidates", return_value=diag_mock) as mock_find:
                        with patch("journal_downloader.StealthyFetcher.fetch") as mock_stealth:
                            try:
                                journal_downloader.main()
                            except SystemExit:
                                pass
                            mock_bsk_fetch.assert_called_once()
                            mock_find.assert_called_once()
                            mock_stealth.assert_not_called()

    def test_scansci_supp_downloader_cli_accepts_bsk_flags(self):
        with patch("sys.argv", ["scansci_supp_downloader.py", "https://example.com/article/1", "--bsk", "--bsk-tab", "tab_5", "--no-api", "--skip-cdn", "--no-vpn", "--list-only", "--no-oa"]):
            with patch("os.makedirs"):
                with patch("scansci_supp_downloader.fetch_page_via_browserskill", return_value=("<html></html>", 200, "https://target")) as mock_bsk_fetch:
                    cand_mock = MagicMock(url="https://example.com/s1.xlsx", filename="s1.xlsx")
                    diag_mock = MagicMock()
                    diag_mock.candidates = [cand_mock]
                    diag_mock.is_incomplete = False
                    diag_mock.incomplete_reason = None
                    diag_mock.declared_count = 1
                    diag_mock.found_count = 1
                    with patch("scansci_supp_downloader.find_all_candidates", return_value=diag_mock) as mock_find:
                        with patch("scansci_supp_downloader.StealthyFetcher.fetch") as mock_stealth:
                            try:
                                scansci_supp_downloader.main()
                            except SystemExit:
                                pass
                            mock_bsk_fetch.assert_called_once()
                            mock_find.assert_called_once()
                            mock_stealth.assert_not_called()


class TestAdapterMainCLI(unittest.TestCase):

    def test_main_status_exit_code_when_unavailable(self):
        with patch("browserskill_adapter.find_bsk_executable", return_value=None):
            with patch("sys.argv", ["browserskill_adapter.py", "status"]):
                with self.assertRaises(SystemExit) as cm:
                    bsk.main()
                self.assertEqual(cm.exception.code, 1)

    def test_main_status_exit_code_when_ready(self):
        status_info = {
            "available": True,
            "bsk_path": "/bin/bsk",
            "version": "bsk 0.3.3",
            "daemon_running": True,
            "browser_connected": True,
            "browsers": ["chrome"],
            "message": "Ready",
            "raw": {},
        }
        with patch("browserskill_adapter.check_browserskill_status", return_value=status_info):
            with patch("sys.argv", ["browserskill_adapter.py", "status"]):
                with self.assertRaises(SystemExit) as cm:
                    bsk.main()
                self.assertEqual(cm.exception.code, 0)

    def test_main_sessions(self):
        with patch("browserskill_adapter.BrowserSkillSession.list_sessions", return_value=[{"id": "sess_cli"}]):
            with patch("sys.argv", ["browserskill_adapter.py", "sessions"]):
                with self.assertRaises(SystemExit) as cm:
                    bsk.main()
                self.assertEqual(cm.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
