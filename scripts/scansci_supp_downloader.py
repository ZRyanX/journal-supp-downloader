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

# Reconfigure stdout/stderr to UTF-8 to prevent encoding crashes on Windows cp936 console
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import json
import time
import subprocess
import urllib3
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Suppress SSL warnings for expired WebVPN certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Ensure scripts directory and scansci_pdf are in path
scripts_dir = os.path.dirname(os.path.abspath(__file__))
if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)

sys.path.append(str(Path(__file__).resolve().parents[1] / "scansci-pdf" / "src"))
sys.path.append(str(Path(__file__).resolve().parents[2] / "scansci-pdf" / "src"))

import requests

# Import unified candidate recognition and file validation module
from supp_finder import (
    find_all_candidates,
    validate_downloaded_file,
    sanitize_filename,
    extract_filename_from_url,
    extract_filename_from_content_disposition,
    infer_file_extension,
    cookies_to_dict,
    extract_effective_base_url,
    detect_declared_supplement_count,
    is_result_obviously_incomplete,
    Candidate,
    FindingDiagnostics,
    DATA_EXTENSIONS,
    MMC_PATTERN,
    SUPP_KEYWORD_PATTERN,
    TABLE_FILENAME_PATTERN,
    TABLE_LABEL_PATTERN,
    EXCLUDE_URL_PATTERNS,
    ARTICLE_FIGURE_PATTERN,
    is_excluded_url,
    is_article_figure_url,
    check_data_extension,
    is_explicit_supp_table,
    is_table_text_or_attribute,
    should_match_candidate,
    load_download_manifest,
    save_download_manifest,
    disambiguate_target_filename,
)

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


def load_saved_cookies_standalone():
    """Load cookies in standalone mode from login wizard profile or local cookies.json."""
    paths = [
        os.path.expanduser("~/.journal_supp_downloader_profile/cookies.json"),
        os.path.abspath("cookies.json"),
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                    if isinstance(cdata, list) and cdata:
                        return cdata
                    elif isinstance(cdata, dict) and cdata:
                        return [{"name": k, "value": v, "domain": "", "path": "/"} for k, v in cdata.items()]
            except Exception:
                pass
    return []


# ── Helpers ──────────────────────────────────────────────────────────────────

def resolve_doi_url(doi_or_url):
    """
    Resolve a DOI or DOI URL to its final destination publisher URL.
    Handles '10.xxxx/...', 'https://doi.org/...', and 'http://dx.doi.org/...'.
    Uses curl (cross-platform) as robust fallback for SSL/proxy issues.
    """
    doi_or_url = (doi_or_url or "").strip()
    if not doi_or_url:
        return ""

    # If it's already a direct publisher URL (not a doi.org link), return directly
    if doi_or_url.startswith("http") and "doi.org" not in doi_or_url:
        return doi_or_url

    if doi_or_url.startswith("http"):
        url = doi_or_url
        doi = doi_or_url.split("doi.org/")[-1].strip("/")
    else:
        doi = doi_or_url
        url = f"https://doi.org/{doi}"

    print(f"Resolving DOI: {doi} via doi.org ...")

    # Method 1: Requests (HEAD)
    try:
        resp = requests.head(url, allow_redirects=True, timeout=15, verify=False,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        if resp.url and resp.url.startswith("http") and "doi.org" not in resp.url:
            print(f"  [requests] Resolved to: {resp.url}")
            return resp.url
    except Exception:
        pass

    # Method 2: Requests (GET, stream)
    try:
        resp = requests.get(url, allow_redirects=True, timeout=15, stream=True, verify=False,
                            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        resp.close()
        if resp.url and resp.url.startswith("http") and "doi.org" not in resp.url:
            print(f"  [requests] Resolved to: {resp.url}")
            return resp.url
    except Exception:
        pass

    # Method 3: curl (using subprocess, cross-platform)
    try:
        curl_cmd = "curl.exe" if sys.platform.startswith("win") else "curl"
        null_dev = "NUL" if sys.platform.startswith("win") else "/dev/null"
        cmd = [curl_cmd, "-w", "%{url_effective}", "-o", null_dev, "-s", "-L", url]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = res.stdout.strip()
        if out.startswith("http") and "doi.org" not in out:
            print(f"  [{curl_cmd}] Resolved to: {out}")
            return out
    except Exception:
        pass

    # Method 4: Scrapling Fetcher (if available)
    if HAS_SCRAPLING:
        try:
            from scrapling import Fetcher
            r = Fetcher.get(url, stealthy_headers=True, timeout=15000)
            if r.url and r.url.startswith("http") and "doi.org" not in r.url:
                print(f"  [scrapling] Resolved to: {r.url}")
                return r.url
        except Exception:
            pass

    # Method 5: Publisher direct constructor heuristics (fallback if everything else fails)
    if "10.1007" in doi or "10.1038" in doi:
        fallback = f"https://link.springer.com/article/{doi}"
        print(f"  [fallback] Springer/Nature URL: {fallback}")
        return fallback

    print(f"  [warning] Resolution failed, using default: {url}")
    return url


def is_data_url(url, text=""):
    """Check if URL or (URL + text) points to supplementary/data file via unified matcher (F1)."""
    if not url:
        return False
    matched, _ = should_match_candidate(url, text=text)
    return matched


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


def find_supplementary_links(html_content, base_url, original_url=None):
    """Scan HTML content for supplementary material links (uses unified supp_finder)."""
    diag = find_all_candidates(html_content, base_url, original_url=original_url or base_url)
    return sorted({c.url for c in diag.candidates})


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

    try:
        data = resp.json()
        article = data.get("full-text-retrieval-response", {})
    except Exception:
        print("  [Tier A] Failed to parse API JSON response.")
        return []

    if not pii:
        core = article.get("coredata", {})
        raw_pii = core.get("pii", "")
        pii = re.sub(r"[^A-Z0-9]", "", raw_pii, flags=re.IGNORECASE)
        if pii:
            print(f"  PII from API: {pii}")

    if not pii:
        print("  [Tier A] Cannot determine PII, skipping CDN download.")
        return []

    original_text = article.get("originalText", "")
    if isinstance(original_text, dict):
        original_text = json.dumps(original_text)
    elif not isinstance(original_text, str):
        original_text = str(original_text)

    mmc_files = set()
    pattern = re.compile(r"mmc(\d+)\.(\w+)", re.IGNORECASE)
    valid_exts = {e.lstrip('.') for e in DATA_EXTENSIONS} | {"pdf"}
    for m in pattern.finditer(original_text):
        num = m.group(1)
        ext = m.group(2).lower()
        if ext in valid_exts:
            mmc_files.add((int(num), ext))

    detected_mmcs = set()
    for m in re.finditer(r"mmc(\d+)", original_text, re.IGNORECASE):
        detected_mmcs.add(int(m.group(1)))

    if not mmc_files:
        if detected_mmcs:
            print(f"  Found mmc references without extensions: {sorted(detected_mmcs)}")
            print(f"  Falling through to CDN brute-force for extension detection.")
        return [], detected_mmcs

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

        if os.path.exists(filepath):
            is_val, _ = validate_downloaded_file(filepath)
            if is_val:
                print(f"  [SKIP] {fname} (already exists)")
                found_files.append(filepath)
                continue

        print(f"  [API→CDN] {fname} ...", end=" ", flush=True)
        try:
            dl_resp = dl_session.get(url, timeout=60, stream=True)
            if dl_resp.status_code == 200:
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

                is_val, reason = validate_downloaded_file(
                    filepath,
                    response_status=dl_resp.status_code,
                    headers=dl_resp.headers
                )
                if is_val:
                    actual_size = os.path.getsize(filepath)
                    print(f"OK ({actual_size / 1024:.1f} KB) → {fname}")
                    found_files.append(filepath)
                else:
                    if os.path.exists(filepath):
                        os.remove(filepath)
                    print(f"FAIL ({reason})")
            else:
                print(f"FAIL (HTTP {dl_resp.status_code})")
        except Exception as e:
            if os.path.exists(filepath):
                os.remove(filepath)
            print(f"FAIL ({e})")

    total_time = time.time() - t0
    if found_files:
        print(f"  [Tier A] ✓ {len(found_files)} file(s) downloaded in {total_time:.1f}s (API + CDN).")
    else:
        print(f"  [Tier A] No files downloaded.")
    return found_files, detected_mmcs


# ── Tier 0: Elsevier CDN Brute-Force ────────────────────────────────────────

def try_elsevier_cdn_brute_force(
    pii,
    output_dir,
    max_mmc=25,
    max_consecutive_misses=4,
    known_mmcs=None,
):
    """
    Elsevier hosts supplement files on a public CDN at:
      https://ars.els-cdn.com/content/image/1-s2.0-{PII}-mmc{N}.{ext}
    No authentication is required. Tracks probe diagnostics and does not
    discard small files under 1 KB.
    Probes sequential MMC numbers, handles gaps without redundant requests,
    and directly targets known MMC numbers beyond default limits.
    """
    if not pii:
        return []

    base = f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}"
    exts = ["xlsx", "xls", "csv", "docx", "doc", "pdf", "zip", "pptx", "txt"]
    found_files = []
    probed_mmcs = set()

    known_set = set(known_mmcs) if known_mmcs else set()
    valid_known = {int(m) for m in known_set if isinstance(m, (int, str)) and str(m).isdigit() and 1 <= int(m) <= 999}

    print(f"\n[Tier 0] Scanning Elsevier CDN for PII={pii} (max={max_mmc}, max_misses={max_consecutive_misses}) ...")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    def _probe_single_mmc(i: int) -> bool:
        probed_mmcs.add(i)
        for ext in exts:
            url = f"{base}-mmc{i}.{ext}"
            try:
                resp = session.head(url, timeout=10, allow_redirects=True)
                if resp.status_code == 200:
                    size = int(resp.headers.get("content-length", 0))
                    fname = f"mmc{i}.{ext}"
                    filepath = os.path.join(output_dir, fname)

                    if os.path.exists(filepath):
                        is_val, _ = validate_downloaded_file(filepath)
                        if is_val:
                            print(f"  [SKIP] {fname} (already exists)")
                            found_files.append(filepath)
                            return True

                    # Download the file
                    print(f"  [CDN]  mmc{i}.{ext} ({size / 1024:.1f} KB) ...", end=" ", flush=True)
                    dl_resp = session.get(url, timeout=30, stream=True)
                    if dl_resp.status_code == 200:
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

                        is_val, reason = validate_downloaded_file(
                            filepath,
                            response_status=dl_resp.status_code,
                            headers=dl_resp.headers
                        )
                        if is_val:
                            actual_size = os.path.getsize(filepath)
                            print(f"OK ({actual_size / 1024:.1f} KB) → {fname}")
                            found_files.append(filepath)
                            return True
                        else:
                            if os.path.exists(filepath):
                                os.remove(filepath)
                            print(f"FAIL ({reason})")
                    else:
                        print(f"FAIL (HTTP {dl_resp.status_code})")
            except requests.exceptions.Timeout:
                continue
            except Exception:
                continue
        return False

    # Phase 1: Sequential scan from 1 up to max_mmc
    consecutive_misses = 0
    scanned_count = 0
    stop_reason = ""

    for i in range(1, max_mmc + 1):
        scanned_count = i
        hit = _probe_single_mmc(i)
        if hit:
            consecutive_misses = 0
        else:
            consecutive_misses += 1
            if consecutive_misses >= max_consecutive_misses:
                stop_reason = f"{max_consecutive_misses} consecutive misses after mmc{i}"
                break

    if not stop_reason:
        stop_reason = f"reached scan limit (max_mmc={max_mmc})"

    # Phase 2: Probe any known MMCs that weren't reached or were skipped
    remaining_known = sorted(k for k in valid_known if k not in probed_mmcs)
    for k in remaining_known:
        if k in probed_mmcs:
            continue
        print(f"  [Tier 0] Probing known MMC mmc{k} ...")
        hit = _probe_single_mmc(k)
        if hit:
            follow_misses = 0
            cur = k + 1
            while follow_misses < max_consecutive_misses and cur <= 999:
                if cur in probed_mmcs:
                    break
                follow_hit = _probe_single_mmc(cur)
                if follow_hit:
                    follow_misses = 0
                else:
                    follow_misses += 1
                cur += 1

    total_probed = len(probed_mmcs)
    print(f"  [Tier 0 Diagnostic] Probed mmc1..mmc{scanned_count} ({total_probed} total, limit: {max_mmc}). Hits: {len(found_files)}. Stopped: {stop_reason}.")
    if found_files:
        print(f"  [Tier 0] Found {len(found_files)} file(s) via CDN.")
    else:
        print(f"  [Tier 0] No files found on CDN.")
    return found_files


# ── Tier 1/2: Page Scrape + Link Download ────────────────────────────────────

def is_html_file(filepath):
    is_valid, _ = validate_downloaded_file(filepath)
    return not is_valid


def download_file(cand_or_url, output_dir, session=None, cookies=None):
    """Download a single file, trying requests then Scrapling as fallback."""
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

    if os.path.exists(filepath) and manifest.get(fname) == url:
        is_val, _ = validate_downloaded_file(filepath)
        if is_val:
            print(f"  [SKIP] {fname} (already exists)")
            return filepath

    print(f"  [FETCH] {fname} ...", end=" ", flush=True)

    # Method 1: requests (with cookies, verify=False for WebVPN SSL issues)
    if session:
        try:
            resp = session.get(url, timeout=30, stream=True, verify=False)
            if resp.status_code < 400:
                hdr = {k.lower(): v for k, v in resp.headers.items()}
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

                first_chunk = b""
                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            if not first_chunk:
                                first_chunk = chunk
                            f.write(chunk)

                if "." not in fname or fname.endswith(".bin"):
                    inferred = infer_file_extension(first_chunk, hdr.get("content-type", ""))
                    if inferred:
                        base = fname[:-4] if fname.endswith(".bin") else fname
                        fname = disambiguate_target_filename(output_dir, f"{base}{inferred}", url, manifest)
                        new_path = os.path.join(output_dir, fname)
                        os.rename(filepath, new_path)
                        filepath = new_path

                is_val, reason = validate_downloaded_file(
                    filepath,
                    response_status=resp.status_code,
                    headers=resp.headers
                )
                if is_val:
                    if "HTML table attachment" in reason and (filepath.endswith(".bin") or not os.path.splitext(filepath)[1]):
                        base_html = filepath[:-4] if filepath.endswith(".bin") else filepath
                        html_name = disambiguate_target_filename(output_dir, os.path.basename(base_html) + ".html", url, manifest)
                        new_path = os.path.join(output_dir, html_name)
                        if filepath != new_path:
                            os.rename(filepath, new_path)
                            filepath = new_path
                            fname = html_name
                    manifest[fname] = url
                    save_download_manifest(output_dir, manifest)
                    size_kb = os.path.getsize(filepath) / 1024
                    print(f"OK ({size_kb:.1f} KB - {reason})")
                    return filepath
                else:
                    if os.path.exists(filepath):
                        os.remove(filepath)
        except Exception:
            if os.path.exists(filepath):
                os.remove(filepath)

    # Method 2: Scrapling Fetcher (basic HTTP client)
    if HAS_SCRAPLING:
        try:
            sc_args = {}
            if cookies:
                sc_args["cookies"] = cookies_to_dict(cookies, url)
            resp = Fetcher.get(url, stealthy_headers=True, timeout=30000, **sc_args)
            if resp.status < 400 and resp.body:
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

                if "." not in fname or fname.endswith(".bin"):
                    inferred = infer_file_extension(resp.body, hdr.get("content-type", ""))
                    if inferred:
                        base = fname[:-4] if fname.endswith(".bin") else fname
                        fname = disambiguate_target_filename(output_dir, f"{base}{inferred}", url, manifest)
                        filepath = os.path.join(output_dir, fname)

                with open(filepath, "wb") as f:
                    f.write(resp.body)
                is_val, reason = validate_downloaded_file(
                    filepath,
                    content_bytes=resp.body,
                    response_status=resp.status,
                    headers=resp.headers
                )
                if is_val:
                    if "HTML table attachment" in reason and (filepath.endswith(".bin") or not os.path.splitext(filepath)[1]):
                        base_html = filepath[:-4] if filepath.endswith(".bin") else filepath
                        html_name = disambiguate_target_filename(output_dir, os.path.basename(base_html) + ".html", url, manifest)
                        new_path = os.path.join(output_dir, html_name)
                        if filepath != new_path:
                            os.rename(filepath, new_path)
                            filepath = new_path
                            fname = html_name
                    manifest[fname] = url
                    save_download_manifest(output_dir, manifest)
                    size_kb = len(resp.body) / 1024
                    print(f"OK (via Scrapling, {size_kb:.1f} KB - {reason})")
                    return filepath
                else:
                    if os.path.exists(filepath):
                        os.remove(filepath)
        except Exception:
            if os.path.exists(filepath):
                os.remove(filepath)

    # Method 3: Scrapling StealthySession + Playwright sync download fallback
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

            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}/"
            try:
                page.goto(base_url, wait_until="commit")
            except Exception:
                pass

            with page.expect_download(timeout=60000) as download_info:
                try:
                    page.goto(url, wait_until="commit")
                except Exception as e:
                    if "Download is starting" not in str(e):
                        raise
            download = download_info.value
            if hasattr(download, "suggested_filename") and download.suggested_filename:
                real_name = sanitize_filename(download.suggested_filename)
                filepath = os.path.join(output_dir, real_name)
            download.save_as(filepath)
            session_stealth.close()

            is_val, reason = validate_downloaded_file(filepath)
            if is_val:
                size_kb = os.path.getsize(filepath) / 1024
                print(f"OK (via StealthySession, {size_kb:.1f} KB - {reason})")
                return filepath
            else:
                if os.path.exists(filepath):
                    os.remove(filepath)
        except Exception:
            try:
                session_stealth.close()
            except Exception:
                pass
            if os.path.exists(filepath):
                os.remove(filepath)

    # Method 4: curl (cross-platform fallback)
    try:
        curl_cmd = "curl.exe" if sys.platform.startswith("win") else "curl"
        cmd = [curl_cmd, "-s", "-L", "-o", filepath, url]
        res = subprocess.run(cmd, timeout=120)
        if res.returncode == 0 and os.path.exists(filepath):
            is_val, reason = validate_downloaded_file(filepath)
            if is_val:
                size_kb = os.path.getsize(filepath) / 1024
                print(f"OK (via {curl_cmd}, {size_kb:.1f} KB - {reason})")
                return filepath
            elif os.path.exists(filepath):
                os.remove(filepath)
    except Exception:
        pass

    print("FAIL")
    return None


def fetch_page_html(url, session=None, play_cookies=None, headful=False, force_browser=False):
    """
    Fetch article page HTML, trying requests then Scrapling StealthyFetcher.
    Returns (html_content, status, final_url) for accurate base URL resolution.
    """
    html_content = ""
    final_url = url

    # Method 1: requests session (fast, but often blocked by Cloudflare)
    if session and not force_browser:
        try:
            resp = session.get(url, timeout=30, verify=False)
            if resp.status_code < 400:
                final_url = resp.url or url
                text = resp.text
                cf_signals = ["challenge-platform", "cf-browser-verification", "just a moment", "Checking your browser"]
                if not any(sig.lower() in text.lower() for sig in cf_signals):
                    return text, resp.status_code, final_url
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
            final_url = getattr(page, "url", None) or url
            return html_content, page.status, final_url
        except Exception as e:
            print(f"    StealthyFetcher error: {e}")

    return html_content, 0, final_url


def _finish_success(tier_name, files, article_dir, doi, url, args, config):
    """Common success handler: print summary, rename folder, check for extra supplements."""
    print(f"\n✓ {tier_name} Success: {len(files)} supplementary file(s) downloaded.")
    print(f"  Output: {os.path.abspath(article_dir)}")

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

    print("\n  Checking for additional supplements via page scrape...")
    _try_page_scrape_supplements(url, article_dir, config, args, files)


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
    """
    After CDN success, scrape the page for non-CDN supplement links.
    Falls back to browser rendering if raw HTML is blocked or incomplete (Fixing Issue #4).
    """
    try:
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        session.verify = False

        if HAS_SCANSCI and not args.no_cookies:
            inject_cookies(session, config)
            play_cookies = load_all_scansci_cookies(config)
        elif not args.no_cookies:
            play_cookies = load_saved_cookies_standalone()
            for c in play_cookies:
                session.cookies.set(
                    c.get("name", ""), c.get("value", ""),
                    domain=c.get("domain", ""), path=c.get("path", "/"),
                )
        else:
            play_cookies = []

        html_content, status, final_url = fetch_page_html(url, session, play_cookies, args.headful)

        url_to_use = final_url or url
        effective_base = extract_effective_base_url(html_content, url_to_use)
        diag = find_all_candidates(html_content, effective_base, original_url=url)

        # Fallback to browser rendering if raw HTML yields no candidates or is incomplete
        if (not diag.candidates or diag.is_incomplete) and HAS_SCRAPLING:
            print("    Raw HTML incomplete or blocked. Retrying with browser rendering for page scrape...")
            html_content_b, status_b, final_url_b = fetch_page_html(url, session, play_cookies, args.headful, force_browser=True)
            if html_content_b:
                html_content = html_content_b
                url_to_use = final_url_b or url_to_use
                effective_base = extract_effective_base_url(html_content, url_to_use)
                diag = find_all_candidates(html_content, effective_base, original_url=url)

        if not html_content:
            print("    Could not fetch page for additional links.")
            return

        existing_basenames = {os.path.basename(f) for f in existing_files}
        seen_cand_urls = set()
        new_candidates = []
        for cand in diag.candidates:
            cand_fname = cand.filename or extract_filename_from_url(cand.url)
            if cand_fname in existing_basenames:
                continue
            if cand.url in seen_cand_urls:
                continue
            seen_cand_urls.add(cand.url)
            new_candidates.append(cand)

        if new_candidates:
            print(f"    Found {len(new_candidates)} additional supplement link(s):")
            for cand in new_candidates:
                print(f"      • {cand.filename}: {cand.url} ({cand.match_rule})")
            for cand in new_candidates:
                download_file(cand, article_dir, session, play_cookies)
        else:
            print("    No additional supplements found beyond CDN files.")

        # Check if page mentions MMC files that were not caught by HTML links or CDN
        pii = extract_pii_from_url(url_to_use) or extract_pii_from_url(url)
        if pii and not getattr(args, "skip_cdn", False):
            page_mmcs = {int(m.group(1)) for m in re.finditer(r"\bmmc[-_\s]?([1-9]\d{0,2})\b", html_content, re.IGNORECASE)}
            downloaded_mmcs = set()
            if os.path.exists(article_dir):
                for f in os.listdir(article_dir):
                    m_f = re.search(r"mmc[-_\s]?([1-9]\d{0,2})\b", f, re.IGNORECASE)
                    if m_f:
                        downloaded_mmcs.add(int(m_f.group(1)))
            missing_mmcs = page_mmcs - downloaded_mmcs
            if missing_mmcs:
                print(f"    Page mentions additional MMC file(s) {sorted(missing_mmcs)}; probing CDN...")
                extra_cdn_files = try_elsevier_cdn_brute_force(
                    pii, article_dir,
                    max_mmc=getattr(args, "max_mmc", 25),
                    max_consecutive_misses=getattr(args, "max_misses", 4),
                    known_mmcs=missing_mmcs,
                )
                if extra_cdn_files:
                    existing_files.extend(extra_cdn_files)
    except Exception as e:
        print(f"    Page scrape error: {e}")


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
    parser.add_argument("--max-mmc", type=int, default=25, help="Max mmc number to scan in CDN brute-force (default: 25)")
    parser.add_argument("--max-misses", type=int, default=4, help="Max consecutive misses before stopping CDN probe (default: 4)")
    parser.add_argument("--list-only", action="store_true", help="Only list found links with diagnostic evidence, don't download")
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
    known_mmcs = set()
    if not args.no_api and not args.list_only and "10.1016" in (doi if doi.startswith("10.") else url):
        api_files, detected_mmcs = try_elsevier_api_campus(doi, pii, article_dir, config)
        known_mmcs.update(detected_mmcs)

    if api_files:
        _finish_success("Tier A (API+CDN)", api_files, article_dir, doi, url, args, config)
        return

    # ── Step 3: Tier 0 — CDN Brute-Force (fast, no auth) ─────────────────
    cdn_files = []
    if pii and not args.skip_cdn and not args.list_only:
        cdn_files = try_elsevier_cdn_brute_force(
            pii, article_dir,
            max_mmc=args.max_mmc,
            max_consecutive_misses=args.max_misses,
            known_mmcs=known_mmcs,
        )

    if cdn_files:
        _finish_success("Tier 0 (CDN)", cdn_files, article_dir, doi, url, args, config)
        return

    # ── Step 4: Tier 1/2 — Page Scrape + Link Download ───────────────────
    print(f"\n[Tier 1/2] Fetching article page for supplement links...")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    })
    session.verify = False

    if HAS_SCANSCI and not args.no_cookies:
        inject_cookies(session, config)
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
        play_cookies = load_all_scansci_cookies(config)
    elif not args.no_cookies:
        play_cookies = load_saved_cookies_standalone()
        if play_cookies:
            for c in play_cookies:
                session.cookies.set(
                    c.get("name", ""), c.get("value", ""),
                    domain=c.get("domain", ""), path=c.get("path", "/"),
                )
    else:
        play_cookies = []

    # Fetch page with redirect resolution (Fixing Issue #1)
    html_content, status, final_url = fetch_page_html(url, session, play_cookies, args.headful)
    if not html_content:
        print("  ERROR: Failed to fetch article page.")
        print("  Possible causes: Cloudflare block, network issue, or authentication required.")
        return

    url = final_url
    effective_base = extract_effective_base_url(html_content, url)
    print(f"  Status:    {status}")
    if url != args.url_or_doi:
        print(f"  Final URL: {url}")
    if effective_base != url:
        print(f"  Base URL:  {effective_base}")

    article_dirname = extract_article_title(html_content, default="downloaded_article")
    article_dir = os.path.join(args.output_dir, article_dirname)
    os.makedirs(article_dir, exist_ok=True)
    print(f"  Title:     {article_dirname}")
    print(f"  Folder:    {article_dir}")

    # Scan for candidates with unified rules and diagnostics
    print("\n  Scanning for supplementary data links...")
    diag = find_all_candidates(html_content, effective_base, original_url=args.url_or_doi)
    candidates = diag.candidates

    # Check completeness: trigger browser rendering on partial hits (Fixing Issue #4)
    is_inc, inc_reason = is_result_obviously_incomplete(candidates, html_content, diag.declared_count)
    if (not candidates or is_inc) and HAS_SCRAPLING and session:
        print(f"  Incomplete candidate set ({inc_reason}). Falling back to browser rendering (JS execution)...")
        html_content_browser, status_browser, final_url_browser = fetch_page_html(
            url, session, play_cookies, args.headful, force_browser=True
        )
        if html_content_browser:
            url = final_url_browser or url
            effective_base_b = extract_effective_base_url(html_content_browser, url)
            diag_b = find_all_candidates(html_content_browser, effective_base_b, original_url=args.url_or_doi)
            seen_urls = {c.url for c in candidates}
            new_candidates = [c for c in diag_b.candidates if c.url not in seen_urls]
            if new_candidates:
                candidates.extend(new_candidates)
                html_content = html_content_browser
                diag = diag_b
                print(f"  Found {len(new_candidates)} additional candidate(s) after browser rendering (total: {len(candidates)}).")

                article_dirname = extract_article_title(html_content, default=article_dirname)
                new_article_dir = os.path.join(args.output_dir, article_dirname)
                if new_article_dir != article_dir:
                    os.makedirs(new_article_dir, exist_ok=True)
                    try:
                        os.rmdir(article_dir)
                    except Exception:
                        pass
                    article_dir = new_article_dir
                    print(f"  Updated Title:  {article_dirname}")
                    print(f"  Updated Folder: {article_dir}")

    if not candidates:
        if pii and not args.skip_cdn and not args.list_only:
            page_mmcs = {int(m.group(1)) for m in re.finditer(r"\bmmc[-_\s]?([1-9]\d{0,2})\b", html_content, re.IGNORECASE)}
            if page_mmcs:
                print(f"  Page text/components reference MMC file(s) {sorted(page_mmcs)}. Probing CDN...")
                cdn_extras = try_elsevier_cdn_brute_force(
                    pii, article_dir,
                    max_mmc=args.max_mmc,
                    max_consecutive_misses=args.max_misses,
                    known_mmcs=page_mmcs,
                )
                if cdn_extras:
                    try:
                        title = _quick_title_lookup(doi, url)
                        if title and title != "supplements":
                            new_dir = os.path.join(args.output_dir, sanitize_filename(title))
                            if not os.path.exists(new_dir):
                                os.rename(article_dir, new_dir)
                                article_dir = new_dir
                    except Exception:
                        pass
                    print(f"\n✓ Done: {len(cdn_extras)} files downloaded via targeted CDN probe to {os.path.abspath(article_dir)}")
                    return

        print("  No supplementary links found in HTML.")
        if diag.incomplete_reason:
            print(f"  [Diagnostic] {diag.incomplete_reason}")
        if diag.excluded_links:
            print(f"  [Diagnostic] Evaluated and excluded {len(diag.excluded_links)} links (figures/references/anchors).")
        if pii:
            print("  (CDN brute-force also found nothing — this paper may have no supplements.)")
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

    # Download files
    print(f"\n  Downloading {len(candidates)} file(s)...")
    success_count = 0
    for cand in candidates:
        res = download_file(cand, article_dir, session, play_cookies)
        if res:
            success_count += 1

    # Check if page text references additional MMCs not in candidate URLs
    extra_cdn_files = []
    if pii and not args.skip_cdn and not args.list_only:
        page_mmcs = {int(m.group(1)) for m in re.finditer(r"\bmmc[-_\s]?([1-9]\d{0,2})\b", html_content, re.IGNORECASE)}
        downloaded_mmcs = set()
        if os.path.exists(article_dir):
            for f in os.listdir(article_dir):
                m_f = re.search(r"mmc[-_\s]?([1-9]\d{0,2})\b", f, re.IGNORECASE)
                if m_f:
                    downloaded_mmcs.add(int(m_f.group(1)))
        missing_mmcs = page_mmcs - downloaded_mmcs
        if missing_mmcs:
            print(f"  Page text mentions additional MMC file(s) {sorted(missing_mmcs)}; probing CDN...")
            extra_cdn_files = try_elsevier_cdn_brute_force(
                pii, article_dir,
                max_mmc=args.max_mmc,
                max_consecutive_misses=args.max_misses,
                known_mmcs=missing_mmcs,
            )
            if extra_cdn_files:
                success_count += len(extra_cdn_files)

    try:
        title = _quick_title_lookup(doi, url)
        if title and title != "supplements":
            new_dir = os.path.join(args.output_dir, sanitize_filename(title))
            if not os.path.exists(new_dir):
                os.rename(article_dir, new_dir)
                article_dir = new_dir
    except Exception:
        pass

    total_target = len(candidates) + len(extra_cdn_files)
    print(f"\n✓ Done: {success_count}/{total_target} files downloaded to {os.path.abspath(article_dir)}")


if __name__ == "__main__":
    main()
