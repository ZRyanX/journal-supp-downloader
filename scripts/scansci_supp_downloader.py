#!/usr/bin/env python3
"""
ScanSci PDF Integrated Supplementary Downloader v3
===================================================
Multi-tier approach for downloading supplementary materials from Elsevier
and other publishers:

  Tier A (instant)  – Elsevier API campus-IP (API Key + 机构IP, ~3s total)
  Tier 0 (fastest)  – Elsevier CDN brute-force (no auth, ~1s per file)
  Tier 1 (fast)     – Publisher direct + cookies (requests, ~3s)
  Tier 2 (moderate) – Scrapling StealthyFetcher (bypasses Cloudflare, ~15s)

Integrates with scansci-pdf config (campus network, API keys, cookies).
"""

import argparse
import os
import re
import sys
import json
import time
import subprocess
import urllib3
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Suppress SSL warnings for expired WebVPN certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Ensure scansci_pdf is in path
sys.path.append(str(Path(__file__).resolve().parents[2] / "scansci-pdf" / "src"))

import requests

# Try importing scrapling
try:
    from scrapling.fetchers import Fetcher, StealthyFetcher
    HAS_SCRAPLING = True
except ImportError:
    HAS_SCRAPLING = False

# Try importing scansci_pdf components
try:
    import scansci_pdf
    from scansci_pdf.config import load_config
    from scansci_pdf.browser_cookies import inject_cookies, load_saved_cookies
    from scansci_pdf.sources.vpnsci import (
        convert_url,
        vpnsci_is_configured,
        _get_webvpn_base,
        _load_cookies as load_webvpn_cookies,
        vpnsci_cookie_path,
    )
    HAS_SCANSCI = True
except ImportError as e:
    HAS_SCANSCI = False
    print(f"Warning: scansci_pdf not found. Running in standalone mode. Error: {e}")

# Data file extensions (supplementary files)
DATA_EXTENSIONS = {
    ".xlsx", ".xls", ".csv", ".tsv", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".txt", ".json", ".xml", ".r", ".py", ".ipynb", ".m", ".nb",
    ".cif", ".pdb", ".mol", ".sdf", ".xyz", ".fasta", ".fa", ".gb",
    ".nii", ".nii.gz", ".dcm", ".h5", ".hdf5", ".mat", ".pkl", ".rda", ".sav",
    ".docx", ".doc", ".pptx", ".ppt",
}

MMC_PATTERN = re.compile(r"mmc\d+", re.IGNORECASE)
SUPP_FILE_PATTERN = re.compile(r"(suppl?e?m?e?n?t?a?r?y|supp?_?|si_?|appendix|esm)", re.IGNORECASE)
EXCLUDE_URL_PATTERNS = [
    r"scholar\.google\.com", r"scholar_lookup", r"plu\.mx", r"relx\.com",
    r"elsevier\.com/(?!cdn)", r"#(m\d{4}|s\d{4}|!)", r"/journal/.*/vol/",
    r"doi\.org/journal/", r"service\.elsevier\.com", r"linkedin\.com",
    r"facebook\.com", r"twitter\.com", r"hub\.elsevier\.com", r"mendeley\.com",
    r"crossmark", r"crossref\.org", r"orcid\.org", r"doi\.org/10\.\d+/",
]
ARTICLE_FIGURE_PATTERN = re.compile(r"-(?:gr|ga|fx)\d+[a-z]?_(?:lrg|sml)?", re.IGNORECASE)


# ── Helpers ──────────────────────────────────────────────────────────────────

def resolve_doi_url(doi_or_url):
    """Resolve a DOI or URL to its final destination, using curl.exe as a robust fallback for SSL issues."""
    if doi_or_url.startswith("http"):
        return doi_or_url

    doi = doi_or_url.strip()
    url = f"https://doi.org/{doi}"
    print(f"Resolving DOI: {doi} via doi.org ...")

    # Method 1: Requests (HEAD)
    try:
        resp = requests.head(url, allow_redirects=True, timeout=15, verify=False,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        if resp.url and resp.url.startswith("http"):
            print(f"  [requests] Resolved to: {resp.url}")
            return resp.url
    except Exception:
        pass

    # Method 2: Requests (GET, stream)
    try:
        resp = requests.get(url, allow_redirects=True, timeout=15, stream=True, verify=False,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        resp.close()
        if resp.url and resp.url.startswith("http"):
            print(f"  [requests] Resolved to: {resp.url}")
            return resp.url
    except Exception:
        pass

    # Method 3: curl.exe (using subprocess, extremely robust for SSL issues on Windows)
    try:
        cmd = ["curl.exe", "-w", "%{url_effective}", "-o", "NUL", "-s", "-L", url]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = res.stdout.strip()
        if out.startswith("http"):
            print(f"  [curl.exe] Resolved to: {out}")
            return out
    except Exception:
        pass

    # Method 4: Scrapling Fetcher (if available)
    if HAS_SCRAPLING:
        try:
            from scrapling import Fetcher
            r = Fetcher.get(url, stealthy_headers=True, timeout=15000)
            if r.url and r.url.startswith("http"):
                print(f"  [scrapling] Resolved to: {r.url}")
                return r.url
        except Exception:
            pass

    # Method 5: Publisher direct constructor heuristics (fallback if everything else fails)
    # Springer / Nature:
    if "10.1007" in doi or "10.1038" in doi:
        fallback = f"https://link.springer.com/article/{doi}"
        print(f"  [fallback] Springer/Nature URL: {fallback}")
        return fallback

    print(f"  [warning] Resolution failed, using default: {url}")
    return url


def is_excluded_url(url):
    for pattern in EXCLUDE_URL_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False


def is_data_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    
    # 1. First check if the path ends with any data extension
    for ext in DATA_EXTENSIONS:
        if path.endswith(ext):
            return True
            
    # Check for MDPI-style /s1, /s2 extensionless paths
    if re.search(r'/s\d+$', path) or re.search(r'/s\d+/', path):
        return True
            
    # 2. Check general URL string matching but be strict about extension boundaries
    # to avoid false positives (e.g. matching .mc for .m)
    url_lower = url.lower()
    if MMC_PATTERN.search(url_lower):
        return True

    # Check for supplementary keyword + data extension (or PDF)
    if SUPP_FILE_PATTERN.search(url_lower):
        if path.endswith(".pdf"):
            return True
        for ext in DATA_EXTENSIONS:
            pattern = re.escape(ext) + r'(?:[^a-z0-9]|$)'
            if re.search(pattern, url_lower):
                return True

    # Check for table keyword + data extension (or PDF)
    # e.g., table_a1.pdf, table-s1.pdf, table1.pdf, tbl_s2.pdf
    TABLE_FILE_PATTERN = re.compile(r"(?:table|tbl|附表)_?[a-z]?\.?\d+", re.IGNORECASE)
    if TABLE_FILE_PATTERN.search(url_lower):
        if path.endswith(".pdf") or any(path.endswith(ext) for ext in DATA_EXTENSIONS):
            return True

    # General fallback for any data extension in URL (with strict boundary check)
    for ext in DATA_EXTENSIONS:
        pattern = re.escape(ext) + r'(?:[^a-z0-9]|$)'
        if re.search(pattern, url_lower):
            return True

    return False


def is_article_figure_url(url):
    return bool(ARTICLE_FIGURE_PATTERN.search(url))


def sanitize_filename(name):
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    return name.strip(". ") or "download"


def extract_article_title(html_content, default="unknown_article"):
    m = re.search(r'<title[^>]*>(.*?)</title>', html_content, re.IGNORECASE | re.DOTALL)
    if not m:
        return default
    title = m.group(1).strip()
    for suffix in [
        " - ScienceDirect", " - SpringerLink", " - Springer",
        " | Nature", " | PNAS", " - Wiley Online Library",
        " - PubMed", " - PubMed Central", " - PMC",
        " | Oxford Academic", " - IEEE Xplore",
    ]:
        title = title.replace(suffix, "")
    title = title.strip()
    title = re.sub(r'[\\/*?:"<>|]', "_", title)
    title = re.sub(r'\s+', ' ', title)
    if len(title) > 120:
        title = title[:120]
    return title.rstrip(". ") or default


def extract_pii_from_url(url):
    """Extract Elsevier PII from a URL or DOI string."""
    m = re.search(r"pii/([A-Z0-9]+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def normalize_url(url):
    """Rewrite linkinghub.elsevier.com → www.sciencedirect.com."""
    if "linkinghub.elsevier.com/retrieve/pii/" in url:
        pii = url.split("/retrieve/pii/")[-1]
        return f"https://www.sciencedirect.com/science/article/pii/{pii}"
    return url


def find_supplementary_links(html_content, base_url):
    """Scan HTML content for supplementary material links."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html_content, 'html.parser')
    links = set()

    # Table pattern like: Table A1, Table S1, Table A.1, Tab A1, 附表1
    table_text_pattern = re.compile(r"^\s*(?:Table|Tab\.|附表)\s*[a-zA-Z]?\.?\d+", re.IGNORECASE)

    # 1. Look for hrefs matching mmc, suppl, etc. or having "Table A1" in anchor text
    for a in soup.find_all('a', href=True):
        href = a['href']
        full_url = urljoin(base_url, href)
        if is_excluded_url(full_url):
            continue
            
        link_text = a.get_text().strip()
        
        # Check if the URL is a data URL (Excel, zip, etc. or table PDF)
        is_data = is_data_url(full_url)
        
        # Or check if link text is "Table A1" etc.
        is_table_text = bool(table_text_pattern.match(link_text))
        
        # If the text indicates a table, and the URL is a potential file download (not HTML page)
        is_valid_table_link = False
        if is_table_text:
            parsed = urlparse(full_url)
            path = parsed.path.lower()
            if not any(path.endswith(html_ext) for html_ext in [".html", ".htm", ".php", ".asp", ".jsp"]):
                is_valid_table_link = True

        if is_data or is_valid_table_link:
            if not is_article_figure_url(full_url):
                links.add(full_url)

    # 2. Look for headings with supplementary keywords
    for heading in soup.find_all(['h2', 'h3', 'h4', 'section', 'div']):
        text = heading.get_text().lower()
        if any(kw in text for kw in ["appendix", "supplementary", "supplement", "supporting information", "additional file"]):
            parent = heading.parent
            if parent:
                for a in parent.find_all('a', href=True):
                    href = a['href']
                    full_url = urljoin(base_url, href)
                    if is_excluded_url(full_url):
                        continue
                        
                    link_text = a.get_text().strip()
                    is_data = is_data_url(full_url)
                    is_table_text = bool(table_text_pattern.match(link_text))
                    
                    is_valid_table_link = False
                    if is_table_text:
                        parsed = urlparse(full_url)
                        path = parsed.path.lower()
                        if not any(path.endswith(html_ext) for html_ext in [".html", ".htm", ".php", ".asp", ".jsp"]):
                            is_valid_table_link = True

                    if is_data or is_valid_table_link:
                        if not is_article_figure_url(full_url):
                            links.add(full_url)

    return sorted(links)


# ── Cookie Loading ───────────────────────────────────────────────────────────

def load_vpnsci_cookies_unified(config):
    """Load WebVPN cookies from either vpnsci-cookies.json or vpnsci_cookies.json."""
    if not HAS_SCANSCI:
        return {}
    from scansci_pdf.config import DEFAULT_CONFIG
    cache_dir = Path(config.get("cache_dir", DEFAULT_CONFIG["cache_dir"])).expanduser()

    paths = [
        cache_dir / "vpnsci-cookies.json",
        cache_dir / "vpnsci_cookies.json",
    ]

    jar = {}
    for path in paths:
        if path.exists():
            try:
                cookies = json.loads(path.read_text(encoding="utf-8"))
                for c in cookies:
                    name = c.get("name")
                    value = c.get("value")
                    if name and value is not None:
                        jar[name] = value
            except Exception:
                pass
    return jar


def load_all_scansci_cookies(config):
    """Load and merge all scansci-pdf cookies (WebVPN, publisher, CARSI) as Playwright list."""
    cookies = []

    # 1. WebVPN cookies
    if config.get("vpnsci_enabled"):
        try:
            vpn_cookies = load_vpnsci_cookies_unified(config)
            base_url = _get_webvpn_base(config) if HAS_SCANSCI else ""
            domain = base_url.split("://")[-1].split(":")[0] if "://" in base_url else base_url
            for name, value in vpn_cookies.items():
                cookies.append({
                    "name": name, "value": value, "domain": domain,
                    "path": "/", "secure": False, "httpOnly": False,
                })
        except Exception:
            pass

    # 2. Publisher cookies
    try:
        for c in load_saved_cookies(config):
            cookies.append({
                "name": c["name"], "value": c["value"],
                "domain": c.get("domain", ""), "path": c.get("path", "/"),
                "secure": c.get("secure", False), "httpOnly": c.get("httpOnly", False),
            })
    except Exception:
        pass

    # 3. CARSI cookies
    cache_dir = Path(config.get("cache_dir", str(Path.home() / ".scansci-pdf" / "cache")))
    carsi_dir = cache_dir / "carsi_cookies"
    if carsi_dir.is_dir():
        for cf in carsi_dir.glob("*.json"):
            try:
                for c in json.loads(cf.read_text(encoding="utf-8")):
                    cookies.append({
                        "name": c.get("name", ""), "value": c.get("value", ""),
                        "domain": c.get("domain", ""), "path": c.get("path", "/"),
                        "secure": c.get("secure", False), "httpOnly": c.get("httpOnly", False),
                    })
            except Exception:
                pass

    # Deduplicate
    deduped = {}
    for c in cookies:
        if not c.get("name") or not c.get("domain"):
            continue
        key = (c["name"], c["domain"], c.get("path", "/"))
        deduped[key] = c
    return list(deduped.values())


# ── Tier A: Elsevier API Campus-IP ──────────────────────────────────────────

def try_elsevier_api_campus(doi, pii, output_dir, config):
    """
    On campus network, the Elsevier Article Retrieval API returns the full
    article XML/JSON. We parse it to extract the precise mmc file references
    (including correct extensions), then download directly from CDN.

    This is faster than CDN brute-force because:
    - No extension guessing (API tells us mmc1.xlsx, mmc4.docx, etc.)
    - Single API call to discover ALL supplements
    - CDN files are public, download is instant

    Requires: campus network IP + elsevier_api_key in scansci-pdf config.
    """
    if not config.get("is_campus_network") and not config.get("elsevier_insttoken"):
        return []

    api_key = config.get("elsevier_api_key", "")
    if not api_key:
        return []

    print(f"\n[Tier A] Elsevier API campus-IP access (DOI: {doi}) ...")
    t0 = time.time()

    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    insttoken = config.get("elsevier_insttoken", "")
    if insttoken:
        headers["X-ELS-InstToken"] = insttoken

    # Resolve DOI to clean format
    doi_clean = doi
    if not doi_clean.startswith("10."):
        m = re.search(r"10\.\d{4,}/[^\s?&]+", doi)
        if m:
            doi_clean = m.group(0)
        else:
            print("  [Tier A] Cannot extract DOI, skipping.")
            return []

    api_url = f"https://api.elsevier.com/content/article/doi/{doi_clean}"
    try:
        resp = requests.get(api_url, headers=headers, timeout=30)
    except Exception as e:
        print(f"  [Tier A] API request failed: {e}")
        return []

    if resp.status_code != 200:
        if resp.status_code == 401:
            print(f"  [Tier A] HTTP 401 — API key rejected (not on campus network?).")
        elif resp.status_code == 403:
            print(f"  [Tier A] HTTP 403 — Access denied (API quota or IP not recognized).")
        elif resp.status_code == 404:
            print(f"  [Tier A] HTTP 404 — Article not in API index yet.")
        else:
            print(f"  [Tier A] HTTP {resp.status_code}")
        return []

    elapsed_api = time.time() - t0
    print(f"  API responded in {elapsed_api:.1f}s")

    # Parse the response to find mmc references
    try:
        data = resp.json()
        article = data.get("full-text-retrieval-response", {})
    except Exception:
        print("  [Tier A] Failed to parse API JSON response.")
        return []

    # Extract PII from API response if not provided
    if not pii:
        core = article.get("coredata", {})
        raw_pii = core.get("pii", "")
        # API returns formatted PII like 'S0169-1368(26)00248-9', convert to URL form
        pii = re.sub(r"[^A-Z0-9]", "", raw_pii, flags=re.IGNORECASE)
        if pii:
            print(f"  PII from API: {pii}")

    if not pii:
        print("  [Tier A] Cannot determine PII, skipping CDN download.")
        return []

    # Scan originalText for mmc file references with extensions
    original_text = article.get("originalText", "")
    if isinstance(original_text, dict):
        original_text = json.dumps(original_text)
    elif not isinstance(original_text, str):
        original_text = str(original_text)
    # Pattern: mmc{N}.{ext} in the XML
    mmc_files = set()
    # Look for e-component references like: 1-s2.0-{PII}-mmc{N}.{ext}
    pattern = re.compile(r"mmc(\d+)\.(\w+)", re.IGNORECASE)
    for m in pattern.finditer(original_text):
        num = m.group(1)
        ext = m.group(2).lower()
        if ext in {"xlsx", "xls", "csv", "docx", "doc", "pdf", "zip", "pptx",
                   "txt", "gz", "tar", "rar", "7z", "xml", "json"}:
            mmc_files.add((int(num), ext))

    if not mmc_files:
        # Fallback: just find mmc{N} references without extensions
        mmc_nums = set()
        for m in re.finditer(r"mmc(\d+)", original_text, re.IGNORECASE):
            mmc_nums.add(int(m.group(1)))
        if mmc_nums:
            print(f"  Found mmc references without extensions: {sorted(mmc_nums)}")
            print(f"  Falling through to CDN brute-force for extension detection.")
        return []

    mmc_sorted = sorted(mmc_files)
    print(f"  Found {len(mmc_sorted)} supplement(s) from API: {['mmc'+str(n)+'.'+e for n,e in mmc_sorted]}")

    # Download from CDN
    found_files = []
    cdn_base = f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}"
    dl_session = requests.Session()
    dl_session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    for num, ext in mmc_sorted:
        fname = f"mmc{num}.{ext}"
        url = f"{cdn_base}-{fname}"
        filepath = os.path.join(output_dir, fname)

        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            print(f"  [SKIP] {fname} (already exists)")
            found_files.append(filepath)
            continue

        print(f"  [API→CDN] {fname} ...", end=" ", flush=True)
        try:
            dl_resp = dl_session.get(url, timeout=60, stream=True)
            if dl_resp.status_code == 200:
                # Check Content-Disposition for real filename
                cd = dl_resp.headers.get("content-disposition", "")
                if cd:
                    cd_match = re.search(r'filename[^;=\n]*=(["\']?)(.+?)\1(;|$)', cd)
                    if cd_match:
                        real_name = sanitize_filename(cd_match.group(2).strip())
                        filepath = os.path.join(output_dir, real_name)
                        fname = real_name

                with open(filepath, "wb") as f:
                    for chunk in dl_resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                actual_size = os.path.getsize(filepath)
                print(f"OK ({actual_size / 1024:.1f} KB) → {fname}")
                found_files.append(filepath)
            else:
                print(f"FAIL (HTTP {dl_resp.status_code})")
        except Exception as e:
            print(f"FAIL ({e})")

    total_time = time.time() - t0
    if found_files:
        print(f"  [Tier A] ✓ {len(found_files)} file(s) downloaded in {total_time:.1f}s (API + CDN).")
    else:
        print(f"  [Tier A] No files downloaded.")
    return found_files


# ── Tier 0: Elsevier CDN Brute-Force ────────────────────────────────────────

def try_elsevier_cdn_brute_force(pii, output_dir, max_mmc=15):
    """
    Elsevier hosts supplement files on a public CDN at:
      https://ars.els-cdn.com/content/image/1-s2.0-{PII}-mmc{N}.{ext}
    No authentication is required. This is the fastest method.
    """
    if not pii:
        return []

    base = f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}"
    exts = ["xlsx", "xls", "csv", "docx", "doc", "pdf", "zip", "pptx", "txt"]
    found_files = []
    consecutive_misses = 0

    print(f"\n[Tier 0] Scanning Elsevier CDN for PII={pii} ...")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    for i in range(1, max_mmc + 1):
        found_this_mmc = False
        for ext in exts:
            url = f"{base}-mmc{i}.{ext}"
            try:
                resp = session.head(url, timeout=10, allow_redirects=True)
                if resp.status_code == 200:
                    size = int(resp.headers.get("content-length", 0))
                    ct = resp.headers.get("content-type", "")
                    if size < 100:
                        continue  # likely an error page
                    fname = f"mmc{i}.{ext}"
                    filepath = os.path.join(output_dir, fname)

                    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                        print(f"  [SKIP] {fname} (already exists)")
                        found_files.append(filepath)
                        found_this_mmc = True
                        break

                    # Download the file
                    print(f"  [CDN]  mmc{i}.{ext} ({size / 1024:.1f} KB) ...", end=" ", flush=True)
                    dl_resp = session.get(url, timeout=30, stream=True)
                    if dl_resp.status_code == 200:
                        # Check Content-Disposition for real filename
                        cd = dl_resp.headers.get("content-disposition", "")
                        if cd:
                            cd_match = re.search(r'filename[^;=\n]*=((["\']).*?\2|[^;\n]*)', cd)
                            if cd_match:
                                real_name = cd_match.group(1).strip('"\'')
                                real_name = sanitize_filename(real_name)
                                filepath = os.path.join(output_dir, real_name)
                                fname = real_name

                        with open(filepath, "wb") as f:
                            for chunk in dl_resp.iter_content(chunk_size=8192):
                                if chunk:
                                    f.write(chunk)
                        actual_size = os.path.getsize(filepath)
                        print(f"OK ({actual_size / 1024:.1f} KB) → {fname}")
                        found_files.append(filepath)
                        found_this_mmc = True
                        break
                    else:
                        print(f"FAIL (HTTP {dl_resp.status_code})")
            except requests.exceptions.Timeout:
                continue
            except Exception:
                continue

        if found_this_mmc:
            consecutive_misses = 0
        else:
            consecutive_misses += 1
            if consecutive_misses >= 3:
                break  # Likely no more supplement files

    if found_files:
        print(f"  [Tier 0] Found {len(found_files)} file(s) via CDN.")
    else:
        print(f"  [Tier 0] No files found on CDN.")
    return found_files


# ── Tier 1/2: Page Scrape + Link Download ────────────────────────────────────

def download_file(url, output_dir, session=None, cookies=None):
    """Download a single file, trying requests then Scrapling as fallback."""
    fname = os.path.basename(urlparse(url).path)
    if not fname or fname == "/":
        fname = f"download_{hash(url) % 100000}.bin"
    fname = sanitize_filename(fname)
    filepath = os.path.join(output_dir, fname)

    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        print(f"  [SKIP] {fname} (already exists)")
        return filepath

    print(f"  [FETCH] {fname} ...", end=" ", flush=True)

    # Method 1: requests (with cookies, verify=False for WebVPN SSL issues)
    if session:
        try:
            resp = session.get(url, timeout=30, stream=True, verify=False)
            if resp.status_code < 400:
                cd = resp.headers.get("content-disposition", "")
                if cd:
                    cd_match = re.search(r'filename[^;=\n]*=((["\']).*?\2|[^;\n]*)', cd)
                    if cd_match:
                        fname = sanitize_filename(cd_match.group(1).strip('"\''))
                        filepath = os.path.join(output_dir, fname)
                        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                            print(f"[SKIP] {fname} (already exists)")
                            return filepath

                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                size_kb = os.path.getsize(filepath) / 1024
                if size_kb > 1.0: # Check if it's a real file (not a tiny error page)
                    print(f"OK ({size_kb:.1f} KB)")
                    return filepath
                else:
                    os.remove(filepath)
        except Exception:
            pass

    # Method 2: Scrapling Fetcher (basic HTTP client)
    if HAS_SCRAPLING:
        try:
            sc_args = {}
            if cookies:
                sc_args["cookies"] = cookies
            resp = Fetcher.get(url, stealthy_headers=True, timeout=30000, **sc_args)
            if resp.status < 400 and resp.body and len(resp.body) > 1000:
                with open(filepath, "wb") as f:
                    f.write(resp.body)
                size_kb = len(resp.body) / 1024
                print(f"OK (via Scrapling, {size_kb:.1f} KB)")
                return filepath
        except Exception:
            pass

    # Method 3: Scrapling StealthySession + Playwright sync download fallback (for Cloudflare/Akamai WAF bypass)
    if HAS_SCRAPLING:
        try:
            from scrapling.fetchers import StealthySession
            session_stealth = StealthySession(
                headless=True,
                solve_cloudflare=True,
                network_idle=True,
                timeout=60000
            )
            session_stealth.start()
            context = session_stealth.context
            page = context.new_page()
            
            # Establish cookies on base domain
            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}/"
            try:
                page.goto(base_url, wait_until="commit")
            except Exception:
                pass
                
            # Perform download expecting download event
            with page.expect_download(timeout=60000) as download_info:
                try:
                    page.goto(url, wait_until="commit")
                except Exception as e:
                    if "Download is starting" not in str(e):
                        raise
            download = download_info.value
            download.save_as(filepath)
            session_stealth.close()
            
            size_kb = os.path.getsize(filepath) / 1024
            if size_kb > 1.0:
                print(f"OK (via StealthySession, {size_kb:.1f} KB)")
                return filepath
            else:
                os.remove(filepath)
        except Exception:
            try:
                session_stealth.close()
            except Exception:
                pass

    # Method 4: curl.exe (robust fallback for SSL/proxy negotiation issues on Windows)
    try:
        cmd = ["curl.exe", "-s", "-L", "-o", filepath, url]
        res = subprocess.run(cmd, timeout=120)
        if res.returncode == 0 and os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
            size_kb = os.path.getsize(filepath) / 1024
            print(f"OK (via curl.exe, {size_kb:.1f} KB)")
            return filepath
        elif os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass

    print("FAIL")
    return None


def fetch_page_html(url, session=None, play_cookies=None, headful=False, force_browser=False):
    """Fetch article page HTML, trying requests then Scrapling StealthyFetcher."""
    html_content = ""

    # Method 1: requests session (fast, but often blocked by Cloudflare)
    if session and not force_browser:
        try:
            resp = session.get(url, timeout=30, verify=False)
            if resp.status_code < 400:
                # Check if it's a real page (not Cloudflare challenge)
                text = resp.text
                cf_signals = ["challenge-platform", "cf-browser-verification", "just a moment", "Checking your browser"]
                if not any(sig.lower() in text.lower() for sig in cf_signals):
                    return text, resp.status_code
                else:
                    print("    (Cloudflare detected, falling through to browser...)")
        except Exception:
            pass

    # Method 2: Scrapling StealthyFetcher (bypasses Cloudflare)
    if HAS_SCRAPLING:
        try:
            if force_browser:
                print("    (Forcing browser rendering to execute client-side JS...)")
            sc_args = {
                "headless": not headful,
                "solve_cloudflare": True,
                "network_idle": True,
                "timeout": 60000,
            }
            if play_cookies:
                sc_args["cookies"] = play_cookies
            page = StealthyFetcher.fetch(url, **sc_args)
            html_content = str(page.html_content)
            return html_content, page.status
        except Exception as e:
            print(f"    StealthyFetcher error: {e}")

    return html_content, 0


def _finish_success(tier_name, files, article_dir, doi, url, args, config):
    """Common success handler: print summary, rename folder, check for extra supplements."""
    print(f"\n✓ {tier_name} Success: {len(files)} supplementary file(s) downloaded.")
    print(f"  Output: {os.path.abspath(article_dir)}")

    # Try to get article title for better folder naming
    try:
        title = _quick_title_lookup(doi, url)
        if title and title != "supplements":
            new_dir = os.path.join(args.output_dir, sanitize_filename(title))
            if not os.path.exists(new_dir):
                os.rename(article_dir, new_dir)
                article_dir = new_dir
                print(f"  Renamed to: {os.path.abspath(article_dir)}")
    except Exception:
        pass

    # Try scraping the page to find any additional supplements not on CDN
    print("\n  Checking for additional supplements via page scrape...")
    _try_page_scrape_supplements(url, article_dir, config, args, files)


# ── Main Logic ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ScanSci Integrated Supplementary Downloader v3")
    parser.add_argument("url_or_doi", help="DOI or landing page URL of the paper")
    parser.add_argument("-o", "--output-dir", default="./journal_downloads", help="Output directory")
    parser.add_argument("--no-vpn", action="store_true", help="Disable WebVPN proxying")
    parser.add_argument("--no-cookies", action="store_true", help="Disable scansci-pdf cookies injection")
    parser.add_argument("--no-api", action="store_true", help="Skip Elsevier API campus-IP tier")
    parser.add_argument("--headful", action="store_true", help="Show browser window during scraping")
    parser.add_argument("--skip-cdn", action="store_true", help="Skip CDN brute-force (Tier 0)")
    parser.add_argument("--max-mmc", type=int, default=15, help="Max mmc number to scan in CDN brute-force")
    args = parser.parse_args()

    config = load_config() if HAS_SCANSCI else {}
    os.makedirs(args.output_dir, exist_ok=True)

    # ── Step 1: Resolve DOI / URL ─────────────────────────────────────────
    doi = args.url_or_doi
    url = resolve_doi_url(doi)
    url = normalize_url(url)
    print(f"  Normalized:  {url}")

    # Extract PII for Elsevier CDN
    pii = extract_pii_from_url(url)
    if pii:
        print(f"  Elsevier PII: {pii}")

    # ── Step 2: Tier A — Elsevier API Campus-IP (fastest if on campus) ────
    article_dir = os.path.join(args.output_dir, "supplements")
    os.makedirs(article_dir, exist_ok=True)

    api_files = []
    if not args.no_api and "10.1016" in (doi if doi.startswith("10.") else url):
        api_files = try_elsevier_api_campus(doi, pii, article_dir, config)

    if api_files:
        _finish_success("Tier A (API+CDN)", api_files, article_dir, doi, url, args, config)
        return

    # ── Step 3: Tier 0 — CDN Brute-Force (fast, no auth) ─────────────────
    cdn_files = []
    if pii and not args.skip_cdn:
        cdn_files = try_elsevier_cdn_brute_force(pii, article_dir, args.max_mmc)

    if cdn_files:
        _finish_success("Tier 0 (CDN)", cdn_files, article_dir, doi, url, args, config)
        return

    # ── Step 4: Tier 1/2 — Page Scrape + Link Download ───────────────────
    print(f"\n[Tier 1/2] Fetching article page for supplement links...")

    # Setup requests session with all cookies
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    })
    session.verify = False  # WebVPN SSL cert is expired

    if HAS_SCANSCI and not args.no_cookies:
        inject_cookies(session, config)
        # Inject CARSI cookies
        cache_dir = Path(config.get("cache_dir", str(Path.home() / ".scansci-pdf" / "cache")))
        carsi_dir = cache_dir / "carsi_cookies"
        if carsi_dir.is_dir():
            for cf in carsi_dir.glob("*.json"):
                try:
                    for c in json.loads(cf.read_text(encoding="utf-8")):
                        session.cookies.set(
                            c.get("name", ""), c.get("value", ""),
                            domain=c.get("domain", ""), path=c.get("path", "/"),
                        )
                except Exception:
                    pass

    play_cookies = load_all_scansci_cookies(config) if HAS_SCANSCI and not args.no_cookies else []

    # Fetch the page
    html_content, status = fetch_page_html(url, session, play_cookies, args.headful)
    if not html_content:
        print("  ERROR: Failed to fetch article page.")
        print("  Possible causes: Cloudflare block, network issue, or authentication required.")
        return

    print(f"  Status: {status}")

    # Extract title
    article_dirname = extract_article_title(html_content, default="downloaded_article")
    article_dir = os.path.join(args.output_dir, article_dirname)
    os.makedirs(article_dir, exist_ok=True)
    print(f"  Title:  {article_dirname}")
    print(f"  Folder: {article_dir}")

    # Scan for links
    print("\n  Scanning for supplementary data links...")
    links = find_supplementary_links(html_content, url)
    
    # Fallback to browser rendering if requests succeeded but found no links
    if not links and HAS_SCRAPLING and session:
        print("  No links found in raw HTML. Falling back to browser rendering (JS execution)...")
        html_content_browser, status_browser = fetch_page_html(url, session, play_cookies, args.headful, force_browser=True)
        if html_content_browser:
            links_browser = find_supplementary_links(html_content_browser, url)
            if links_browser:
                links = links_browser
                html_content = html_content_browser
                print(f"  Found {len(links)} link(s) after browser rendering.")
                
                # Re-extract title in case the JS updated it
                article_dirname = extract_article_title(html_content, default=article_dirname)
                new_article_dir = os.path.join(args.output_dir, article_dirname)
                if new_article_dir != article_dir:
                    os.makedirs(new_article_dir, exist_ok=True)
                    try:
                        os.rmdir(article_dir) # remove the old empty directory
                    except Exception:
                        pass
                    article_dir = new_article_dir
                    print(f"  Updated Title:  {article_dirname}")
                    print(f"  Updated Folder: {article_dir}")

    if not links:
        print("  No supplementary links found in HTML.")
        if pii:
            print("  (CDN brute-force also found nothing — this paper may have no supplements.)")
        return

    print(f"  Found {len(links)} potential supplementary file(s):")
    for link in links:
        print(f"    • {os.path.basename(urlparse(link).path)}")
        print(f"      {link}")

    # Download files
    print(f"\n  Downloading {len(links)} file(s)...")
    success_count = 0
    for link in links:
        res = download_file(link, article_dir, session, play_cookies)
        if res:
            success_count += 1

    print(f"\n✓ Done: {success_count}/{len(links)} files downloaded to {os.path.abspath(article_dir)}")


def _quick_title_lookup(doi, url):
    """Try to get article title from CrossRef API (fast, no auth)."""
    try:
        doi_clean = doi if doi.startswith("10.") else ""
        if not doi_clean:
            m = re.search(r"10\.\d{4,}/[^\s?&]+", url)
            if m:
                doi_clean = m.group(0)
        if doi_clean:
            resp = requests.get(
                f"https://api.crossref.org/works/{doi_clean}",
                timeout=10,
                headers={"User-Agent": "ScanSci-Supp-Downloader/2.0 (mailto:scansci@example.com)"},
            )
            if resp.status_code == 200:
                data = resp.json()
                titles = data.get("message", {}).get("title", [])
                if titles:
                    title = titles[0]
                    title = re.sub(r'[\\/*?:"<>|]', "_", title)
                    title = re.sub(r'\s+', ' ', title).strip()
                    if len(title) > 120:
                        title = title[:120]
                    return title
    except Exception:
        pass
    return None


def _try_page_scrape_supplements(url, article_dir, config, args, existing_files):
    """After CDN success, optionally scrape the page for non-CDN supplement links."""
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        session.verify = False

        if HAS_SCANSCI and not args.no_cookies:
            inject_cookies(session, config)

        play_cookies = load_all_scansci_cookies(config) if HAS_SCANSCI and not args.no_cookies else []
        html_content, status = fetch_page_html(url, session, play_cookies, args.headful)
        if not html_content:
            print("    Could not fetch page for additional links.")
            return

        links = find_supplementary_links(html_content, url)
        existing_basenames = {os.path.basename(f) for f in existing_files}

        new_links = []
        for link in links:
            link_basename = os.path.basename(urlparse(link).path)
            # Skip links that match already-downloaded CDN files
            if link_basename in existing_basenames:
                continue
            # Skip if the link is a CDN link we already tried
            if "ars.els-cdn.com" in link:
                continue
            new_links.append(link)

        if new_links:
            print(f"    Found {len(new_links)} additional non-CDN supplement link(s):")
            for link in new_links:
                print(f"      • {os.path.basename(urlparse(link).path)}: {link}")
            for link in new_links:
                download_file(link, article_dir, session, play_cookies)
        else:
            print("    No additional supplements found beyond CDN files.")
    except Exception as e:
        print(f"    Page scrape error: {e}")


if __name__ == "__main__":
    main()
