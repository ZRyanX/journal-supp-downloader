"""P1-F regression tests: downloaded-file authenticity, Tier 0 success
visibility, and single-line FAIL exceptions.

Cause F/G in ~/workspace/jsd-50-paper-test-p1-report.md (and the first-round
report): BMC #43/#44 and eLife #34/#36/#37 landed only ``Table N.html`` /
``Appendix 7.html`` / Client Challenge pages, yet the tool counted "a file
on disk" as "a supplement downloaded"; #47's 3 mmc files were (probably)
downloaded via Tier 0 but the log only said ``Preserved ...; page
completeness remains unverified``; and Playwright exception text is
multi-line, so ``FAIL (...)``'s closing paren lands 2-3 lines later and a
single-line regex misreads it as a bare FAIL.
"""
import io
import os
import re
import sys
import zipfile
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from supp_finder import (  # noqa: E402
    load_download_manifest,
    record_manifest_entry,
    save_download_manifest,
    validate_downloaded_file,
)

# A genuine data table, byte-identical in shape to the standalone HTML
# table attachment the validator has always accepted.
REAL_TABLE_HTML = (
    b"<!DOCTYPE html><html><body><table>"
    b"<tr><th>Sample</th><th>Value</th></tr>"
    b"<tr><td>A</td><td>1.2</td></tr></table></body></html>"
)

# eLife anti-bot wall: a challenge page that *does* contain a table
# (layout table), which is exactly why the old validator let it through.
CHALLENGE_HTML = (
    b"<!DOCTYPE html><html><head><title>Client Challenge</title></head><body>"
    b"<h1>Client Challenge</h1><p>Checking your browser before accessing "
    b"elifesciences.org. Please verify you are human.</p>"
    b"<table><tr><td>challenge</td></tr><tr><td>platform</td></tr></table>"
    b"</body></html>"
)

# CC BY 4.0 deed page: licence text plus a layout table (the old deed
# pages render their content inside tables), served under the bare
# version name "4.0" with no extension.
CC_LICENSE_HTML = (
    b"<!DOCTYPE html><html><head><title>Creative Commons -- Attribution 4.0 "
    b"International -- CC BY 4.0</title></head><body>"
    b"<h1>Creative Commons Attribution 4.0 International</h1>"
    b"<p>This work is licensed under a "
    b'<a href="https://creativecommons.org/licenses/by/4.0/">Creative Commons '
    b"Attribution 4.0 International License</a>.</p>"
    b"<table><tr><td>License</td></tr></table></body></html>"
)


def _xlsx_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Override PartName="/xl/workbook.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet.main+xml"/></Types>',
        )
        zf.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook/>')
    return buf.getvalue()


def _write(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


# ── (a) Garbage downloads are rejected ──────────────────────────────────────

def test_table_n_html_with_real_table_is_garbage(tmp_path):
    """#8/#34/#37: 'Table 1.html' is an in-article table rendered as a web
    page, not a supplement -- even when it contains a genuine data table."""
    path = _write(tmp_path, "Table 1.html", REAL_TABLE_HTML)
    is_valid, reason = validate_downloaded_file(path)
    assert not is_valid, f"Table 1.html accepted: {reason}"
    assert "Table 1" in reason or "navigation" in reason.lower()


def test_appendix_n_html_with_real_table_is_garbage(tmp_path):
    """#36: 'Appendix 7.html' downloaded 18 times; none was a supplement."""
    path = _write(tmp_path, "Appendix 7.html", REAL_TABLE_HTML)
    is_valid, reason = validate_downloaded_file(path)
    assert not is_valid, f"Appendix 7.html accepted: {reason}"


def test_table_n_html_dup_suffix_is_garbage(tmp_path):
    path = _write(tmp_path, "Table 1_2.html", REAL_TABLE_HTML)
    assert not validate_downloaded_file(path)[0]


def test_client_challenge_page_with_table_is_garbage(tmp_path):
    """A challenge page must be rejected even when it carries a table:
    the challenge markers, not the table, decide."""
    path = _write(tmp_path, "challenge.html", CHALLENGE_HTML)
    is_valid, reason = validate_downloaded_file(path)
    assert not is_valid, f"Client Challenge page accepted: {reason}"
    assert "challenge" in reason.lower()


def test_client_challenge_disguised_as_xlsx_is_garbage(tmp_path):
    path = _write(tmp_path, "S1.xlsx", CHALLENGE_HTML)
    assert not validate_downloaded_file(path)[0]


def test_cc_license_page_as_bare_40_is_garbage(tmp_path):
    """#46: the CC BY 4.0 deed served as bare '4.0' is a licence page,
    not a supplement file."""
    path = _write(tmp_path, "4.0", CC_LICENSE_HTML)
    is_valid, reason = validate_downloaded_file(path)
    assert not is_valid, f"CC licence page accepted: {reason}"
    assert "licen" in reason.lower() or "Creative Commons" in reason


def test_challenge_text_in_csv_is_garbage(tmp_path):
    """A challenge wall served with a data extension must not pass as
    a text data file."""
    path = _write(tmp_path, "S1.csv", b"Client Challenge\nPlease verify you are human.\n")
    assert not validate_downloaded_file(path)[0]


# ── (b) Real supplements still validate ─────────────────────────────────────

@pytest.mark.parametrize(
    "name,data",
    [
        ("S1.xlsx", _xlsx_bytes()),
        ("S1.csv", b"Gene,FoldChange\nTP53,2.4\nBRCA1,-1.8\n"),
        ("S1.pdf", b"%PDF-1.4\n%data\n"),
        ("S1.zip", None),  # built below (needs a real zip container)
        ("S1_table.html", REAL_TABLE_HTML),
        ("Table S1.html", REAL_TABLE_HTML),
        ("Supplementary Table S1.html", REAL_TABLE_HTML),
        # No separator: the D1 .bin -> .html rename path for a genuine
        # HTML table attachment, not a navigation page.
        ("table1.html", REAL_TABLE_HTML),
    ],
)
def test_real_supplements_still_valid(tmp_path, name, data):
    if data is None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("data.txt", "gene,value\nTP53,2.4\n")
        data = buf.getvalue()
    path = _write(tmp_path, name, data)
    is_valid, reason = validate_downloaded_file(path)
    assert is_valid, f"Real supplement {name} rejected: {reason}"


# ── (a, end-to-end) Garbage never lands, never enters the manifest ─────────

class _FakeResponse:
    def __init__(self, body, content_type="text/html"):
        self.status_code = 200
        self.headers = {"content-type": content_type}
        self._body = body

    def iter_content(self, chunk_size=8192):
        yield self._body


class _FakeSession:
    def __init__(self, body):
        self._body = body

    def get(self, *args, **kwargs):
        return _FakeResponse(self._body)


def _no_curl(monkeypatch, module):
    class FakeRun:
        returncode = 7

    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: FakeRun())


def test_download_table_n_html_lands_nothing(tmp_path, capsys, monkeypatch):
    """scansci download_file: a Table 1.html navigation page must return
    None, leave no file on disk, record no manifest entry, and FAIL with
    a reason naming the garbage."""
    import scansci_supp_downloader as a

    monkeypatch.setattr(a, "HAS_SCRAPLING", False)
    _no_curl(monkeypatch, a)
    result = a.download_file(
        "https://example.com/Table%201.html",
        str(tmp_path),
        session=_FakeSession(CHALLENGE_HTML),
    )
    out = capsys.readouterr().out
    assert result is None
    assert not (tmp_path / "Table 1.html").exists()
    assert load_download_manifest(str(tmp_path)) == {}
    assert "FAIL (" in out
    assert "navigation" in out.lower() or "challenge" in out.lower()


def test_stale_garbage_file_is_removed_not_counted(tmp_path, capsys, monkeypatch):
    """A garbage file landed by an earlier run must not be [SKIP]-counted
    as success: it is removed from disk and from the manifest, with a
    log line giving the reason."""
    import scansci_supp_downloader as a

    url = "https://example.com/Table%201.html"
    stale = _write(tmp_path, "Table 1.html", REAL_TABLE_HTML)
    manifest = load_download_manifest(str(tmp_path))
    record_manifest_entry(manifest, "Table 1.html", url)
    save_download_manifest(str(tmp_path), manifest)

    monkeypatch.setattr(a, "HAS_SCRAPLING", False)
    _no_curl(monkeypatch, a)

    class BoomSession:
        def get(self, *args, **kwargs):
            raise ConnectionError("boom")

    result = a.download_file(url, str(tmp_path), session=BoomSession())
    out = capsys.readouterr().out
    assert result is None
    assert not os.path.exists(stale)
    assert "Table 1.html" not in load_download_manifest(str(tmp_path))
    assert "[Removed]" in out


# ── (c) Tier 0 success is explicit ──────────────────────────────────────────

def test_tier0_success_line_names_file_size_and_validation(tmp_path, capsys):
    """A Tier 0 CDN hit must be confirmed on its own line with the file
    name, size and validation result -- not just 'Found N file(s)'."""
    from scansci_supp_downloader import try_elsevier_cdn_brute_force

    payload = _xlsx_bytes()

    def fake_head(url, timeout=10, allow_redirects=True):
        resp = MagicMock()
        if "mmc1.xlsx" in url:
            resp.status_code = 200
            resp.headers = {"content-length": str(len(payload))}
        else:
            resp.status_code = 404
            resp.headers = {}
        return resp

    def fake_get(url, timeout=30, stream=True):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-disposition": ""}
        resp.iter_content = MagicMock(return_value=[payload])
        return resp

    with patch("requests.Session.head", side_effect=fake_head), \
         patch("requests.Session.get", side_effect=fake_get), \
         patch("time.sleep"):
        files = try_elsevier_cdn_brute_force(
            "S12345", str(tmp_path), max_mmc=2, max_consecutive_misses=2)
    out = capsys.readouterr().out
    assert [os.path.basename(f) for f in files] == ["mmc1.xlsx"]
    tier0_lines = [ln for ln in out.splitlines() if "[Tier 0]" in ln]
    assert any(
        "mmc1.xlsx" in ln and "KB" in ln and "valid" in ln.lower()
        for ln in tier0_lines
    ), f"No explicit Tier 0 success line in:\n{out}"


def test_no_candidate_branch_confirms_tier0_files_by_name(tmp_path, capsys):
    """#47: 3 mmc files downloaded via Tier 0/API with no candidates must
    be confirmed by name instead of the vague 'Preserved ...; page
    completeness remains unverified' line."""
    import scansci_supp_downloader as a
    from types import SimpleNamespace as NS

    mmc = tmp_path / "mmc1.xlsx"
    mmc.write_bytes(_xlsx_bytes())
    base = "https://www.sciencedirect.com/science/article/pii/S1234567890123456"
    argv = ["scansci_supp_downloader.py", "10.1016/j.fake",
            "-o", str(tmp_path), "--no-bsk", "--no-vpn"]
    with patch.object(sys, "argv", argv), \
         patch.object(a, "load_config", return_value={}), \
         patch.object(a, "resolve_doi_url", return_value=base), \
         patch.object(a, "try_elsevier_api_campus",
                      return_value=([str(mmc)], {1})), \
         patch.object(a, "try_elsevier_cdn_brute_force", return_value=[]), \
         patch.object(a, "_setup_authenticated_session_and_cookies",
                      return_value=(NS(), [])), \
         patch.object(a, "fetch_page_html", return_value=("", 0, base)), \
         patch.object(a, "resolve_oa_info", return_value=None):
        a.main()
    out = capsys.readouterr().out
    assert "mmc1.xlsx" in out
    assert "page completeness remains unverified" not in out
    assert re.search(r"\[Tier [A0]\].*mmc1\.xlsx", out), out


# ── (d) Multi-line Playwright exceptions collapse to one FAIL line ─────────

PLAYWRIGHT_ERR = (
    'Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://blocked.example/article\n'
    'Call log:\n'
    '  - navigating to "https://blocked.example/article", waiting until "commit"\n'
)

def test_single_line_collapses_playwright_exception():
    from supp_finder import single_line

    flat = single_line(PLAYWRIGHT_ERR)
    assert "\n" not in flat and "\r" not in flat
    assert "ERR_TUNNEL_CONNECTION_FAILED" in flat
    assert "Call log:" in flat
    # A single-line FAIL(...) regex must now parse the whole reason.
    line = f"FAIL ({flat})"
    assert re.match(r"FAIL \(([^)]+)\)$", line)
    assert "ERR_TUNNEL_CONNECTION_FAILED" in re.match(r"FAIL \(([^)]+)\)$", line).group(1)


def test_download_fail_line_is_single_line_when_exception_is_multiline(
        tmp_path, capsys, monkeypatch):
    """The final FAIL line of scansci download_file must stay on one
    line even when the underlying exception spans several lines."""
    import scansci_supp_downloader as a

    monkeypatch.setattr(a, "HAS_SCRAPLING", False)
    _no_curl(monkeypatch, a)

    class BoomSession:
        def get(self, *args, **kwargs):
            raise ConnectionError(PLAYWRIGHT_ERR)

    result = a.download_file(
        "http://127.0.0.1:9/nope.xlsx", str(tmp_path), session=BoomSession()
    )
    out = capsys.readouterr().out
    assert result is None
    fail_lines = [ln for ln in out.splitlines() if "FAIL (" in ln]
    assert len(fail_lines) == 1, f"FAIL split across lines:\n{out}"
    assert re.match(r".*FAIL \(([^)]+)\)\s*$", fail_lines[0])
    assert "ERR_TUNNEL_CONNECTION_FAILED" in fail_lines[0]
    assert "Call log:" in fail_lines[0]


def test_tier0_fail_line_is_single_line_when_exception_is_multiline(
        tmp_path, capsys):
    """Same single-line rule for the Tier 0 CDN download path."""
    from scansci_supp_downloader import try_elsevier_cdn_brute_force

    def fake_head(url, timeout=10, allow_redirects=True):
        resp = MagicMock()
        if "mmc1.xlsx" in url:
            resp.status_code = 200
            resp.headers = {"content-length": "1024"}
        else:
            resp.status_code = 404
            resp.headers = {}
        return resp

    def fake_get(url, timeout=30, stream=True):
        raise ValueError(PLAYWRIGHT_ERR)

    with patch("requests.Session.head", side_effect=fake_head), \
         patch("requests.Session.get", side_effect=fake_get), \
         patch("time.sleep"):
        files = try_elsevier_cdn_brute_force(
            "S12345", str(tmp_path), max_mmc=1, max_consecutive_misses=1)
    out = capsys.readouterr().out
    assert files == []
    fail_lines = [ln for ln in out.splitlines() if "FAIL (" in ln]
    assert len(fail_lines) == 1, f"FAIL split across lines:\n{out}"
    assert re.match(r".*FAIL \(([^)]+)\)\s*$", fail_lines[0])
