"""Behavioral regressions for the supplementary completeness audit."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
import io
import json
import zipfile
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from supp_finder import Candidate, find_all_candidates, validate_downloaded_file
from repository_resolver import expand_repository_candidates

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
