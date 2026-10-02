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
    validate_downloaded_file,
    sanitize_filename,
    extract_filename_from_url,
    extract_filename_from_content_disposition,
    infer_file_extension,
    cookies_to_dict,
    extract_effective_base_url,
    Candidate,
    FindingDiagnostics,
    DATA_EXTENSIONS,
    MMC_PATTERN,
    is_excluded_url,
    is_article_figure_url,
    load_download_manifest,
    save_download_manifest,
    disambiguate_target_filename,
    extract_article_title,
)


try:
    from playwright_utils import normalize_playwright_cookies
except ImportError:
    normalize_playwright_cookies = lambda c, default_url=None: c


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


def download_file(cand_or_url, output_dir, cookies=None):
    """
    Download a single file using HTTP Fetcher (not browser-based).
    Validates content signatures and HTTP headers, avoiding arbitrary 1 KB threshold.
    Returns path or None.
    """
    if isinstance(cand_or_url, Candidate):
        url = cand_or_url.url
        suggested_fname = cand_or_url.filename
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
        get_kwargs = {"stealthy_headers": True, "timeout": 30000}
        if cookies:
            get_kwargs["cookies"] = cookies_to_dict(cookies, url)

        resp = Fetcher.get(url, **get_kwargs)

        if resp.status >= 400:
            print(f"HTTP {resp.status}")
            return None

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
        else:
            if os.path.exists(filepath):
                os.remove(filepath)
            print(f"FAIL: {reason}")
            return None

    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        print(f"FAIL: {e}")
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
        "--list-only", action="store_true",
        help="Only list found links with diagnostic evidence, don't download"
    )
    args = parser.parse_args()

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
        "google_search": False,
    }
    if cookies:
        fetch_kwargs["cookies"] = normalize_playwright_cookies(cookies, default_url=fetch_url)

    page = StealthyFetcher.fetch(fetch_url, **fetch_kwargs)

    # Resolve effective base URL (Fixing Issue #1: DOI redirect resolution)
    final_url = getattr(page, "url", None) or fetch_url
    html_content = str(page.html_content)
    effective_base = extract_effective_base_url(html_content, final_url)

    # Extract and sanitize article title for folder name
    raw_title = page.css('title::text').get() or "N/A"
    article_dirname = extract_article_title(page)
    article_dir = os.path.join(args.output_dir, article_dirname)
    os.makedirs(article_dir, exist_ok=True)

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

    if diag.is_incomplete and not args.wait_selector:
        print(f"  [Diagnostic] {diag.incomplete_reason}")
        print("  Attempting browser retry with supplementary container wait...")
        retry_kwargs = dict(fetch_kwargs)
        retry_kwargs["wait_selector"] = "#supplementary-material, .supplementary-material, #extended-data, [data-section='supp-table'], [id*='suppl']"
        retry_kwargs["timeout"] = min(args.timeout, 30000)
        try:
            retry_page = StealthyFetcher.fetch(fetch_url, **retry_kwargs)
            retry_html = str(retry_page.html_content)
            retry_diag = find_all_candidates(retry_html, effective_base, original_url=args.url)
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

    if not candidates:
        print("  No supplementary data links found.")
        if diag.is_incomplete:
            print(f"  [Diagnostic] {diag.incomplete_reason}")
        if diag.excluded_links:
            print(f"  [Diagnostic] Evaluated and excluded {len(diag.excluded_links)} links (figures/references/anchors).")
        print("  Try running with --headful and --no-cloudflare to debug.")
        return

    print(f"  Found {len(candidates)} potential supplementary file(s):")
    for i, cand in enumerate(candidates, 1):
        print(f"    [{i}] {cand.filename}")
        print(f"        URL:     {cand.url}")
        if cand.link_text:
            print(f"        Text:    {cand.link_text}")
        print(f"        Section: {cand.section} | Rule: {cand.match_rule}")

    if diag.declared_count:
        print(f"  [Info] Article declares {diag.declared_count} supplementary item(s).")
    if diag.is_incomplete:
        print(f"  [Warning] Result may be incomplete: {diag.incomplete_reason}")

    if args.list_only:
        return

    # --- Step 3: Download files ---
    print(f"\n[3/3] Downloading {len(candidates)} file(s)...")
    success = 0
    for cand in candidates:
        result = download_file(cand, article_dir, cookies=cookies)
        if result:
            success += 1

    print(f"\nDone: {success}/{len(candidates)} files downloaded to {os.path.abspath(article_dir)}")


if __name__ == "__main__":
    main()
