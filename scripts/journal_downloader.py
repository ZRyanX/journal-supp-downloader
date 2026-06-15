#!/usr/bin/env python3
"""
Journal Supplementary Data Downloader
Uses Scrapling's StealthyFetcher to bypass Cloudflare and download
attachments/supplementary data from journal websites (Elsevier, Springer, etc.)

Usage:
    python journal_downloader.py <article_url>
    python journal_downloader.py <article_url> --output-dir ./downloads
    python journal_downloader.py <article_url> --headful  # show browser
    python journal_downloader.py <article_url> --proxy http://user:pass@host:port

Dependencies:
    pip install "scrapling[all]"
    scrapling install
"""

import argparse
import mimetypes
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin, urlparse

from scrapling.fetchers import Fetcher, StealthyFetcher


# --- Data file extensions (NOT images - those are article figures, not supplementary data) ---
DATA_EXTENSIONS = {
    ".xlsx", ".xls", ".csv", ".tsv", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".txt", ".json", ".xml", ".r", ".py", ".ipynb", ".m", ".nb",
    ".cif", ".pdb", ".mol", ".sdf", ".xyz", ".fasta", ".fa", ".gb",
    ".nii", ".nii.gz", ".dcm", ".h5", ".hdf5", ".mat", ".pkl", ".rda", ".sav",
    ".docx", ".doc", ".pptx", ".ppt",
}

# --- Elsevier-specific supplementary data patterns ---
MMC_PATTERN = re.compile(r"mmc\d+", re.IGNORECASE)  # e.g. mmc1.xlsx, mmc2.docx
SUPP_FILE_PATTERN = re.compile(
    r"(suppl?e?m?e?n?t?a?r?y|supp?_?|si_?|appendix|esm)",
    re.IGNORECASE,
)

# --- URL patterns to EXCLUDE (references, external sites, anchors, article figures) ---
EXCLUDE_URL_PATTERNS = [
    r"scholar\.google\.com",
    r"scholar_lookup",
    r"plu\.mx",
    r"relx\.com",
    r"elsevier\.com/(?!cdn)",
    r"#(m\d{4}|s\d{4}|!)",  # in-page anchor links
    r"/journal/.*/vol/",  # journal volume navigation
    r"doi\.org/journal/",  # journal-level DOI
    r"service\.elsevier\.com",
    r"linkedin\.com",
    r"facebook\.com",
    r"twitter\.com",
    r"/article/pii/\w+/pdfft\?md5=",  # reference PDF links (not supplementary)
    r"/science/article/pii/\w+/pdf\?",  # reference PDF links
    r"hub\.elsevier\.com",
    r"mendeley\.com",
    r"crossmark",
    r"crossref\.org",
    r"orcid\.org",
    r"doi\.org/10\.\d+/",  # DOI links to other articles (references)
]

# --- Article figure file patterns (gr1, gr2, ga1, fx1, etc.) ---
ARTICLE_FIGURE_PATTERN = re.compile(
    r"-(?:gr|ga|fx)\d+[a-z]?_(?:lrg|sml)?" ,
    re.IGNORECASE,
)


def is_excluded_url(url):
    """Check if a URL should be excluded (references, external sites, anchors, etc.)."""
    for pattern in EXCLUDE_URL_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False


def is_data_url(url):
    """Check if a URL points to a data file (based on extension or MMC naming)."""
    url_lower = url.lower()
    parsed = urlparse(url)
    path = parsed.path.lower()

    # Elsevier MMC (MultiMedia Component) = supplementary data
    if MMC_PATTERN.search(url_lower):
        return True

    # Supplementary keywords in URL path
    if SUPP_FILE_PATTERN.search(url_lower):
        if path.endswith(".pdf"):
            return True
        for ext in DATA_EXTENSIONS:
            if ext in url_lower:
                return True

    # Check for table keyword + data extension (or PDF)
    TABLE_FILE_PATTERN = re.compile(r"(?:table|tbl|附表)_?[a-z]?\.?\d+", re.IGNORECASE)
    if TABLE_FILE_PATTERN.search(url_lower):
        if path.endswith(".pdf") or any(ext in url_lower for ext in DATA_EXTENSIONS):
            return True

    # Direct data file extensions
    for ext in DATA_EXTENSIONS:
        if ext in url_lower:
            return True

    return False


def is_article_figure_url(url):
    """Check if a URL is an article figure (not supplementary data)."""
    return bool(ARTICLE_FIGURE_PATTERN.search(url))


def find_supplementary_links(page, base_url):
    """Find supplementary data links (not article figures, not references)."""
    links = set()
    current_pii = _extract_pii(base_url)

    # Strategy 1: targeted selectors for supplementary materials
    selectors = [
        "a[href*='mmc']",
        "a[href*='suppl']",
        "a[href*='supp']",
        "a[href*='esm']",
        "a[href$='.xlsx']",
        "a[href$='.xls']",
        "a[href$='.csv']",
        "a[href$='.zip']",
        "a[href$='.docx']",
        "a[href$='.doc']",
        "#appendix a",
        "#supplementary-material a",
        "#supplementary-data a",
        ".supplementary-data a",
        ".supplementary-material a",
        "[id*='supplementary'] a",
        "[id*='appendix'] a",
    ]

    for selector in selectors:
        try:
            for el in page.css(selector):
                href = el.attrib.get("href", "")
                if href:
                    full_url = urljoin(base_url, href)
                    if not is_excluded_url(full_url) and is_data_url(full_url):
                        if not is_article_figure_url(full_url):
                            links.add(full_url)
        except Exception:
            pass

    # Strategy 2: scan all <a> tags near "Appendix"/"Supplementary" headings
    try:
        # Find containers near supplementary headings
        for heading_sel in ["h2", "h3", "h4", "section", "div"]:
            try:
                headings = page.css(heading_sel)
                for h in headings:
                    text = (h.get_all_text() or "").lower()
                    if any(kw in text for kw in [
                        "appendix", "supplementary", "supplement",
                        "supporting information", "additional file",
                    ]):
                        # Get all links in/near this heading's section
                        parent = h.parent
                        if parent:
                            for a in parent.css("a"):
                                href = a.attrib.get("href", "")
                                if href:
                                    full_url = urljoin(base_url, href)
                                    if not is_excluded_url(full_url) and is_data_url(full_url):
                                        if not is_article_figure_url(full_url):
                                            links.add(full_url)
            except Exception:
                pass
    except Exception:
        pass

    # Strategy 3: direct MMC links from els-cdn
    if current_pii:
        try:
            all_links = page.css("a")
            for el in all_links:
                href = el.attrib.get("href", "")
                if href and f"mmc" in href.lower():
                    full_url = urljoin(base_url, href)
                    if not is_excluded_url(full_url):
                        links.add(full_url)
        except Exception:
            pass

    # Strategy 4: scan all <a> tags for "Table A1" etc. in anchor text
    try:
        table_text_pattern = re.compile(r"^\s*(?:Table|Tab\.|附表)\s*[a-zA-Z]?\.?\d+", re.IGNORECASE)
        all_links = page.css("a")
        for el in all_links:
            href = el.attrib.get("href", "")
            if href:
                full_url = urljoin(base_url, href)
                if not is_excluded_url(full_url):
                    link_text = (el.get_all_text() or "").strip()
                    is_table_text = bool(table_text_pattern.match(link_text))
                    
                    is_valid_table_link = False
                    if is_table_text:
                        parsed = urlparse(full_url)
                        path = parsed.path.lower()
                        if not any(path.endswith(html_ext) for html_ext in [".html", ".htm", ".php", ".asp", ".jsp"]):
                            is_valid_table_link = True
                            
                    if is_data_url(full_url) or is_valid_table_link:
                        if not is_article_figure_url(full_url):
                            links.add(full_url)
    except Exception:
        pass

    return sorted(links)


def _extract_pii(url):
    """Extract PII (Elsevier article ID) from URL."""
    m = re.search(r"pii/([A-Z]?\d+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{4,})[/-]", url)
    if m:
        return m.group(1)
    return None


def download_file(url, output_dir):
    """Download a single file using HTTP Fetcher (not browser-based). Returns path or None."""
    fname = os.path.basename(urlparse(url).path)
    if not fname or fname == "/":
        fname = f"download_{hash(url) % 100000}.bin"

    fname = sanitize_filename(fname)
    filepath = os.path.join(output_dir, fname)

    if os.path.exists(filepath):
        print(f"  [SKIP] {fname} (already exists)")
        return filepath

    print(f"  [FETCH] {fname} ...", end=" ", flush=True)
    try:
        resp = Fetcher.get(url, stealthy_headers=True)

        if resp.status >= 400:
            print(f"HTTP {resp.status}")
            return None

        # Try to get filename from Content-Disposition
        cd = resp.headers.get("content-disposition", "")
        if cd:
            cd_match = re.search(r'filename[^;=\n]*=((["\']).*?\2|[^;\n]*)', cd)
            if cd_match:
                fname = cd_match.group(1).strip('"\'')
                fname = sanitize_filename(fname)
                filepath = os.path.join(output_dir, fname)
                if os.path.exists(filepath):
                    print(f"[SKIP] {fname} (already exists)")
                    return filepath

        with open(filepath, "wb") as f:
            f.write(resp.body)

        size_kb = len(resp.body) / 1024
        print(f"OK ({size_kb:.1f} KB)")
        return filepath

    except Exception as e:
        print(f"FAIL: {e}")
        return None


def sanitize_filename(name):
    """Remove or replace problematic filename characters."""
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = name.strip(". ")
    return name or "download"


def extract_article_title(page):
    """Extract article title from the page, sanitized for use as folder name."""
    title = page.css('title::text').get()
    if not title:
        return "unknown_article"

    # Remove common journal suffixes
    for suffix in [
        " - ScienceDirect", " - SpringerLink", " - Springer",
        " | Nature", " | PNAS", " - Wiley Online Library",
        " - PubMed", " - PubMed Central", " - PMC",
        " | Oxford Academic", " - IEEE Xplore",
    ]:
        title = title.replace(suffix, "")

    # Trim and sanitize
    title = title.strip()
    title = re.sub(r'[\\/*?:"<>|]', "_", title)
    title = re.sub(r'\s+', ' ', title)
    # Truncate if too long (macOS max filename ~255 chars, keep it short)
    if len(title) > 120:
        title = title[:120]
    title = title.rstrip(". ")
    return title or "unknown_article"


def guess_filename_from_url(url):
    """Try to derive a readable filename from the URL."""
    path = urlparse(url).path
    name = os.path.basename(path)
    if name and name != "/":
        return name
    parts = [p for p in path.split("/") if p]
    if parts:
        return parts[-1] + ".bin"
    return f"download_{abs(hash(url))}.bin"


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
    parser.add_argument("url", help="URL of the journal article page")
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
        help="Only list found links, don't download"
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    solve_cf = not args.no_cloudflare and args.solve_cloudflare

    print(f"Target: {args.url}")
    print(f"Output: {os.path.abspath(args.output_dir)}")
    print(f"Cloudflare bypass: {'ON' if solve_cf else 'OFF'}")
    print(f"Mode: {'Headful' if args.headful else 'Headless'}")
    if args.proxy:
        print(f"Proxy: {args.proxy}")
    print()

    # --- Step 1: Fetch the article page ---
    print("[1/3] Fetching article page (bypassing protections)...")
    page = StealthyFetcher.fetch(
        args.url,
        headless=not args.headful,
        solve_cloudflare=solve_cf,
        block_webrtc=True,
        hide_canvas=True,
        real_chrome=args.real_chrome,
        network_idle=True,
        timeout=args.timeout,
        proxy=args.proxy,
        wait_selector=args.wait_selector,
        google_search=False,
    )

    # Extract and sanitize article title for folder name
    raw_title = page.css('title::text').get() or "N/A"
    article_dirname = extract_article_title(page)
    article_dir = os.path.join(args.output_dir, article_dirname)
    os.makedirs(article_dir, exist_ok=True)

    print(f"  Status: {page.status}")
    print(f"  Title:  {raw_title}")
    print(f"  Folder: {article_dirname}/")

    # --- Step 2: Find supplementary links ---
    print("\n[2/3] Scanning for supplementary data links...")
    links = find_supplementary_links(page, args.url)

    if not links:
        print("  No supplementary data links found.")
        print("  Try running with --headful and --no-cloudflare to debug.")
        return

    print(f"  Found {len(links)} potential supplementary file(s):")
    for url in links:
        fname = guess_filename_from_url(url)
        print(f"    - {fname}")
        print(f"      {url}")

    if args.list_only:
        return

    # --- Step 3: Download files ---
    print(f"\n[3/3] Downloading {len(links)} file(s)...")
    success = 0
    for url in links:
        result = download_file(url, article_dir)
        if result:
            success += 1

    print(f"\nDone: {success}/{len(links)} files downloaded to {os.path.abspath(article_dir)}")


if __name__ == "__main__":
    main()
