"""Regression tests for session ownership, real CLI envelopes and download recovery."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import browserskill_adapter as bsk
import journal_downloader as light
import scansci_supp_downloader as advanced
from supp_finder import load_download_manifest


@pytest.fixture(autouse=True)
def isolate_exit_handlers():
    with patch.object(bsk.atexit, "register"), patch.object(bsk.atexit, "unregister"):
        yield


def proc(payload, code=0):
    return subprocess.CompletedProcess([], code, json.dumps(payload), "")


def active_session():
    session = bsk.BrowserSkillSession(bsk_bin="/fake/bsk")
    session.session_id = "session-1"
    return session


@pytest.mark.parametrize("payload,ready", [
    ({"daemon_version": "0.9.0", "protocol_version": "1", "pid": 42,
      "browsers": [{"instance_id": "chrome-1"}], "sessions": []}, True),
    ({"daemon_version": "0.9.0", "pid": 42, "browsers": []}, False),
    ({"daemon_version": "0.9.0", "browsers": [{"connected": False}]}, False),
    ({"daemon": False, "browsers": [{"id": "stale"}]}, False),
])
def test_status_uses_upstream_schema(payload, ready):
    with patch.object(bsk.subprocess, "run", side_effect=[proc("bsk 0.9.0"), proc(payload)]):
        result = bsk.check_browserskill_status(bsk_bin="/fake/bsk")
    assert result["ready"] is ready


def test_disconnected_text_is_not_ready():
    with patch.object(bsk.subprocess, "run", side_effect=[proc("bsk"), subprocess.CompletedProcess([], 0, "daemon not running; extension disconnected", "")]):
        result = bsk.check_browserskill_status(bsk_bin="/fake/bsk")
    assert not result["ready"]


def test_status_cli_fails_without_browser(capsys):
    info = dict(available=True, daemon_running=True, browser_connected=False, ready=False)
    with patch.object(bsk, "check_browserskill_status", return_value=info), patch.object(sys, "argv", ["adapter", "status", "--json"]):
        with pytest.raises(SystemExit) as raised:
            bsk.main()
    assert raised.value.code == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False


@pytest.mark.parametrize("payload,code", [({"ok": False, "error": {"text": "JS failed"}}, 0), ({"error": "permission_denied"}, 1)])
def test_evaluate_failure_is_not_replayed(payload, code):
    with patch.object(bsk.subprocess, "run", return_value=proc(payload, code)) as run:
        with pytest.raises(bsk.BrowserSkillExecutionError):
            active_session().evaluate("sideEffect()")
    assert run.call_count == 1


def test_get_html_rejects_truncated_result():
    session = active_session()
    with patch.object(bsk.subprocess, "run", return_value=proc({"html": "<html>partial", "truncated": True})), patch.object(session, "evaluate", return_value="<html>complete</html>"):
        assert session.get_html() == "<html>complete</html>"


@pytest.mark.parametrize("interrupted", [False, True])
def test_runtime_is_lazy_and_closes_on_early_return_or_interrupt(interrupted):
    session = MagicMock()
    with patch.object(bsk, "BrowserSkillSession", return_value=session) as factory:
        def run():
            with bsk.BrowserSkillRuntime(enabled=True, browser="edge", tab_id="42") as runtime:
                factory.assert_not_called()
                assert runtime.get_session() is runtime.get_session()
                if interrupted:
                    raise KeyboardInterrupt
                return "early result"
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                run()
        else:
            assert run() == "early result"
        factory.assert_called_once_with(browser="edge", timeout=60.0)
        session.start.assert_called_once()
        session.tab_borrow.assert_called_once_with("42")
        session.stop.assert_called_once()


def test_failed_start_is_not_retried_for_every_attachment():
    session = MagicMock()
    session.start.side_effect = bsk.BrowserSkillSessionError("extension disconnected")
    with patch.object(bsk, "BrowserSkillSession", return_value=session):
        with bsk.BrowserSkillRuntime(enabled=True) as runtime:
            for _ in range(3):
                with pytest.raises(bsk.BrowserSkillSessionError, match="extension disconnected"):
                    runtime.get_session()
    session.start.assert_called_once()


def test_disabled_runtime_never_starts_browser():
    with patch.object(bsk, "BrowserSkillSession") as factory:
        with bsk.BrowserSkillRuntime() as runtime:
            with pytest.raises(bsk.BrowserSkillUnavailableError):
                runtime.get_session()
    factory.assert_not_called()


def test_stop_failure_retains_id_for_retry():
    session = active_session()
    with patch.object(bsk.subprocess, "run", side_effect=[proc({}, 1), proc({})]):
        with pytest.warns(RuntimeWarning, match="could not be stopped"):
            assert not session.stop()
        assert session.session_id == "session-1"
        assert session.stop()
        assert session.session_id is None


def test_borrow_failure_never_navigates_some_other_tab():
    session = active_session()
    with patch.object(session, "tab_borrow", side_effect=bsk.BrowserSkillExecutionError("denied")), patch.object(session, "navigate") as navigate:
        with pytest.raises(bsk.BrowserSkillExecutionError, match="denied"):
            bsk.fetch_page_via_browserskill("https://example.org", tab_id="42", session=session)
    navigate.assert_not_called()


def test_borrowed_tab_is_retained_until_session_cleanup():
    session = active_session()
    calls = []
    def cli(command, **kwargs):
        calls.append(command)
        if command[1] == "evaluate":
            assert command[-2:] == ["--tab-id", "42"]
            return proc({"ok": True, "value": {"html": "<html>article</html>", "url": "https://example.org/article"}})
        return proc({})
    with patch.object(bsk.subprocess, "run", side_effect=cli):
        bsk.fetch_page_via_browserskill("https://example.org/article", tab_id="42", session=session, expand_accordions=False)
        bsk.fetch_page_via_browserskill("https://example.org/article", tab_id="42", session=session, expand_accordions=False)
        assert session.borrowed_tabs == {"42"}
        assert sum(c[1:3] == ["tab", "borrow"] for c in calls) == 1
        assert not any(c[1:3] == ["tab", "return"] for c in calls)
        session.stop()
    assert calls[-2][1:3] == ["tab", "return"]
    assert calls[-1][1:3] == ["session", "stop"]


def test_owned_session_returns_tab_even_when_dom_extraction_fails():
    session = active_session()
    session.borrowed_tabs.add("42")
    with patch.object(bsk, "BrowserSkillSession", return_value=session), patch.object(session, "start"), patch.object(session, "tab_borrow"), patch.object(session, "evaluate", side_effect=bsk.BrowserSkillExecutionError("failure")), patch.object(session, "get_html", return_value=""), patch.object(bsk.subprocess, "run", return_value=proc({})) as run:
        with pytest.raises(bsk.BrowserSkillExecutionError):
            bsk.fetch_page_via_browserskill("", tab_id="42", bsk_bin="/fake/bsk")
    assert run.call_args_list[0].args[0][1:3] == ["tab", "return"]
    assert session.session_id is None


def test_selector_timeout_is_reported():
    session = active_session()
    with patch.object(session, "navigate"), patch.object(session, "evaluate", return_value=False), patch.object(bsk.time, "sleep"):
        with pytest.raises(bsk.BrowserSkillExecutionError, match="Timed out waiting"):
            bsk.fetch_page_via_browserskill("https://example.org", session=session, wait_selector="#missing", timeout=0.001, expand_accordions=False)


def test_challenge_page_is_not_reported_as_article():
    session = active_session()
    with patch.object(session, "navigate"), patch.object(session, "evaluate", return_value={"html": "<html><title>Just a moment...</title></html>", "url": "https://example.org"}):
        with pytest.raises(bsk.BrowserSkillExecutionError, match="requires verification"):
            bsk.fetch_page_via_browserskill("https://example.org", session=session, expand_accordions=False)


def test_fallback_dom_uses_redirected_landing_url():
    session = active_session()
    with patch.object(session, "navigate"), patch.object(session, "evaluate", side_effect=[None, {"url": "https://publisher.org/article"}]), patch.object(session, "get_html", return_value="<html>article</html>"):
        html, _, final = bsk.fetch_page_via_browserskill("10.1234/example", session=session, expand_accordions=False)
    assert final == "https://publisher.org/article"


def test_native_url_fallback_uses_selector_and_removes_temporary_link(tmp_path):
    session = active_session()
    session.tab_id = "42"
    output = tmp_path / "table.csv"
    def native(command, **kwargs):
        assert command[2].startswith("#journal-bsk-")
        assert command[-2:] == ["--tab-id", "42"]
        Path(command[command.index("--out") + 1]).write_text("a,b\n1,2\n")
        return proc({})
    with patch.object(session, "download_url_in_session", side_effect=bsk.BrowserSkillExecutionError("CORS")), patch.object(session, "evaluate", return_value=True) as evaluate, patch.object(bsk.subprocess, "run", side_effect=native):
        assert session.download("https://cdn.org/table.csv", str(output)) == str(output)
    assert output.read_text() == "a,b\n1,2\n"
    assert "remove()" in evaluate.call_args.args[0]
    assert list(tmp_path.iterdir()) == [output]


def test_native_success_without_file_does_not_destroy_existing_file(tmp_path):
    output = tmp_path / "table.csv"
    output.write_text("original")
    with patch.object(bsk.subprocess, "run", return_value=proc({})):
        with pytest.raises(bsk.BrowserSkillExecutionError, match="nonempty file"):
            active_session().download("@e1", str(output))
    assert output.read_text() == "original"
    assert list(tmp_path.iterdir()) == [output]


def test_html_error_download_is_not_committed(tmp_path):
    session = active_session()
    output = tmp_path / "table.csv"
    output.write_text("original")
    def download(target, path, **kwargs):
        Path(path).write_text("<html><title>Access denied</title><body>Forbidden</body></html>")
    with patch.object(session, "download", side_effect=download):
        assert bsk.download_file_via_browserskill("@e1", str(output), session=session) is None
    assert output.read_text() == "original"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize("result", [
    {"ok": True, "base64": "eA==", "size": 20},
    {"ok": True, "base64": "invalid%%%", "size": 1},
    {"ok": True, "base64": "x" * (12 * 1024 * 1024), "size": 9 * 1024 * 1024},
])
def test_inpage_transfer_rejects_corruption_and_oversize(tmp_path, result):
    session = active_session()
    output = tmp_path / "data.csv"
    with patch.object(session, "evaluate", return_value=result):
        with pytest.raises(Exception):
            session.download_url_in_session("https://example.org/data", str(output))
    assert not output.exists()


def test_403_reaches_authenticated_download_and_updates_manifest(tmp_path):
    session = active_session()
    session.current_url = "https://publisher.org/article"
    def download(target, path, **kwargs):
        Path(path).write_text("x,y\n1,2\n")
    with patch.object(light.Fetcher, "get", return_value=SimpleNamespace(status=403)), patch("requests.Session") as http, patch.object(session, "download", side_effect=download):
        http.return_value.get.return_value.status_code = 403
        result = light.download_file("https://cdn.org/table.csv", str(tmp_path), bsk_session=session, referer=session.current_url)
    assert Path(result).read_text() == "x,y\n1,2\n"
    assert load_download_manifest(str(tmp_path))["table.csv"] == "https://cdn.org/table.csv"


def test_default_download_does_not_discover_or_use_personal_browser(tmp_path):
    with patch.object(light.Fetcher, "get", return_value=SimpleNamespace(status=403)), patch("requests.Session") as http, patch.object(light, "download_file_via_browserskill") as browser:
        http.return_value.get.return_value.status_code = 403
        assert light.download_file("https://cdn.org/table.csv", str(tmp_path)) is None
    browser.assert_not_called()


def test_post_cdn_scan_passes_runtime_to_missing_attachment(tmp_path):
    runtime = bsk.BrowserSkillRuntime(enabled=True)
    html = '<html><a href="/table-s2.csv">Supplementary Table S2</a></html>'
    args = SimpleNamespace(no_cookies=True, headful=False, browserskill=True, skip_cdn=True)
    with patch.object(advanced, "fetch_page_html", return_value=(html, 200, "https://publisher.org/article")), patch.object(advanced, "download_file") as download:
        advanced._try_page_scrape_supplements("https://publisher.org/article", str(tmp_path), {}, args, [], bsk_runtime=runtime)
    download.assert_called_once()
    assert download.call_args.kwargs["bsk_runtime"] is runtime


@pytest.mark.parametrize("module", [light, advanced])
@pytest.mark.parametrize("outcome", ["early", "exception", "interrupt"])
def test_downloader_entrypoint_always_cleans_up(module, outcome):
    session = MagicMock()
    def run(args, runtime):
        runtime.get_session()
        if outcome == "exception":
            raise ValueError("broken parser")
        if outcome == "interrupt":
            raise KeyboardInterrupt
        return "done"
    with patch.object(sys, "argv", ["downloader", "https://example.org", "--bsk"]), patch.object(bsk, "BrowserSkillSession", return_value=session), patch.object(module, "run_download", side_effect=run):
        if outcome == "early":
            assert module.main() == "done"
        else:
            with pytest.raises(ValueError if outcome == "exception" else KeyboardInterrupt):
                module.main()
    session.stop.assert_called_once()


def test_cli_browser_selector_enables_integration_and_rejects_conflict():
    parser = argparse.ArgumentParser()
    bsk.add_browserskill_arguments(parser)
    args = parser.parse_args(["--bsk-browser", "edge"])
    runtime = bsk.runtime_from_args(args, parser)
    assert runtime.enabled and args.browserskill and runtime.browser == "edge"
    args = parser.parse_args(["--no-bsk", "--bsk-browser", "edge"])
    with pytest.raises(SystemExit):
        bsk.runtime_from_args(args, parser)


def test_requested_browser_failure_is_not_swallowed_by_download_helper(tmp_path):
    with patch.object(bsk, "BrowserSkillSession", side_effect=bsk.BrowserSkillUnavailableError("missing")):
        with bsk.BrowserSkillRuntime(enabled=True, browser="required-profile") as runtime:
            with pytest.raises(bsk.BrowserSkillBindingError, match="missing"):
                bsk.download_file_via_browserskill("https://example.org/s1.csv", str(tmp_path / "s1.csv"), runtime=runtime)


@pytest.mark.parametrize("module", [light, advanced])
def test_full_download_with_external_cli_process_and_borrowed_tab(module, tmp_path, monkeypatch):
    """Exercise subprocess arguments, JSON envelopes, native staging and cleanup together."""
    executable = tmp_path / "bsk"
    log = tmp_path / "commands.jsonl"
    executable.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['JOURNAL_BSK_TEST_LOG'], 'a') as stream:
    stream.write(json.dumps(args) + '\n')
if args[:2] == ['session', 'start']:
    assert args[args.index('--browser') + 1] == 'edge-profile'
    print(json.dumps({'session_id': 'session-real-process'}))
elif args[0] == 'evaluate':
    assert args[args.index('--tab-id') + 1] == '42'
    script = args[1]
    if 'credentials:' in script:
        print(json.dumps({'ok': False, 'error': {'text': 'CORS blocked'}}))
    elif 'document.documentElement.outerHTML' in script:
        print(json.dumps({'ok': True, 'value': {
            'html': '<html><title>Test Article</title><a href="/table-s1.csv">Supplementary Table S1</a></html>',
            'url': 'https://publisher.example/article'
        }}))
    else:
        print(json.dumps({'ok': True, 'value': True}))
elif args[0] == 'download':
    assert args[1].startswith('#journal-bsk-')
    assert args[args.index('--tab-id') + 1] == '42'
    Path(args[args.index('--out') + 1]).write_text('element,value\nCu,12\n')
    print(json.dumps({'path': args[args.index('--out') + 1]}))
else:
    assert args[0] in ('tab', 'session'), args
    print('{}')
''')
    executable.chmod(0o755)
    monkeypatch.setenv("BSK_BIN", str(executable))
    monkeypatch.setenv("JOURNAL_BSK_TEST_LOG", str(log))
    output = tmp_path / "downloads"
    argv = ["downloader", "https://publisher.example/article", "--bsk-browser", "edge-profile", "--bsk-tab", "42", "--no-oa", "-o", str(output)]
    if module is advanced:
        argv += ["--no-api", "--skip-cdn", "--no-vpn", "--no-cookies"]
    monkeypatch.setattr(sys, "argv", argv)
    with patch("requests.Session") as http, patch.object(module.Fetcher, "get", return_value=SimpleNamespace(status=403)), patch.object(module.StealthyFetcher, "fetch", side_effect=AssertionError("must use selected browser")):
        http.return_value.get.return_value.status_code = 403
        if module is advanced:
            with patch.object(module, "HAS_SCANSCI", False), patch.object(module, "_quick_title_lookup", return_value=None):
                module.main()
        else:
            with patch.object(module, "load_saved_cookies", return_value=[]):
                module.main()
    files = list(output.rglob("*.csv"))
    assert len(files) == 1
    assert files[0].read_text() == "element,value\nCu,12\n"
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert sum(c[:2] == ["session", "start"] for c in commands) == 1
    assert sum(c[:2] == ["tab", "borrow"] for c in commands) == 1
    assert commands[-2][:2] == ["tab", "return"]
    assert commands[-1] == ["session", "stop", "session-real-process"]
    assert not list(output.rglob(".bsk-*"))
    assert load_download_manifest(str(files[0].parent))[files[0].name] == "https://publisher.example/table-s1.csv"
