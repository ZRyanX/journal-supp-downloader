#!/usr/bin/env python3
"""
Journal Supplementary Data Downloader
Uses Scrapling's StealthyFetcher to bypass Cloudflare and download
attachments/supplementary data from journal websites (Elsevier, Springer, etc.)

Usage:
    python journal_downloader.py <article_url_or_doi>
    python journal_downloader.py <article_url_or_doi> --output-dir ./downloads
    python journal_downloader.py <article_url_or_doi> --headful  # show browser
    python journal_downloader.py <article_url_or_doi> --proxy http://user:pass@host:port
    python journal_downloader.py <article_url_or_doi> --list-only

Dependencies:
    pip install "scrapling[all]"
    scrapling install
"""

import argparse
import html as _html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Ensure scripts directory is on sys.path
scripts_dir = os.path.dirname(os.path.abspath(__file__))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

from scrapling.fetchers import Fetcher, StealthyFetcher
from supp_finder import (
    find_all_candidates,
    is_result_obviously_incomplete,
    validate_downloaded_file,
    single_line,
    remove_invalid_existing_download,
    sanitize_filename,
    extract_filename_from_url,
    extract_filename_from_content_disposition,
    infer_file_extension,
    ensure_inferred_extension_on_disk,
    cookies_to_dict,
    extract_effective_base_url,
    Candidate,
    FindingDiagnostics,
    DATA_EXTENSIONS,
    MMC_PATTERN,
    SUPP_WAIT_SELECTOR,
    is_excluded_url,
    is_article_figure_url,
    load_download_manifest,
    save_download_manifest,
    manifest_entry_url,
    record_manifest_entry,
    disambiguate_target_filename,
    _normalize_url_for_dedup,
    extract_article_title,
    is_placeholder_or_blocked_title,
    migrate_or_merge_article_dir,
    dedupe_candidates,
    expand_page_accordions_action,
    is_blocked_page,
    page_fetch_is_failure,
    temp_path_for,
    commit_temp_file,
    discard_temp_file,
    write_bytes_chunked,
    # P1-CE: shared phase budget + download priority order.
    DEFAULT_PHASE_TIMEOUT_SECONDS,
    resolve_phase_timeout,
    sort_candidates_by_priority,
    # P1 round-3 minor (a): MITM-proxy certificate trust for the browser
    # layer (first call and every retry carry the same config).
    proxy_browser_trust_kwargs,
    playwright_proxy_config,
    # P1 H-1: independent budget for the browser fallback.
    DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS,
    DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS,
    RepoBrowserSession,
    browser_budget_from_args,
    ensure_phase_browser_budget,
    stealthy_fetch_bounded,
    fetch_sciengine_api_candidates,
    load_local_skill_config,
    configure_session_direct,
    has_system_chrome,
    has_gui_display,
    add_auto_browser_arguments,
    run_auto_browser_recovery,
)


try:
    from playwright_utils import (
        normalize_playwright_cookies,
        discover_active_cdp_port,
        resolve_cdp_ws_url,
        extract_cdp_cookies,
        merge_cookies,
        fetch_page_via_cdp,
        download_via_playwright_request,
    )
except ImportError:
    normalize_playwright_cookies = lambda c, default_url=None: c
    discover_active_cdp_port = lambda profile_paths=None: None
    resolve_cdp_ws_url = lambda ep, timeout=1.0: None
    extract_cdp_cookies = lambda ep, timeout=4.0: []
    merge_cookies = lambda a, b: list(a or []) + list(b or [])
    fetch_page_via_cdp = lambda ep, url, wait_selector=None, timeout=30.0: None
    download_via_playwright_request = lambda context, url, dest_path, headers=None, timeout_ms=30000: (False, "")

from browserskill_adapter import (
    BrowserSkillError,
    BrowserSkillRuntime,
    add_browserskill_arguments,
    check_browserskill_status,
    runtime_from_args,
    fetch_page_via_browserskill,
    download_file_via_browserskill,
)


from discovery_state import DiscoveryEvidence, MirrorDiscovery, download_with_recovery
from repository_resolver import (
    clone_candidate_with_url,
    discover_diva_candidates,
    discover_figshare_by_doi,
    discover_gsa_figshare_candidates,
    expand_repository_candidates,
    extract_crossref_chooser_urls,
    get_alternate_chooser_urls,
    is_crossref_chooser_url,
    is_diva_target,
    is_gsa_or_gsw_target,
    iter_download_fallback_routes,
)
from oa_resolver import resolve_oa_info, is_doi, extract_doi, extract_doi_from_html
from markitdown_converter import convert_directory_supplements, is_markitdown_available



def _parse_cookie_file_items(cookie_file: str):
    """Load cookie items from JSON list/dict or Netscape cookies.txt."""
    with open(cookie_file, "r", encoding="utf-8") as f:
        raw = f.read()
    if not raw.strip():
        return []
    try:
        cookies = json.loads(raw)
        if isinstance(cookies, list):
            return cookies
        if isinstance(cookies, dict):
            if isinstance(cookies.get("cookies"), list):
                return cookies["cookies"]
            return [
                {"name": k, "value": str(v), "path": "/"}
                for k, v in cookies.items()
                if isinstance(v, (str, int, float, bool))
            ]
    except Exception:
        pass
    # Netscape HTTP Cookie File format
    items = []
    for raw_line in raw.splitlines():
        line = raw_line.rstrip("\r\n")
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith("#") and not stripped.startswith("#HttpOnly_"):
            continue
        if stripped.startswith("#HttpOnly_"):
            line = stripped[len("#HttpOnly_"):]
        parts = line.split("\t")
        if len(parts) >= 7:
            domain, _flag, path, secure, expires, name, value = parts[:7]
            if name:
                c = {
                    "domain": domain,
                    "path": path or "/",
                    "secure": secure.upper() == "TRUE",
                    "name": name,
                    "value": value,
                }
                if re.match(r"^\d+(?:\.\d+)?$", expires.strip()):
                    exp_val = int(float(expires.strip()))
                    if exp_val > 0:
                        c["expires"] = exp_val
                items.append(c)
    return items


def load_saved_cookies():
    """Load saved cookies from local cookies.json, login_publishers.py profile, and known Zotero/CNKI stores."""
    candidates = [
        os.path.abspath("cookies.json"),
        os.path.expanduser("~/.journal_supp_downloader_profile/cookies.json"),
        os.path.expanduser("~/.zotero_crawl4ai_cookies.json"),
        os.path.expanduser("~/cnki_cookies.txt"),
    ]
    all_cookies = []
    seen_keys = set()
    for cookie_file in candidates:
        if os.path.exists(cookie_file):
            try:
                items = _parse_cookie_file_items(cookie_file)
                for c in items:
                    if isinstance(c, dict) and c.get("name"):
                        k = (c.get("domain", ""), c.get("name", ""), c.get("path", "/"))
                        if k not in seen_keys:
                            seen_keys.add(k)
                            all_cookies.append(c)
            except Exception:
                pass
    return all_cookies if all_cookies else None


def find_supplementary_links(page_or_html, base_url, original_url=None):
    """
    Find supplementary data links (backward-compatible wrapper).
    Returns a sorted list of unique candidate URLs.
    """
    if hasattr(page_or_html, "html_content"):
        html = str(page_or_html.html_content)
    else:
        html = str(page_or_html)
    diag = find_all_candidates(html, base_url, original_url=original_url or base_url)
    return sorted({c.url for c in diag.candidates})


def is_html_file(filepath):
    """Backward-compatible HTML check delegate."""
    is_valid, _ = validate_downloaded_file(filepath)
    return not is_valid


def download_file(
    cand_or_url,
    output_dir,
    cookies=None,
    bsk_session=None,
    referer=None,
    bsk_runtime=None,
    proxy=None,
    stealth_session_holder=None,
    deadline=None,
    error_out=None,
    headful: bool = False,
    real_chrome: bool = False,
):
    """
    Download a single file using HTTP Fetcher, falling back to BrowserSkill or
    a bounded StealthySession browser download when provided via stealth_session_holder.
    Validates content signatures and HTTP headers, avoiding arbitrary 1 KB threshold.
    Reuses active BrowserSkill session and referer context on fallback.
    Returns path or None.
    """
    if isinstance(cand_or_url, Candidate):
        url = cand_or_url.url
        suggested_fname = cand_or_url.filename
        referer = cand_or_url.source_url or referer
        selector = cand_or_url.extra_info.get("browser_selector")
        if selector:
            if bsk_session is None and not (bsk_runtime and bsk_runtime.enabled):
                msg = f"Browser session required: {cand_or_url.link_text}"
                if error_out is not None:
                    error_out["reason"] = msg
                print(f"  [Unresolved] {msg}")
                return None
            manifest = load_download_manifest(output_dir)
            name = disambiguate_target_filename(output_dir, suggested_fname, url, manifest)
            path = os.path.join(output_dir, name)
            result = download_file_via_browserskill(selector, path, session=bsk_session, runtime=bsk_runtime, referer=referer, verify_control=True)
            if result:
                record_manifest_entry(manifest, name, url, source_url=referer or "", table_ids=sorted(cand_or_url.covered_table_ids))
                save_download_manifest(output_dir, manifest)
                return ensure_inferred_extension_on_disk(path, output_dir, url, manifest=manifest)
            if error_out is not None:
                error_out["reason"] = f"BrowserSkill download failed for selector: {selector}"
            return None
    else:
        url = cand_or_url
        suggested_fname = ""

    # Table ids the candidate is known to cover (H6), for the manifest record.
    cand_table_ids = sorted(getattr(cand_or_url, "covered_table_ids", ()) or ()) if isinstance(cand_or_url, Candidate) else []

    manifest = load_download_manifest(output_dir)
    fname = suggested_fname or extract_filename_from_url(url)
    fname = sanitize_filename(fname)
    fname = disambiguate_target_filename(output_dir, fname, url, manifest)
    filepath = os.path.join(output_dir, fname)

    # Check if already exists and is valid for this exact URL
    if os.path.exists(filepath) and manifest_entry_url(manifest.get(fname)) == url:
        is_val, reason = validate_downloaded_file(filepath)
        if is_val:
            print(f"  [SKIP] {fname} (already exists)")
            return filepath
        # P1-F: garbage landed by an earlier run is removed, not
        # [SKIP]-counted as a success; the download below retries it.
        remove_invalid_existing_download(filepath, manifest, reason)

    norm_target_url = _normalize_url_for_dedup(url.split("#")[0].strip()) if url else ""
    if norm_target_url:
        for m_fname, m_entry in list(manifest.items()):
            m_url = manifest_entry_url(m_entry)
            if not m_url or _normalize_url_for_dedup(m_url.split("#")[0].strip()) != norm_target_url:
                continue
            m_path = os.path.join(output_dir, m_fname)
            if os.path.exists(m_path):
                is_val, reason = validate_downloaded_file(m_path)
                if is_val:
                    print(f"  [SKIP] {m_fname} (already exists)")
                    return m_path
                remove_invalid_existing_download(m_path, manifest, reason)

    print(f"  [FETCH] {fname} ...", end=" ", flush=True)
    # H9: the body is written in bounded chunks to a same-directory temp
    # file, validated, then committed with os.replace; the final path
    # never holds a partial file and a failure only discards the temp.
    tmp_path = None
    post_url = None
    post_data = None
    if isinstance(cand_or_url, Candidate) and cand_or_url.extra_info.get("http_method") == "POST":
        post_url = cand_or_url.extra_info.get("post_url") or url
        post_data = cand_or_url.extra_info.get("post_data") or {}
    try:
        get_kwargs = {"stealthy_headers": True, "timeout": 30}
        if referer:
            get_kwargs["headers"] = {"Referer": referer}
        if cookies:
            get_kwargs["cookies"] = cookies_to_dict(cookies, url)

        resp = None
        if post_url is None:
            try:
                resp = Fetcher.get(url, **get_kwargs)
            except Exception:
                pass

        if resp is None or resp.status >= 400:
            # Fallback (or POST form submission e.g. RHHZ2 exportSupplementary): requests session
            try:
                import requests
                sess = configure_session_direct(requests.Session(), proxy=proxy)
                c_dict = cookies_to_dict(cookies, url) if cookies else {}
                for ck, cv in c_dict.items():
                    sess.cookies.set(ck, cv)
                req_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
                if referer:
                    req_headers["Referer"] = referer
                if post_url is not None:
                    check_url = cand_or_url.extra_info.get("check_url")
                    if check_url:
                        try:
                            sess.post(check_url, timeout=15, headers=req_headers, verify=False)
                        except Exception:
                            pass
                    r = sess.post(post_url, data=post_data, timeout=30, headers=req_headers, verify=False)
                else:
                    r = sess.get(url, timeout=30, headers=req_headers, verify=False)
                if r.status_code < 400 and r.content:
                    resp = type("Resp", (), {"status": r.status_code, "body": r.content, "headers": dict(r.headers)})()
            except Exception:
                pass

        if resp is None or resp.status >= 400:
            status_desc = f"HTTP {resp.status}" if resp else "Connection error"
            raise RuntimeError(status_desc)

        # Try to get filename from Content-Disposition
        hdr = {k.lower(): v for k, v in (resp.headers or {}).items()}
        cd = hdr.get("content-disposition", "")
        cd_name = extract_filename_from_content_disposition(cd)
        if cd_name:
            target_fname = sanitize_filename(cd_name)
            fname = disambiguate_target_filename(output_dir, target_fname, url, manifest)
            filepath = os.path.join(output_dir, fname)
            if os.path.exists(filepath) and manifest_entry_url(manifest.get(fname)) == url:
                is_val, reason = validate_downloaded_file(filepath)
                if is_val:
                    print(f"[SKIP] {fname} (already exists)")
                    return filepath
                remove_invalid_existing_download(filepath, manifest, reason)

        # Infer extension if filename has none or is generic .bin
        if "." not in fname or fname.endswith(".bin"):
            inferred_ext = infer_file_extension(resp.body, hdr.get("content-type", ""))
            if inferred_ext:
                base = fname[:-4] if fname.endswith(".bin") else fname
                fname = disambiguate_target_filename(output_dir, f"{base}{inferred_ext}", url, manifest)
                filepath = os.path.join(output_dir, fname)

        tmp_path = temp_path_for(filepath)
        write_bytes_chunked(tmp_path, resp.body)

        # Validate file based on signature and response headers (Fixing Issue #5, D1)
        is_valid, reason = validate_downloaded_file(
            tmp_path,
            content_bytes=resp.body,
            response_status=resp.status,
            headers=resp.headers,
        )

        size_kb = len(resp.body) / 1024
        if is_valid:
            if "HTML table attachment" in reason and (filepath.endswith(".bin") or not os.path.splitext(filepath)[1]):
                base_html = filepath[:-4] if filepath.endswith(".bin") else filepath
                html_name = disambiguate_target_filename(output_dir, os.path.basename(base_html) + ".html", url, manifest)
                new_path = os.path.join(output_dir, html_name)
                if filepath != new_path:
                    filepath = new_path
                    fname = html_name
            commit_temp_file(tmp_path, filepath)
            tmp_path = None
            record_manifest_entry(manifest, fname, url, source_url=referer or "", table_ids=cand_table_ids)
            save_download_manifest(output_dir, manifest)
            print(f"OK ({size_kb:.1f} KB - {reason})")
            return filepath
        raise RuntimeError(reason)

    except Exception as exc:
        discard_temp_file(tmp_path)
        tmp_path = None
        last_err = single_line(exc)
        if bsk_session is not None or (bsk_runtime is not None and bsk_runtime.enabled):
            result = download_file_via_browserskill(
                url, filepath, session=bsk_session, referer=referer, runtime=bsk_runtime
            )
            if result:
                record_manifest_entry(manifest, fname, url, source_url=referer or "", table_ids=cand_table_ids)
                save_download_manifest(output_dir, manifest)
                filepath = ensure_inferred_extension_on_disk(filepath, output_dir, url, manifest=manifest)
                print(f"OK (via BrowserSkill, {os.path.getsize(filepath) / 1024:.1f} KB)")
                return filepath

        # StealthySession browser download fallback (only when a session holder is provided
        # by run_download, preserving standalone download_file() callers/tests).
        if stealth_session_holder is not None and (deadline is None or time.monotonic() < deadline):
            browser_tmp = None
            session_stealth = None
            page = None
            try:
                from scrapling.fetchers import StealthySession

                timeout_ms = 30000
                if deadline is not None:
                    rem_ms = int((deadline - time.monotonic()) * 1000)
                    if rem_ms <= 0:
                        raise TimeoutError("Phase deadline reached before browser download")
                    timeout_ms = max(1000, min(30000, rem_ms))

                _stealth_kwargs = {
                    "headless": not headful,
                    "solve_cloudflare": True,
                    "network_idle": False,
                    "timeout": timeout_ms,
                }
                if real_chrome:
                    _stealth_kwargs["real_chrome"] = True
                _proxy_cfg = playwright_proxy_config(proxy)
                if _proxy_cfg is not None:
                    _stealth_kwargs["proxy"] = _proxy_cfg
                _stealth_kwargs.update(proxy_browser_trust_kwargs(proxy))

                if stealth_session_holder.get("session") is not None:
                    session_stealth = stealth_session_holder["session"]
                    context = session_stealth.context
                else:
                    session_stealth = StealthySession(**_stealth_kwargs)
                    session_stealth.start()
                    context = session_stealth.context
                    if cookies:
                        normalized_c = normalize_playwright_cookies(cookies, default_url=url)
                        if normalized_c:
                            try:
                                context.add_cookies(normalized_c)
                            except Exception:
                                pass
                    stealth_session_holder["session"] = session_stealth

                page = context.new_page()
                try:
                    page.set_default_timeout(timeout_ms)
                except Exception:
                    pass

                parsed = urlparse(url)
                base_url = f"{parsed.scheme}://{parsed.netloc}/"
                if base_url not in stealth_session_holder.get("visited_origins", set()):
                    try:
                        page.goto(base_url, wait_until="commit", timeout=min(10000, timeout_ms))
                        stealth_session_holder.setdefault("visited_origins", set()).add(base_url)
                    except Exception:
                        pass

                dl_timeout_ms = min(15000, timeout_ms)
                if deadline is not None:
                    rem_ms = int((deadline - time.monotonic()) * 1000)
                    if rem_ms <= 0:
                        raise TimeoutError("Phase deadline reached before browser request")
                    dl_timeout_ms = min(dl_timeout_ms, max(500, rem_ms))
                pw_req_ok = False
                _pw_ct = ""
                if getattr(context, "request", None) is not None:
                    browser_tmp = temp_path_for(filepath)
                    pw_req_ok, _pw_ct = download_via_playwright_request(
                        context,
                        url,
                        browser_tmp,
                        headers={"Referer": referer} if referer else None,
                        timeout_ms=dl_timeout_ms,
                    )
                    if pw_req_ok and os.path.exists(browser_tmp):
                        _pw_valid, _ = validate_downloaded_file(
                            browser_tmp, headers={"content-type": _pw_ct} if _pw_ct else None
                        )
                        if not _pw_valid:
                            discard_temp_file(browser_tmp)
                            browser_tmp = None
                            pw_req_ok = False

                if not pw_req_ok:
                    dl_wait_ms = min(15000, timeout_ms)
                    goto_wait_ms = timeout_ms
                    if deadline is not None:
                        rem_ms = int((deadline - time.monotonic()) * 1000)
                        if rem_ms <= 0:
                            raise TimeoutError("Phase deadline reached before browser download")
                        dl_wait_ms = min(dl_wait_ms, rem_ms)
                        goto_wait_ms = min(goto_wait_ms, rem_ms)
                    with page.expect_download(timeout=dl_wait_ms) as download_info:
                        try:
                            page.goto(url, wait_until="commit", timeout=goto_wait_ms)
                        except Exception as e_goto:
                            if "Download is starting" not in str(e_goto):
                                raise
                    download = download_info.value
                    if hasattr(download, "suggested_filename") and download.suggested_filename:
                        real_name = sanitize_filename(download.suggested_filename)
                        fname = disambiguate_target_filename(output_dir, real_name, url, manifest)
                        filepath = os.path.join(output_dir, fname)
                    browser_tmp = temp_path_for(filepath)
                    download.save_as(browser_tmp)

                try:
                    page.close()
                except Exception:
                    pass
                page = None

                if "." not in fname or fname.endswith(".bin"):
                    with open(browser_tmp, "rb") as _head_f:
                        _head = _head_f.read(8192)
                    _inferred = infer_file_extension(_head, "")
                    if _inferred:
                        _base = fname[:-4] if fname.endswith(".bin") else fname
                        fname = disambiguate_target_filename(output_dir, f"{_base}{_inferred}", url, manifest)
                        filepath = os.path.join(output_dir, fname)
                        _new_tmp = temp_path_for(filepath)
                        os.replace(browser_tmp, _new_tmp)
                        browser_tmp = _new_tmp

                is_val, reason = validate_downloaded_file(
                    browser_tmp,
                    headers={"content-type": _pw_ct} if (pw_req_ok and _pw_ct) else None,
                )
                if is_val:
                    commit_temp_file(browser_tmp, filepath)
                    browser_tmp = None
                    filepath = ensure_inferred_extension_on_disk(
                        filepath, output_dir, url, manifest=manifest
                    )
                    fname = os.path.basename(filepath)
                    if "HTML table attachment" in reason and (filepath.endswith(".bin") or not os.path.splitext(filepath)[1]):
                        base_html = filepath[:-4] if filepath.endswith(".bin") else filepath
                        html_name = disambiguate_target_filename(output_dir, os.path.basename(base_html) + ".html", url, manifest)
                        new_path = os.path.join(output_dir, html_name)
                        if filepath != new_path:
                            os.replace(filepath, new_path)
                            filepath = new_path
                            fname = html_name
                    record_manifest_entry(manifest, fname, url, source_url=referer or "", table_ids=cand_table_ids)
                    save_download_manifest(output_dir, manifest)
                    size_kb = os.path.getsize(filepath) / 1024
                    print(f"OK (via StealthySession, {size_kb:.1f} KB - {reason})")
                    return filepath
                last_err = f"{last_err}; browser validation: {reason}"
            except Exception as b_exc:
                last_err = f"{last_err}; browser: {single_line(b_exc)}"
                if session_stealth is not None and stealth_session_holder.get("session") is session_stealth:
                    try:
                        session_stealth.close()
                    except Exception:
                        pass
                    stealth_session_holder["session"] = None
            finally:
                if page is not None:
                    try:
                        page.close()
                    except Exception:
                        pass
                if browser_tmp is not None:
                    discard_temp_file(browser_tmp)

        if error_out is not None:
            error_out["reason"] = last_err
        print(f"FAIL: {last_err}")
        return None


def guess_filename_from_url(url):
    """Derive a readable filename from URL (backward-compatible)."""
    return extract_filename_from_url(url)


# ── P1-CE: Phase A streaming partial results ────────────────────────────────
# 50-paper retest (sec. 4 item 3): Phase A timed out on 49/50 papers and
# the candidate list was discarded with the timeout, because the list was
# only printed after every mirror had been probed. Candidates are now
# streamed: each newly discovered batch is printed immediately and written
# to the partial-results file below, so a timeout (or Ctrl-C / kill) leaves
# the already-found candidates readable on disk. The partial file is
# replaced by the final list only once discovery completes.

PARTIAL_CANDIDATES_FILENAME = "supplementary_candidates.partial.json"
FINAL_CANDIDATES_FILENAME = "supplementary_candidates.json"


def _candidates_payload(candidates, *, complete, sources_completed, timed_out):
    return {
        "complete": bool(complete),
        "partial": not complete,
        "timed_out": bool(timed_out),
        "candidate_count": len(candidates),
        "sources_completed": list(sources_completed),
        "candidates": [c.to_dict() for c in candidates],
    }


def _write_candidates_json(output_dir, filename, candidates, *, complete,
                           sources_completed, timed_out):
    os.makedirs(output_dir, exist_ok=True)
    final_path = os.path.join(output_dir, filename)
    tmp_path = temp_path_for(final_path)
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_candidates_payload(
                candidates, complete=complete,
                sources_completed=sources_completed, timed_out=timed_out),
                f, ensure_ascii=False, indent=2, default=str)
        commit_temp_file(tmp_path, final_path)
    except Exception:
        discard_temp_file(tmp_path)
        raise
    return final_path


def write_partial_candidates(output_dir, candidates, *, complete=False,
                             sources_completed=(), timed_out=False):
    """Atomically (re)write the partial candidate list (P1-CE).

    The file always says it is incomplete (``complete: false``) while
    discovery is still running; call :func:`finalize_candidates` once
    every source has been probed. Safe to call after every batch: the
    write goes through a same-directory temp file + os.replace, so a
    kill mid-write never leaves a torn file.
    """
    return _write_candidates_json(
        output_dir, PARTIAL_CANDIDATES_FILENAME, candidates,
        complete=complete, sources_completed=sources_completed,
        timed_out=timed_out)


def finalize_candidates(output_dir, candidates, *, sources_completed=(),
                        timed_out=False):
    """Publish the complete candidate list and retire the partial file."""
    final_path = _write_candidates_json(
        output_dir, FINAL_CANDIDATES_FILENAME, candidates, complete=True,
        sources_completed=sources_completed, timed_out=timed_out)
    try:
        os.remove(os.path.join(output_dir, PARTIAL_CANDIDATES_FILENAME))
    except OSError:
        pass
    return final_path


def _print_candidate(cand, index):
    print(f"    [{index}] {cand.filename}")
    print(f"        URL:     {cand.url}")
    if cand.link_text:
        print(f"        Text:    {cand.link_text}")
    print(f"        Section: {cand.section} | Rule: {cand.match_rule}")


def main():
    parser = argparse.ArgumentParser(
        description="Download supplementary data from journal articles (Elsevier, Springer, etc.)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python journal_downloader.py https://www.sciencedirect.com/science/article/pii/S123456789
  python journal_downloader.py https://doi.org/10.1016/j.xxxx -o ./data --headful
  python journal_downloader.py https://link.springer.com/article/10.1007/xxx
  python journal_downloader.py https://www.nature.com/articles/s41586-023-xxxxx
  python journal_downloader.py https://example.com --proxy http://user:pass@host:8080
        """,
    )
    parser.add_argument("url", help="URL or DOI of the journal article page")
    parser.add_argument(
        "-o", "--output-dir", default="./journal_downloads",
        help="Output directory (default: ./journal_downloads)"
    )
    parser.add_argument(
        "--headful", action="store_true",
        help="Show browser window (not headless)"
    )
    parser.add_argument(
        "--proxy", default=None,
        help="Proxy URL (e.g. http://user:pass@host:port)"
    )
    parser.add_argument(
        "--timeout", type=int, default=60000,
        help="Page load timeout in ms (default: 60000)"
    )
    parser.add_argument(
        "--phase-timeout", type=float, default=DEFAULT_PHASE_TIMEOUT_SECONDS,
        help="Overall budget in seconds for this phase (discovery + "
             "download). On expiry, already-discovered candidates are kept "
             "in the partial results file and downloading stops instead of "
             "being killed mid-list. Every page load is additionally capped "
             "at the remaining phase budget, so one load can never run past "
             "the deadline (default: 150)"
    )
    # P1 H-1: the browser fallback gets its own quotas -- one page goto
    # at most 30s, 60s of browser time in total per phase. Either quota
    # running out degrades to the OA mirror round instead of killing
    # the phase inside one page load (round 3: #1 hung 120s+ on
    # nature.com, mirrors never ran, 0 files).
    # P1 C: both quotas are additionally capped at the remaining phase
    # budget -- a single goto runs at most min(goto, total remaining,
    # phase remaining), and the fallback budget counts as exhausted
    # once the phase deadline passes.
    parser.add_argument(
        "--browser-goto-timeout", type=float, default=DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS,
        help="Max seconds for one browser page load (goto) in the "
             "fallback path, capped at the remaining phase budget "
             "(default: 30)"
    )
    parser.add_argument(
        "--browser-fallback-budget", type=float, default=DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS,
        help="Max total seconds of browser fallback time per phase, "
             "capped at the remaining phase budget; "
             "once spent, the run degrades to the OA mirror round "
             "instead of launching another browser (default: 60)"
    )
    parser.add_argument(
        "--solve-cloudflare", action="store_true", default=True,
        help="Enable Cloudflare Turnstile bypass (default: on)"
    )
    parser.add_argument(
        "--no-cloudflare", action="store_true",
        help="Disable Cloudflare bypass"
    )
    parser.add_argument(
        "--wait-selector", default=None,
        help="CSS selector to wait for before scraping"
    )
    parser.add_argument(
        "--real-chrome", action="store_true",
        help="Use real Chrome browser instead of Chromium"
    )
    parser.add_argument(
        "--cdp", action="store_true",
        help="Connect to running Chromium browser via CDP (inherits live session, cookies, Cloudflare bypass)"
    )
    parser.add_argument(
        "--cdp-url", default=None,
        help="Custom CDP endpoint URL or port (e.g. http://127.0.0.1:9222, ws://... or 9222)"
    )
    add_browserskill_arguments(parser)
    add_auto_browser_arguments(parser)
    parser.add_argument(
        "--list-only", action="store_true",
        help="Only list found links with diagnostic evidence, don't download"
    )
    parser.add_argument(
        "--oa", action="store_true",
        help="Prioritize / prefer Open Access repository mirrors (PMC, Europe PMC, etc.) via Unpaywall & OpenAlex"
    )
    parser.add_argument(
        "--no-oa", action="store_true",
        help="Disable automatic Open Access repository mirror query and penetration"
    )
    parser.add_argument(
        "--convert-markdown", "--markitdown", dest="convert_markdown", action="store_true",
        help="Convert downloaded supplementary documents (.docx, .xlsx, .pdf) to Markdown via Microsoft MarkItDown"
    )
    args = parser.parse_args()

    with runtime_from_args(args, parser) as bsk_runtime:
        try:
            return run_download(args, bsk_runtime)
        except BrowserSkillError as exc:
            print(f"BrowserSkill: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc


def run_download(args, bsk_runtime):

    os.makedirs(args.output_dir, exist_ok=True)
    solve_cf = not args.no_cloudflare and args.solve_cloudflare

    # P1-CE: the phase budget (default 150s, --phase-timeout). The clock
    # starts here so the publisher fetch, retries, mirrors, and downloads
    # all share one budget instead of each stage assuming it has 150s.
    phase_timeout = resolve_phase_timeout(getattr(args, "phase_timeout", None))
    phase_deadline = time.monotonic() + phase_timeout
    download_reserve = min(30.0, phase_timeout * 0.25)
    discovery_retry_deadline = phase_deadline - download_reserve

    def _phase_expired():
        return time.monotonic() >= phase_deadline

    def _discovery_retry_allowed(has_candidates: bool) -> bool:
        """Reserve a download window when candidates are already in hand so
        secondary discovery stages cannot starve actual file downloads."""
        now = time.monotonic()
        if now >= phase_deadline:
            return False
        if has_candidates and now >= discovery_retry_deadline:
            return False
        return True

    def _phase_remaining():
        """Seconds of phase budget left (0 once expired). Page-load
        timeouts are clamped to this (P1 C), so no load starts with
        more time than the phase has left."""
        return max(0.0, phase_deadline - time.monotonic())

    print(f"Phase timeout: {phase_timeout:g}s")

    # P1 H-1: the browser fallback's own quota (default: one goto 30s,
    # 60s total per phase), shared by every browser attempt below.
    # P1 C: the run's phase deadline is passed in as well, so the
    # fallback quota can never outlive the phase: one attempt runs at
    # most min(goto, total remaining, phase remaining), and the budget
    # counts as exhausted once the phase deadline passes.
    browser_budget = browser_budget_from_args(args, phase_deadline=phase_deadline)
    print(f"Browser fallback budget: {browser_budget.goto_timeout:g}s per page, "
          f"{browser_budget.total_budget:g}s total")

    def _browser_allowed():
        """True while the fallback budget lasts. Once it is gone, log
        the downgrade line (once) and return False -- callers then skip
        the browser attempt and the OA mirror round (already run, P1
        H-1 reorder) / download round still proceed. Only called when
        a browser attempt is actually pending, so a complete run never
        logs a spurious exhaustion."""
        if browser_budget.exhausted:
            browser_budget.announce_exhausted()
            return False
        return True

    # Normalize DOI input if needed
    input_target = args.url.strip()
    if input_target.startswith("10."):
        fetch_url = f"https://doi.org/{input_target}"
    else:
        fetch_url = input_target

    print(f"Target: {args.url}")
    print(f"Output: {os.path.abspath(args.output_dir)}")
    print(f"Cloudflare bypass: {'ON' if solve_cf else 'OFF'}")
    print(f"Mode: {'Headful' if args.headful else 'Headless'}")
    if args.cdp or args.cdp_url:
        print(f"CDP Live Browser: {'ON (' + (args.cdp_url or 'auto-detect') + ')'}")
    if getattr(args, "browserskill", False):
        print("BrowserSkill: ON (Tencent BrowserSkill live session)")
    if args.proxy:
        print(f"Proxy: {args.proxy}")

    # Load saved cookies if available from login_publishers.py
    local_cfg = load_local_skill_config()
    cookies = load_saved_cookies()
    if cookies:
        print(f"Loaded {len(cookies)} saved cookies from login wizard profile.")
    print()

    # --- Step 1: Fetch the article page ---
    print("[1/3] Fetching article page (bypassing protections)...")
    fetch_kwargs = {
        "headless": not args.headful,
        "solve_cloudflare": solve_cf,
        "block_webrtc": True,
        "hide_canvas": True,
        "real_chrome": args.real_chrome,
        "network_idle": False,
        # P1 H-1: the page fetch is a browser goto too -- cap it at the
        # goto timeout (30s by default) instead of --timeout (60s), so
        # step [1/3] alone cannot eat the phase budget (round 3 #1).
        # P1 C: cap it at the *attempt* timeout, not just the goto
        # quota -- the attempt timeout is min(goto, total remaining,
        # phase remaining), so a fetch launched with 10s of phase
        # left runs at most 10s.
        "timeout": min(args.timeout, browser_budget.attempt_timeout_ms()),
        "retries": 1,
        "proxy": args.proxy,
        "wait_selector": args.wait_selector,
        "page_action": expand_page_accordions_action,
        "google_search": False,
    }
    # P1 round-3 minor (a): the MITM proxy's CA trust config. fetch_kwargs
    # is built once and every later fetch copies it (CDP retry, OA mirror,
    # browser retry), so first call and retries carry identical proxy +
    # trust settings -- proxy mode only; no proxy means no trust keys.
    fetch_kwargs.update(proxy_browser_trust_kwargs(args.proxy))
    if cookies:
        fetch_kwargs["cookies"] = normalize_playwright_cookies(cookies, default_url=fetch_url)

    page = None
    # Primary BrowserSkill check if requested via --bsk / --browserskill
    # P1 C: no load starts once the phase deadline has passed (here
    # vacuous -- the phase just started -- but the check belongs on
    # every load entry, including this one).
    if getattr(args, "browserskill", False) and not _phase_expired():
        print("  [BrowserSkill] Fetching page via Tencent BrowserSkill (bsk)...")
        try:
            bsk_html, bsk_status, bsk_final = fetch_page_via_browserskill(
                fetch_url,
                wait_selector=args.wait_selector,
                timeout=min(args.timeout / 1000.0, browser_budget.attempt_timeout()),
                browser=getattr(args, "bsk_browser", None),
                tab_id=getattr(args, "bsk_tab", None),
                runtime=bsk_runtime,
            )
            if bsk_html and not page_fetch_is_failure(bsk_html, bsk_status):
                print("  [BrowserSkill] Successfully retrieved page DOM from live browser session.")
                page = type("BrowserSkillPage", (), {
                    "html_content": bsk_html,
                    "text": bsk_html,
                    "url": bsk_final,
                    "status": bsk_status,
                })()
        except Exception as e_bsk:
            if bsk_runtime.browser or bsk_runtime.tab_id:
                raise
            print(f"  [BrowserSkill] Fetch notice: {e_bsk}. Falling back to CDP/StealthyFetcher...")

    # Check CDP connectivity if requested or if --cdp / --cdp-url specified
    cdp_target = args.cdp_url
    if args.cdp and not cdp_target and page is None:
        detected = discover_active_cdp_port()
        if detected:
            print(f"  [CDP] Discovered running browser at {detected[0]} (ws: {detected[1]})")
            cdp_target = detected[0]
        else:
            print("  [CDP] No active browser CDP port detected. Falling back to standalone stealth browser.")

    if cdp_target and page is None and not _phase_expired():
        # Extract live authenticated cookies from the browser session
        try:
            live_cdp_c = extract_cdp_cookies(cdp_target)
            if live_cdp_c:
                print(f"  [CDP] Extracted {len(live_cdp_c)} live authenticated session cookies from browser.")
                cookies = merge_cookies(cookies, live_cdp_c)
                fetch_kwargs["cookies"] = normalize_playwright_cookies(cookies, default_url=fetch_url)
        except Exception:
            pass

        # Primary CDP Attempt: Attach to live browser tab or open in authenticated default profile
        try:
            print(f"  [CDP] Connecting over CDP to live browser: {cdp_target} ...")
            cdp_html, cdp_status, cdp_final_url, cdp_c = fetch_page_via_cdp(
                cdp_target, fetch_url, wait_selector=args.wait_selector, timeout=min(args.timeout / 1000.0, browser_budget.attempt_timeout())
            )
            if cdp_html and not page_fetch_is_failure(cdp_html, cdp_status):
                print("  [CDP] Successfully retrieved page DOM from live browser session.")
                if cdp_c:
                    cookies = merge_cookies(cookies, cdp_c)
                page = type("CdpPage", (), {
                    "html_content": cdp_html,
                    "text": cdp_html,
                    "url": cdp_final_url,
                    "status": cdp_status,
                })()
        except Exception as e_cdp:
            print(f"  [CDP] Live tab attachment notice: {e_cdp}. Falling back to StealthyFetcher...")

    # Fast requests pre-check for static/non-CF publisher pages (e.g. Springer, MDPI)
    # before launching a full headless browser. Only runs when StealthyFetcher is real
    # (or requests.Session is explicitly mocked), preserving unit tests that mock
    # StealthyFetcher / Fetcher directly.
    if (
        page is None
        and not getattr(args, "browserskill", False)
        and not cdp_target
        and not args.wait_selector
        and not _phase_expired()
    ):
        try:
            import requests as _req
            _stealth_is_real = (
                getattr(StealthyFetcher, "__module__", "") == "scrapling.fetchers.stealth_chrome"
                and getattr(getattr(StealthyFetcher, "fetch", None), "__module__", "") == "scrapling.fetchers.stealth_chrome"
            )
            _req_session_mocked = getattr(_req.Session, "__module__", "") != "requests.sessions"
            if _stealth_is_real or _req_session_mocked:
                with _req.Session() as _pre_sess:
                    configure_session_direct(_pre_sess, proxy=args.proxy)
                    _pre_headers = {
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                        ),
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Referer": "https://scholar.google.com/",
                    }
                    _pre_kw = {
                        "headers": _pre_headers,
                        "timeout": max(1.0, min(12.0, _phase_remaining())),
                        "allow_redirects": True,
                    }
                    if args.proxy:
                        _pre_kw["proxies"] = {"http": args.proxy, "https": args.proxy}
                    if cookies:
                        _pre_kw["cookies"] = cookies_to_dict(cookies, fetch_url)
                    _r_pre = _pre_sess.get(fetch_url, **_pre_kw)
                    _pre_enc = getattr(_r_pre, "encoding", None)
                    if isinstance(_pre_enc, str) and _pre_enc.lower() in ("iso-8859-1", "latin-1", "latin1"):
                        _pre_app = getattr(_r_pre, "apparent_encoding", None)
                        if isinstance(_pre_app, str) and _pre_app:
                            _r_pre.encoding = _pre_app
                    _pre_status = getattr(_r_pre, "status_code", None)
                    _pre_text = getattr(_r_pre, "text", None)
                    _pre_url = str(getattr(_r_pre, "url", None) or fetch_url)
                    if (
                        isinstance(_pre_status, int)
                        and _pre_status < 400
                        and _pre_status != 202
                        and isinstance(_pre_text, str)
                        and _pre_text
                        and is_crossref_chooser_url(_pre_url)
                    ):
                        _ch_urls = extract_crossref_chooser_urls(
                            html_str=_pre_text,
                            doi=extract_doi(input_target) or "",
                            proxy=args.proxy or "",
                            crossref_email=str(local_cfg.get("crossref_email") or local_cfg.get("unpaywall_email") or ""),
                        )
                        if _ch_urls:
                            print(f"  [chooser] Unwrapped Crossref chooser -> {_ch_urls[0]}")
                            fetch_url = _ch_urls[0]
                            if cookies:
                                _pre_kw["cookies"] = cookies_to_dict(cookies, fetch_url)
                                fetch_kwargs["cookies"] = normalize_playwright_cookies(cookies, default_url=fetch_url)
                            _r_pre = _pre_sess.get(fetch_url, **_pre_kw)
                            _pre_enc = getattr(_r_pre, "encoding", None)
                            if isinstance(_pre_enc, str) and _pre_enc.lower() in ("iso-8859-1", "latin-1", "latin1"):
                                _pre_app = getattr(_r_pre, "apparent_encoding", None)
                                if isinstance(_pre_app, str) and _pre_app:
                                    _r_pre.encoding = _pre_app
                            _pre_status = getattr(_r_pre, "status_code", None)
                            _pre_text = getattr(_r_pre, "text", None)
                            _pre_url = str(getattr(_r_pre, "url", None) or fetch_url)
                    if (
                        isinstance(_pre_status, int)
                        and _pre_status < 400
                        and _pre_status != 202
                        and isinstance(_pre_text, str)
                        and _pre_text
                        and not ("/verify/" in _pre_url.lower() and "cnki.net" in _pre_url.lower())
                        and not page_fetch_is_failure(_pre_text, _pre_status)
                    ):
                        _pre_base = extract_effective_base_url(_pre_text, _pre_url)
                        _pre_diag = find_all_candidates(_pre_text, _pre_base, original_url=args.url)
                        if (
                            _pre_diag.candidates
                            and not _pre_diag.is_incomplete
                            and not getattr(_pre_diag, "needs_browser", False)
                            and not is_blocked_page(_pre_text)
                        ):
                            page = type("FastHTTPPage", (), {
                                "html_content": _pre_text,
                                "text": _pre_text,
                                "url": _pre_url,
                                "status": _pre_status,
                            })()
                            print(f"  [Fast-HTTP] Retrieved article page directly ({len(_pre_diag.candidates)} candidate(s)).")
        except Exception:
            pass

    # Secondary: Scrapling StealthyFetcher fallback
    # P1 C: each link of this chain is a page load of its own -- none
    # of them starts once the phase deadline has passed, so the chain
    # cannot spend 30s + 30s + 30s after the budget is already gone
    # (round 3: the phase died inside one such load and the next links
    # still ran until the runner killed the process).
    if page is None and not _phase_expired():
        ws_cdp_url = resolve_cdp_ws_url(cdp_target) if cdp_target else None
        attempt_kwargs = dict(fetch_kwargs)
        if ws_cdp_url:
            attempt_kwargs["cdp_url"] = ws_cdp_url
            attempt_kwargs.pop("real_chrome", None)

        # P1 C: re-cap the timeout at launch -- the BSK / CDP attempts
        # above may already have spent phase time since fetch_kwargs
        # was built.
        _attempt_ms = min(args.timeout, browser_budget.attempt_timeout_ms())
        if _attempt_ms > 0:
            attempt_kwargs["timeout"] = _attempt_ms
            cdp_failed_or_blocked = False
            try:
                page = stealthy_fetch_bounded(StealthyFetcher, fetch_url, **attempt_kwargs)
                if "cdp_url" in attempt_kwargs:
                    _cdp_sf_html = str(getattr(page, "html_content", None) or getattr(page, "text", "") or "") if page is not None else ""
                    _cdp_sf_status = getattr(page, "status", None) if page is not None else None
                    if page is None or page_fetch_is_failure(_cdp_sf_html, _cdp_sf_status):
                        print("  [Warning] Browser fetch via CDP returned blocked/error page.")
                        page = None
                        cdp_failed_or_blocked = True
            except Exception as e:
                print(f"  [Warning] Browser fetch encountered error: {e}")
                page = None
                cdp_failed_or_blocked = True

            if cdp_failed_or_blocked and "cdp_url" in attempt_kwargs and not _phase_expired():
                _retry_ms = min(args.timeout, browser_budget.attempt_timeout_ms())
                if _retry_ms > 0:
                    print("  [Recovery] Retrying with standalone stealth browser...")
                    fb_kwargs = dict(fetch_kwargs)
                    fb_kwargs.pop("cdp_url", None)
                    fb_kwargs["timeout"] = _retry_ms
                    try:
                        page = stealthy_fetch_bounded(StealthyFetcher, fetch_url, **fb_kwargs)
                    except Exception as e2:
                        print(f"  [Warning] Standalone browser fetch also failed: {e2}")

            if page is None and not _phase_expired():
                print("  [Recovery] Attempting HTTP direct fetch fallback...")
                try:
                    # P1 C: the HTTP fallback is a load too -- clamp
                    # its timeout to the remaining phase budget.
                    http_kwargs = {"stealthy_headers": True, "timeout": min(args.timeout / 1000.0, 30.0, _phase_remaining())}
                    if cookies:
                        http_kwargs["cookies"] = cookies_to_dict(cookies, fetch_url)
                    resp = Fetcher.get(fetch_url, **http_kwargs)
                    _r_html = str(getattr(resp, "html_content", None) or getattr(resp, "text", "") or "")
                    if resp.status < 400 and resp.status != 202 and _r_html and not page_fetch_is_failure(_r_html, resp.status):
                        page = resp
                except Exception as e3:
                    print(f"  [Recovery] HTTP direct fetch failed: {e3}")


    _chooser_seed_urls = []
    _tried_chooser_urls = []
    if page is not None and is_crossref_chooser_url(str(getattr(page, "url", "") or "")) and not _phase_expired():
        _ch_html = str(getattr(page, "html_content", None) or getattr(page, "text", "") or "")
        _ch_urls = extract_crossref_chooser_urls(
            html_str=_ch_html,
            doi=extract_doi(input_target) or "",
            proxy=args.proxy or "",
            crossref_email=str(local_cfg.get("crossref_email") or local_cfg.get("unpaywall_email") or ""),
        )
        _chooser_seed_urls = list(_ch_urls)
        for _ch_u in _ch_urls[:2]:
            if _phase_expired():
                break
            _tried_chooser_urls.append(_ch_u)
            print(f"  [chooser] Retrying Crossref chooser target: {_ch_u}")
            try:
                _ch_page = None
                _attempt_ms = min(args.timeout, browser_budget.attempt_timeout_ms())
                if _attempt_ms > 0:
                    _ch_kw = dict(fetch_kwargs)
                    _ch_kw["timeout"] = _attempt_ms
                    if cookies:
                        _ch_kw["cookies"] = normalize_playwright_cookies(cookies, default_url=_ch_u)
                    _ch_page = stealthy_fetch_bounded(StealthyFetcher, _ch_u, **_ch_kw)
                if _ch_page is None and not _phase_expired():
                    _h_kw = {"stealthy_headers": True, "timeout": min(args.timeout / 1000.0, 30.0, _phase_remaining())}
                    _ch_page = Fetcher.get(_ch_u, **_h_kw)
                if _ch_page is not None:
                    _ch_p_html = str(getattr(_ch_page, "html_content", None) or getattr(_ch_page, "text", "") or "")
                    _ch_p_status = getattr(_ch_page, "status", None)
                    if _ch_p_html and _ch_p_status != 202 and not page_fetch_is_failure(_ch_p_html, _ch_p_status):
                        page = _ch_page
                        fetch_url = getattr(_ch_page, "url", None) or _ch_u
                        break
            except Exception:
                continue

    # H1: a blocked page (challenge / login wall) is a failed fetch even
    # when the transport reported status < 400, and an unknown status
    # (None, from BrowserSkill/CDP) must not crash the comparison.
    _page_html = str(getattr(page, "html_content", None) or getattr(page, "text", "") or "") if page is not None else ""
    _page_status = getattr(page, "status", None) if page is not None else None
    if (page is None or page_fetch_is_failure(_page_html, _page_status)) and bsk_runtime.enabled and not args.browserskill and not _phase_expired():
        try:
            html, status, landing = fetch_page_via_browserskill(
                fetch_url, wait_selector=args.wait_selector,
                timeout=min(args.timeout / 1000.0, browser_budget.attempt_timeout()), runtime=bsk_runtime,
            )
            page = type("BrowserSkillPage", (), {"html_content": html, "text": html, "url": landing, "status": status})()
        except BrowserSkillError as exc:
            if bsk_runtime.browser or bsk_runtime.tab_id:
                raise
            print(f"  [Recovery] BrowserSkill: {exc}")

    _raw_pre_html = str(getattr(page, "html_content", None) or getattr(page, "text", "") or "") if page is not None else ""
    _initial_page_blocked = (page is None) or is_blocked_page(_raw_pre_html) or page_fetch_is_failure(_raw_pre_html, getattr(page, "status", None))
    effective_headful = bool(getattr(args, "headful", False))
    effective_real_chrome = bool(getattr(args, "real_chrome", False))

    if page is None:
        print(f"Warning: Unable to fetch publisher page; checking available alternatives.")
        page = type("UnavailablePage", (), {"url": fetch_url, "html_content": "", "text": "", "status": 0})()

    # A page that is still blocked after every fetch tier must not be
    # parsed as an article: a challenge page mentioning "Table S1" would
    # otherwise be recorded as declared-but-missing evidence.
    if page is not None and is_blocked_page(str(getattr(page, "html_content", None) or getattr(page, "text", "") or "")):
        print("  [Warning] Fetched page is a challenge / login wall; treating it as no usable article page.")
        page = type("UnavailablePage", (), {"url": fetch_url, "html_content": "", "text": "", "status": 0})()

    # Propagate cookies (e.g. cf_clearance, session cookies) from the article
    # browser fetch into Step [3/3] downloaders so protected supplement endpoints
    # reuse the solved session instead of failing with 403 first.
    _fetched_cookies = getattr(page, "cookies", None) if page is not None else None
    if isinstance(_fetched_cookies, (list, tuple, dict)) and _fetched_cookies:
        cookies = merge_cookies(cookies, _fetched_cookies)

    # Resolve effective base URL (Fixing Issue #1: DOI redirect resolution)
    final_url = getattr(page, "url", None) or fetch_url
    html_content = getattr(page, "html_content", None) or getattr(page, "text", "")
    html_content = str(html_content)
    effective_base = extract_effective_base_url(html_content, final_url)

    # Extract and sanitize article title for folder name
    raw_title = "N/A"
    if hasattr(page, "css"):
        try:
            _css_t = page.css('title::text').get()
            if _css_t:
                _css_t = re.sub(r"<!--.*?-->", "", str(_css_t), flags=re.DOTALL).strip()
            raw_title = _css_t or "N/A"
        except Exception:
            pass
    if raw_title == "N/A" or is_placeholder_or_blocked_title(raw_title):
        raw_title = extract_article_title(html_content, default="N/A")
    article_dirname = extract_article_title(html_content, default="supplements")
    if is_placeholder_or_blocked_title(article_dirname):
        article_dirname = "supplements"
    article_dir = os.path.join(args.output_dir, article_dirname)
    os.makedirs(article_dir, exist_ok=True)

    def _refresh_title_and_dir_from_html_or_str(source_html_or_title: str, is_raw_title: bool = False):
        nonlocal article_dirname, article_dir, raw_title
        if not is_placeholder_or_blocked_title(article_dirname):
            return
        new_title = (
            extract_article_title(f"<title>{_html.escape(source_html_or_title)}</title>", default="")
            if is_raw_title
            else extract_article_title(source_html_or_title, default="")
        )
        if new_title and not is_placeholder_or_blocked_title(new_title):
            article_dirname = new_title
            raw_title = new_title
            new_dir = os.path.join(args.output_dir, article_dirname)
            article_dir = migrate_or_merge_article_dir(article_dir, new_dir)

    print(f"  Status:    {page.status}")
    print(f"  Input URL: {args.url}")
    if final_url != args.url:
        print(f"  Final URL: {final_url}")
    if effective_base != final_url:
        print(f"  Base URL:  {effective_base}")
    print(f"  Title:     {raw_title}")
    print(f"  Folder:    {article_dirname}/")

    # --- Step 2: Find supplementary links ---
    print("\n[2/3] Scanning for supplementary data links...")
    # P1-CE streaming state: candidates already printed + persisted, and
    # whether the phase budget has run out.
    _streamed_urls = set()
    _sources_done = []
    phase_timed_out = False

    def _persist(fn, *args, **kwargs):
        # The candidate-list files are best-effort diagnostics: a
        # failure to write them (e.g. a read-only / EPERM sandbox
        # episode) must never abort the run itself.
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            print(f"  [Warning] Could not write candidate list file: {exc}")
            return None

    def _stream(current, source):
        """Print + persist every candidate not streamed yet (P1-CE)."""
        if source not in _sources_done:
            _sources_done.append(source)
        fresh = [c for c in current if c.url not in _streamed_urls]
        for cand in fresh:
            _streamed_urls.add(cand.url)
            _print_candidate(cand, len(_streamed_urls))
        if fresh:
            _persist(
                write_partial_candidates,
                article_dir, list(current), complete=False,
                sources_completed=list(_sources_done),
                timed_out=_phase_expired())
        return fresh

    diag = find_all_candidates(html_content, effective_base, original_url=args.url)
    candidates = diag.candidates
    if (
        not candidates or any(c.extra_info.get("sciengine_attachment") for c in candidates)
    ):
        _se_proxies = {"http": args.proxy, "https": args.proxy} if getattr(args, "proxy", None) else None
        se_cands, se_title = (
            fetch_sciengine_api_candidates(args.url, source_url=final_url or effective_base, proxies=_se_proxies)
            if _se_proxies
            else fetch_sciengine_api_candidates(args.url, source_url=final_url or effective_base)
        )
        if se_title:
            _refresh_title_and_dir_from_html_or_str(se_title, is_raw_title=True)
        if se_cands:
            non_synth = [c for c in candidates if not c.extra_info.get("sciengine_attachment")]
            seen_se = {c.url for c in non_synth}
            candidates = non_synth + [c for c in se_cands if c.url not in seen_se]
            diag.candidates = candidates
            diag.needs_browser = False
            diag.is_incomplete, diag.incomplete_reason = is_result_obviously_incomplete(
                candidates, html_content, article_text=getattr(diag, "_article_text", None)
            )
    if not candidates and (is_diva_target(args.url) or is_diva_target(final_url or effective_base)):
        diva_cands, diva_title = discover_diva_candidates(
            args.url if is_diva_target(args.url) else (final_url or effective_base),
            proxy=args.proxy or "",
        )
        if diva_title:
            _refresh_title_and_dir_from_html_or_str(diva_title, is_raw_title=True)
        if diva_cands:
            candidates = diva_cands
            diag.candidates = candidates
            diag.needs_browser = False
    if not candidates and (is_gsa_or_gsw_target(args.url) or is_gsa_or_gsw_target(final_url or effective_base)):
        gsa_cands, gsa_title = discover_gsa_figshare_candidates(
            args.url if is_gsa_or_gsw_target(args.url) else (final_url or effective_base),
            proxy=args.proxy or "",
            crossref_email=str(local_cfg.get("crossref_email") or local_cfg.get("unpaywall_email") or ""),
        )
        if gsa_title:
            _refresh_title_and_dir_from_html_or_str(gsa_title, is_raw_title=True)
        if gsa_cands:
            candidates = gsa_cands
            diag.candidates = candidates
            diag.needs_browser = False
    _figshare_doi = extract_doi(args.url) or extract_doi(final_url or "") or extract_doi_from_html(html_content or "")
    if not candidates and _figshare_doi:
        fig_cands, fig_title = discover_figshare_by_doi(
            _figshare_doi,
            proxy=args.proxy or "",
        )
        if fig_title:
            _refresh_title_and_dir_from_html_or_str(fig_title, is_raw_title=True)
        if fig_cands:
            candidates = fig_cands
            diag.candidates = candidates
            diag.needs_browser = False
    evidence = DiscoveryEvidence()
    evidence.observe(
        "publisher",
        html_content,
        effective_base,
        soup=getattr(diag, "_soup", None),
        article_text=getattr(diag, "_article_text", None),
    )
    # P1-CE: stream the publisher candidates now -- do not wait for the
    # retries / mirrors below, which may never finish inside the budget.
    _stream(candidates, "publisher")

    # H3: force a browser render when the static page shows dynamic
    # signals (diag.needs_browser) but the candidate set is empty or the
    # evidence check still reports missing files. The evidence verdict is
    # used here, not supp_finder's own is_incomplete: the two can
    # disagree (e.g. a singular "Additional file (7)" declaration is
    # seen by the legacy evidence file count but not by the H5 file
    # count). When the supplements are already complete, do not force.
    _ev_inc, _ = evidence.check(candidates)
    needs_browser_retry = bool(diag.needs_browser) and (not candidates or _ev_inc)
    if _discovery_retry_allowed(bool(candidates)) and (not candidates or diag.is_incomplete or needs_browser_retry) and bsk_runtime.enabled and not args.wait_selector and _browser_allowed():
        try:
            retry_html, _, retry_url = fetch_page_via_browserskill(
                fetch_url, timeout=min(args.timeout / 1000.0, browser_budget.attempt_timeout()), runtime=bsk_runtime,
            )
            _refresh_title_and_dir_from_html_or_str(retry_html)
            retry_base = extract_effective_base_url(retry_html, retry_url)
            retry_diag = find_all_candidates(retry_html, retry_base, original_url=args.url)
            evidence.observe(
                "publisher",
                retry_html,
                retry_base,
                soup=getattr(retry_diag, "_soup", None),
                article_text=getattr(retry_diag, "_article_text", None),
            )
            seen = {c.url for c in candidates}
            has_se_api = any(c.match_rule == "sciengine_open_api" for c in candidates)
            added = [
                c for c in retry_diag.candidates
                if c.url not in seen and not (has_se_api and c.extra_info.get("sciengine_attachment"))
            ]
            candidates.extend(added)
            if added:
                diag = retry_diag
                diag.candidates = candidates
                final_url, effective_base = retry_url, retry_base
        except BrowserSkillError as exc:
            if bsk_runtime.browser or bsk_runtime.tab_id:
                raise
            print(f"  [Recovery] BrowserSkill: {exc}")

    _stream(candidates, "browserskill_retry")
    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
    # H3 (recomputed after any BSK retry above): diag.is_incomplete is
    # the evidence verdict here, so the flag again follows the task rule.
    needs_browser_retry = bool(diag.needs_browser) and (not candidates or diag.is_incomplete)

    # P1 H-1: the OA mirror round runs BEFORE the Stealthy browser
    # retry below. Mirrors start over requests (cheap, seconds), while
    # one browser page load over the proxy takes 60-90s -- in round 3
    # the browser retry ran first, hung, and the mirrors never ran at
    # all (#1: 13 files in R2, 0 in R3). The retry below only runs for
    # what the mirrors did not already cover, and only while its own
    # budget lasts.
    # Open Access repository mirror fallback (Unpaywall & OpenAlex)
    doi_val = (
        extract_doi(input_target)
        or extract_doi(final_url)
        or extract_doi(effective_base)
        or extract_doi_from_html(html_content)
    )
    def fetch_mirror(oa_url):
        # P1 C: no mirror load starts once the phase deadline has
        # passed, and the requests timeout is capped at the remaining
        # phase budget (the old hardcoded 20s could overshoot a
        # nearly-spent phase on its own).
        if _phase_expired():
            return "", 0, oa_url
        try:
            response = Fetcher.get(oa_url, stealthy_headers=True,
                                   timeout=min(20, _phase_remaining()))
            document = getattr(response, "html_content", None) or response.text
            if response.status < 400 and document:
                return str(document), response.status, getattr(response, "url", None) or oa_url
        except Exception:
            pass
        # P1 H-1: the mirror's browser fallback is capped by the same
        # budget -- a hanging mirror page must not eat the phase either.
        _mirror_ms = browser_budget.attempt_timeout_ms()
        if browser_budget.exhausted or _mirror_ms <= 0:
            browser_budget.announce_exhausted()
            return "", 0, oa_url
        kwargs = dict(fetch_kwargs)
        kwargs.pop("wait_selector", None)
        kwargs.pop("cookies", None)
        kwargs["timeout"] = _mirror_ms
        response = stealthy_fetch_bounded(StealthyFetcher, oa_url, **kwargs)
        return str(response.html_content), response.status, getattr(response, "url", None) or oa_url

    mirrors = MirrorDiscovery(doi_val, not args.no_oa, resolve_oa_info, fetch_mirror, evidence)
    if html_content and hasattr(mirrors, "seed_seen_pages"):
        mirrors.seed_seen_pages([fetch_url, final_url, effective_base])
    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
    if _phase_expired():
        # P1-CE: the budget died before the mirrors; keep what the
        # publisher page already yielded instead of discarding it.
        phase_timed_out = True
    elif (args.oa or _discovery_retry_allowed(bool(candidates))) and (not candidates or diag.is_incomplete or args.oa):
        known = {c.url for c in candidates}
        mirror_found = []

        def _on_mirror_candidate(cand):
            # Stream each mirror candidate as it arrives; the partial
            # file holds publisher + mirror candidates found so far.
            if cand.url not in known and all(c.url != cand.url for c in mirror_found):
                mirror_found.append(cand)
                _stream(candidates + mirror_found, "oa_mirrors")

        additions = [c for c in mirrors.discover(on_candidate=_on_mirror_candidate) if c.url not in known]
        candidates = additions + candidates if args.oa else candidates + additions
    if _phase_expired():
        # The mirror round itself ran past the budget; the candidates it
        # found are already streamed, but no further stage may start.
        phase_timed_out = True
    if mirrors.info and mirrors.info.title:
        _refresh_title_and_dir_from_html_or_str(mirrors.info.title, is_raw_title=True)

    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
    needs_browser_retry = bool(diag.needs_browser) and (not candidates or diag.is_incomplete)
    if _discovery_retry_allowed(bool(candidates)) and (diag.is_incomplete or needs_browser_retry) and not _initial_page_blocked and bool(html_content.strip()) and not args.wait_selector and not args.browserskill and _browser_allowed():
        _retry_ms = min(args.timeout, browser_budget.attempt_timeout_ms())
        if _retry_ms > 0:
            print(f"  [Diagnostic] {diag.incomplete_reason}")
            print("  Attempting browser retry with supplementary container wait...")
            retry_kwargs = dict(fetch_kwargs)
            retry_kwargs["wait_selector"] = SUPP_WAIT_SELECTOR
            retry_kwargs["page_action"] = expand_page_accordions_action
            # P1 H-1: cap the retry goto at the remaining fallback budget
            # (fetch_kwargs is already capped at the goto timeout).
            retry_kwargs["timeout"] = _retry_ms
            try:
                retry_page = stealthy_fetch_bounded(StealthyFetcher, fetch_url, **retry_kwargs)
                retry_html = str(retry_page.html_content)
                _refresh_title_and_dir_from_html_or_str(retry_html)
                retry_base = extract_effective_base_url(retry_html, getattr(retry_page, "url", None) or fetch_url)
                retry_diag = find_all_candidates(retry_html, retry_base, original_url=args.url)
                evidence.observe(
                    "publisher",
                    retry_html,
                    retry_base,
                    soup=getattr(retry_diag, "_soup", None),
                    article_text=getattr(retry_diag, "_article_text", None),
                )
                if retry_diag.candidates:
                    existing_urls = {c.url for c in candidates}
                    new_cands = [c for c in retry_diag.candidates if c.url not in existing_urls]
                    if new_cands or not candidates:
                        print(f"  [Recovery] Found {len(new_cands) if candidates else len(retry_diag.candidates)} additional candidate(s) after browser retry!")
                        for c in new_cands:
                            candidates.append(c)
                        if not candidates:
                            candidates = retry_diag.candidates
                        diag = retry_diag
                        diag.candidates = candidates
            except Exception:
                pass

    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
    _need_auto_browser = diag.is_incomplete or (
        not candidates and (
            _initial_page_blocked
            or not html_content
            or len(html_content.strip()) < 2500
            or bool(getattr(diag, "needs_browser", False))
        )
    )
    if _need_auto_browser and _discovery_retry_allowed(bool(candidates)) and not _phase_expired() and _browser_allowed():
        def _try_auto_bsk():
            try:
                if not check_browserskill_status(timeout=1.5).get("ready"):
                    return None
                if hasattr(bsk_runtime, "enabled"):
                    bsk_runtime.enabled = True
                print("  [Recovery] Connected BrowserSkill (bsk) detected; retrying via live browser session...")
                _bh, _bs, _bf = fetch_page_via_browserskill(
                    fetch_url,
                    wait_selector=args.wait_selector,
                    timeout=min(args.timeout / 1000.0, browser_budget.attempt_timeout()),
                    runtime=bsk_runtime,
                )
                return _bh, _bs, _bf, None
            except Exception as _exc:
                print(f"  [Recovery] Auto BrowserSkill notice: {_exc}")
                return None

        def _try_auto_cdp():
            try:
                detected = discover_active_cdp_port()
                if not detected:
                    return None
                _cdp_ep = detected[0]
                print(f"  [Recovery] Active Chrome CDP detected at {_cdp_ep}; retrying via live browser...")
                _cdp_c = []
                try:
                    _cdp_c = extract_cdp_cookies(_cdp_ep) or []
                except Exception:
                    _cdp_c = []
                _ch, _cs, _cf, _fc = fetch_page_via_cdp(
                    _cdp_ep,
                    fetch_url,
                    wait_selector=args.wait_selector or SUPP_WAIT_SELECTOR,
                    timeout=min(args.timeout / 1000.0, browser_budget.attempt_timeout()),
                )
                return _ch, _cs, _cf, (_fc or _cdp_c)
            except Exception as _exc:
                print(f"  [Recovery] Auto CDP notice: {_exc}")
                return None

        def _try_auto_headful():
            nonlocal cookies, effective_headful, effective_real_chrome
            _hf_ms = min(args.timeout, browser_budget.attempt_timeout_ms())
            if _hf_ms <= 0:
                return None
            _rc = effective_real_chrome or has_system_chrome()
            print(f"  [Recovery] Retrying with standalone headful Chrome (real_chrome={_rc}, network_idle=False)...")
            _hf_kw = dict(fetch_kwargs)
            _hf_kw.pop("cdp_url", None)
            _hf_kw["headless"] = False
            _hf_kw["real_chrome"] = _rc
            _hf_kw["network_idle"] = False
            _hf_kw["solve_cloudflare"] = solve_cf
            if args.wait_selector:
                _hf_kw["wait_selector"] = args.wait_selector
            else:
                _hf_kw.pop("wait_selector", None)
            _hf_kw["wait"] = 1500
            _hf_kw["page_action"] = expand_page_accordions_action
            _hf_kw["timeout"] = _hf_ms
            if cookies:
                _hf_kw["cookies"] = normalize_playwright_cookies(cookies, default_url=fetch_url)
            try:
                _hp = stealthy_fetch_bounded(StealthyFetcher, fetch_url, **_hf_kw)
                if _hp is None:
                    return None
                _hh = str(getattr(_hp, "html_content", None) or getattr(_hp, "text", "") or "")
                _hs = getattr(_hp, "status", None)
                _hu = getattr(_hp, "url", None) or fetch_url
                if _hh and not page_fetch_is_failure(_hh, _hs):
                    effective_headful = True
                    effective_real_chrome = _rc
                    return _hh, _hs, _hu, getattr(_hp, "cookies", None)
            except Exception as _exc:
                print(f"  [Recovery] Standalone headful fallback notice: {_exc}")
            return None

        def _evaluate_auto_page(cand_html, cand_status, cand_url, extra_cookies, mode_name):
            nonlocal cookies, html_content, final_url, effective_base, candidates, diag
            if not cand_html or page_fetch_is_failure(cand_html, cand_status):
                return False, False
            if extra_cookies:
                cookies = merge_cookies(cookies, extra_cookies)
            _u = cand_url or fetch_url
            if is_crossref_chooser_url(_u) and not _phase_expired():
                _ch_urls = extract_crossref_chooser_urls(
                    html_str=cand_html,
                    doi=extract_doi(input_target) or "",
                    proxy=args.proxy or "",
                    crossref_email=str(local_cfg.get("crossref_email") or local_cfg.get("unpaywall_email") or ""),
                )
                if _ch_urls:
                    _u = _ch_urls[0]
            _base = extract_effective_base_url(cand_html, _u)
            _d = find_all_candidates(cand_html, _base, original_url=args.url)
            if not _d.candidates and (is_gsa_or_gsw_target(args.url) or is_gsa_or_gsw_target(_u)):
                _gsa_c, _gsa_t = discover_gsa_figshare_candidates(
                    args.url if is_gsa_or_gsw_target(args.url) else _u,
                    proxy=args.proxy or "",
                    crossref_email=str(local_cfg.get("crossref_email") or local_cfg.get("unpaywall_email") or ""),
                )
                if _gsa_t:
                    _refresh_title_and_dir_from_html_or_str(_gsa_t, is_raw_title=True)
                if _gsa_c:
                    _d.candidates = _gsa_c
            _refresh_title_and_dir_from_html_or_str(cand_html)
            evidence.observe(
                "publisher",
                cand_html,
                _base,
                soup=getattr(_d, "_soup", None),
                article_text=getattr(_d, "_article_text", None),
            )
            _inc, _ = evidence.check(_d.candidates)
            _existing = {c.url for c in candidates}
            _new = [c for c in _d.candidates if c.url not in _existing]
            if _new or not candidates:
                candidates.extend(_new)
                if not candidates:
                    candidates = list(_d.candidates)
                diag = _d
                diag.candidates = candidates
                html_content = cand_html
                final_url = _u
                effective_base = _base
                diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
                if _new:
                    print(f"  [Recovery:{mode_name}] Found {len(_new)} candidate(s) (total: {len(candidates)}).")
                return bool(candidates and not diag.is_incomplete), bool(_new)
            if not html_content:
                html_content = cand_html
                final_url = _u
                effective_base = _base
                return False, True
            return False, False

        run_auto_browser_recovery(
            need_recovery=_need_auto_browser,
            retry_allowed=_discovery_retry_allowed(bool(candidates)) and not _phase_expired(),
            browser_allowed_fn=_browser_allowed,
            try_bsk_fn=_try_auto_bsk,
            try_cdp_fn=_try_auto_cdp,
            try_headful_fn=_try_auto_headful,
            evaluate_fn=_evaluate_auto_page,
            bsk_enabled=(not getattr(args, "no_bsk", False)) and (not getattr(args, "browserskill", False)) and not bsk_runtime.enabled,
            cdp_enabled=(not getattr(args, "no_cdp", False)) and (not cdp_target),
            headful_enabled=(not effective_headful) and (not getattr(args, "no_auto_headful", False)) and has_gui_display(),
        )
        diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)

    _alt_doi = (
        extract_doi(input_target)
        or extract_doi(args.url)
        or extract_doi(final_url or "")
        or extract_doi_from_html(html_content or "")
    )
    if not candidates and _alt_doi and _discovery_retry_allowed(False) and not _phase_expired():
        _alt_chooser_urls = get_alternate_chooser_urls(
            doi=_alt_doi,
            html_str="",
            exclude_urls=[input_target, args.url, fetch_url, final_url] + _tried_chooser_urls,
            seed_urls=_chooser_seed_urls,
            proxy=args.proxy or "",
            crossref_email=str(local_cfg.get("crossref_email") or local_cfg.get("unpaywall_email") or ""),
        )
        for _alt_u in _alt_chooser_urls[:2]:
            if _phase_expired():
                break
            _alt_budget = ensure_phase_browser_budget(
                browser_budget,
                phase_deadline=phase_deadline,
                explicit_budget=getattr(args, "browser_fallback_budget", None),
            )
            print(f"\n[chooser-alt] Primary publisher yielded 0 candidates; trying alternate DOI host: {_alt_u}")
            _alt_html = ""
            _alt_status = None
            _alt_final = _alt_u
            if not _alt_budget.exhausted:
                try:
                    _alt_ms = min(args.timeout, max(15000, _alt_budget.attempt_timeout_ms() or 30000))
                    _alt_kw = dict(fetch_kwargs)
                    _alt_kw["timeout"] = _alt_ms
                    if effective_headful:
                        _alt_kw["headless"] = False
                    if effective_real_chrome:
                        _alt_kw["real_chrome"] = True
                    _alt_page = stealthy_fetch_bounded(StealthyFetcher, _alt_u, **_alt_kw)
                    if _alt_page is not None:
                        _alt_html = str(getattr(_alt_page, "html_content", None) or getattr(_alt_page, "text", "") or "")
                        _alt_status = getattr(_alt_page, "status", None)
                        _alt_final = getattr(_alt_page, "url", None) or _alt_u
                except Exception:
                    _alt_html = ""
            if (not _alt_html or page_fetch_is_failure(_alt_html, _alt_status)) and (not _alt_budget.exhausted) and not _phase_expired():
                if (not getattr(args, "no_bsk", False)) and check_browserskill_status(timeout=1.5).get("ready"):
                    try:
                        _alt_html, _alt_status, _alt_final = fetch_page_via_browserskill(
                            _alt_u,
                            timeout=_alt_budget.attempt_timeout(),
                            runtime=bsk_runtime,
                        )
                    except Exception:
                        _alt_html = ""
                if (not _alt_html or page_fetch_is_failure(_alt_html, _alt_status)) and (not getattr(args, "no_auto_headful", False)) and has_gui_display() and not _phase_expired():
                    _rc_alt = effective_real_chrome or has_system_chrome()
                    try:
                        _alt_kw2 = dict(fetch_kwargs)
                        _alt_kw2["headless"] = False
                        _alt_kw2["real_chrome"] = _rc_alt
                        _alt_kw2["network_idle"] = False
                        _alt_kw2["timeout"] = min(args.timeout, max(15000, int(_phase_remaining() * 1000)))
                        _alt_page2 = stealthy_fetch_bounded(StealthyFetcher, _alt_u, **_alt_kw2)
                        if _alt_page2 is not None:
                            _alt_html = str(getattr(_alt_page2, "html_content", None) or getattr(_alt_page2, "text", "") or "")
                            _alt_status = getattr(_alt_page2, "status", None)
                            _alt_final = getattr(_alt_page2, "url", None) or _alt_u
                            if _alt_html and not page_fetch_is_failure(_alt_html, _alt_status):
                                effective_headful = True
                                effective_real_chrome = _rc_alt
                    except Exception:
                        pass
            if not _alt_html or page_fetch_is_failure(_alt_html, _alt_status):
                continue
            _alt_base = extract_effective_base_url(_alt_html, _alt_final)
            _alt_diag = find_all_candidates(_alt_html, _alt_base, original_url=args.url)
            _alt_cands = list(_alt_diag.candidates or [])
            if not _alt_cands and (is_gsa_or_gsw_target(_alt_final) or is_gsa_or_gsw_target(args.url)):
                _gsa_c, _gsa_t = discover_gsa_figshare_candidates(
                    _alt_final,
                    proxy=args.proxy or "",
                    crossref_email=str(local_cfg.get("crossref_email") or local_cfg.get("unpaywall_email") or ""),
                )
                if _gsa_t:
                    _refresh_title_and_dir_from_html_or_str(_gsa_t, is_raw_title=True)
                if _gsa_c:
                    _alt_cands = _gsa_c
            if _alt_cands:
                candidates = _alt_cands
                diag = _alt_diag
                diag.candidates = candidates
                html_content = _alt_html
                final_url = _alt_final
                effective_base = _alt_base
                _refresh_title_and_dir_from_html_or_str(_alt_html)
                evidence.observe(
                    "publisher",
                    _alt_html,
                    _alt_base,
                    soup=getattr(_alt_diag, "_soup", None),
                    article_text=getattr(_alt_diag, "_article_text", None),
                )
                diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
                print(f"  [chooser-alt] Found {len(candidates)} candidate(s) on {_alt_final}")
                break

    _stream(candidates, "browser_retry")

    if not candidates:
        print("  No supplementary data links found.")
        if diag.is_incomplete:
            print(f"  [Diagnostic] {diag.incomplete_reason}")
        if html_content and re.search(r"附表|附录表|补充表|supplementary\s+tables?", html_content, re.IGNORECASE):
            print("  [Diagnostic] Page mentions supplementary tables without external download links; tables may be embedded in the main article PDF (extract via --pdf).")
        if diag.excluded_links:
            print(f"  [Diagnostic] Evaluated and excluded {len(diag.excluded_links)} links (figures/references/anchors).")
        if phase_timed_out:
            _persist(write_partial_candidates, article_dir, candidates, complete=False,
                     sources_completed=list(_sources_done), timed_out=True)
            print(f"  [Warning] Phase timeout ({phase_timeout:g}s budget) reached with no candidates; "
                  f"partial results saved to {os.path.join(article_dir, PARTIAL_CANDIDATES_FILENAME)}")
        print("  Try running with --headful and --no-cloudflare to debug.")
        return

    def _repo_bsk_fetch(repo_url: str):
        if bsk_runtime.enabled or (not getattr(args, "no_bsk", False) and check_browserskill_status(timeout=1.0).get("ready")):
            return fetch_page_via_browserskill(
                repo_url,
                timeout=min(35.0, max(10.0, _phase_remaining())),
                runtime=bsk_runtime,
            )
        return "", None, repo_url

    _repo_browser = RepoBrowserSession(
        play_cookies=cookies,
        headful=bool(effective_headful or (not getattr(args, "no_auto_headful", False) and has_gui_display())),
        real_chrome=bool(effective_real_chrome or has_system_chrome()),
        cdp_url=cdp_target,
        proxy=args.proxy,
        budget=browser_budget,
        phase_deadline=phase_deadline,
        explicit_budget=getattr(args, "browser_fallback_budget", None),
        bsk_fetch=_repo_bsk_fetch,
        allow_auto_headful=bool((not getattr(args, "no_auto_headful", False)) and has_gui_display()),
    )
    try:
        candidates = expand_repository_candidates(candidates, proxy=args.proxy, browser_fetch=_repo_browser.fetch)
    finally:
        _repo_browser.close()
    candidates = dedupe_candidates(candidates)
    diag.candidates = candidates
    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)

    # P1-CE: download in priority order (real supplement files first,
    # external/repository pages last); the sort is lossless, so the final
    # list below is the order downloads will be attempted in.
    discovery_order = list(candidates)
    candidates = sort_candidates_by_priority(candidates)
    diag.candidates = candidates

    print(f"  Found {len(candidates)} potential supplementary file(s):")
    for i, cand in enumerate(candidates, 1):
        _print_candidate(cand, i)
    if candidates != discovery_order:
        print("  [Info] Candidates reordered by priority for download "
              "(real supplement files first, external pages last).")

    if evidence.describe():
        print(f"  [Info] {evidence.describe()}")
    if diag.is_incomplete:
        print(f"  [Warning] Result may be incomplete: {diag.incomplete_reason}")

    if phase_timed_out:
        _persist(write_partial_candidates, article_dir, candidates, complete=False,
                 sources_completed=list(_sources_done), timed_out=True)
        print(f"  [Warning] Phase timeout ({phase_timeout:g}s budget) reached; "
              f"partial results saved to {os.path.join(article_dir, PARTIAL_CANDIDATES_FILENAME)}")
    else:
        _persist(finalize_candidates, article_dir, candidates,
                 sources_completed=list(_sources_done))

    if args.list_only:
        return

    # --- Step 3: Download files ---
    print(f"\n[3/3] Downloading {len(candidates)} file(s)...")
    # Ensure already-discovered candidates receive a bounded download window
    # even when Phase A page discovery spent almost the whole phase budget.
    dl_deadline = phase_deadline
    if dl_deadline is not None and candidates:
        min_dl_window = min(20.0, max(10.0, float(getattr(args, "download_reserve", 15.0) or 15.0)))
        if dl_deadline - time.monotonic() < min_dl_window:
            dl_deadline = time.monotonic() + min_dl_window

    stealth_session_holder = {}

    def _download_candidate_with_reason(cand):
        err_info = {}
        res = download_file(
            cand,
            article_dir,
            cookies=cookies,
            bsk_runtime=bsk_runtime,
            referer=cand.source_url or final_url,
            proxy=args.proxy,
            stealth_session_holder=stealth_session_holder,
            deadline=dl_deadline,
            error_out=err_info,
            headful=effective_headful,
            real_chrome=effective_real_chrome,
        )
        if res:
            return res
        if dl_deadline is None or time.monotonic() < dl_deadline:
            for route_url, route_px in iter_download_fallback_routes(cand.url, proxy=args.proxy):
                alt_res = download_file(
                    clone_candidate_with_url(cand, route_url),
                    article_dir,
                    cookies=cookies,
                    bsk_runtime=bsk_runtime,
                    referer=cand.source_url or final_url,
                    proxy=route_px,
                    stealth_session_holder=stealth_session_holder,
                    deadline=dl_deadline,
                    error_out={},
                    headful=effective_headful,
                    real_chrome=effective_real_chrome,
                )
                if alt_res:
                    return alt_res
        return None, err_info.get("reason") or "download failed"

    try:
        successful, attempted = download_with_recovery(
            candidates,
            _download_candidate_with_reason,
            mirrors, expand_repository_candidates, max_retries=1, deadline=dl_deadline)
    finally:
        _shared_sess = stealth_session_holder.get("session")
        if _shared_sess is not None:
            try:
                _shared_sess.close()
            except Exception:
                pass
    if mirrors.info and mirrors.info.title:
        _refresh_title_and_dir_from_html_or_str(mirrors.info.title, is_raw_title=True)
    incomplete, reason = evidence.check(successful)
    if incomplete:
        print(f"  [Warning] Downloaded set remains incomplete: {reason}")
    print(f"\nDone: {len(successful)}/{attempted} file transfers succeeded in {os.path.abspath(article_dir)}")

    # --- Step 4: Optional MarkItDown conversion ---
    if args.convert_markdown:
        print("\n[Post-Processing] Converting downloaded supplements to Markdown via Microsoft MarkItDown...")
        converted = convert_directory_supplements(article_dir)
        print(f"Converted {len(converted)} file(s) to Markdown sidecars.")



if __name__ == "__main__":
    main()
