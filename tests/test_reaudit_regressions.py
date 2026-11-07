"""Whole-flow cases missed by the initial audit fixes."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from supp_finder import Candidate, find_all_candidates, is_result_obviously_incomplete

BASE = 'https://journal.example/article'


def test_file_count_cannot_be_satisfied_by_table_count():
    result = find_all_candidates('<section id="supplementary"><p>3 Supplementary Files</p><a href="/all.xlsx">Supplementary Tables S1-S10</a></section>', BASE)
    assert result.is_incomplete
    assert 'file' in result.incomplete_reason.lower()


def test_appendix_table_identities_do_not_merge_formats():
    result = find_all_candidates('<section id="supplementary">Supplementary Tables A1-A2<a href="/A1.csv">Table A1</a><a href="/A1.pdf">Table A1</a></section>', BASE)
    assert result.is_incomplete and 'A2' in result.incomplete_reason


def test_empty_section_without_button_is_unresolved():
    result = find_all_candidates('<section id="supplementary"><a href="/data.xlsx">Supplementary data</a></section><section id="supplementary-tables"><h2>Supplementary tables</h2></section>', BASE)
    assert result.is_incomplete


def test_repository_keeps_parent_coverage_and_section():
    from repository_resolver import expand_repository_candidates
    html = '<section id="supplementary"><a href="https://doi.org/10.5281/zenodo.123">Supplementary Tables S1-S2</a></section>'
    initial = find_all_candidates(html, BASE)
    response = Mock(url='https://zenodo.org/records/123', encoding='utf-8', headers={'Content-Type': 'text/html'})
    response.iter_content.return_value = [b'<a href="/records/123/files/data.xlsx">Download dataset</a>']
    session = Mock(); session.get.return_value = response
    resolved = expand_repository_candidates(initial.candidates, session)
    assert not is_result_obviously_incomplete(resolved, html, initial.declared_count)[0]
    assert resolved[0].source_url == response.url


def extract_controls(labels):
    from browserskill_adapter import PAGE_EXTRACTION_SCRIPT
    if not shutil.which('node'):
        pytest.skip('Node required to execute the actual extraction script')
    js = 'const labels=' + json.dumps(labels) + ';' + '''
const els=labels.map((textContent,i)=>({textContent,value:'',title:'',id:'b'+i,
getAttribute(k){return null},hasAttribute(k){return false},removeAttribute(k){delete this[k]},
setAttribute(k,v){this[k]=v}}));
global.document={querySelectorAll(){return els},documentElement:{outerHTML:''},title:''};
global.window={location:{href:'https://journal.example/article'}};
''' + PAGE_EXTRACTION_SCRIPT + ';console.log(JSON.stringify(els.map(el=>!!el["data-journal-download-selector"])));'
    return json.loads(subprocess.run(['node','-e',js],capture_output=True,text=True,check=True).stdout)


def test_only_download_controls_are_marked():
    assert extract_controls(['Delete supplementary file','Edit supplementary table','Download Table S1', '删除补充文件', '下载附表 S2']) == [False, False, True, False, True]


@pytest.mark.parametrize('advanced', [False, True])
def test_retry_new_missing_identity_survives_final_report(tmp_path, capsys, advanced):
    import journal_downloader as j
    import scansci_supp_downloader as a
    first = '<p>Supplementary Table S1</p>'
    second = '<section id="supplementary">Supplementary Tables S1-S3<a href="/S1.csv">Table S1</a><a href="/S2.csv">Table S2</a></section>'
    with contextlib.ExitStack() as stack:
        if advanced:
            stack.enter_context(patch.object(sys,'argv',['scansci_supp_downloader.py',BASE,'-o',str(tmp_path),'--list-only','--no-oa','--no-bsk','--no-api','--skip-cdn','--no-vpn']))
            stack.enter_context(patch.object(a,'load_config',return_value={}))
            stack.enter_context(patch.object(a,'resolve_doi_url',return_value=BASE))
            stack.enter_context(patch.object(a,'_setup_authenticated_session_and_cookies',return_value=(Mock(),[])))
            stack.enter_context(patch.object(a,'fetch_page_html',side_effect=[(first,200,BASE),(second,200,BASE)]))
            a.main()
        else:
            stack.enter_context(patch.object(sys,'argv',['journal_downloader.py',BASE,'-o',str(tmp_path),'--list-only','--no-oa','--no-bsk']))
            stack.enter_context(patch.object(j,'load_saved_cookies',return_value=[]))
            stack.enter_context(patch.object(j.StealthyFetcher,'fetch',side_effect=[NS(url=BASE,status=200,text=first,html_content=first),NS(url=BASE,status=200,text=second,html_content=second)]))
            j.main()
    output = capsys.readouterr().out
    assert any('[Warning]' in line and 'S3' in line for line in output.splitlines())


@pytest.mark.parametrize('advanced', [False, True])
@pytest.mark.parametrize('disabled', [False, True])
def test_download_failure_recovers_from_oa(tmp_path, advanced, disabled):
    import journal_downloader as j
    import scansci_supp_downloader as a
    from oa_resolver import OAResult
    html = '<title>Audit article</title><section id="supplementary"><a href="/S1.csv">Table S1</a></section>'
    mirror = 'https://mirror.example/article'
    module = a if advanced else j
    with contextlib.ExitStack() as stack:
        argv = ['downloader.py','10.1234/article','-o',str(tmp_path),'--no-bsk'] + (['--no-oa'] if disabled else [])
        if advanced:
            argv += ['--no-api','--skip-cdn','--no-vpn']
            stack.enter_context(patch.object(a,'load_config',return_value={}))
            stack.enter_context(patch.object(a,'resolve_doi_url',return_value=BASE))
            stack.enter_context(patch.object(a,'_setup_authenticated_session_and_cookies',return_value=(Mock(),[])))
            stack.enter_context(patch.object(a,'fetch_page_html',side_effect=lambda url,*args,**kw: (html,200,url)))
        else:
            stack.enter_context(patch.object(j,'load_saved_cookies',return_value=[]))
            stack.enter_context(patch.object(j.StealthyFetcher,'fetch',return_value=NS(url=BASE,status=200,text=html,html_content=html)))
            stack.enter_context(patch.object(j.Fetcher,'get',return_value=NS(url=mirror,status=200,text=html)))
        stack.enter_context(patch.object(sys,'argv',argv))
        resolver = stack.enter_context(patch.object(module,'resolve_oa_info',return_value=OAResult(oa_landing_urls=[mirror])))
        download = stack.enter_context(patch.object(module,'download_file',side_effect=lambda c,*args,**kw: str(tmp_path/'S1.csv') if 'mirror.example' in c.url else None))
        module.main()
    assert resolver.call_count == (0 if disabled else 1)
    assert download.call_count == (1 if disabled else 2)


@pytest.mark.parametrize("envelope", ["data", "result", None])
def test_running_operation_is_awaited_until_saved(tmp_path, envelope):
    from browserskill_adapter import BrowserSkillSession
    session=BrowserSkillSession(bsk_bin='/bin/bsk');session.session_id='s'
    polls=[]
    def wrapped(data):
        return {envelope:data} if envelope else data
    def invoke(argv,**kwargs):
        if argv[1]=='download':
            return NS(returncode=6, stdout=json.dumps({'data':{'operation_id':'op','dialog':{'id':'d','type':'confirm','message':'Download supplementary file?'}}}), stderr='')
        if argv[1]=='operation':
            polls.append(argv)
            if len(polls)==1:
                return NS(returncode=0,stdout=json.dumps(wrapped({'state':'running'})),stderr='')
            Path(argv[argv.index('--out')+1]).write_bytes(b'x,y\n1,2\n')
            return NS(returncode=0,stdout=json.dumps(wrapped({'state':'completed','result':{'saved':True}})),stderr='')
        return NS(returncode=0,stdout='{}',stderr='')
    with patch('browserskill_adapter.subprocess.run',side_effect=invoke), patch('browserskill_adapter.time.sleep'):
        session.download('#file',str(tmp_path/'data.csv'))
    assert len(polls)==2


def test_latest_snapshot_clears_stale_empty_section_but_keeps_declarations():
    from discovery_state import DiscoveryEvidence
    evidence=DiscoveryEvidence()
    evidence.observe('publisher','<section id="supplementary"><h2>Tables S1-S2</h2></section>',BASE)
    loaded='<section id="supplementary"><a href="/S1.csv">Table S1</a><a href="/S2.csv">Table S2</a></section>'
    evidence.observe('publisher',loaded,BASE)
    candidates=find_all_candidates(loaded,BASE).candidates
    assert not evidence.check(candidates)[0]
    assert evidence.check(candidates[:1])[0]


def test_repository_parent_requires_all_children_for_coverage():
    from repository_resolver import expand_repository_candidates
    html='<section id="supplementary"><a href="https://doi.org/10.5281/zenodo.123">Tables S1-S2</a></section>'
    initial=find_all_candidates(html,BASE)
    response=Mock(url='https://zenodo.org/records/123',encoding='utf-8',headers={'Content-Type':'text/html'})
    response.iter_content.return_value=[b'<a href="/files/a.csv">Dataset A</a><a href="/files/b.csv">Dataset B</a>']
    http=Mock();http.get.return_value=response
    children=expand_repository_candidates(initial.candidates,http)
    assert len(children)==2
    assert not is_result_obviously_incomplete(children,html,initial.declared_count)[0]
    assert is_result_obviously_incomplete(children[:1],html,initial.declared_count)[0]


@pytest.mark.parametrize('message', ['Delete supplementary file?', 'Download then delete original?', 'Continue?', ''])
def test_unrelated_confirm_is_never_accepted(tmp_path,message):
    from browserskill_adapter import BrowserSkillSession, BrowserSkillExecutionError
    session=BrowserSkillSession(bsk_bin='/bin/bsk');session.session_id='s'
    response=NS(returncode=6,stdout=json.dumps({'data':{'operation_id':'op','dialog':{'id':'d','type':'confirm','message':message}}}),stderr='')
    with patch('browserskill_adapter.subprocess.run',return_value=response) as run:
        with pytest.raises(BrowserSkillExecutionError,match='explicit decision'):
            session.download('#file',str(tmp_path/'data.csv'))
    assert run.call_count==1


@pytest.mark.parametrize('state,result', [('failed',{}), ('completed',{'saved':False}), ('unknown',{})])
def test_unsaved_operation_preserves_existing_file(tmp_path,state,result):
    from browserskill_adapter import BrowserSkillSession, BrowserSkillExecutionError
    session=BrowserSkillSession(bsk_bin='/bin/bsk');session.session_id='s'
    path=tmp_path/'data.csv';path.write_bytes(b'original')
    pending=NS(returncode=6,stdout=json.dumps({'data':{'operation_id':'op','dialog':{'id':'d','type':'alert'}}}),stderr='')
    success=NS(returncode=0,stdout='{}',stderr='')
    final=NS(returncode=0,stdout=json.dumps({'data':{'state':state,'result':result}}),stderr='')
    with patch('browserskill_adapter.subprocess.run',side_effect=[pending,success,final]):
        with pytest.raises(BrowserSkillExecutionError):
            session.download('#file',str(path))
    assert path.read_bytes()==b'original'


def test_operation_deadline_is_bounded(tmp_path):
    from browserskill_adapter import BrowserSkillSession, BrowserSkillExecutionError
    session=BrowserSkillSession(bsk_bin='/bin/bsk');session.session_id='s'
    pending=NS(returncode=6,stdout=json.dumps({'data':{'operation_id':'op','dialog':{'id':'d','type':'alert'}}}),stderr='')
    with patch('browserskill_adapter.subprocess.run',return_value=pending) as run, patch('browserskill_adapter.time.monotonic',side_effect=[0,2]):
        with pytest.raises(BrowserSkillExecutionError,match='Timed out'):
            session.download('#file',str(tmp_path/'data.csv'),timeout=1)
    assert run.call_count==1


def test_control_is_rechecked_before_click(tmp_path):
    from browserskill_adapter import download_file_via_browserskill
    session=Mock(session_id='s',current_url=BASE,tab_id='t')
    session.evaluate.return_value=False
    result=download_file_via_browserskill('#replaced-button',str(tmp_path/'data.csv'),session=session,verify_control=True)
    assert result is None
    session.download.assert_not_called()


@pytest.mark.parametrize("advanced", [False, True])
def test_real_http_failure_recovers_and_saves_csv(tmp_path,capsys,advanced):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import journal_downloader as j
    from oa_resolver import OAResult
    hits=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            hits.append(self.path)
            if self.path=='/blocked.csv':
                self.send_response(403);self.end_headers();self.wfile.write(b'Forbidden');return
            if self.path=='/article':
                body=html.encode();content_type='text/html'
            elif self.path=='/mirror':
                body=b'<section id="supplementary"><a href="/TableS1.csv">Table S1</a></section>'
                content_type='text/html'
            elif self.path=='/TableS1.csv':
                body=b'mineral,value\nQuartz,42\n';content_type='text/csv'
            else:
                self.send_response(404);self.end_headers();return
            self.send_response(200);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    origin=f'http://127.0.0.1:{server.server_port}'
    html='<title>Recovery fixture</title><section id="supplementary"><a href="/blocked.csv">Table S1</a></section>'
    try:
        with contextlib.ExitStack() as stack:
            if advanced:
                import scansci_supp_downloader as a
                stack.enter_context(patch.object(sys,'argv',['scansci_supp_downloader.py','10.1234/article','-o',str(tmp_path),'--no-bsk','--no-api','--skip-cdn','--no-vpn','--no-cookies']))
                stack.enter_context(patch.object(a,'load_config',return_value={}))
                stack.enter_context(patch.object(a,'resolve_doi_url',return_value=origin+'/article'))
                stack.enter_context(patch.object(a,'HAS_SCRAPLING',False))
                stack.enter_context(patch.object(a,'resolve_oa_info',return_value=OAResult(oa_landing_urls=[origin+'/mirror'])))
                a.main()
            else:
                stack.enter_context(patch.object(sys,'argv',['journal_downloader.py','10.1234/article','-o',str(tmp_path),'--no-bsk']))
                stack.enter_context(patch.object(j,'load_saved_cookies',return_value=[]))
                stack.enter_context(patch.object(j.StealthyFetcher,'fetch',return_value=NS(url=origin+'/article',status=200,text=html,html_content=html)))
                stack.enter_context(patch.object(j,'resolve_oa_info',return_value=OAResult(oa_landing_urls=[origin+'/mirror'])))
                j.main()

    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
    assert '/blocked.csv' in hits and '/mirror' in hits and '/TableS1.csv' in hits
    files=list(tmp_path.rglob('*.csv'))
    assert len(files)==1 and files[0].read_bytes()==b'mineral,value\nQuartz,42\n'
    assert 'Downloaded set remains incomplete' not in capsys.readouterr().out


def test_http_fallback_uses_full_scrapling_html(tmp_path,capsys):
    import journal_downloader as j
    from scrapling.parser import Selector
    response=Selector('<title>Fallback</title><a href="/S1.csv">Table S1</a>')
    page=NS(status=200,url=BASE,text=response.text,html_content=response.html_content)
    assert not page.text
    with patch.object(sys,'argv',['journal_downloader.py',BASE,'-o',str(tmp_path),'--no-bsk','--no-oa','--list-only']),patch.object(j,'load_saved_cookies',return_value=[]),patch.object(j.StealthyFetcher,'fetch',side_effect=TimeoutError),patch.object(j.Fetcher,'get',return_value=page):
        j.main()
    assert 'https://journal.example/S1.csv' in capsys.readouterr().out
