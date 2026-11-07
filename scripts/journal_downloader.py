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
    disambiguate_target_filename,
    extract_article_title,
    is_placeholder_or_blocked_title,
    migrate_or_merge_article_dir,
    expand_page_accordions_action,
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
                manifest[name] = url
                save_download_manifest(output_dir, manifest)
                return ensure_inferred_extension_on_disk(path, output_dir, url, manifest=manifest)
            return None
    else:
        url = cand_or_url
        suggested_fname = ""

    manifest = load_download_manifest(output_dir)
    fname = suggested_fname or extract_filename_from_url(url)
    fname = sanitize_filename(fname)
    fname = disambiguate_target_filename(output_dir, fname, url, manifest)
    filepath = os.path.join(output_dir, fname)

    # Check if already exists and is valid for this exact URL
    if os.path.exists(filepath) and manifest.get(fname) == url:
        is_val, _ = validate_downloaded_file(filepath)
        if is_val:
            print(f"  [SKIP] {fname} (already exists)")
            return filepath

    print(f"  [FETCH] {fname} ...", end=" ", flush=True)
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
            if os.path.exists(filepath) and manifest.get(fname) == url:
                is_val, _ = validate_downloaded_file(filepath)
                if is_val:
                    print(f"[SKIP] {fname} (already exists)")
                    return filepath

        # Infer extension if filename has none or is generic .bin
        if "." not in fname or fname.endswith(".bin"):
            inferred_ext = infer_file_extension(resp.body, hdr.get("content-type", ""))
            if inferred_ext:
                base = fname[:-4] if fname.endswith(".bin") else fname
                fname = disambiguate_target_filename(output_dir, f"{base}{inferred_ext}", url, manifest)
                filepath = os.path.join(output_dir, fname)

        with open(filepath, "wb") as f:
            f.write(resp.body)

        # Validate file based on signature and response headers (Fixing Issue #5, D1)
        is_valid, reason = validate_downloaded_file(
            filepath,
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
                    os.replace(filepath, new_path)
                    filepath = new_path
                    fname = html_name
            manifest[fname] = url
            save_download_manifest(output_dir, manifest)
            print(f"OK ({size_kb:.1f} KB - {reason})")
            return filepath
        raise RuntimeError(reason)

    except Exception as exc:
        if os.path.exists(filepath):
            os.remove(filepath)
        if bsk_session is not None or (bsk_runtime is not None and bsk_runtime.enabled):
            result = download_file_via_browserskill(
                url, filepath, session=bsk_session, referer=referer, runtime=bsk_runtime
            )
            if result:
                manifest[fname] = url
                save_download_manifest(output_dir, manifest)
                filepath = ensure_inferred_extension_on_disk(filepath, output_dir, url, manifest=manifest)
                print(f"OK (via BrowserSkill, {os.path.getsize(filepath) / 1024:.1f} KB)")
                return filepath
        print(f"FAIL: {exc}")
        return None


def guess_filename_from_url(url):
    """Derive a readable filename from URL (backward-compatible)."""
    return extract_filename_from_url(url)


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
        "timeout": args.timeout,
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
                timeout=args.timeout / 1000.0,
                browser=getattr(args, "bsk_browser", None),
                tab_id=getattr(args, "bsk_tab", None),
                runtime=bsk_runtime,
            )
            if bsk_html and bsk_status < 400:
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
                cdp_target, fetch_url, wait_selector=args.wait_selector, timeout=args.timeout / 1000.0
            )
            if cdp_html and cdp_status < 400:
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


    if (page is None or getattr(page, "status", 200) >= 400) and bsk_runtime.enabled and not args.browserskill:
        try:
            html, status, landing = fetch_page_via_browserskill(
                fetch_url, wait_selector=args.wait_selector,
                timeout=args.timeout / 1000.0, runtime=bsk_runtime,
            )
            page = type("BrowserSkillPage", (), {"html_content": html, "text": html, "url": landing, "status": status})()
        except BrowserSkillError as exc:
            if bsk_runtime.browser or bsk_runtime.tab_id:
                raise
            print(f"  [Recovery] BrowserSkill: {exc}")

    if page is None:
        print(f"Warning: Unable to fetch publisher page; checking available alternatives.")
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
    diag = find_all_candidates(html_content, effective_base, original_url=args.url)
    candidates = diag.candidates
    evidence = DiscoveryEvidence()
    evidence.observe("publisher", html_content, effective_base)

    if (not candidates or diag.is_incomplete) and bsk_runtime.enabled and not args.wait_selector:
        try:
            retry_html, _, retry_url = fetch_page_via_browserskill(
                fetch_url, timeout=args.timeout / 1000.0, runtime=bsk_runtime,
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

    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
    if diag.is_incomplete and not args.wait_selector and not args.browserskill:
        print(f"  [Diagnostic] {diag.incomplete_reason}")
        print("  Attempting browser retry with supplementary container wait...")
        retry_kwargs = dict(fetch_kwargs)
        retry_kwargs["wait_selector"] = SUPP_WAIT_SELECTOR
        retry_kwargs["page_action"] = expand_page_accordions_action
        retry_kwargs["timeout"] = min(args.timeout, 30000)
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
        kwargs = dict(fetch_kwargs)
        kwargs.pop("wait_selector", None)
        kwargs.pop("cookies", None)
        response = StealthyFetcher.fetch(oa_url, **kwargs)
        return str(response.html_content), response.status, getattr(response, "url", None) or oa_url

    mirrors = MirrorDiscovery(doi_val, not args.no_oa, resolve_oa_info, fetch_mirror, evidence)
    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)
    if not candidates or diag.is_incomplete or args.oa:
        known = {c.url for c in candidates}
        additions = [c for c in mirrors.discover() if c.url not in known]
        candidates = additions + candidates if args.oa else candidates + additions
    if mirrors.info and mirrors.info.title:
        _refresh_title_and_dir_from_html_or_str(mirrors.info.title, is_raw_title=True)

    if not candidates:
        print("  No supplementary data links found.")
        if diag.is_incomplete:
            print(f"  [Diagnostic] {diag.incomplete_reason}")
        if diag.excluded_links:
            print(f"  [Diagnostic] Evaluated and excluded {len(diag.excluded_links)} links (figures/references/anchors).")
        print("  Try running with --headful and --no-cloudflare to debug.")
        return

    candidates = expand_repository_candidates(candidates)
    diag.candidates = candidates
    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)

    print(f"  Found {len(candidates)} potential supplementary file(s):")
    for i, cand in enumerate(candidates, 1):
        print(f"    [{i}] {cand.filename}")
        print(f"        URL:     {cand.url}")
        if cand.link_text:
            print(f"        Text:    {cand.link_text}")
        print(f"        Section: {cand.section} | Rule: {cand.match_rule}")

    if evidence.describe():
        print(f"  [Info] {evidence.describe()}")
    if diag.is_incomplete:
        print(f"  [Warning] Result may be incomplete: {diag.incomplete_reason}")

    if args.list_only:
        return

    # --- Step 3: Download files ---
    print(f"\n[3/3] Downloading {len(candidates)} file(s)...")
    successful, attempted = download_with_recovery(
        candidates,
        lambda cand: download_file(cand, article_dir, cookies=cookies, bsk_runtime=bsk_runtime,
                                   referer=cand.source_url or final_url),
        mirrors, expand_repository_candidates)
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
