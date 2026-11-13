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
    extract_article_title,
    is_placeholder_or_blocked_title,
    migrate_or_merge_article_dir,
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
    # P1 H-1: independent budget for the browser fallback.
    DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS,
    DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS,
    browser_budget_from_args,
)


try:
    from playwright_utils import (
        normalize_playwright_cookies,
        discover_active_cdp_port,
        resolve_cdp_ws_url,
        extract_cdp_cookies,
        merge_cookies,
        fetch_page_via_cdp,
    )
except ImportError:
    normalize_playwright_cookies = lambda c, default_url=None: c
    discover_active_cdp_port = lambda profile_paths=None: None
    resolve_cdp_ws_url = lambda ep, timeout=1.0: None
    extract_cdp_cookies = lambda ep, timeout=4.0: []
    merge_cookies = lambda a, b: list(a or []) + list(b or [])
    fetch_page_via_cdp = lambda ep, url, wait_selector=None, timeout=30.0: None

from browserskill_adapter import (
    BrowserSkillError,
    BrowserSkillRuntime,
    add_browserskill_arguments,
    runtime_from_args,
    fetch_page_via_browserskill,
    download_file_via_browserskill,
)


from discovery_state import DiscoveryEvidence, MirrorDiscovery, download_with_recovery
from repository_resolver import expand_repository_candidates
from oa_resolver import resolve_oa_info, is_doi, extract_doi, extract_doi_from_html
from markitdown_converter import convert_directory_supplements, is_markitdown_available



def load_saved_cookies():
    """Load saved cookies from local cookies.json and login_publishers.py profile."""
    candidates = [
        os.path.abspath("cookies.json"),
        os.path.expanduser("~/.journal_supp_downloader_profile/cookies.json"),
    ]
    all_cookies = []
    seen_keys = set()
    for cookie_file in candidates:
        if os.path.exists(cookie_file):
            try:
                with open(cookie_file, "r", encoding="utf-8") as f:
                    cookies = json.load(f)
                items = []
                if isinstance(cookies, list):
                    items = cookies
                elif isinstance(cookies, dict):
                    items = [{"name": k, "value": v, "path": "/"} for k, v in cookies.items()]
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


def download_file(cand_or_url, output_dir, cookies=None, bsk_session=None, referer=None, bsk_runtime=None):
    """
    Download a single file using HTTP Fetcher (not browser-based).
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
                print(f"  [Unresolved] Browser session required: {cand_or_url.link_text}")
                return None
            manifest = load_download_manifest(output_dir)
            name = disambiguate_target_filename(output_dir, suggested_fname, url, manifest)
            path = os.path.join(output_dir, name)
            result = download_file_via_browserskill(selector, path, session=bsk_session, runtime=bsk_runtime, referer=referer, verify_control=True)
            if result:
                record_manifest_entry(manifest, name, url, source_url=referer or "", table_ids=sorted(cand_or_url.covered_table_ids))
                save_download_manifest(output_dir, manifest)
                return ensure_inferred_extension_on_disk(path, output_dir, url, manifest=manifest)
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

    print(f"  [FETCH] {fname} ...", end=" ", flush=True)
    # H9: the body is written in bounded chunks to a same-directory temp
    # file, validated, then committed with os.replace; the final path
    # never holds a partial file and a failure only discards the temp.
    tmp_path = None
    try:
        get_kwargs = {"stealthy_headers": True, "timeout": 30}
        if referer:
            get_kwargs["headers"] = {"Referer": referer}
        if cookies:
            get_kwargs["cookies"] = cookies_to_dict(cookies, url)

        resp = None
        try:
            resp = Fetcher.get(url, **get_kwargs)
        except Exception:
            pass

        if resp is None or resp.status >= 400:
            # Fallback: try requests session with cookies and browser user agent
            try:
                import requests
                sess = requests.Session()
                c_dict = cookies_to_dict(cookies, url) if cookies else {}
                for ck, cv in c_dict.items():
                    sess.cookies.set(ck, cv)
                req_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
                if referer:
                    req_headers["Referer"] = referer
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
        print(f"FAIL: {single_line(exc)}")
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
             "being killed mid-list (default: 150)"
    )
    # P1 H-1: the browser fallback gets its own quotas -- one page goto
    # at most 30s, 60s of browser time in total per phase. Either quota
    # running out degrades to the OA mirror round instead of killing
    # the phase inside one page load (round 3: #1 hung 120s+ on
    # nature.com, mirrors never ran, 0 files).
    parser.add_argument(
        "--browser-goto-timeout", type=float, default=DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS,
        help="Max seconds for one browser page load (goto) in the "
             "fallback path (default: 30)"
    )
    parser.add_argument(
        "--browser-fallback-budget", type=float, default=DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS,
        help="Max total seconds of browser fallback time per phase; "
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

    def _phase_expired():
        return time.monotonic() >= phase_deadline

    print(f"Phase timeout: {phase_timeout:g}s")

    # P1 H-1: the browser fallback's own quota (default: one goto 30s,
    # 60s total per phase), shared by every browser attempt below.
    browser_budget = browser_budget_from_args(args)
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
        "network_idle": True,
        # P1 H-1: the page fetch is a browser goto too -- cap it at the
        # goto timeout (30s by default) instead of --timeout (60s), so
        # step [1/3] alone cannot eat the phase budget (round 3 #1).
        "timeout": min(args.timeout, int(browser_budget.goto_timeout * 1000)),
        "proxy": args.proxy,
        "wait_selector": args.wait_selector,
        "page_action": expand_page_accordions_action,
        "google_search": False,
    }
    if cookies:
        fetch_kwargs["cookies"] = normalize_playwright_cookies(cookies, default_url=fetch_url)

    page = None
    # Primary BrowserSkill check if requested via --bsk / --browserskill
    if getattr(args, "browserskill", False):
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

    if cdp_target and page is None:
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

    # Secondary: Scrapling StealthyFetcher fallback
    if page is None:
        ws_cdp_url = resolve_cdp_ws_url(cdp_target) if cdp_target else None
        if ws_cdp_url:
            fetch_kwargs["cdp_url"] = ws_cdp_url
            fetch_kwargs.pop("real_chrome", None)

        try:
            page = StealthyFetcher.fetch(fetch_url, **fetch_kwargs)
        except Exception as e:
            print(f"  [Warning] Browser fetch encountered error: {e}")
            if "cdp_url" in fetch_kwargs:
                print("  [Recovery] Retrying with standalone stealth browser...")
                fb_kwargs = dict(fetch_kwargs)
                fb_kwargs.pop("cdp_url", None)
                try:
                    page = StealthyFetcher.fetch(fetch_url, **fb_kwargs)
                except Exception as e2:
                    print(f"  [Warning] Standalone browser fetch also failed: {e2}")

            if page is None:
                print("  [Recovery] Attempting HTTP direct fetch fallback...")
                try:
                    http_kwargs = {"stealthy_headers": True, "timeout": min(args.timeout / 1000.0, 30.0)}
                    if cookies:
                        http_kwargs["cookies"] = cookies_to_dict(cookies, fetch_url)
                    resp = Fetcher.get(fetch_url, **http_kwargs)
                    if resp.status < 400 and (getattr(resp, "html_content", None) or resp.text):
                        page = resp
                except Exception as e3:
                    print(f"  [Recovery] HTTP direct fetch failed: {e3}")


    # H1: a blocked page (challenge / login wall) is a failed fetch even
    # when the transport reported status < 400, and an unknown status
    # (None, from BrowserSkill/CDP) must not crash the comparison.
    _page_html = str(getattr(page, "html_content", None) or getattr(page, "text", "") or "") if page is not None else ""
    _page_status = getattr(page, "status", None) if page is not None else None
    if (page is None or page_fetch_is_failure(_page_html, _page_status)) and bsk_runtime.enabled and not args.browserskill:
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

    if page is None:
        print(f"Warning: Unable to fetch publisher page; checking available alternatives.")
        page = type("UnavailablePage", (), {"url": fetch_url, "html_content": "", "text": "", "status": 0})()

    # A page that is still blocked after every fetch tier must not be
    # parsed as an article: a challenge page mentioning "Table S1" would
    # otherwise be recorded as declared-but-missing evidence.
    if page is not None and is_blocked_page(str(getattr(page, "html_content", None) or getattr(page, "text", "") or "")):
        print("  [Warning] Fetched page is a challenge / login wall; treating it as no usable article page.")
        page = type("UnavailablePage", (), {"url": fetch_url, "html_content": "", "text": "", "status": 0})()

    # Resolve effective base URL (Fixing Issue #1: DOI redirect resolution)
    final_url = getattr(page, "url", None) or fetch_url
    html_content = getattr(page, "html_content", None) or getattr(page, "text", "")
    html_content = str(html_content)
    effective_base = extract_effective_base_url(html_content, final_url)

    # Extract and sanitize article title for folder name
    raw_title = "N/A"
    if hasattr(page, "css"):
        try:
            raw_title = page.css('title::text').get() or "N/A"
        except Exception:
            pass
    if raw_title == "N/A":
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
            extract_article_title(f"<title>{source_html_or_title}</title>", default="")
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
    evidence = DiscoveryEvidence()
    evidence.observe("publisher", html_content, effective_base)
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
    if not _phase_expired() and (not candidates or diag.is_incomplete or needs_browser_retry) and bsk_runtime.enabled and not args.wait_selector and _browser_allowed():
        try:
            retry_html, _, retry_url = fetch_page_via_browserskill(
                fetch_url, timeout=min(args.timeout / 1000.0, browser_budget.attempt_timeout()), runtime=bsk_runtime,
            )
            _refresh_title_and_dir_from_html_or_str(retry_html)
            retry_base = extract_effective_base_url(retry_html, retry_url)
            evidence.observe("publisher", retry_html, retry_base)
            retry_diag = find_all_candidates(retry_html, retry_base, original_url=args.url)
            seen = {c.url for c in candidates}
            candidates.extend(c for c in retry_diag.candidates if c.url not in seen)
            if retry_diag.candidates:
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
        try:
            response = Fetcher.get(oa_url, stealthy_headers=True, timeout=20)
            document = getattr(response, "html_content", None) or response.text
            if response.status < 400 and document:
                return str(document), response.status, getattr(response, "url", None) or oa_url
        except Exception:
            pass
        # P1 H-1: the mirror's browser fallback is capped by the same
        # budget -- a hanging mirror page must not eat the phase either.
        if browser_budget.exhausted:
            browser_budget.announce_exhausted()
            return "", 0, oa_url
        kwargs = dict(fetch_kwargs)
        kwargs.pop("wait_selector", None)
        kwargs.pop("cookies", None)
        kwargs["timeout"] = browser_budget.attempt_timeout_ms()
        response = StealthyFetcher.fetch(oa_url, **kwargs)
        return str(response.html_content), response.status, getattr(response, "url", None) or oa_url

    mirrors = MirrorDiscovery(doi_val, not args.no_oa, resolve_oa_info, fetch_mirror, evidence)
    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
    if _phase_expired():
        # P1-CE: the budget died before the mirrors; keep what the
        # publisher page already yielded instead of discarding it.
        phase_timed_out = True
    elif not candidates or diag.is_incomplete or args.oa:
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
    if not _phase_expired() and (diag.is_incomplete or needs_browser_retry) and not args.wait_selector and not args.browserskill and _browser_allowed():
        print(f"  [Diagnostic] {diag.incomplete_reason}")
        print("  Attempting browser retry with supplementary container wait...")
        retry_kwargs = dict(fetch_kwargs)
        retry_kwargs["wait_selector"] = SUPP_WAIT_SELECTOR
        retry_kwargs["page_action"] = expand_page_accordions_action
        # P1 H-1: cap the retry goto at the remaining fallback budget
        # (fetch_kwargs is already capped at the goto timeout).
        retry_kwargs["timeout"] = min(args.timeout, browser_budget.attempt_timeout_ms())
        try:
            retry_page = StealthyFetcher.fetch(fetch_url, **retry_kwargs)
            retry_html = str(retry_page.html_content)
            _refresh_title_and_dir_from_html_or_str(retry_html)
            retry_base = extract_effective_base_url(retry_html, getattr(retry_page, "url", None) or fetch_url)
            evidence.observe("publisher", retry_html, retry_base)
            retry_diag = find_all_candidates(retry_html, retry_base, original_url=args.url)
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

    _stream(candidates, "browser_retry")

    if not candidates:
        print("  No supplementary data links found.")
        if diag.is_incomplete:
            print(f"  [Diagnostic] {diag.incomplete_reason}")
        if diag.excluded_links:
            print(f"  [Diagnostic] Evaluated and excluded {len(diag.excluded_links)} links (figures/references/anchors).")
        if phase_timed_out:
            _persist(write_partial_candidates, article_dir, candidates, complete=False,
                     sources_completed=list(_sources_done), timed_out=True)
            print(f"  [Warning] Phase timeout ({phase_timeout:g}s budget) reached with no candidates; "
                  f"partial results saved to {os.path.join(article_dir, PARTIAL_CANDIDATES_FILENAME)}")
        print("  Try running with --headful and --no-cloudflare to debug.")
        return

    candidates = expand_repository_candidates(candidates)
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
    # H8: no separate transport exists at this call site -- download_file
    # already cascades Fetcher -> requests -> BrowserSkill internally, so
    # there is no distinct retry_download to wire in. A transient failure
    # therefore retries the same download once, with backoff
    # (max_retries=1); a bare None/False with no reason is not retried.
    successful, attempted = download_with_recovery(
        candidates,
        lambda cand: download_file(cand, article_dir, cookies=cookies, bsk_runtime=bsk_runtime,
                                   referer=cand.source_url or final_url),
        mirrors, expand_repository_candidates, max_retries=1, deadline=phase_deadline)
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
