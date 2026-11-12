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
