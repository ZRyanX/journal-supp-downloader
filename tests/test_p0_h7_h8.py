"""P0 H7/H8 regressions: evidence must not be overwritten (H7) and recovery
must split on failure reason instead of relying on the attempted set (H8).

Local fixtures / mocks only: no network, no journal accounts, no paid APIs.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from supp_finder import Candidate  # noqa: E402
from discovery_state import DiscoveryEvidence, MirrorDiscovery, download_with_recovery  # noqa: E402

BASE = 'https://journal.example/article'


def _cand(url, table_id):
    return Candidate(url=url, filename=f'{table_id}.xlsx',
                     link_text=f'Table {table_id}',
                     covered_table_ids={table_id})


# ── H7: DiscoveryEvidence keeps every observation ─────────────────────────

PAGE1 = ('<section id="supplementary"><h2>Supplementary Tables S1-S3.</h2>'
         '<p>Table S1.1 is included.</p>'
         '<a href="/s1.xlsx">Table S1</a></section>')
# Retry landing page: a different page with no declaration block at all.
PAGE2 = ('<html><body><h1>Article</h1>'
         '<a href="/s2.xlsx">Table S2</a></body></html>')


def test_h7_second_observe_does_not_overwrite_first_page():
    evidence = DiscoveryEvidence()
    evidence.observe('publisher', PAGE1, BASE)
    evidence.observe('publisher', PAGE2, BASE)
    # Both observations are retained, keyed per round, not overwritten.
    assert len(evidence.pages['publisher']) == 2
    assert evidence.tables == {'S1', 'S2', 'S3', 'S1.1'}


def test_h7_missing_is_union_over_all_pages_and_names_its_source():
    evidence = DiscoveryEvidence()
    evidence.observe('publisher', PAGE1, BASE)
    evidence.observe('publisher', PAGE2, BASE)
    # Only S1 and S2 are covered; S3 and S1.1 were declared on the first
    # page and must still be reported, attributed to that page.
    incomplete, reason = evidence.check([_cand(BASE + '/s1.xlsx', 'S1'),
                                         _cand(BASE + '/s2.xlsx', 'S2')])
    assert incomplete
    assert 'S3' in reason and 'S1.1' in reason
    assert 'publisher#1' in reason


def test_h7_file_counts_are_recorded_per_source_not_global_max():
    evidence = DiscoveryEvidence()
    evidence.observe('publisher', '<p>3 Supplementary Files</p>', BASE)
    evidence.observe('publisher', '<p>1 Supplementary File</p>', BASE)
    assert evidence.files_by_source == {'publisher#1': 3, 'publisher#2': 1}
    # Two candidates satisfy the second source but not the first.
    incomplete, reason = evidence.check([_cand(BASE + '/a.xlsx', 'S1'),
                                         _cand(BASE + '/b.xlsx', 'S2')])
    assert incomplete
    assert 'publisher#1' in reason
    assert 'publisher#2' not in reason
    # Three candidates satisfy both sources.
    assert not evidence.check([_cand(BASE + '/a.xlsx', 'S1'),
                               _cand(BASE + '/b.xlsx', 'S2'),
                               _cand(BASE + '/c.xlsx', 'S3')])[0]


def test_h7_observe_accepts_explicit_source_label():
    evidence = DiscoveryEvidence()
    evidence.observe('oa', '<p>2 Supplementary Files</p>', BASE, source='oa-mirror')
    assert evidence.files_by_source == {'oa-mirror': 2}
    assert evidence.pages['oa'][0][0] == 'oa-mirror'


def test_h7_describe_names_sources_and_retained_pages():
    evidence = DiscoveryEvidence()
    evidence.observe('publisher', PAGE1, BASE)
    evidence.observe('publisher', PAGE2, BASE)
    text = evidence.describe()
    assert 'S1.1' in text and 'S3' in text
    assert '2 page observation' in text


def test_h7_empty_observe_records_nothing():
    evidence = DiscoveryEvidence()
    evidence.observe('publisher', '', BASE)
    evidence.observe('publisher', None, BASE)
    assert evidence.pages == {}
    assert evidence.tables == set()
    assert evidence.files_by_source == {}


# ── H8: recovery splits on failure reason ──────────────────────────────────

class _Mirrors:
    """Minimal stand-in for MirrorDiscovery: discover() returns preset items."""
    def __init__(self, items=()):
        self.items = list(items)
        self.calls = 0

    def discover(self):
        self.calls += 1
        return list(self.items)


def _cand_url(url, table_id='S1'):
    return Candidate(url=url, filename=f'{table_id}.xlsx',
                     link_text=f'Table {table_id}',
                     covered_table_ids={table_id})


def test_h8_transient_tuple_failure_retries_via_alternate_transport():
    sleeps = []
    calls = {'download': 0, 'retry': 0}

    def download(cand):
        calls['download'] += 1
        return (False, 'HTTP 429 Too Many Requests')

    def retry_download(cand):
        calls['retry'] += 1
        return True

    ok, attempted = download_with_recovery(
        [_cand_url(BASE + '/s1.xlsx')], download, _Mirrors(), list,
        retry_download=retry_download, sleep=sleeps.append)
    assert [c.url for c in ok] == [BASE + '/s1.xlsx']
    assert attempted == 1
    assert calls == {'download': 1, 'retry': 1}
    assert sleeps == [0.5]


def test_h8_transient_exception_retries_with_exponential_backoff():
    sleeps = []
    attempts = {'retry': 0}

    def download(cand):
        raise TimeoutError('timed out')

    def retry_download(cand):
        attempts['retry'] += 1
        if attempts['retry'] < 2:
            raise ConnectionResetError('connection reset by peer')
        return (True, 'ok')

    ok, _ = download_with_recovery(
        [_cand_url(BASE + '/s1.xlsx')], download, _Mirrors(), list,
        retry_download=retry_download, sleep=sleeps.append)
    assert len(ok) == 1
    assert attempts['retry'] == 2
    assert sleeps == [0.5, 1.0]


def test_h8_deterministic_failures_are_never_retried():
    for reason in ('HTTP 404 Not Found', 'validation failed: not a zip file', 404):
        calls = {'download': 0, 'retry': 0}

        def download(cand, _calls=calls):
            _calls['download'] += 1
            return (False, reason)

        def retry_download(cand, _calls=calls):
            _calls['retry'] += 1
            return True

        ok, _ = download_with_recovery(
            [_cand_url(BASE + '/s1.xlsx')], download, _Mirrors(), list,
            retry_download=retry_download, sleep=lambda s: None)
        assert ok == []
        assert calls == {'download': 1, 'retry': 0}


def test_h8_unclassified_bare_failure_is_not_retried():
    calls = {'download': 0, 'retry': 0}

    def download(cand):
        calls['download'] += 1
        return False  # no reason reported: cannot be proven transient

    def retry_download(cand):
        calls['retry'] += 1
        return True

    ok, _ = download_with_recovery(
        [_cand_url(BASE + '/s1.xlsx')], download, _Mirrors(), list,
        retry_download=retry_download, sleep=lambda s: None)
    assert ok == []
    assert calls == {'download': 1, 'retry': 0}


def test_h8_no_alternate_transport_retries_same_download_once():
    sleeps = []
    calls = {'n': 0}

    def download(cand):
        calls['n'] += 1
        if calls['n'] == 1:
            return (False, 'HTTP 503 Service Unavailable')
        return True

    ok, _ = download_with_recovery(
        [_cand_url(BASE + '/s1.xlsx')], download, _Mirrors(), list,
        sleep=sleeps.append)
    assert len(ok) == 1
    assert calls['n'] == 2
    assert sleeps == [0.5]


def test_h8_transient_failure_is_reattempted_in_the_mirror_round():
    # Old code skipped any URL already in `attempted`; a transient failure
    # that exhausts its retries must still get a fresh attempt when the
    # mirror round re-discovers the same URL.
    calls = {'n': 0}
    cand = _cand_url(BASE + '/s1.xlsx')

    def download(c):
        calls['n'] += 1
        return (False, 'HTTP 429')

    ok, _ = download_with_recovery(
        [cand], download, _Mirrors([cand]), list,
        sleep=lambda s: None)
    # Round 1: initial + 1 same-download retry; round 2: the re-discovered
    # alternative is attempted again (initial + 1 retry).
    assert calls['n'] == 4
    assert ok == []


def test_h8_failure_reasons_are_recorded_and_printed(capsys):
    failures = {}

    def download(cand):
        return (False, 'HTTP 404 Not Found')

    ok, _ = download_with_recovery(
        [_cand_url(BASE + '/s1.xlsx')], download, _Mirrors(), list,
        sleep=lambda s: None, failures=failures)
    assert ok == []
    assert failures == {BASE + '/s1.xlsx': 'HTTP 404 Not Found'}
    assert 'HTTP 404' in capsys.readouterr().out


def test_h8_mirror_rediscover_reprobes_after_failure():
    from oa_resolver import OAResult
    mirror_url = 'https://mirror.example/article'
    mirror_html = ('<section id="supplementary">'
                   '<a href="/s3.xlsx">Table S3</a></section>')
    evidence = DiscoveryEvidence()
    attempts = {'n': 0}

    def fetch(url):
        attempts['n'] += 1
        if attempts['n'] == 1:
            raise ConnectionError('connection reset by peer')
        return mirror_html, 200, url

    mirrors = MirrorDiscovery('10.1234/article', True,
                              lambda doi: OAResult(oa_landing_urls=[mirror_url]),
                              fetch, evidence)
    assert mirrors.discover() == []
    assert mirror_url in mirrors.failures
    assert 'ConnectionError' in mirrors.failures[mirror_url]
    # A second plain discover() returns the memoized empty result.
    assert mirrors.discover() == []
    assert attempts['n'] == 1
    # rediscover(force=True) re-probes and recovers the candidate.
    found = mirrors.rediscover()
    assert [c.url for c in found] == ['https://mirror.example/s3.xlsx']
    assert mirror_url not in mirrors.failures
    assert attempts['n'] == 2


def test_h8_mirror_none_status_uses_page_fetch_is_failure():
    from oa_resolver import OAResult
    mirror_url = 'https://mirror.example/article'
    good_html = ('<section id="supplementary">'
                 '<a href="/s1.xlsx">Table S1</a></section>')
    blocked_html = '<html><title>Just a moment</title><body>checking</body></html>'
    evidence = DiscoveryEvidence()

    def fetch_blocked(url):
        return blocked_html, None, url

    mirrors = MirrorDiscovery('10.1234/article', True,
                              lambda doi: OAResult(oa_landing_urls=[mirror_url]),
                              fetch_blocked, evidence)
    assert mirrors.discover() == []
    assert mirrors.failures[mirror_url] == 'blocked page (challenge/login)'

    def fetch_good(url):
        return good_html, None, url

    mirrors2 = MirrorDiscovery('10.1234/article', True,
                               lambda doi: OAResult(oa_landing_urls=[mirror_url]),
                               fetch_good, evidence)
    found = mirrors2.discover()
    assert [c.url for c in found] == ['https://mirror.example/s1.xlsx']
    assert mirror_url not in mirrors2.failures
