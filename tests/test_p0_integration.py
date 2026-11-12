"""P0 multi-tier integration smoke test.

Simulates one end-to-end flow with local fixtures / mocks only (no
network, no journal accounts, no paid APIs) and asserts the *final*
missing list, not just single functions:

  1. challenge page (fake HTTP 200) -> rejected, never observed;
  2. publisher page 1 declares "Supplementary Tables S1-S3." plus
     "Table S1.1" and links S1; retry page 2 links only S2 and declares
     nothing (H7: the first page's declaration must survive);
  3. Tier 0 CDN: mmc1/mmc2/mmc4 exist, mmc3 is a hole (404), mmc2's
     first HEAD is a 429 and only succeeds after retry (H2);
  4. OA mirror discovery fails first (connection reset), then
     rediscover(force=True) recovers the S3 candidate (H8);
  5. download: S2 fails transiently (429) and succeeds via the
     alternate transport (H8);
  6. final verdict: only S1.1 is missing, attributed to page 1.

A new file (tests/test_p0_integration.py) rather than extending
tests/test_reaudit_regressions.py: the smoke test wires several
subsystems together and reads better as a standalone scenario.
"""
import io
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from supp_finder import find_all_candidates, page_fetch_is_failure  # noqa: E402
from discovery_state import (DiscoveryEvidence, MirrorDiscovery,  # noqa: E402
                             download_with_recovery)
from oa_resolver import OAResult  # noqa: E402

BASE = 'https://journal.example/article'
MIRROR = 'https://mirror.example/article'

CHALLENGE_HTML = ('<html><head><title>Just a moment</title></head>'
                  '<body>Checking your browser before accessing '
                  'journal.example. Table S1 Table S2</body></html>')

PAGE1 = ('<section id="supplementary"><h2>Supplementary Tables S1-S3.</h2>'
         '<p>Table S1.1 is included.</p>'
         '<a href="/s1.xlsx">Table S1</a></section>')
PAGE2 = ('<html><body><h1>Article</h1>'
         '<a href="/s2.xlsx">Table S2</a></body></html>')
MIRROR_HTML = ('<section id="supplementary">'
               '<a href="/s3.xlsx">Table S3</a></section>')


def _zip_bytes():
    """Minimal valid .xlsx payload (OOXML structure, so validation passes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('[Content_Types].xml',
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    '</Types>')
        zf.writestr('xl/workbook.xml',
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        zf.writestr('xl/worksheets/sheet1.xml',
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>')
    return buf.getvalue()


def test_p0_multitier_smoke_final_missing_list_is_s1_1_only(tmp_path):
    import scansci_supp_downloader as scansci

    evidence = DiscoveryEvidence()

    # 1. Challenge page with a fake 200: rejected, never observed, so its
    #    text cannot pollute the evidence (H1).
    assert page_fetch_is_failure(CHALLENGE_HTML, 200) is True

    # 2. Publisher pages: page 1 declares S1-S3 + S1.1, page 2 only links S2.
    evidence.observe('publisher', PAGE1, BASE)
    evidence.observe('publisher', PAGE2, BASE)
    assert evidence.tables == {'S1', 'S2', 'S3', 'S1.1'}
    candidates = (find_all_candidates(PAGE1, BASE).candidates
                  + find_all_candidates(PAGE2, BASE).candidates)
    assert {c.url for c in candidates} == {
        'https://journal.example/s1.xlsx', 'https://journal.example/s2.xlsx'}

    # 3. Tier 0 CDN: mmc1 hit, mmc2 429-then-hit, mmc3 hole (404), mmc4 hit.
    payload = _zip_bytes()
    head_calls = {}
    head_lock = threading.Lock()

    def fake_head(url, **kwargs):
        with head_lock:
            head_calls[url] = head_calls.get(url, 0) + 1
            n = head_calls[url]
        if url.endswith('-mmc1.xlsx') or url.endswith('-mmc4.xlsx'):
            return NS(status_code=200, headers={'content-length': str(len(payload))})
        if url.endswith('-mmc2.xlsx'):
            if n == 1:
                return NS(status_code=429, headers={})
            return NS(status_code=200, headers={'content-length': str(len(payload))})
        return NS(status_code=404, headers={})

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def head(self, url, **kwargs):
            return fake_head(url, **kwargs)

        def get(self, url, **kwargs):
            resp = NS(status_code=200, headers={})
            resp.iter_content = lambda chunk_size: [payload]
            return resp

    with patch.object(scansci.requests, 'Session', FakeSession), \
            patch.object(scansci.time, 'sleep', lambda s: None):
        found = scansci.try_elsevier_cdn_brute_force(
            'S1234567890', str(tmp_path), max_mmc=4, max_consecutive_misses=4)
    found_names = {Path(f).name for f in found}
    assert found_names == {'mmc1.xlsx', 'mmc2.xlsx', 'mmc4.xlsx'}
    assert 'mmc3.xlsx' not in found_names  # the hole did not stop the scan

    # 4. OA mirror: first discovery fails, rediscovery recovers S3 (H8).
    fetch_attempts = {'n': 0}

    def fetch_mirror(url):
        fetch_attempts['n'] += 1
        if fetch_attempts['n'] == 1:
            raise ConnectionError('connection reset by peer')
        return MIRROR_HTML, 200, url

    mirrors = MirrorDiscovery(
        '10.1234/article', True,
        lambda doi: OAResult(oa_landing_urls=[MIRROR]), fetch_mirror, evidence)
    assert mirrors.discover() == []
    assert MIRROR in mirrors.failures
    recovered = mirrors.rediscover()
    assert [c.url for c in recovered] == ['https://mirror.example/s3.xlsx']
    assert MIRROR not in mirrors.failures
    candidates = candidates + recovered

    # 5. Download: S1 ok, S2 transient 429 then ok via the alternate
    #    transport, S3 (mirror) ok.
    retry_calls = []

    def download(cand):
        if cand.url.endswith('/s2.xlsx'):
            return (False, 'HTTP 429 Too Many Requests')
        return '/tmp/' + Path(cand.url).name

    def retry_download(cand):
        retry_calls.append(cand.url)
        return '/tmp/' + Path(cand.url).name

    successful, attempted = download_with_recovery(
        candidates, download, mirrors, lambda items: list(items),
        retry_download=retry_download, sleep=lambda s: None)
    assert {c.url for c in successful} == {
        'https://journal.example/s1.xlsx',
        'https://journal.example/s2.xlsx',
        'https://mirror.example/s3.xlsx'}
    assert retry_calls == ['https://journal.example/s2.xlsx']
    assert attempted == 3

    # 6. Final verdict: S3 was recovered via the mirror, S2 via retry,
    #    the mmc hole never stopped Tier 0 -- only S1.1 is still missing,
    #    and the reason says it came from the first publisher page.
    incomplete, reason = evidence.check(successful)
    assert incomplete
    assert 'S1.1' in reason
    assert 'publisher#1' in reason
    assert 'S3' not in reason and 'S2' not in reason
