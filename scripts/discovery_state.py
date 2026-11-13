"""Shared evidence and recovery state for both downloader entry points."""
import time

from supp_finder import (BeautifulSoup, DEFAULT_PARSER, supplementary_table_ids,
                         declared_file_count, covered_table_ids,
                         detect_declared_supplement_count,
                         detect_declared_table_range,
                         filename_dedup_key,
                         is_result_obviously_incomplete,
                         page_fetch_is_failure)


class DiscoveryEvidence:
    """Accumulate what every observed page declares, without overwriting.

    H7: observations are appended per (key, round/source) instead of
    replacing ``pages[key]``. A retry landing page that lacks the
    supplementary block can therefore no longer erase the declaration
    seen on the first page, and a stale file-count declaration stays
    attributable to the page that made it instead of lingering as a
    global ``max``. ``self.pages`` maps each key to a list of
    ``(source, html, base_url)`` tuples in observation order; ``source``
    defaults to ``"<key>#<n>"`` (1-based per key) and may be passed
    explicitly (e.g. an OA mirror URL round). ``self.files`` is kept as
    the largest per-source file count for backward compatibility, but
    :meth:`check` compares each source separately.
    """

    def __init__(self):
        self.pages = {}
        self.tables = set()
        self.files = 0
        self.files_by_source = {}
        self._observe_counts = {}

    def observe(self, key, html, base_url, source=None):
        if not html:
            return
        n = self._observe_counts.get(key, 0) + 1
        self._observe_counts[key] = n
        label = source or f'{key}#{n}'
        self.pages.setdefault(key, []).append((label, html, base_url))
        text = BeautifulSoup(html, DEFAULT_PARSER).get_text(' ', strip=True)
        self.tables.update(supplementary_table_ids(text))
        count = declared_file_count(text)
        self.files_by_source[label] = count
        self.files = max(self.files_by_source.values(), default=0)

    def _observations(self):
        """Every retained observation as (source, html, base_url)."""
        for entries in self.pages.values():
            yield from entries

    def check(self, candidates):
        covered = covered_table_ids(candidates)
        unique_urls = {c.url for c in candidates}
        reasons = []

        # Missing tables: the union over *all* retained pages, with each
        # page's contribution attributed to its source.
        missing_by_source = {}
        for label, html, _base in self._observations():
            text = BeautifulSoup(html, DEFAULT_PARSER).get_text(' ', strip=True)
            expected = supplementary_table_ids(text) | detect_declared_table_range(html)
            missing = expected - covered
            if missing:
                missing_by_source.setdefault(label, set()).update(missing)
        if missing_by_source:
            parts = ['{} (from {})'.format(', '.join(sorted(ids)), label)
                     for label, ids in sorted(missing_by_source.items())]
            reasons.append('Missing supplementary tables: ' + '; '.join(parts))

        # Declared file counts: compared per source, not against a global
        # max, so the reason names the page that is still unsatisfied.
        for label in sorted(self.files_by_source):
            count = self.files_by_source[label]
            if count and count > len(unique_urls):
                reasons.append(
                    f'File count ({len(unique_urls)}) is less than declared '
                    f'file count ({count}) by {label}')

        # Per-page section / count verdicts that the two checks above do
        # not already cover (e.g. an unresolved supplementary container).
        # The missing-table and file-count verdicts of
        # is_result_obviously_incomplete are skipped here because the
        # aggregated, source-annotated versions above already state them.
        for label, html, _base in self._observations():
            incomplete, reason = is_result_obviously_incomplete(
                candidates, html, detect_declared_supplement_count(html))
            if incomplete and not (
                    reason.startswith('Missing supplementary tables')
                    or reason.startswith('File count')):
                annotated = f'{reason} (from {label})'
                if annotated not in reasons:
                    reasons.append(annotated)

        if reasons:
            return True, '; '.join(reasons)
        return (False, '') if candidates else (True, 'Zero supplementary candidates found')

    def describe(self):
        parts = []
        for label in sorted(self.files_by_source):
            count = self.files_by_source[label]
            if count:
                parts.append(f'{count} file(s) declared by {label}')
        if self.tables:
            parts.append('table identities: ' + ', '.join(sorted(self.tables)))
        n_pages = sum(len(entries) for entries in self.pages.values())
        if n_pages > 1:
            parts.append(f'{n_pages} page observation(s) retained')
        return '; '.join(parts)


class MirrorDiscovery:
    """Memoize one bounded OA discovery round, reusable after transfer failures."""
    def __init__(self, doi, enabled, resolve, fetch, evidence):
        self.doi, self.enabled = doi, enabled
        self.resolve, self.fetch, self.evidence = resolve, fetch, evidence
        self.info = None
        self.queried = False
        self.candidates = []
        # H8: every probed mirror URL and why it failed, for diagnosis.
        # Entries are removed when a later probe of the same URL succeeds.
        self.failures = {}

    def discover(self, force=False):
        if (self.queried and not force) or not self.enabled or not self.doi:
            return self.candidates
        if force:
            # Fresh round: re-probe every mirror, including ones that
            # failed before; previously found candidates are rebuilt so a
            # re-discovery cannot duplicate them.
            self.queried = False
            self.candidates = []
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
                if not html:
                    self.failures[url] = 'empty response'
                    continue
                if page_fetch_is_failure(html, status):
                    # H1 rule: status may be None (no real navigation
                    # status exposed), so never compare it directly.
                    if status is not None and status >= 400:
                        self.failures[url] = f'HTTP {status}'
                    else:
                        self.failures[url] = 'blocked page (challenge/login)'
                    continue
                self.failures.pop(url, None)
                base = extract_effective_base_url(html, final or url)
                self.evidence.observe(url, html, base)
                for candidate in find_all_candidates(html, base, original_url=url).candidates:
                    if candidate.url not in seen_files:
                        self.candidates.append(candidate)
                        seen_files.add(candidate.url)
            except Exception as exc:
                self.failures[url] = f'{type(exc).__name__}: {exc}'
                print(f'  [OA Mirror] Fetch failed: {exc}')
        return self.candidates

    def rediscover(self, force=True):
        """Force a fresh discovery round (H8).

        Re-probes mirrors that failed before instead of returning the
        memoized (possibly empty) result of the first round.
        """
        return self.discover(force=force)


# ── H8: failure classification ──────────────────────────────────────────────

_TRANSIENT_MARKERS = (
    'timeout', 'timed out', 'connection reset', 'reset by peer',
    'broken pipe', 'connection refused', 'connection aborted',
    'temporarily', 'too many requests', 'rate limit', 'rate-limit',
    'service unavailable', 'bad gateway', 'gateway timeout',
    'econnreset', 'econnrefused', 'econnaborted',
)
_TRANSIENT_CODES = {'429', '502', '503', '504'}
_DETERMINISTIC_MARKERS = (
    'not found', 'gone', 'validation', 'invalid', 'checksum',
    'corrupt', 'file is empty', 'unsupported', 'malformed',
    'not a valid',
)
_DETERMINISTIC_CODES = {'404', '410'}


def _classify_reason(reason):
    """Classify a reported download failure reason.

    Returns ``'transient'`` (worth retrying on another transport:
    timeouts, connection resets, 429 / 5xx, and 403, where an
    authenticated session may succeed where a plain fetch did not),
    ``'deterministic'`` (404 / gone, validation failures: retrying the
    same URL cannot help), or ``'unknown'`` (nothing recognisable was
    reported, e.g. a bare ``False``/``None`` with no reason). Only
    ``'transient'`` is retried: retrying an unclassified failure would
    double every genuinely deterministic failure and delay the OA
    mirror round, which is the real recovery path for those.
    """
    if reason is None:
        return 'unknown'
    if isinstance(reason, int):
        if reason == 429 or reason >= 500:
            return 'transient'
        return 'deterministic'
    text = str(reason).lower()
    for code in _DETERMINISTIC_CODES:
        if code in text:
            return 'deterministic'
    for marker in _DETERMINISTIC_MARKERS:
        if marker in text:
            return 'deterministic'
    for code in _TRANSIENT_CODES:
        if code in text:
            return 'transient'
    for marker in _TRANSIENT_MARKERS:
        if marker in text:
            return 'transient'
    if '403' in text or 'forbidden' in text:
        return 'transient'
    return 'unknown'


def _classify_exception(exc):
    """Classify a raised download error by type (H8)."""
    import requests as _requests
    if isinstance(exc, (TimeoutError, ConnectionError,
                        _requests.Timeout, _requests.ConnectionError)):
        return 'transient'
    if isinstance(exc, (FileNotFoundError, ValueError)):
        return 'deterministic'
    status = getattr(exc, 'status', None) or getattr(exc, 'code', None)
    if isinstance(status, int):
        return 'transient' if (status == 429 or status >= 500 or status == 403) else 'deterministic'
    http_error = getattr(_requests, 'HTTPError', None)
    if http_error is not None and isinstance(exc, http_error):
        response = getattr(exc, 'response', None)
        code = getattr(response, 'status_code', None)
        if isinstance(code, int):
            return 'transient' if (code == 429 or code >= 500 or code == 403) else 'deterministic'
    text = str(exc).lower()
    if any(marker in text for marker in _DETERMINISTIC_MARKERS):
        return 'deterministic'
    if any(marker in text for marker in _TRANSIENT_MARKERS):
        return 'transient'
    return 'unknown'


def _normalize_download_result(result):
    """Normalize a download callable's return value to (ok, reason).

    Accepts a bool/path (truthy = success), ``None``/``False`` (failure
    with no reason), or a ``(ok, reason)`` tuple where reason may be an
    HTTP status int or a message string.
    """
    if isinstance(result, (tuple, list)) and len(result) == 2:
        ok, reason = result
        return bool(ok), reason
    return bool(result), None


def download_with_recovery(candidates, download, mirrors, expand,
                           retry_download=None, max_retries=2, sleep=None,
                           failures=None):
    """Attempt each URL, retrying transient failures on another transport.

    H8: the failure *reason* decides what happens next, not the
    ``attempted`` set. A transient failure (timeout / connection reset /
    429 / 5xx / 403) retries the same URL -- via ``retry_download`` when
    the caller supplies a different transport, otherwise via ``download``
    itself -- up to ``max_retries`` times with exponential backoff
    (0.5s, 1s, ...). A deterministic failure (404 / validation) and an
    unclassified failure (bare ``False``/``None`` with no reason, or an
    exception with no recognisable cause) are never retried, so a
    genuinely dead URL falls through to the OA mirror round instead of
    being retried blindly. When no alternate transport is supplied the
    same download is retried at most once, since re-running an identical
    transport is a weaker remedy. URLs that fail in the first round are
    *not* skipped in the mirror round: a re-discovered alternative for
    the same URL gets a fresh attempt. ``failures``, when given, is
    filled with ``{url: reason}`` for diagnosis; the unresolved warning
    prints the reason per URL.
    """
    if sleep is None:
        sleep = time.sleep
    if failures is None:
        failures = {}
    allowed_retries = max_retries if retry_download is not None else min(max_retries, 1)
    attempted, successful, failed = set(), [], []
    successful_urls = set()
    # P1-B: normalized filenames already downloaded in this run. A second
    # URL for the same filename (e.g. a PMC mirror of a Springer file) is
    # not downloaded again; a *failed* file is NOT in this set, so it still
    # gets its fresh mirror-round attempt (H8).
    successful_filenames = set()

    def attempt(items):
        seen_this_round = set()
        seen_fnames_this_round = set()
        for candidate in items:
            if candidate.url in seen_this_round:
                continue
            seen_this_round.add(candidate.url)
            if candidate.url in successful_urls:
                # Already downloaded in an earlier round; only failed
                # URLs get a fresh attempt in a later round.
                continue
            fname_key = filename_dedup_key(getattr(candidate, "filename", "") or "")
            if fname_key is not None:
                if fname_key in successful_filenames or fname_key in seen_fnames_this_round:
                    continue
                seen_fnames_this_round.add(fname_key)
            attempted.add(candidate.url)
            ok, last_reason = False, None
            for n in range(allowed_retries + 1):
                fn = download if (n == 0 or retry_download is None) else retry_download
                try:
                    ok, reason = _normalize_download_result(fn(candidate))
                    kind = 'transient' if not ok else None
                    if not ok:
                        kind = _classify_reason(reason)
                        last_reason = str(reason) if reason is not None else 'unknown failure (no reason reported)'
                except Exception as exc:  # noqa: BLE001 - classified below
                    ok = False
                    kind = _classify_exception(exc)
                    last_reason = f'{type(exc).__name__}: {exc}'
                if ok:
                    break
                if kind in ('deterministic', 'unknown') or n == allowed_retries:
                    break
                delay = min(0.5 * (2 ** n), 8.0)
                print(f'  [Recovery] Transient failure for {candidate.url} '
                      f'({last_reason}); retrying in {delay}s...')
                sleep(delay)
            if ok:
                successful.append(candidate)
                successful_urls.add(candidate.url)
                if fname_key is not None:
                    successful_filenames.add(fname_key)
            else:
                failed.append(candidate)
                failures[candidate.url] = last_reason

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
        # A URL that failed in both rounds is listed once.
        seen_unresolved, deduped = set(), []
        for c in unresolved:
            if c.url not in seen_unresolved:
                seen_unresolved.add(c.url)
                deduped.append(c)
        unresolved = deduped
        if unresolved:
            details = ', '.join(
                f'{c.filename or c.url} ({failures.get(c.url, "unknown")})'
                for c in unresolved)
            print('  [Warning] Unresolved downloads: ' + details)
    return successful, len(attempted)
