"""Behavioral regressions for the supplementary completeness audit."""
import sys
import os
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, Mock, patch
import io
import json
import zipfile
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from supp_finder import Candidate, find_all_candidates, validate_downloaded_file
from repository_resolver import expand_repository_candidates
from archive_fixture import office_archive

BASE = 'https://journal.example/article'


def test_formats_do_not_cover_missing_table():
    result = find_all_candidates('<section id="supplementary">Tables S1-S2<a href="/TableS1.xlsx">Table S1</a><a href="/TableS1.pdf">Table S1</a></section>', BASE)
    assert result.is_incomplete and 'S2' in result.incomplete_reason


def test_one_workbook_can_cover_explicit_range():
    result = find_all_candidates('<section id="supplementary"><a href="/all.xlsx">Supplementary Tables S1-S10</a></section>', BASE)
    assert not result.is_incomplete


def test_empty_neighbouring_section_remains_incomplete():
    result = find_all_candidates('<section id="supplementary"><a href="/data.xlsx">Supplementary data</a></section><section id="supplementary-tables"><button aria-expanded="false">Expand tables</button></section>', BASE)
    assert result.is_incomplete


def test_attribute_evidence_survives_filtering():
    result = find_all_candidates('<a href="https://doi.org/10.1234/dataset" aria-label="Supplementary Table S1">Download</a>', BASE)
    assert len(result.candidates) == 1
    assert result.candidates[0].source_url == BASE


def test_download_attribute_precedes_article_href():
    result = find_all_candidates(f'<a href="{BASE}" data-download-url="/files/S1.xlsx">Supplementary Table S1</a>', BASE)
    assert result.candidates[0].url == 'https://journal.example/files/S1.xlsx'


def test_truncated_office_is_rejected(tmp_path):
    path = tmp_path / 'S1.xlsx'
    path.write_bytes(b'PK\x03\x04truncated')
    assert not validate_downloaded_file(str(path))[0]


def test_non_office_zip_is_not_excel(tmp_path):
    path = tmp_path / 'S1.xlsx'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('other.txt', 'data')
    assert not validate_downloaded_file(str(path))[0]


def test_repository_landing_is_expanded():
    response = Mock(url='https://zenodo.org/records/123', encoding='utf-8', headers={'Content-Type': 'text/html'})
    response.iter_content.return_value = [b'<a href="/records/123/files/TableS1.xlsx">Table S1</a>']
    session = Mock()
    session.get.return_value = response
    results = expand_repository_candidates([Candidate('https://doi.org/10.5281/zenodo.123', source_url=BASE)], session)
    assert len(results) == 1 and results[0].url.endswith('/files/TableS1.xlsx')
    assert results[0].source_url == response.url
    response.close.assert_called_once()


def test_bsk_control_is_retained_without_url():
    result = find_all_candidates('<section id="supplementary"><button id="s1" data-journal-download-selector="#s1">Table S1</button></section>', BASE)
    assert result.candidates[0].extra_info['browser_selector'] == '#s1'


def test_publisher_failure_still_queries_oa(tmp_path):
    import journal_downloader as j
    with patch.object(sys, 'argv', ['journal_downloader.py', '10.1234/article', '-o', str(tmp_path), '--no-bsk', '--oa', '--list-only']), patch.object(j, 'load_saved_cookies', return_value=[]), patch.object(j.StealthyFetcher, 'fetch', side_effect=TimeoutError), patch.object(j.Fetcher, 'get', side_effect=TimeoutError), patch.object(j, 'resolve_oa_info', return_value=None) as resolver:
        j.main()
    resolver.assert_called_once_with('10.1234/article')


def test_all_mirrors_are_considered(tmp_path):
    import journal_downloader as j
    from oa_resolver import OAResult
    h = '<section id="supplementary">Tables S1-S3<a href="/S1.xlsx">Table S1</a></section>'
    page = NS(url=BASE, status=200, html_content=h, text=h)
    def mirror(url, **kwargs):
        number = 2 if 'first' in url else 3
        return NS(url=url, status=200, text=f'<a href="/S{number}.xlsx">Table S{number}</a>')
    with patch.object(sys, 'argv', ['journal_downloader.py', '10.1234/article', '-o', str(tmp_path), '--no-bsk', '--list-only']), patch.object(j, 'load_saved_cookies', return_value=[]), patch.object(j.StealthyFetcher, 'fetch', return_value=page), patch.object(j.Fetcher, 'get', side_effect=mirror) as fetch, patch.object(j, 'resolve_oa_info', return_value=OAResult(oa_landing_urls=['https://first.example/article', 'https://second.example/article'])):
        j.main()
    assert fetch.call_count == 2


def test_partial_api_continues_to_cdn_and_oa(tmp_path):
    import scansci_supp_downloader as a
    with patch.object(sys, 'argv', ['scansci_supp_downloader.py', '10.1016/j.fake', '-o', str(tmp_path), '--no-bsk', '--no-vpn']), patch.object(a, 'load_config', return_value={}), patch.object(a, 'resolve_doi_url', return_value='https://www.sciencedirect.com/science/article/pii/S1234567890123456'), patch.object(a, 'try_elsevier_api_campus', return_value=([str(tmp_path / 'mmc1.xlsx')], {1, 2})), patch.object(a, 'try_elsevier_cdn_brute_force', return_value=[]) as cdn, patch.object(a, '_setup_authenticated_session_and_cookies', return_value=(NS(), [])), patch.object(a, 'fetch_page_html', return_value=('', 0, BASE)), patch.object(a, 'resolve_oa_info', return_value=None) as oa:
        a.main()
    assert cdn.call_count and oa.call_count


def test_suspended_download_resumes_same_operation(tmp_path):
    from browserskill_adapter import BrowserSkillSession
    session = BrowserSkillSession(bsk_bin='/bin/bsk')
    session.session_id = 'session'
    def invoke(argv, **kwargs):
        if argv[1] == 'download':
            return NS(returncode=6, stdout=json.dumps({'data': {'operation_id': 'op1', 'dialog': {'id': 'd1', 'type': 'confirm', 'message': 'Download supplementary file?'}}}), stderr='')
        if argv[1] == 'operation':
            Path(argv[argv.index('--out') + 1]).write_bytes(b'a,b\n1,2\n')
            return NS(returncode=0, stdout=json.dumps({'data': {'state': 'completed', 'result': {'saved': True}}}), stderr='')
        return NS(returncode=0, stdout='{}', stderr='')
    with patch('browserskill_adapter.subprocess.run', side_effect=invoke) as run:
        session.download('#download', str(tmp_path / 'S1.csv'))
    assert [c.args[0][1] for c in run.call_args_list] == ['download', 'dialog', 'operation']
    assert (tmp_path / 'S1.csv').read_bytes() == b'a,b\n1,2\n'


def test_explicit_table_list_detects_missing_member():
    result = find_all_candidates('<section id="supplementary">Tables S1, S2, and S3<a href="/S1.xlsx">Table S1</a><a href="/S3.xlsx">Table S3</a></section>', BASE)
    assert result.is_incomplete and 'S2' in result.incomplete_reason


def test_real_multisheet_excel_conversion(tmp_path):
    openpyxl = pytest.importorskip('openpyxl')
    pytest.importorskip('pandas')
    from markitdown_converter import convert_file_to_markdown
    workbook = openpyxl.Workbook()
    workbook.active.title = 'Table S1'
    workbook.active.append(['Mineral', 'Value'])
    workbook.active.append(['Quartz', 42])
    second = workbook.create_sheet('Table S2')
    second.append(['Mineral', 'Value'])
    second.append(['Calcite', 17])
    path = tmp_path / 'tables.xlsx'
    workbook.save(path)
    original = path.read_bytes()
    assert validate_downloaded_file(str(path))[0]
    converted = convert_file_to_markdown(str(path))
    assert 'Quartz' in converted and 'Calcite' in converted
    assert path.read_bytes() == original


def test_candidate_referer_overrides_other_source(tmp_path):
    import journal_downloader as j
    candidate = Candidate('https://mirror.example/S1.csv', 'S1.csv', source_url='https://mirror.example/article')
    response = NS(status=200, body=b'x,y\n1,2\n', headers={'Content-Type': 'text/csv'})
    with patch.object(j.Fetcher, 'get', return_value=response) as fetch:
        assert j.download_file(candidate, str(tmp_path), referer=BASE)
    assert fetch.call_args.kwargs['headers']['Referer'] == candidate.source_url


def test_redirected_retry_uses_new_base(tmp_path, capsys):
    import journal_downloader as j
    first = NS(url=BASE, status=200, text='<p>Tables S1</p>', html_content='<p>Tables S1</p>')
    html = '<a href="files/S1.xlsx">Table S1</a>'
    retry = NS(url='https://mirror.example/paper/', status=200, text=html, html_content=html)
    with patch.object(sys, 'argv', ['journal_downloader.py', BASE, '-o', str(tmp_path), '--no-bsk', '--no-oa', '--list-only']), patch.object(j, 'load_saved_cookies', return_value=[]), patch.object(j.StealthyFetcher, 'fetch', side_effect=[first, retry]):
        j.main()
    assert 'https://mirror.example/paper/files/S1.xlsx' in capsys.readouterr().out


# ── P0 H4: table ids keep hierarchy, 'to' ranges, Appendix / Extended Data ──

def test_h4_hierarchical_table_ids_do_not_collapse():
    from supp_finder import supplementary_table_ids
    assert supplementary_table_ids('Table S1.1 and Table S1.2') == {'S1.1', 'S1.2'}


def test_h4_to_range_is_expanded():
    from supp_finder import supplementary_table_ids
    assert supplementary_table_ids('Tables S1 to S3') == {'S1', 'S2', 'S3'}


def test_h4_bare_s_range_is_expanded():
    from supp_finder import supplementary_table_ids
    assert supplementary_table_ids('S1-S3') == {'S1', 'S2', 'S3'}


def test_h4_appendix_and_extended_data_prefixes():
    from supp_finder import supplementary_table_ids
    assert supplementary_table_ids('Appendix A1-A3') == {'A1', 'A2', 'A3'}
    assert supplementary_table_ids('Extended Data Table 1') == {'E1'}
    assert supplementary_table_ids('Extended Data Tables 1-3') == {'E1', 'E2', 'E3'}


def test_h4_to_range_missing_member_is_reported():
    result = find_all_candidates('<section id="supplementary">Tables S1 to S3<a href="/S1.xlsx">Table S1</a><a href="/S2.xlsx">Table S2</a></section>', BASE)
    assert result.is_incomplete and 'S3' in result.incomplete_reason


def test_h4_hierarchical_missing_member_is_reported():
    result = find_all_candidates('<section id="supplementary">Tables S1.1 and S1.2<a href="/S1-1.xlsx">Table S1.1</a></section>', BASE)
    assert result.is_incomplete and 'S1.2' in result.incomplete_reason


# ── P0 H5: file-count and table-range declarations are separate ──

def test_h5_file_count_ignores_table_mention():
    from supp_finder import detect_declared_supplement_count, detect_declared_file_count, detect_declared_table_range
    text = '3 Supplementary Files. Detailed metrics are available in Supplementary Table S10.'
    assert detect_declared_file_count(text) == 3
    assert detect_declared_table_range(text) == {'S10'}
    # Compat entry keeps the file-count reading only; the S10 mention must not inflate it.
    assert detect_declared_supplement_count(text) == 3


def test_h5_single_table_mention_is_not_a_count_of_ten():
    from supp_finder import detect_declared_supplement_count, detect_declared_table_range
    assert detect_declared_supplement_count('See Supplementary Table S10.') is None
    assert detect_declared_table_range('See Supplementary Table S10.') == {'S10'}


def test_h5_chinese_table_word_without_supplement_qualifier_is_ignored():
    from supp_finder import detect_declared_supplement_count, detect_declared_table_range
    assert detect_declared_supplement_count('本文包含 4 个表格。') is None
    assert detect_declared_table_range('本文包含 4 个表格。') == set()


def test_h5_bare_table_count_expands_from_one():
    from supp_finder import detect_declared_supplement_count, detect_declared_table_range
    assert detect_declared_table_range('包含 5 个附表及相关数据。') == {'S1', 'S2', 'S3', 'S4', 'S5'}
    assert detect_declared_supplement_count('包含 5 个附表及相关数据。') is None


def test_h5_table_range_does_not_count_as_files():
    from supp_finder import detect_declared_supplement_count, detect_declared_table_range
    assert detect_declared_table_range('Tables S1-S3 in Supplementary Information') == {'S1', 'S2', 'S3'}
    assert detect_declared_supplement_count('Tables S1-S3 in Supplementary Information') is None


def test_h5_high_table_number_does_not_fake_incompleteness():
    # A 'Table S99' that only exists inside a <script> block is not a visible
    # declaration, and a high table number must never inflate the file count:
    # 3 declared files with 3 candidates is complete.
    html = ('<html><head><script>const ref = "Table S99";</script></head><body>'
            '<section id="supplementary">3 Supplementary Files.'
            '<a href="/a.xlsx">Table S1</a><a href="/b.xlsx">Table S2</a><a href="/c.xlsx">Table S3</a>'
            '</section></body></html>')
    result = find_all_candidates(html, BASE)
    assert not result.is_incomplete


def test_h5_table_range_still_drives_missing_check():
    html = '<section id="supplementary">Tables S1-S3<a href="/S1.xlsx">Table S1</a><a href="/S2.xlsx">Table S2</a></section>'
    result = find_all_candidates(html, BASE)
    assert result.is_incomplete and 'S3' in result.incomplete_reason


# ── P0 H6: explicit candidate table ids, dedup merge, structured manifest ──

def test_h6_same_url_two_labels_keeps_both_table_ids():
    html = ('<section id="supplementary">Tables S1-S2'
            '<a href="/all.xlsx">Table S1</a><a href="/all.xlsx">Table S2</a></section>')
    result = find_all_candidates(html, BASE)
    assert len(result.candidates) == 1
    assert result.candidates[0].covered_table_ids == {'S1', 'S2'}
    assert not result.is_incomplete


def test_h6_download_marker_query_params_do_not_duplicate():
    html = ('<section id="supplementary">'
            '<a href="/all.xlsx?download=1">Table S1</a>'
            '<a href="/all.xlsx?download=2">Table S1</a></section>')
    result = find_all_candidates(html, BASE)
    assert len(result.candidates) == 1


def test_h6_distinct_query_params_are_not_merged():
    html = ('<section id="supplementary">'
            '<a href="/dl?id=1">Table S1</a><a href="/dl?id=2">Table S2</a></section>')
    result = find_all_candidates(html, BASE)
    assert len(result.candidates) == 2


def test_h6_parent_text_label_is_recorded_on_candidate():
    html = '<section id="supplementary"><ul><li>Table S2 <a href="/dl">Download</a></li></ul></section>'
    result = find_all_candidates(html, BASE)
    assert len(result.candidates) == 1
    assert 'S2' in result.candidates[0].covered_table_ids


def test_h6_covered_table_ids_prefers_explicit_field():
    from supp_finder import Candidate, covered_table_ids
    cand = Candidate(url='https://journal.example/dl', filename='dl.bin', link_text='Download',
                     covered_table_ids={'S9'})
    assert covered_table_ids([cand]) == {'S9'}


def test_h6_manifest_is_structured_and_atomic(tmp_path):
    from supp_finder import save_download_manifest, load_download_manifest
    manifest = {}
    manifest['S1.xlsx'] = {'url': 'https://journal.example/S1.xlsx', 'source_url': BASE,
                           'table_ids': ['S1'], 'size': 12, 'sha256': 'ab' * 32, 'status': 'downloaded'}
    save_download_manifest(str(tmp_path), manifest)
    assert not [p for p in tmp_path.iterdir() if p.suffix == '.tmp']
    loaded = load_download_manifest(str(tmp_path))
    entry = loaded['S1.xlsx']
    assert entry['url'] == 'https://journal.example/S1.xlsx'
    assert entry['source_url'] == BASE
    assert entry['table_ids'] == ['S1']
    assert entry['size'] == 12 and entry['sha256'] == 'ab' * 32 and entry['status'] == 'downloaded'


def test_h6_corrupt_manifest_warns_instead_of_silently_empty(tmp_path, capsys):
    from supp_finder import load_download_manifest
    (tmp_path / '.download_manifest.json').write_text('{not json')
    assert load_download_manifest(str(tmp_path)) == {}
    captured = capsys.readouterr()
    assert 'manifest' in (captured.err + captured.out).lower()


def test_h6_old_string_manifest_still_loads(tmp_path):
    from supp_finder import save_download_manifest, load_download_manifest, manifest_entry_url
    save_download_manifest(str(tmp_path), {'S1.xlsx': 'https://journal.example/S1.xlsx'})
    loaded = load_download_manifest(str(tmp_path))
    assert manifest_entry_url(loaded['S1.xlsx']) == 'https://journal.example/S1.xlsx'


# ── P0 H3: dynamic 'not loaded' signal + M13 settle/scroll ──

def test_h3_supp_heading_without_candidates_needs_browser():
    html = '<section id="supplementary"><h2>Supplementary Material</h2><p>Loading...</p></section>'
    result = find_all_candidates(html, BASE)
    assert result.candidates == []
    assert result.needs_browser is True


def test_h3_next_data_without_parsed_url_needs_browser():
    html = ('<html><head><script id="__NEXT_DATA__" type="application/json">'
            '{"props": {"pageProps": {"article": {"title": "Test"}}}}</script></head><body></body></html>')
    result = find_all_candidates(html, BASE)
    assert result.needs_browser is True


def test_h3_next_data_with_parsed_url_does_not_need_browser():
    html = ('<html><head><script id="__NEXT_DATA__" type="application/json">'
            '{"supplementaryMaterials": [{"fileName": "Supplementary_Table_S1.xlsx", "downloadUrl": "/api/dl/1"}]}'
            '</script></head><body></body></html>')
    result = find_all_candidates(html, BASE)
    assert len(result.candidates) == 1
    assert result.needs_browser is False


def test_h3_plain_page_with_candidates_does_not_need_browser():
    result = find_all_candidates('<section id="supplementary"><a href="/S1.xlsx">Table S1</a></section>', BASE)
    assert result.needs_browser is False


def test_h3_plain_page_without_signals_does_not_need_browser():
    result = find_all_candidates('<html><body><p>Results text.</p></body></html>', BASE)
    assert result.needs_browser is False


def test_h3_accordion_action_scrolls_in_segments_and_waits_for_settle():
    import re
    from supp_finder import expand_page_accordions_action

    class FakePage:
        def __init__(self):
            self.scrolls = []
            self.waits = []
            self._count_calls = 0

        def evaluate(self, js):
            if 'scrollTo' in js:
                m = re.search(r'scrollTo\(0, (\d+)\)', js)
                self.scrolls.append(int(m.group(1)) if m else 0)
                return None
            if 'scrollHeight' in js:
                return 2500
            if 'innerHeight' in js:
                return 1000
            if 'querySelectorAll' in js:
                self._count_calls += 1
                return 3 if self._count_calls > 2 else self._count_calls
            return 1  # clicked count

        def wait_for_timeout(self, ms):
            self.waits.append(ms)

    page = FakePage()
    expand_page_accordions_action(page)
    # Segmented scroll to the bottom in viewport steps, then a final scroll to the bottom.
    assert page.scrolls == [1000, 2000, 2500, 2500]
    # Settle polling ran (counts 1, 2, 3, then stable 3) instead of one fixed sleep.
    assert len(page.waits) >= 4


# ── H1: real fetch status + is_blocked_page ──────────────────────────────────

def test_h1_is_blocked_page_detects_challenge_and_login():
    from supp_finder import is_blocked_page, page_fetch_is_failure

    assert is_blocked_page(
        "<html><head><title>Just a moment...</title></head>"
        "<body>Checking your browser before accessing the site</body></html>"
    )
    assert is_blocked_page(
        "<html><body><div>Please verify you are human to continue</div></body></html>"
    )
    assert is_blocked_page(
        "<html><head><title>Sign in</title></head>"
        "<body><form>Sign in to access this article</form></body></html>"
    )
    assert is_blocked_page(
        "<html><body><div>Access denied: your institution does not subscribe</div></body></html>"
    )
    # A normal article page that merely links to a sign-in page is NOT blocked.
    assert not is_blocked_page(
        '<html><body><a href="/login">Sign in</a><h1>Article title</h1>'
        '<a href="/s1.xlsx">Supplementary Table S1</a></body></html>'
    )
    assert not is_blocked_page(
        "<html><body><h1>Article</h1><p>Supplementary data are available.</p></body></html>"
    )
    # Caller rule: unknown status (None) + blocked features = failure;
    # unknown status alone is not a failure (BrowserSkill exposes no status).
    assert page_fetch_is_failure("<html><title>Just a moment</title></html>", None) is True
    assert page_fetch_is_failure("<html><body>Article text</body></html>", None) is False
    assert page_fetch_is_failure("<html><body>Article text</body></html>", 200) is False
    assert page_fetch_is_failure("<html><body>Article text</body></html>", 403) is True
    # A blocked page is a failure even when the transport reported 200.
    assert page_fetch_is_failure("<html><title>Access denied</title></html>", 200) is True


def test_h1_cdp_returns_real_navigation_status():
    import playwright_utils as pu
    from types import SimpleNamespace as NS

    mock_page = MagicMock()
    mock_page.url = "https://example.com/article"
    mock_page.content.return_value = "<html><body>Forbidden page</body></html>"
    mock_context = MagicMock()
    mock_context.pages = []
    mock_context.new_page.return_value = mock_page
    mock_context.cookies.return_value = []
    mock_browser = MagicMock()
    mock_browser.contexts = [mock_context]
    # A real navigation that answered 403 must not come back as a fake 200.
    mock_page.goto.return_value = NS(status=403)
    with patch("playwright.sync_api.sync_playwright") as mock_pw:
        mock_inst = MagicMock()
        mock_pw.return_value.__enter__.return_value = mock_inst
        mock_inst.chromium.connect_over_cdp.return_value = mock_browser
        _, status, _, _ = pu.fetch_page_via_cdp("http://127.0.0.1:9222", "https://example.com/article")
        assert status == 403
    # Reusing an already-open tab performs no fresh navigation, so the
    # status is unknown (None) rather than a synthetic 200.
    mock_page.goto.reset_mock()
    mock_context.pages = [mock_page]
    with patch("playwright.sync_api.sync_playwright") as mock_pw:
        mock_inst = MagicMock()
        mock_pw.return_value.__enter__.return_value = mock_inst
        mock_inst.chromium.connect_over_cdp.return_value = mock_browser
        _, status, _, _ = pu.fetch_page_via_cdp("http://127.0.0.1:9222", "https://example.com/article")
        assert status is None


def test_h1_bsk_blocked_body_is_rejected_and_status_is_none():
    import browserskill_adapter as bsk

    session = MagicMock()
    session.session_id = "sess-h1"
    session.bsk_bin = "/bin/bsk"
    session.tab_id = None
    session.current_url = None
    session.borrowed_tabs = set()
    # Normal title, but the body is a Turnstile challenge: must raise, not
    # return a synthetic 200.
    with patch.object(session, "navigate"), patch.object(
        session, "evaluate",
        return_value={"html": "<html><head><title>Article</title></head>"
                              "<body>Please verify you are human. Turnstile challenge.</body></html>",
                      "url": "https://example.org/article"},
    ):
        with pytest.raises(bsk.BrowserSkillExecutionError, match="verification"):
            bsk.fetch_page_via_browserskill("https://example.org/article", session=session, expand_accordions=False)
    # A normal page fetch has no HTTP status to report: None, not fake 200.
    with patch.object(session, "navigate"), patch.object(
        session, "evaluate",
        return_value={"html": "<html><body><h1>Article</h1></body></html>",
                      "url": "https://example.org/article"},
    ):
        html, status, final = bsk.fetch_page_via_browserskill(
            "https://example.org/article", session=session, expand_accordions=False)
        assert status is None
        assert "Article" in html


def test_h1_fetch_page_html_accepts_unknown_status_and_falls_back_on_blocked():
    import scansci_supp_downloader as a

    good_html = "<html><body><h1>Article</h1></body></html>"
    blocked_html = "<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"
    # BrowserSkill returned a usable DOM but no HTTP status: accept it
    # (previously `status < 400` raised TypeError on None and discarded it).
    with patch.object(a, "fetch_page_via_browserskill", return_value=(good_html, None, "https://example.org/article")):
        html, status, final = a.fetch_page_html("https://example.org/article", use_bsk=True)
        assert html == good_html
        assert status is None
    # BrowserSkill returned a blocked page with unknown status: fall through
    # to the requests path instead of treating it as a successful fetch.
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.url = "https://example.org/article"
    resp.text = good_html
    session.get.return_value = resp
    with patch.object(a, "fetch_page_via_browserskill", return_value=(blocked_html, None, "https://example.org/article")), \
         patch.object(a, "HAS_SCANSCI", False):
        html, status, final = a.fetch_page_html("https://example.org/article", session=session, use_bsk=True)
        assert html == good_html
        assert status == 200


# ── H2: Tier 0 failure classification ────────────────────────────────────────

def _h2_office_get():
    def fake_get(url, timeout=30, stream=True):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-disposition": ""}
        resp.iter_content = MagicMock(return_value=[office_archive()])
        return resp
    return fake_get


def test_h2_transient_head_errors_do_not_count_as_misses(tmp_path):
    """429 / timeout HEAD probes must not count toward consecutive misses:
    mmc1-mmc4 rate-limited, mmc5 exists -> mmc5 must still be found."""
    from scansci_supp_downloader import try_elsevier_cdn_brute_force

    def fake_head(url, timeout=10, allow_redirects=True):
        resp = MagicMock()
        if "mmc5.xlsx" in url:
            resp.status_code = 200
            resp.headers = {"content-length": "2048"}
        elif "mmc1." in url or "mmc2." in url or "mmc3." in url or "mmc4." in url:
            resp.status_code = 429
            resp.headers = {"retry-after": "0"}
        else:
            resp.status_code = 404
            resp.headers = {}
        return resp

    with patch("requests.Session.head", side_effect=fake_head), \
         patch("requests.Session.get", side_effect=_h2_office_get()), \
         patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")), \
         patch("time.sleep") as sleep:
        files = try_elsevier_cdn_brute_force("S12345", str(tmp_path), max_mmc=10, max_consecutive_misses=4)
        basenames = [os.path.basename(f) for f in files]
        assert "mmc5.xlsx" in basenames
        assert sleep.called  # transient probes were retried with backoff


def test_h2_timeout_head_probes_do_not_stop_the_scan(tmp_path):
    """HEAD timeouts must not count as misses either."""
    from scansci_supp_downloader import try_elsevier_cdn_brute_force
    import requests as _requests

    def fake_head(url, timeout=10, allow_redirects=True):
        if "mmc5.xlsx" in url:
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-length": "2048"}
            return resp
        if any(f"mmc{n}." in url for n in (1, 2, 3, 4)):
            raise _requests.exceptions.Timeout("timed out")
        resp = MagicMock()
        resp.status_code = 404
        resp.headers = {}
        return resp

    with patch("requests.Session.head", side_effect=fake_head), \
         patch("requests.Session.get", side_effect=_h2_office_get()), \
         patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")), \
         patch("time.sleep"):
        files = try_elsevier_cdn_brute_force("S12345", str(tmp_path), max_mmc=10, max_consecutive_misses=4)
        assert "mmc5.xlsx" in [os.path.basename(f) for f in files]


def test_h2_retry_after_header_is_respected(tmp_path):
    """A 429 with Retry-After must be retried after that delay, then succeed."""
    from scansci_supp_downloader import try_elsevier_cdn_brute_force

    calls = {"n": 0}

    def fake_head(url, timeout=10, allow_redirects=True):
        resp = MagicMock()
        if "mmc1.xlsx" in url:
            calls["n"] += 1
            if calls["n"] <= 2:
                resp.status_code = 429
                resp.headers = {"retry-after": "2"}
                return resp
            resp.status_code = 200
            resp.headers = {"content-length": "1024"}
            return resp
        resp.status_code = 404
        resp.headers = {}
        return resp

    with patch("requests.Session.head", side_effect=fake_head), \
         patch("requests.Session.get", side_effect=_h2_office_get()), \
         patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")), \
         patch("time.sleep") as sleep:
        files = try_elsevier_cdn_brute_force("S12345", str(tmp_path), max_mmc=3, max_consecutive_misses=2)
        assert "mmc1.xlsx" in [os.path.basename(f) for f in files]
        assert calls["n"] == 3
        assert any(call.args and call.args[0] == 2 for call in sleep.call_args_list)


def test_h2_extensions_come_from_data_extensions(tmp_path):
    """Tier 0 must probe DATA_EXTENSIONS formats the old 15-item list
    missed (.parquet/.ipynb) and composite extensions (.tar.gz)."""
    from scansci_supp_downloader import try_elsevier_cdn_brute_force
    from supp_finder import DATA_EXTENSIONS

    assert ".parquet" in DATA_EXTENSIONS and ".ipynb" in DATA_EXTENSIONS and ".tar.gz" in DATA_EXTENSIONS

    def fake_head(url, timeout=10, allow_redirects=True):
        resp = MagicMock()
        if "mmc1.parquet" in url or "mmc1.ipynb" in url or "mmc1.tar.gz" in url:
            resp.status_code = 200
            resp.headers = {"content-length": "1024"}
        else:
            resp.status_code = 404
            resp.headers = {}
        return resp

    with patch("requests.Session.head", side_effect=fake_head), \
         patch("requests.Session.get", side_effect=_h2_office_get()), \
         patch("scansci_supp_downloader.validate_downloaded_file", return_value=(True, "valid")), \
         patch("time.sleep"):
        files = try_elsevier_cdn_brute_force("S12345", str(tmp_path), max_mmc=3, max_consecutive_misses=2)
        basenames = [os.path.basename(f) for f in files]
        assert "mmc1.parquet" in basenames
        assert "mmc1.ipynb" in basenames
        assert "mmc1.tar.gz" in basenames
