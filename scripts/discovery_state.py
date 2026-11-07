"""Shared evidence and recovery state for both downloader entry points."""
from supp_finder import (BeautifulSoup, DEFAULT_PARSER, supplementary_table_ids,
                         declared_file_count, covered_table_ids,
                         detect_declared_supplement_count, is_result_obviously_incomplete)


class DiscoveryEvidence:
    def __init__(self):
        self.pages = {}
        self.tables = set()
        self.files = 0

    def observe(self, key, html, base_url):
        if not html:
            return
        self.pages[key] = (html, base_url)
        text = BeautifulSoup(html, DEFAULT_PARSER).get_text(' ', strip=True)
        self.tables.update(supplementary_table_ids(text))
        self.files = max(self.files, declared_file_count(text))

    def check(self, candidates):
        missing = self.tables - covered_table_ids(candidates)
        if missing:
            return True, 'Missing supplementary tables: ' + ', '.join(sorted(missing))
        if self.files > len({c.url for c in candidates}):
            return True, f'File count ({len(candidates)}) is less than declared file count ({self.files})'
        for html, base in self.pages.values():
            incomplete, reason = is_result_obviously_incomplete(candidates, html, detect_declared_supplement_count(html))
            if incomplete:
                return incomplete, reason
        return (False, '') if candidates else (True, 'Zero supplementary candidates found')

    def describe(self):
        parts = []
        if self.files:
            parts.append(f'{self.files} file(s) declared')
        if self.tables:
            parts.append('table identities: ' + ', '.join(sorted(self.tables)))
        return '; '.join(parts)


class MirrorDiscovery:
    """Memoize one bounded OA discovery round, reusable after transfer failures."""
    def __init__(self, doi, enabled, resolve, fetch, evidence):
        self.doi, self.enabled = doi, enabled
        self.resolve, self.fetch, self.evidence = resolve, fetch, evidence
        self.info = None
        self.queried = False
        self.candidates = []

    def discover(self):
        if self.queried or not self.enabled or not self.doi:
            return self.candidates
        self.queried = True
        from supp_finder import find_all_candidates, extract_effective_base_url
        info = self.resolve(self.doi)
        self.info = info
        seen_pages, seen_files = set(), set()
        for url in (info.oa_landing_urls if info else []):
            if url in seen_pages:
                continue
            seen_pages.add(url)
            print(f'  [OA Mirror] Probing: {url}')
            try:
                html, status, final = self.fetch(url)
                if not html or status >= 400:
                    continue
                base = extract_effective_base_url(html, final or url)
                self.evidence.observe(url, html, base)
                for candidate in find_all_candidates(html, base, original_url=url).candidates:
                    if candidate.url not in seen_files:
                        self.candidates.append(candidate)
                        seen_files.add(candidate.url)
            except Exception as exc:
                print(f'  [OA Mirror] Fetch failed: {exc}')
        return self.candidates


def download_with_recovery(candidates, download, mirrors, expand):
    """Attempt each URL once; discover alternatives only when a transfer fails."""
    attempted, successful, failed = set(), [], []
    def attempt(items):
        for candidate in items:
            if candidate.url in attempted:
                continue
            attempted.add(candidate.url)
            result = download(candidate)
            (successful if result else failed).append(candidate)
    attempt(candidates)
    if failed:
        alternatives = expand(mirrors.discover())
        attempt(alternatives)
        known = {c.url for c in candidates}
        candidates.extend(c for c in alternatives if c.url not in known)
    if failed:
        # Never infer success merely because an unrelated alternative downloaded.
        covered = covered_table_ids(successful)
        unresolved = [c for c in failed if not covered_table_ids([c]) or not covered_table_ids([c]) <= covered]
        if unresolved:
            print('  [Warning] Unresolved downloads: ' + ', '.join(c.filename or c.url for c in unresolved))
    return successful, len(attempted)
