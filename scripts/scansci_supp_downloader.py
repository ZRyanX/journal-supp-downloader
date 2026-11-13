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
import html
import os
import re
import sys

# Reconfigure stdout/stderr to UTF-8 to prevent encoding crashes on Windows cp936 console
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

import json
import threading
import time
import subprocess
import urllib3
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    single_line,
    remove_invalid_existing_download,
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
    filename_dedup_key,
    dedupe_candidates,
    is_explicit_supp_table,
    is_table_text_or_attribute,
    should_match_candidate,
    load_download_manifest,
    save_download_manifest,
    manifest_entry_url,
    record_manifest_entry,
    disambiguate_target_filename,
    extract_article_title,
    expand_page_accordions_action,
    SUPP_WAIT_SELECTOR,
    ensure_inferred_extension_on_disk,
    is_placeholder_or_blocked_title,
    migrate_or_merge_article_dir,
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
    BrowserFallbackBudget,
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
    """Load cookies in standalone mode from local cookies.json and login wizard profile."""
    paths = [
        os.path.abspath("cookies.json"),
        os.path.expanduser("~/.journal_supp_downloader_profile/cookies.json"),
    ]
    all_cookies = []
    seen_keys = set()
    for p in paths:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    cdata = json.load(f)
                items = []
                if isinstance(cdata, list):
                    items = cdata
                elif isinstance(cdata, dict):
                    items = [{"name": k, "value": v, "path": "/"} for k, v in cdata.items()]
                for c in items:
                    if isinstance(c, dict) and c.get("name"):
                        k = (c.get("domain", ""), c.get("name", ""), c.get("path", "/"))
                        if k not in seen_keys:
                            seen_keys.add(k)
                            all_cookies.append(c)
            except Exception:
                pass
    return all_cookies


# ── Helpers ──────────────────────────────────────────────────────────────────

# P1-D (50-paper test, Cause D): the scansci browser layer had no --proxy
# and every StealthyFetcher.fetch / StealthySession call ran with
# proxy=None, so in a proxy-only environment every browser attempt died
# with ERR_TUNNEL_CONNECTION_FAILED -- 521/584 FAILs in the P1 retest --
# and each candidate burned its own retries until the time budget ran out.
# The proxy value uses the same syntax as journal_downloader.py
# (--proxy http://[user:pass@]host:port) and is plumbed unchanged to
# StealthyFetcher.fetch and, as a Playwright proxy dict, to StealthySession
# (which builds the launch_persistent_context underneath).
# CDP / BrowserSkill paths attach to a live browser that was launched
# elsewhere, so no per-request proxy can be injected there; they are
# intentionally untouched.

TUNNEL_ERROR_MARKER = "ERR_TUNNEL_CONNECTION_FAILED"
DEFAULT_TUNNEL_FAILURE_THRESHOLD = 3
# Log line printed when a host is abandoned early (kept verbatim: tests
# and the P1 report grep for this phrase).
TUNNEL_FAST_FAIL_MESSAGE = "隧道连续失败、提前放弃该站点"


def is_tunnel_error(error):
    """True when an exception/message is a browser tunnel failure, i.e. the
    browser could not reach the site at all (proxy missing / proxy down),
    as opposed to an HTTP error or a blocked page."""
    return TUNNEL_ERROR_MARKER in str(error or "")


def _tunnel_host_key(url_or_host):
    """Normalize a URL (or bare host) to the per-host breaker key."""
    text = str(url_or_host or "").strip().lower()
    if not text:
        return ""
    if "://" not in text:
        return text.split("/")[0].split(":")[0]
    try:
        return urlparse(text).hostname or text
    except Exception:
        return text


def tunnel_failure_host(url):
    """Public alias: host (lowercased) a URL belongs to for tunnel accounting."""
    return _tunnel_host_key(url)


def redact_proxy_url(proxy):
    """Proxy URL with any password masked, for safe log lines."""
    if not proxy:
        return ""
    text = str(proxy)
    try:
        parsed = urlparse(text)
        if parsed.password:
            return text.replace(f":{parsed.password}@", ":****@", 1)
    except Exception:
        pass
    return text


def playwright_proxy_config(proxy):
    """Convert a --proxy value to the Playwright proxy dict scrapling
    passes to launch_persistent_context: {"server", "username", "password"}.
    Accepts the same ``http://[user:pass@]host:port`` string as
    journal_downloader.py (or an already-built dict, returned as-is).
    Returns None for an empty value; raises ValueError for a value with
    no host so a typo'd proxy fails fast instead of silently going direct.
    """
    if not proxy:
        return None
    if isinstance(proxy, dict):
        return dict(proxy)
    parsed = urlparse(str(proxy))
    try:
        port = parsed.port
    except ValueError:
        raise ValueError(
            f"Invalid proxy URL (expected http://[user:pass@]host:port): {redact_proxy_url(proxy)!r}"
        ) from None
    if not parsed.hostname:
        raise ValueError(
            f"Invalid proxy URL (expected http://[user:pass@]host:port): {redact_proxy_url(proxy)!r}"
        )
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if port:
        server += f":{port}"
    config = {"server": server}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


class TunnelFailureTracker:
    """Per-host circuit breaker for browser tunnel failures.

    Counts *consecutive* ERR_TUNNEL_CONNECTION_FAILED failures per host:
    any non-tunnel failure is ignored and any browser success resets the
    count, so a transient blip does not trip the breaker and a recovered
    host is usable again. Once the count reaches ``threshold`` the host is
    tripped: callers must skip further browser attempts for it instead of
    retrying every candidate. Thread-safe; one instance is shared by all
    browser attempts within a run.
    """

    def __init__(self, threshold=DEFAULT_TUNNEL_FAILURE_THRESHOLD):
        self.threshold = max(1, int(threshold))
        self._counts = {}
        self._tripped = set()
        self._skip_announced = set()
        self._lock = threading.Lock()

    def consecutive_failures(self, url_or_host):
        """Consecutive tunnel failures recorded for this host (0 if none)."""
        with self._lock:
            return self._counts.get(_tunnel_host_key(url_or_host), 0)

    def is_tripped(self, url_or_host):
        """True once this host reached the failure threshold."""
        return self.consecutive_failures(url_or_host) >= self.threshold

    def record_failure(self, url_or_host, error):
        """Record one failure. Only tunnel errors count. Returns True when
        this failure *newly* trips the threshold (the caller should log
        the fast-fail line exactly then); False otherwise."""
        if not is_tunnel_error(error):
            return False
        host = _tunnel_host_key(url_or_host)
        if not host:
            return False
        with self._lock:
            count = self._counts.get(host, 0) + 1
            self._counts[host] = count
            if count < self.threshold:
                return False
            newly = host not in self._tripped
            self._tripped.add(host)
            return newly

    def record_success(self, url_or_host):
        """A browser success for this host clears its failure count."""
        host = _tunnel_host_key(url_or_host)
        if not host:
            return
        with self._lock:
            self._counts.pop(host, None)
            self._tripped.discard(host)
            self._skip_announced.discard(host)

    def take_skip_notice(self, url_or_host):
        """True only the first time a tripped host's skip is noticed, so
        the 'skipping browser' line is logged once per host per run
        instead of once per candidate."""
        host = _tunnel_host_key(url_or_host)
        with self._lock:
            if host in self._skip_announced:
                return False
            self._skip_announced.add(host)
            return True

    def reset(self, host=None):
        """Clear counts (one host, or all hosts when host is None)."""
        with self._lock:
            if host is None:
                self._counts.clear()
                self._tripped.clear()
                self._skip_announced.clear()
            else:
                key = _tunnel_host_key(host)
                self._counts.pop(key, None)
                self._tripped.discard(key)
                self._skip_announced.discard(key)


# Shared breaker for one run; run_download() resets it and applies the
# CLI threshold. Callers may pass their own tracker (tests do) instead.
_TUNNEL_TRACKER = TunnelFailureTracker()


def get_tunnel_tracker():
    """The run-shared tunnel failure tracker."""
    return _TUNNEL_TRACKER


def reset_tunnel_failure_tracker():
    """Clear the run-shared tracker (fresh run / test isolation)."""
    _TUNNEL_TRACKER.reset()


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
            r = Fetcher.get(url, stealthy_headers=True, timeout=15)
            if r.url and r.url.startswith("http") and "doi.org" not in r.url:
                print(f"  [scrapling] Resolved to: {r.url}")
                return r.url
        except Exception:
            pass

    # Method 4.5: Unpaywall & OpenAlex API resolution (fast, authoritative)
    if doi and is_doi(doi):
        try:
            oa_res = resolve_oa_info(doi, timeout=8)
            if oa_res and oa_res.publisher_url and "doi.org" not in oa_res.publisher_url:
                print(f"  [oa_resolver:{oa_res.source}] Resolved to: {oa_res.publisher_url}")
                return oa_res.publisher_url
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


def extract_pii_from_url(url):
    """Extract Elsevier PII from a URL or DOI string, stripping formatting characters."""
    if not url:
        return None
    url_str = str(url)
    m = re.search(r"pii/([A-Z0-9().-]+)", url_str, re.IGNORECASE)
    if m:
        cleaned = re.sub(r"[^A-Z0-9]", "", m.group(1), flags=re.IGNORECASE)
        if cleaned:
            return cleaned
    m_cell = re.search(
        r"(?:cell\.com|thelancet\.com)/[^?#]*/(?:fulltext|pdf|abs|article|retrieve)/([S0-9X().-]{10,})",
        url_str,
        re.IGNORECASE,
    )
    if m_cell:
        cleaned = re.sub(r"[^A-Z0-9]", "", m_cell.group(1), flags=re.IGNORECASE)
        if len(cleaned) >= 10:
            return cleaned
    return None


def normalize_url(url):
    """Rewrite linkinghub.elsevier.com or ScienceDirect abs/pdfft URLs → canonical www.sciencedirect.com URL."""
    if not url:
        return url
    if "linkinghub.elsevier.com/retrieve/pii/" in url:
        raw_tail = url.split("/retrieve/pii/")[-1].split("?")[0].split("#")[0].strip("/")
        pii = extract_pii_from_url(f"pii/{raw_tail}") or raw_tail
        return f"https://www.sciencedirect.com/science/article/pii/{pii}"
    if "sciencedirect.com/science/article/" in url:
        pii = extract_pii_from_url(url)
        if pii and (
            "/abs/pii/" in url
            or "/pdfft" in url
            or url.split("?")[0].split("#")[0].rstrip("/").endswith("/pdf")
        ):
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


def load_all_scansci_cookies(config, no_vpn=False):
    """Load and merge all scansci-pdf cookies (WebVPN, publisher, CARSI) as Playwright list."""
    cookies = []

    # 1. WebVPN cookies
    if not no_vpn and config.get("vpnsci_enabled"):
        try:
            vpn_cookies = load_vpnsci_cookies_unified(config)
            if HAS_SCANSCI:
                try:
                    base_url = _get_webvpn_base(config)
                except Exception:
                    base_url = config.get("webvpn_base", "")
            else:
                base_url = config.get("webvpn_base", "")
            domain = base_url.split("://")[-1].split(":")[0] if "://" in base_url else (base_url or "localhost")
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


def _setup_authenticated_session_and_cookies(config, args, cdp_target=None):
    """
    Create a requests.Session and Playwright cookie list populated uniformly from:
    1. scansci-pdf publisher cookies, WebVPN cookies, and CARSI cookies (when HAS_SCANSCI)
    2. standalone local cookies.json / ~/.journal_supp_downloader_profile/cookies.json
    3. live CDP browser cookies (when cdp_target is provided)
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    })
    session.verify = False
    play_cookies = []

    if getattr(args, "no_cookies", False):
        return session, play_cookies

    no_vpn = getattr(args, "no_vpn", False)
    if HAS_SCANSCI:
        try:
            inject_cookies(session, config)
        except Exception:
            pass
        # Inject WebVPN cookies into requests session as well
        if not no_vpn and config.get("vpnsci_enabled"):
            try:
                vpn_cookies = load_vpnsci_cookies_unified(config)
                try:
                    base_url = _get_webvpn_base(config)
                except Exception:
                    base_url = config.get("webvpn_base", "")
                domain = base_url.split("://")[-1].split(":")[0] if "://" in base_url else None
                for name, value in vpn_cookies.items():
                    session.cookies.set(name, value, domain=domain, path="/")
            except Exception:
                pass
        # Inject CARSI cookies into requests session
        cache_dir = Path(config.get("cache_dir", str(Path.home() / ".scansci-pdf" / "cache")))
        carsi_dir = cache_dir / "carsi_cookies"
        if carsi_dir.is_dir():
            for cf in carsi_dir.glob("*.json"):
                try:
                    for c in json.loads(cf.read_text(encoding="utf-8")):
                        if c.get("name"):
                            session.cookies.set(
                                c.get("name", ""), c.get("value", ""),
                                domain=c.get("domain") or None, path=c.get("path", "/"),
                            )
                except Exception:
                    pass
        play_cookies = load_all_scansci_cookies(config, no_vpn=no_vpn)

    # Always merge standalone cookies (cookies.json and login wizard profile)
    standalone_cookies = load_saved_cookies_standalone()
    if standalone_cookies:
        for c in standalone_cookies:
            if c.get("name"):
                session.cookies.set(
                    c.get("name", ""), c.get("value", ""),
                    domain=c.get("domain") or None, path=c.get("path", "/"),
                )
        play_cookies = merge_cookies(play_cookies, standalone_cookies)

    if cdp_target:
        try:
            live_cdp_c = extract_cdp_cookies(cdp_target)
            if live_cdp_c:
                print(f"  [CDP] Extracted {len(live_cdp_c)} live authenticated cookies from browser.")
                for c in live_cdp_c:
                    if c.get("name"):
                        session.cookies.set(
                            c.get("name", ""), c.get("value", ""),
                            domain=c.get("domain") or None, path=c.get("path", "/"),
                        )
                play_cookies = merge_cookies(play_cookies, live_cdp_c)
        except Exception:
            pass

    return session, play_cookies


# ── Tier A: Elsevier API Campus-IP ──────────────────────────────────────────

ELSEVIER_MIME_TO_EXT = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/x-excel": "xls",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/pdf": "pdf",
    "application/zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/x-7z-compressed": "7z",
    "application/x-rar-compressed": "rar",
    "application/vnd.rar": "rar",
    "application/gzip": "gz",
    "application/x-gzip": "gz",
    "application/x-tar": "tar",
    "application/x-bzip2": "bz2",
    "application/x-xz": "xz",
    "text/csv": "csv",
    "application/csv": "csv",
    "text/tab-separated-values": "tsv",
    "text/plain": "txt",
    "application/json": "json",
    "application/xml": "xml",
    "text/xml": "xml",
    "chemical/x-cif": "cif",
    "application/x-hdf": "h5",
    "application/x-hdf5": "h5",
    "application/x-netcdf": "nc",
}


def _resolve_elsevier_ext(raw_ext, valid_exts):
    """Resolve single or multi-part extension (e.g. tar.gz, xlsx) against valid_exts."""
    if not raw_ext:
        return ""
    ext = raw_ext.lower().strip(".")
    if ext in valid_exts:
        return ext
    first_part = ext.split(".")[0]
    if first_part in valid_exts:
        return first_part
    return ""


def _mime_or_format_to_ext(fmt_str, valid_exts):
    """Map an Elsevier API @format or @mimetype string to a valid file extension without substring false positives."""
    if not fmt_str:
        return ""
    fmt = fmt_str.lower().split(";")[0].strip().lstrip(".")
    if fmt in valid_exts:
        return fmt
    if fmt in ELSEVIER_MIME_TO_EXT:
        mapped = ELSEVIER_MIME_TO_EXT[fmt]
        if mapped in valid_exts:
            return mapped
    for mime_key, mapped_ext in ELSEVIER_MIME_TO_EXT.items():
        if mime_key in fmt and mapped_ext in valid_exts:
            return mapped_ext
    # Check multi-character extension tokens bounded by non-alphanumeric delimiters
    for token in re.split(r"[^a-z0-9]+", fmt):
        if len(token) >= 2 and token in valid_exts:
            return token
    return ""


def try_elsevier_api_campus(doi, pii, output_dir, config):
    """
    On campus network, the Elsevier Article Retrieval API returns the full
    article XML/JSON. We parse it to extract the precise mmc file references
    (including correct extensions), then download directly from CDN.
    """
    if not config.get("is_campus_network") and not config.get("elsevier_insttoken"):
        return [], set()

    api_key = config.get("elsevier_api_key", "")
    if not api_key:
        return [], set()

    print(f"\n[Tier A] Elsevier API campus-IP access (DOI/URL: {doi}) ...")
    t0 = time.time()

    headers = {
        "X-ELS-APIKey": api_key,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    insttoken = config.get("elsevier_insttoken", "")
    if insttoken:
        headers["X-ELS-InstToken"] = insttoken

    doi_clean = extract_doi(doi)
    if doi_clean:
        api_url = f"https://api.elsevier.com/content/article/doi/{doi_clean}"
    elif pii and (
        not doi
        or extract_pii_from_url(str(doi))
        or any(d in str(doi).lower() for d in ("sciencedirect.com", "elsevier.com", "cell.com", "thelancet.com"))
    ):
        api_url = f"https://api.elsevier.com/content/article/pii/{pii}"
    else:
        print("  [Tier A] Cannot extract DOI or Elsevier PII, skipping.")
        return [], set()

    try:
        resp = requests.get(api_url, headers=headers, timeout=30)
    except Exception as e:
        print(f"  [Tier A] API request failed: {e}")
        return [], set()

    if resp.status_code != 200:
        if resp.status_code == 401:
            print(f"  [Tier A] HTTP 401 — API key rejected (not on campus network?).")
        elif resp.status_code == 403:
            print(f"  [Tier A] HTTP 403 — Access denied (API quota or IP not recognized).")
        elif resp.status_code == 404:
            print(f"  [Tier A] HTTP 404 — Article not in API index yet.")
        else:
            print(f"  [Tier A] HTTP {resp.status_code}")
        return [], set()

    elapsed_api = time.time() - t0
    print(f"  API responded in {elapsed_api:.1f}s")

    try:
        data = resp.json()
        article = data.get("full-text-retrieval-response", {})
    except Exception:
        print("  [Tier A] Failed to parse API JSON response.")
        return [], set()

    core = article.get("coredata", {}) if isinstance(article.get("coredata"), dict) else {}
    if core:
        api_title = str(core.get("dc:title") or "").strip()
        if api_title:
            config["_last_api_title"] = api_title
        api_doi = extract_doi(str(core.get("prism:doi") or ""))
        if api_doi:
            config["_last_api_doi"] = api_doi

    if not pii:
        raw_pii = core.get("pii", "")
        pii = re.sub(r"[^A-Z0-9]", "", raw_pii, flags=re.IGNORECASE)
        if pii:
            print(f"  PII from API: {pii}")

    if not pii:
        print("  [Tier A] Cannot determine PII, skipping CDN download.")
        return [], set()

    original_text = article.get("originalText", "")
    if isinstance(original_text, dict):
        original_text = json.dumps(original_text)
    elif not isinstance(original_text, str):
        original_text = str(original_text)

    mmc_files = set()
    detected_mmcs = set()
    valid_exts = {e.lstrip('.') for e in DATA_EXTENSIONS} | {"pdf"}

    # 1. Parse text pattern mmc{N}.{ext} (including multi-part extensions like tar.gz)
    pattern = re.compile(r"mmc(\d+)\.([a-z0-9]+(?:\.[a-z0-9]+)?)", re.IGNORECASE)
    for m in pattern.finditer(original_text):
        num = m.group(1)
        ext = _resolve_elsevier_ext(m.group(2), valid_exts)
        if ext:
            mmc_files.add((int(num), ext))

    for m in re.finditer(r"(?:es)?mmc(\d+)", original_text, re.IGNORECASE):
        detected_mmcs.add(int(m.group(1)))

    # 2. Parse Elsevier API JSON 'objects' field
    objects = article.get("objects", {})
    obj_list = []
    if isinstance(objects, dict):
        obj_val = objects.get("object", [])
        obj_list = obj_val if isinstance(obj_val, list) else [obj_val]
    elif isinstance(objects, list):
        obj_list = objects

    for obj in obj_list:
        if isinstance(obj, dict):
            ref = str(obj.get("@ref", "") or obj.get("ref", "") or "")
            m_ref = re.search(r"(?:es)?mmc(\d+)(?:\.([a-z0-9]+(?:\.[a-z0-9]+)?))?", ref, re.IGNORECASE)
            if m_ref:
                num = int(m_ref.group(1))
                detected_mmcs.add(num)
                ext = _resolve_elsevier_ext(m_ref.group(2) or "", valid_exts)
                if not ext:
                    obj_url = str(obj.get("$", "") or obj.get("@href", "") or "")
                    m_url = re.search(r"mmc\d+\.([a-z0-9]+(?:\.[a-z0-9]+)?)", obj_url, re.IGNORECASE)
                    if m_url:
                        ext = _resolve_elsevier_ext(m_url.group(1), valid_exts)
                if not ext:
                    fmt = str(obj.get("@format", "") or obj.get("format", "") or obj.get("@mimetype", ""))
                    ext = _mime_or_format_to_ext(fmt, valid_exts)
                if ext and ext in valid_exts:
                    mmc_files.add((num, ext))

    # 3. Parse <ce:e-component> tags in originalText
    for ecomp in re.finditer(r'<ce:e-component[^>]*>(.*?)</ce:e-component>', original_text, re.DOTALL | re.IGNORECASE):
        comp_str = ecomp.group(0)
        for m in re.finditer(r'id=["\'](?:esmmc|mmc)(\d+)["\']', comp_str, re.IGNORECASE):
            detected_mmcs.add(int(m.group(1)))
        for m in re.finditer(r'mmc(\d+)\.([a-z0-9]+(?:\.[a-z0-9]+)?)', comp_str, re.IGNORECASE):
            num = int(m.group(1))
            ext = _resolve_elsevier_ext(m.group(2), valid_exts)
            if ext:
                mmc_files.add((num, ext))

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
    manifest = load_download_manifest(output_dir)

    for num, ext in mmc_sorted:
        target_fname = f"mmc{num}.{ext}"
        url = f"{cdn_base}-{target_fname}"
        fname = disambiguate_target_filename(output_dir, target_fname, url, manifest)
        filepath = os.path.join(output_dir, fname)

        if os.path.exists(filepath) and manifest_entry_url(manifest.get(fname)) == url:
            is_val, reason = validate_downloaded_file(filepath)
            if is_val:
                print(f"  [SKIP] {fname} (already exists)")
                found_files.append(filepath)
                continue
            # P1-F: garbage landed by an earlier run is removed, not
            # [SKIP]-counted; the download below then retries it.
            remove_invalid_existing_download(filepath, manifest, reason)

        print(f"  [API→CDN] {fname} ...", end=" ", flush=True)
        tmp_path = None
        try:
            dl_resp = dl_session.get(url, timeout=60, stream=True)
            if dl_resp.status_code == 200:
                cd = dl_resp.headers.get("content-disposition", "")
                cd_name = extract_filename_from_content_disposition(cd)
                if cd_name:
                    target_fname = sanitize_filename(cd_name)
                    fname = disambiguate_target_filename(output_dir, target_fname, url, manifest)
                    filepath = os.path.join(output_dir, fname)
                    if os.path.exists(filepath) and manifest_entry_url(manifest.get(fname)) == url:
                        is_val, reason = validate_downloaded_file(filepath)
                        if is_val:
                            print(f"[SKIP] {fname} (already exists)")
                            found_files.append(filepath)
                            continue
                        remove_invalid_existing_download(filepath, manifest, reason)

                # H9: stream to a same-directory temp file, validate it,
                # then commit with os.replace; the final path never holds
                # a partial file.
                os.makedirs(output_dir, exist_ok=True)
                tmp_path = temp_path_for(filepath)
                with open(tmp_path, "wb") as f:
                    for chunk in dl_resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)

                is_val, reason = validate_downloaded_file(
                    tmp_path,
                    response_status=dl_resp.status_code,
                    headers=dl_resp.headers
                )
                if is_val:
                    commit_temp_file(tmp_path, filepath)
                    tmp_path = None
                    actual_size = os.path.getsize(filepath)
                    print(f"OK ({actual_size / 1024:.1f} KB) → {fname}")
                    record_manifest_entry(manifest, fname, url)
                    save_download_manifest(output_dir, manifest)
                    found_files.append(filepath)
                else:
                    print(f"FAIL ({single_line(reason)})")
            else:
                print(f"FAIL (HTTP {dl_resp.status_code})")
        except Exception as e:
            print(f"FAIL ({single_line(e)})")
        finally:
            discard_temp_file(tmp_path)

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
    max_head_retries=2,
):
    """
    Elsevier hosts supplement files on a public CDN at:
      https://ars.els-cdn.com/content/image/1-s2.0-{PII}-mmc{N}.{ext}
    No authentication is required. Tracks probe diagnostics and does not
    discard small files under 1 KB.
    Probes sequential MMC numbers, handles gaps without redundant requests,
    and directly targets known MMC numbers beyond default limits.

    H2 failure classification: only a definitive miss (404, or another
    non-transient 4xx for every probed extension) counts toward
    consecutive misses. 429 / 403 / 5xx / timeouts / connection errors are
    transient: they are retried with exponential backoff (up to
    max_head_retries retries, respecting any Retry-After header) and, if
    still unresolved, leave the MMC as "unknown" -- neither a hit nor a
    miss -- so a rate-limited CDN can no longer stop the scan early.
    """
    if not pii:
        return []

    base = f"https://ars.els-cdn.com/content/image/1-s2.0-{pii}"
    # H2: extensions come from supp_finder.DATA_EXTENSIONS (the same source
    # Tier A uses), plus .pdf which Tier 0 historically probed. Composite
    # extensions such as .tar.gz are kept whole ("tar.gz") so the probe URL
    # becomes ...-mmcN.tar.gz, not ...-mmcN.tar. Deterministic order:
    # common supplement formats first, then the rest alphabetically.
    _tier0_priority = ("xlsx", "xls", "csv", "docx", "doc", "pdf", "zip", "pptx", "txt")
    _all_exts = {e.lstrip(".") for e in DATA_EXTENSIONS} | {"pdf"}
    exts = [e for e in _tier0_priority if e in _all_exts]
    exts += sorted(_all_exts - set(exts))
    found_files = []
    probed_mmcs = set()
    manifest = load_download_manifest(output_dir)

    known_set = set(known_mmcs) if known_mmcs else set()
    valid_known = {int(m) for m in known_set if isinstance(m, (int, str)) and str(m).isdigit() and 1 <= int(m) <= 999}

    print(f"\n[Tier 0] Scanning Elsevier CDN for PII={pii} (max={max_mmc}, max_misses={max_consecutive_misses}) ...")
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})

    def _retry_delay(headers, attempt):
        """Backoff before retry number *attempt* (1-based). A Retry-After
        header (seconds) wins over the exponential default; capped at 30s."""
        raw = None
        try:
            raw = {k.lower(): v for k, v in (headers or {}).items()}.get("retry-after")
        except Exception:
            raw = None
        if raw is not None:
            try:
                delay = float(raw)
                if delay >= 0:
                    return min(delay, 30.0)
            except (TypeError, ValueError):
                pass
        return min(0.5 * (2 ** (attempt - 1)), 8.0)

    def _check_ext(ext, mmc_num):
        """HEAD-probe one extension of one MMC number. Returns
        (ext, url, size, outcome) where outcome is "hit" (200), "miss"
        (404 / other non-transient 4xx) or "transient" (429 / 403 / 5xx /
        timeout / connection error still unresolved after
        max_head_retries retries)."""
        url = f"{base}-mmc{mmc_num}.{ext}"
        headers = {}
        for attempt in range(max_head_retries + 1):
            try:
                resp = session.head(url, timeout=10, allow_redirects=True)
            except Exception:
                outcome = "transient"
            else:
                headers = resp.headers or {}
                code = resp.status_code
                if code == 200:
                    size = int(resp.headers.get("content-length", 0))
                    return ext, url, size, "hit"
                if code == 404:
                    return ext, url, None, "miss"
                if code in (403, 429) or code >= 500:
                    outcome = "transient"
                else:
                    return ext, url, None, "miss"
            if attempt < max_head_retries:
                time.sleep(_retry_delay(headers, attempt + 1))
        return ext, url, None, "transient"

    def _probe_single_mmc(i: int) -> str:
        """Probe one MMC number. Returns "hit", "miss" or "unknown".
        "unknown" means at least one extension stayed transient with no
        hit: the file may exist, so the caller must not count it as a miss.
        """
        probed_mmcs.add(i)
        found_any = False

        # Probe all candidate extensions concurrently
        with ThreadPoolExecutor(max_workers=min(len(exts), 8)) as executor:
            future_to_ext = {executor.submit(_check_ext, ext, i): ext for ext in exts}
            results = []
            for future in as_completed(future_to_ext):
                results.append(future.result())

        hits = [(ext, url, size) for ext, url, size, outcome in results if outcome == "hit"]
        saw_transient = any(outcome == "transient" for *_, outcome in results)

        # Sort hits by order of exts list for deterministic behavior
        hits.sort(key=lambda x: exts.index(x[0]))

        for ext, url, size in hits:
            target_fname = f"mmc{i}.{ext}"
            fname = disambiguate_target_filename(output_dir, target_fname, url, manifest)
            filepath = os.path.join(output_dir, fname)

            if os.path.exists(filepath) and manifest_entry_url(manifest.get(fname)) == url:
                is_val, reason = validate_downloaded_file(filepath)
                if is_val:
                    print(f"  [SKIP] {fname} (already exists)")
                    found_files.append(filepath)
                    found_any = True
                    continue
                # P1-F: garbage landed by an earlier run is removed, not
                # [SKIP]-counted; the download below then retries it.
                remove_invalid_existing_download(filepath, manifest, reason)

            # Download the file (H9: temp file -> validate -> os.replace)
            print(f"  [CDN]  mmc{i}.{ext} ({size / 1024:.1f} KB) ...", end=" ", flush=True)
            tmp_path = None
            try:
                dl_resp = session.get(url, timeout=30, stream=True)
                if dl_resp.status_code == 200:
                    cd = dl_resp.headers.get("content-disposition", "")
                    cd_name = extract_filename_from_content_disposition(cd)
                    if cd_name:
                        target_fname = sanitize_filename(cd_name)
                        fname = disambiguate_target_filename(output_dir, target_fname, url, manifest)
                        filepath = os.path.join(output_dir, fname)
                        if os.path.exists(filepath) and manifest_entry_url(manifest.get(fname)) == url:
                            is_val, reason = validate_downloaded_file(filepath)
                            if is_val:
                                print(f"[SKIP] {fname} (already exists)")
                                found_files.append(filepath)
                                found_any = True
                                continue
                            remove_invalid_existing_download(filepath, manifest, reason)

                    os.makedirs(output_dir, exist_ok=True)
                    tmp_path = temp_path_for(filepath)
                    with open(tmp_path, "wb") as f:
                        for chunk in dl_resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)

                    is_val, reason = validate_downloaded_file(
                        tmp_path,
                        response_status=dl_resp.status_code,
                        headers=dl_resp.headers
                    )
                    if is_val:
                        commit_temp_file(tmp_path, filepath)
                        tmp_path = None
                        actual_size = os.path.getsize(filepath)
                        print(f"OK ({actual_size / 1024:.1f} KB) → {fname}")
                        record_manifest_entry(manifest, fname, url)
                        save_download_manifest(output_dir, manifest)
                        found_files.append(filepath)
                        found_any = True
                    else:
                        print(f"FAIL ({single_line(reason)})")
                else:
                    print(f"FAIL (HTTP {dl_resp.status_code})")
            except Exception as e:
                print(f"FAIL ({single_line(e)})")
            finally:
                discard_temp_file(tmp_path)
        # A HEAD hit means the file exists even if the follow-up GET
        # download itself failed, so it still counts as a hit (not a miss).
        if found_any or hits:
            return "hit"
        if saw_transient:
            return "unknown"
        return "miss"

    # Phase 1: Sequential scan from 1 up to max_mmc. Only consecutive
    # "miss" outcomes stop the scan; "unknown" (transient) outcomes leave
    # the counter untouched, and a "hit" resets it.
    consecutive_misses = 0
    scanned_count = 0
    stop_reason = ""

    for i in range(1, max_mmc + 1):
        scanned_count = i
        outcome = _probe_single_mmc(i)
        if outcome == "hit":
            consecutive_misses = 0
        elif outcome == "miss":
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
        outcome = _probe_single_mmc(k)
        if outcome == "hit":
            follow_misses = 0
            follow_unknowns = 0
            cur = k + 1
            while follow_misses < max_consecutive_misses and cur <= 999:
                if cur in probed_mmcs:
                    break
                follow_outcome = _probe_single_mmc(cur)
                if follow_outcome == "hit":
                    follow_misses = 0
                    follow_unknowns = 0
                elif follow_outcome == "miss":
                    follow_misses += 1
                else:
                    # Unknown: do not count as a miss, but stop following
                    # after that many in a row so a persistently
                    # rate-limited CDN cannot drag the scan out to mmc999.
                    follow_unknowns += 1
                    if follow_unknowns >= max_consecutive_misses:
                        break
                cur += 1

    total_probed = len(probed_mmcs)
    print(f"  [Tier 0 Diagnostic] Probed mmc1..mmc{scanned_count} ({total_probed} total, limit: {max_mmc}). Hits: {len(found_files)}. Stopped: {stop_reason}.")
    if found_files:
        # P1-F: confirm every Tier 0 hit on its own line -- file name,
        # size and validation result -- so a Tier 0 success is visible
        # instead of only a bare count (#47's 3 mmc files were
        # invisible). A file that no longer validates is dropped and
        # deleted, never counted as a success.
        confirmed = []
        for _path in found_files:
            _name = os.path.basename(_path)
            try:
                _size_kb = os.path.getsize(_path) / 1024
            except OSError:
                _size_kb = None
            _is_val, _val_reason = validate_downloaded_file(_path)
            if not _is_val:
                print(f"  [Tier 0] \u2717 {_name} failed re-validation ({single_line(_val_reason)}); discarding.")
                try:
                    os.remove(_path)
                except OSError:
                    pass
                continue
            _size_txt = f"{_size_kb:.1f} KB" if _size_kb is not None else "size unknown"
            print(f"  [Tier 0] \u2713 {_name} ({_size_txt}, validated: {single_line(_val_reason)})")
            confirmed.append(_path)
        found_files = confirmed
        if found_files:
            print(f"  [Tier 0] Downloaded {len(found_files)} file(s) via CDN (validated).")
        else:
            print(f"  [Tier 0] No files found on CDN.")
    else:
        print(f"  [Tier 0] No files found on CDN.")
    return found_files


# ── Tier 1/2: Page Scrape + Link Download ────────────────────────────────────

def is_html_file(filepath):
    is_valid, _ = validate_downloaded_file(filepath)
    return not is_valid


MMC_NUM_REGEX = re.compile(r"(?:^|[^a-z0-9])(?:es)?mmc[-_\s]?([1-9]\d{0,2})(?=$|[^0-9])", re.IGNORECASE)


def download_file(cand_or_url, output_dir, session=None, cookies=None, bsk_session=None, referer=None, bsk_runtime=None, proxy=None, tunnel_tracker=None):
    """Download a single file, trying requests, Scrapling, and authenticated BrowserSkill session.

    P1-D: ``proxy`` (``http://[user:pass@]host:port``, same syntax as
    journal_downloader.py --proxy) is passed to the browser download path
    (Method 3, StealthySession) as a Playwright proxy dict; ``tunnel_tracker``
    (default: the run-shared tracker) fast-fails the browser path for a
    host whose tunnel failures reached the threshold.
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

    # P1-D: resolve the tunnel breaker (explicit tracker wins, else the
    # run-shared one) before any browser attempt is made.
    tracker = tunnel_tracker if tunnel_tracker is not None else _TUNNEL_TRACKER

    # Table ids the candidate is known to cover (H6), for the manifest record.
    cand_table_ids = sorted(getattr(cand_or_url, "covered_table_ids", ()) or ()) if isinstance(cand_or_url, Candidate) else []

    os.makedirs(output_dir, exist_ok=True)
    manifest = load_download_manifest(output_dir)
    fname = suggested_fname or extract_filename_from_url(url)
    fname = sanitize_filename(fname)
    fname = disambiguate_target_filename(output_dir, fname, url, manifest)
    filepath = os.path.join(output_dir, fname)

    if os.path.exists(filepath) and manifest_entry_url(manifest.get(fname)) == url:
        is_val, reason = validate_downloaded_file(filepath)
        if is_val:
            print(f"  [SKIP] {fname} (already exists)")
            return filepath
        # P1-F: garbage landed by an earlier run is removed, not
        # [SKIP]-counted as a success; the download below retries it.
        remove_invalid_existing_download(filepath, manifest, reason)

    print(f"  [FETCH] {fname} ...", end=" ", flush=True)
    last_error = ""  # P1 (50-paper test, Cause G): keep the last failure reason for the final FAIL line

    # Method 1: requests (with cookies, verify=False for WebVPN SSL issues)
    # H9: stream to a same-directory temp file, validate it, then commit
    # with os.replace; the final path never holds a partial file, and a
    # failure only ever discards the temp file.
    if session:
        tmp_path = None
        try:
            resp = session.get(url, timeout=30, stream=True, verify=False, headers={"Referer": referer} if referer else {})
            if resp.status_code < 400:
                hdr = {k.lower(): v for k, v in resp.headers.items()}
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

                first_chunk = b""
                tmp_path = temp_path_for(filepath)
                with open(tmp_path, "wb") as f:
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
                        filepath = os.path.join(output_dir, fname)
                        # Re-home the staged file under the inferred
                        # extension so validation sees the real extension.
                        new_tmp = temp_path_for(filepath)
                        os.replace(tmp_path, new_tmp)
                        tmp_path = new_tmp

                is_val, reason = validate_downloaded_file(
                    tmp_path,
                    response_status=resp.status_code,
                    headers=resp.headers
                )
                if is_val:
                    if "HTML table attachment" in reason and (filepath.endswith(".bin") or not os.path.splitext(filepath)[1]):
                        base_html = filepath[:-4] if filepath.endswith(".bin") else filepath
                        html_name = disambiguate_target_filename(output_dir, os.path.basename(base_html) + ".html", url, manifest)
                        if filepath != os.path.join(output_dir, html_name):
                            filepath = os.path.join(output_dir, html_name)
                            fname = html_name
                    commit_temp_file(tmp_path, filepath)
                    tmp_path = None
                    record_manifest_entry(manifest, fname, url, source_url=referer or "", table_ids=cand_table_ids)
                    save_download_manifest(output_dir, manifest)
                    size_kb = os.path.getsize(filepath) / 1024
                    print(f"OK ({size_kb:.1f} KB - {reason})")
                    return filepath
                else:
                    # P1-F: a validation rejection is a failure reason,
                    # not a silent fall-through -- otherwise the final
                    # FAIL line comes out bare with the reason lost.
                    last_error = f"validation: {reason}"
        except Exception as e:
            last_error = f"requests: {e}"
        finally:
            discard_temp_file(tmp_path)

    # Method 2: Scrapling Fetcher (basic HTTP client)
    # H9: chunked write to a temp file, validate, then os.replace.
    if HAS_SCRAPLING:
        tmp_path = None
        try:
            sc_args = {"headers": {"Referer": referer}} if referer else {}
            if cookies:
                sc_args["cookies"] = cookies_to_dict(cookies, url)
            resp = Fetcher.get(url, stealthy_headers=True, timeout=30, **sc_args)
            if resp.status < 400 and resp.body:
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

                if "." not in fname or fname.endswith(".bin"):
                    inferred = infer_file_extension(resp.body, hdr.get("content-type", ""))
                    if inferred:
                        base = fname[:-4] if fname.endswith(".bin") else fname
                        fname = disambiguate_target_filename(output_dir, f"{base}{inferred}", url, manifest)
                        filepath = os.path.join(output_dir, fname)

                tmp_path = temp_path_for(filepath)
                write_bytes_chunked(tmp_path, resp.body)
                is_val, reason = validate_downloaded_file(
                    tmp_path,
                    content_bytes=resp.body,
                    response_status=resp.status,
                    headers=resp.headers
                )
                if is_val:
                    if "HTML table attachment" in reason and (filepath.endswith(".bin") or not os.path.splitext(filepath)[1]):
                        base_html = filepath[:-4] if filepath.endswith(".bin") else filepath
                        html_name = disambiguate_target_filename(output_dir, os.path.basename(base_html) + ".html", url, manifest)
                        if filepath != os.path.join(output_dir, html_name):
                            filepath = os.path.join(output_dir, html_name)
                            fname = html_name
                    commit_temp_file(tmp_path, filepath)
                    tmp_path = None
                    record_manifest_entry(manifest, fname, url, source_url=referer or "", table_ids=cand_table_ids)
                    save_download_manifest(output_dir, manifest)
                    size_kb = len(resp.body) / 1024
                    print(f"OK (via Scrapling, {size_kb:.1f} KB - {reason})")
                    return filepath
                else:
                    # P1-F: keep the validation reason for the final FAIL line.
                    last_error = f"validation: {reason}"
        except Exception as e:
            last_error = f"scrapling: {e}"
        finally:
            discard_temp_file(tmp_path)

    # Reuse the opted-in authenticated session before a standalone browser.
    if bsk_session is not None or (bsk_runtime is not None and bsk_runtime.enabled):
        result = download_file_via_browserskill(
            url, filepath, session=bsk_session, referer=referer, runtime=bsk_runtime
        )
        if result:
            filepath = ensure_inferred_extension_on_disk(
                filepath, output_dir, url, manifest=manifest
            )
            fname = os.path.basename(filepath)
            record_manifest_entry(manifest, fname, url, source_url=referer or "", table_ids=cand_table_ids)
            save_download_manifest(output_dir, manifest)
            print(f"OK (via BrowserSkill, {os.path.getsize(filepath) / 1024:.1f} KB)")
            return filepath

    # Method 3: Scrapling StealthySession + Playwright sync download fallback
    # H9: the browser download lands in a temp file, is validated, then
    # committed with os.replace; a failure only discards the temp file.
    # P1-D: skip the browser entirely once this host's tunnel failures
    # tripped the breaker -- launching another StealthySession per
    # candidate would burn the time budget on a dead tunnel. The curl
    # fallback below still runs (it may use a working env proxy).
    if HAS_SCRAPLING and tracker.is_tripped(url):
        if tracker.take_skip_notice(url):
            print(f"  [Tunnel] {TUNNEL_FAST_FAIL_MESSAGE}: {tunnel_failure_host(url)} "
                  f"(连续 {tracker.consecutive_failures(url)} 次 {TUNNEL_ERROR_MARKER})，跳过浏览器下载尝试。")
        last_error = f"browser: tunnel failure fast-fail for {tunnel_failure_host(url)}"
    elif HAS_SCRAPLING:
        tmp_path = None
        try:
            from scrapling.fetchers import StealthySession
            # P1-D: Playwright proxy dict (same --proxy syntax as
            # journal_downloader.py); omitted entirely when no proxy.
            _stealth_kwargs = {
                "headless": True,
                "solve_cloudflare": True,
                "network_idle": True,
                "timeout": 60000,
            }
            _proxy_cfg = playwright_proxy_config(proxy)
            if _proxy_cfg is not None:
                _stealth_kwargs["proxy"] = _proxy_cfg
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
            page = context.new_page()

            parsed = urlparse(url)
            base_url = f"{parsed.scheme}://{parsed.netloc}/"
            try:
                page.goto(base_url, wait_until="commit")
            except Exception:
                pass

            with page.expect_download(timeout=10000) as download_info:
                try:
                    page.goto(url, wait_until="commit")
                except Exception as e:
                    if "Download is starting" not in str(e):
                        raise
            download = download_info.value
            if hasattr(download, "suggested_filename") and download.suggested_filename:
                real_name = sanitize_filename(download.suggested_filename)
                fname = disambiguate_target_filename(output_dir, real_name, url, manifest)
                filepath = os.path.join(output_dir, fname)
            tmp_path = temp_path_for(filepath)
            download.save_as(tmp_path)
            session_stealth.close()

            # Re-home under the inferred extension (if any) so validation
            # sees the real extension, as in Method 1.
            if "." not in fname or fname.endswith(".bin"):
                with open(tmp_path, "rb") as _head_f:
                    _head = _head_f.read(8192)
                _inferred = infer_file_extension(_head, "")
                if _inferred:
                    _base = fname[:-4] if fname.endswith(".bin") else fname
                    fname = disambiguate_target_filename(output_dir, f"{_base}{_inferred}", url, manifest)
                    filepath = os.path.join(output_dir, fname)
                    _new_tmp = temp_path_for(filepath)
                    os.replace(tmp_path, _new_tmp)
                    tmp_path = _new_tmp

            is_val, reason = validate_downloaded_file(tmp_path)
            if is_val:
                commit_temp_file(tmp_path, filepath)
                tmp_path = None
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
                tracker.record_success(url)
                return filepath
            else:
                # P1-F: keep the validation reason for the final FAIL line.
                last_error = f"validation: {reason}"
        except Exception as e:
            last_error = f"browser: {e}"
            # P1-D: a tunnel failure counts toward the per-host breaker;
            # log the fast-fail line exactly when the threshold is reached.
            if tracker.record_failure(url, e):
                print(f"  [Tunnel] {TUNNEL_FAST_FAIL_MESSAGE}: {tunnel_failure_host(url)} "
                      f"(连续 {tracker.consecutive_failures(url)} 次 {TUNNEL_ERROR_MARKER})，跳过该站点后续浏览器尝试。")
            try:
                session_stealth.close()
            except Exception:
                pass
        finally:
            discard_temp_file(tmp_path)

    # Method 4: curl (cross-platform fallback)
    # H9: curl writes to a temp file, which is validated and then committed
    # with os.replace; a failure only discards the temp file.
    tmp_path = None
    try:
        curl_cmd = "curl.exe" if sys.platform.startswith("win") else "curl"
        tmp_path = temp_path_for(filepath)
        cmd = [curl_cmd, "--fail", "--max-time", "120", "-s", "-L", "-o", tmp_path, url]
        res = subprocess.run(cmd, timeout=120)
        if res.returncode == 0 and os.path.exists(tmp_path):
            # Re-home under the inferred extension (if any) so validation
            # sees the real extension, as in Method 1.
            if "." not in fname or fname.endswith(".bin"):
                with open(tmp_path, "rb") as _head_f:
                    _head = _head_f.read(8192)
                _inferred = infer_file_extension(_head, "")
                if _inferred:
                    _base = fname[:-4] if fname.endswith(".bin") else fname
                    fname = disambiguate_target_filename(output_dir, f"{_base}{_inferred}", url, manifest)
                    filepath = os.path.join(output_dir, fname)
                    _new_tmp = temp_path_for(filepath)
                    os.replace(tmp_path, _new_tmp)
                    tmp_path = _new_tmp
            is_val, reason = validate_downloaded_file(tmp_path)
            if is_val:
                commit_temp_file(tmp_path, filepath)
                tmp_path = None
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
                size_kb = os.path.getsize(filepath) / 1024
                record_manifest_entry(manifest, fname, url, source_url=referer or "", table_ids=cand_table_ids)
                save_download_manifest(output_dir, manifest)
                print(f"OK (via {curl_cmd}, {size_kb:.1f} KB - {reason})")
                return filepath
            else:
                # P1-F: keep the validation reason for the final FAIL line.
                last_error = f"validation: {reason}"
    except Exception as e:
        last_error = f"curl: {e}"
    finally:
        discard_temp_file(tmp_path)

    print(f"FAIL ({single_line(last_error)})" if last_error else "FAIL")
    return None


def fetch_page_html(
    url,
    session=None,
    play_cookies=None,
    headful=False,
    force_browser=False,
    cdp_url=None,
    use_bsk=False,
    bsk_browser=None,
    bsk_session=None,
    bsk_tab=None,
    bsk_runtime=None,
    proxy=None,
    tunnel_tracker=None,
    browser_budget=None,
    browser_goto_timeout=None,
    deadline=None,
):
    """
    Fetch article page HTML, trying requests then Scrapling StealthyFetcher.
    Returns (html_content, status, final_url) for accurate base URL resolution.

    P1-D: ``proxy`` (``http://[user:pass@]host:port``, same syntax as
    journal_downloader.py --proxy) is passed to StealthyFetcher.fetch;
    ``tunnel_tracker`` (default: the run-shared tracker) fast-fails the
    browser attempt for a host whose tunnel failures reached the threshold.
    CDP / BrowserSkill paths attach to a live browser and cannot take a
    per-request proxy, so they are unchanged.
    P1 H-1: ``browser_budget`` (a run-shared BrowserFallbackBudget, or a
    fresh 30s-goto / 60s-total one when omitted) caps every browser
    attempt here: one goto runs at most the goto timeout (capped at the
    remaining total), and no browser attempt starts once the total is
    spent -- the caller then degrades to the OA mirror round instead of
    being killed inside a page load.
    P1 C: ``deadline`` (a ``time.monotonic()`` value, normally the run's
    phase deadline) is checked before *every* load this function would
    start -- once it has passed, no requests / BSK / CDP / browser load
    starts at all and ("", 0, url) is returned at once, so a load that
    returns late stops the phase instead of starting the next stage.
    The deadline also caps the requests timeout (30s hardcoded before)
    and, via the budget's phase awareness, every browser timeout, so
    one load can never run past the remaining phase budget. A deadline
    passed here always wins over a phase deadline the budget may
    already carry. When the underlying call is already in flight, its
    own timeout is the only interrupt available -- hence the clamp
    before launch, not a mid-flight cancel (no API here offers one).
    """
    html_content = ""
    final_url = url
    # P1-D: resolve the tunnel breaker (explicit tracker wins, else the
    # run-shared one).
    tracker = tunnel_tracker if tunnel_tracker is not None else _TUNNEL_TRACKER
    # P1 H-1: resolve the browser budget (explicit budget wins; a lone
    # goto timeout gets a fresh default-total budget; otherwise a fresh
    # default budget so a standalone call is still capped).
    if browser_budget is not None:
        budget = browser_budget
    elif browser_goto_timeout is not None:
        budget = BrowserFallbackBudget(goto_timeout=browser_goto_timeout)
    else:
        budget = BrowserFallbackBudget()
    # P1 C: the phase deadline caps every attempt below (and makes the
    # budget exhausted once it passes). An explicit deadline always
    # wins over one the budget already carries.
    if deadline is not None:
        budget.set_phase_deadline(deadline)
        if time.monotonic() >= deadline:
            # No load of any kind starts once the deadline has passed.
            return "", 0, url

    # Primary BrowserSkill Method: Fetch via Tencent BrowserSkill session
    if use_bsk and budget.exhausted:
        # P1 H-1: no browser attempt once the fallback budget is gone.
        budget.announce_exhausted()
    elif use_bsk:
        try:
            print(f"    (Fetching via Tencent BrowserSkill bsk ...)")
            bsk_html, bsk_status, bsk_final = fetch_page_via_browserskill(
                url,
                wait_selector=None,
                timeout=budget.attempt_timeout(),
                browser=bsk_browser,
                tab_id=bsk_tab,
                session=bsk_session,
                runtime=bsk_runtime,
            )
            if bsk_html and not page_fetch_is_failure(bsk_html, bsk_status):
                return bsk_html, bsk_status, bsk_final or url
            if bsk_html:
                print("    BrowserSkill page is blocked or an error page; falling through to default pipeline...")
        except Exception as e_bsk:
            if bsk_runtime and (bsk_runtime.browser or bsk_runtime.tab_id):
                raise
            print(f"    BrowserSkill fetch notice: {e_bsk}. Falling back to default pipeline...")

    # Primary CDP Method: Attach directly to running browser via CDP
    # P1 H-1: skipped outright once the fallback budget is gone; the
    # timeout is the capped goto timeout, not the old hardcoded 60s.
    # (CDP attaches to a live browser, so no per-request proxy or goto
    # timeout can be injected beyond this value -- see supp_finder.)
    if cdp_url and budget.exhausted:
        budget.announce_exhausted()
    elif cdp_url:
        try:
            wait_sel = None
            if force_browser:
                wait_sel = SUPP_WAIT_SELECTOR
            print(f"    (Attaching to live browser tab via CDP: {cdp_url} ...)")
            cdp_html, cdp_status, cdp_final, cdp_cookies = fetch_page_via_cdp(
                cdp_url, url, wait_selector=wait_sel, timeout=budget.attempt_timeout()
            )
            if cdp_html and not page_fetch_is_failure(cdp_html, cdp_status):
                if cdp_cookies:
                    if session:
                        for c in cdp_cookies:
                            session.cookies.set(c.get("name"), c.get("value"), domain=c.get("domain") or None, path=c.get("path", "/"))
                    if play_cookies is not None:
                        play_cookies.extend([c for c in cdp_cookies if c not in play_cookies])
                return cdp_html, cdp_status, cdp_final or url
            if cdp_html:
                print("    CDP page is blocked or an error page; falling through to StealthyFetcher...")
        except Exception as e_cdp:
            print(f"    CDP direct tab attachment failed: {e_cdp}. Falling through to StealthyFetcher...")

    # Method 1: requests session (fast, but often blocked by Cloudflare)
    if session and not force_browser and not cdp_url:
        try:
            # P1 C: the requests timeout is a real interrupt, but the
            # old hardcoded 30s could overshoot the phase on its own --
            # clamp it to the remaining phase budget when one is set.
            req_timeout = 30
            if deadline is not None:
                req_timeout = min(30.0, deadline - time.monotonic())
            resp = session.get(url, timeout=req_timeout, verify=False)
            if resp.status_code < 400:
                final_url = resp.url or url
                text = resp.text
                # H1: shared blocked-page check (challenge / login wall),
                # not just the old Cloudflare substring list.
                is_challenge = is_blocked_page(text)
                if HAS_SCANSCI:
                    try:
                        from scansci_pdf.publisher_strategies import _is_challenge_page, _is_elsevier_crasolve
                        if _is_challenge_page(text) or _is_elsevier_crasolve(final_url, text):
                            is_challenge = True
                    except Exception:
                        pass
                if not is_challenge:
                    return text, resp.status_code, final_url
                else:
                    print("    (Cloudflare / anti-bot challenge detected, falling through to browser...)")
        except Exception:
            pass

    # Method 2: Scrapling StealthyFetcher (bypasses Cloudflare)
    # P1-D: skip the browser entirely once this host's tunnel failures
    # tripped the breaker -- the requests path above already had its
    # chance, and another browser launch would only burn the budget.
    # P1 H-1: skip it entirely once the fallback budget is gone, too --
    # the caller degrades to the OA mirror round instead.
    if budget.exhausted:
        budget.announce_exhausted()
        return "", 0, url
    if HAS_SCRAPLING and tracker.is_tripped(url):
        if tracker.take_skip_notice(url):
            print(f"    [Tunnel] {TUNNEL_FAST_FAIL_MESSAGE}: {tunnel_failure_host(url)} "
                  f"(连续 {tracker.consecutive_failures(url)} 次 {TUNNEL_ERROR_MARKER})，跳过浏览器抓取。")
        return "", 0, url
    if HAS_SCRAPLING:
        sc_args = {
            "headless": not headful,
            "solve_cloudflare": True,
            "network_idle": True,
            "timeout": budget.attempt_timeout_ms(),
            "page_action": expand_page_accordions_action,
        }
        # P1-D: same --proxy syntax as journal_downloader.py; omitted
        # entirely when no proxy so unset behaviour is unchanged.
        if proxy:
            sc_args["proxy"] = proxy
        if cdp_url:
            sc_args["cdp_url"] = cdp_url
            print(f"    (Connecting over CDP to live browser: {cdp_url})")
        if force_browser:
            print("    (Forcing browser rendering to execute client-side JS...)")
            sc_args["wait_selector"] = SUPP_WAIT_SELECTOR
        if play_cookies:
            sc_args["cookies"] = normalize_playwright_cookies(play_cookies, default_url=url)

        try:
            page = StealthyFetcher.fetch(url, **sc_args)
            html_content = str(page.html_content)
            final_url = getattr(page, "url", None) or url
            if page_fetch_is_failure(html_content, page.status):
                print("    StealthyFetcher page is blocked or an error page; falling through...")
                if not cdp_url:
                    return "", 0, final_url
                # Retry without CDP via the handler below.
                raise RuntimeError("StealthyFetcher returned a blocked page")
            tracker.record_success(url)
            return html_content, page.status, final_url
        except Exception as e:
            print(f"    StealthyFetcher error: {e}")
            # P1-D: a tunnel failure counts toward the per-host breaker;
            # log the fast-fail line exactly when the threshold is reached.
            if tracker.record_failure(url, e):
                print(f"    [Tunnel] {TUNNEL_FAST_FAIL_MESSAGE}: {tunnel_failure_host(url)} "
                      f"(连续 {tracker.consecutive_failures(url)} 次 {TUNNEL_ERROR_MARKER})，跳过该站点后续浏览器尝试。")
            # P1 H-1: a failed browser attempt that spent the budget is
            # the downgrade point -- say so once, here, so the log shows
            # why the mirror round runs next instead of another browser.
            if budget.exhausted:
                budget.announce_exhausted()
            if cdp_url and not tracker.is_tripped(url) and not budget.exhausted:
                print("    (Retrying without CDP using standalone stealth browser...)")
                try:
                    sc_nocdp = dict(sc_args)
                    sc_nocdp.pop("cdp_url", None)
                    # P1 H-1: re-cap the retry at the remaining budget --
                    # the first attempt already spent goto time.
                    sc_nocdp["timeout"] = budget.attempt_timeout_ms()
                    page = StealthyFetcher.fetch(url, **sc_nocdp)
                    html_content = str(page.html_content)
                    final_url = getattr(page, "url", None) or url
                    if page_fetch_is_failure(html_content, page.status):
                        print("    StealthyFetcher standalone page is blocked or an error page.")
                        return "", 0, final_url
                    tracker.record_success(url)
                    return html_content, page.status, final_url
                except Exception as e2:
                    print(f"    StealthyFetcher standalone error: {e2}")
                    if tracker.record_failure(url, e2):
                        print(f"    [Tunnel] {TUNNEL_FAST_FAIL_MESSAGE}: {tunnel_failure_host(url)} "
                              f"(连续 {tracker.consecutive_failures(url)} 次 {TUNNEL_ERROR_MARKER})，跳过该站点后续浏览器尝试。")
                    # P1 H-1: same downgrade point as the first attempt.
                    if budget.exhausted:
                        budget.announce_exhausted()

    return html_content, 0, final_url


def _finish_success(tier_name, files, article_dir, doi, url, args, config, bsk_runtime=None):
    """Common success handler: print summary, rename folder, check for extra supplements."""
    print(f"\n✓ {tier_name} Success: {len(files)} supplementary file(s) downloaded.")
    print(f"  Output: {os.path.abspath(article_dir)}")

    try:
        lookup_doi = doi if is_doi(extract_doi(doi) or "") else (config.get("_last_api_doi") or doi)
        title = _quick_title_lookup(lookup_doi, url) or config.get("_last_api_title")
        if title:
            clean_t = sanitize_filename(title)
            if not is_placeholder_or_blocked_title(clean_t) and clean_t.lower() != "supplements":
                new_dir = os.path.join(args.output_dir, clean_t)
                if os.path.abspath(new_dir) != os.path.abspath(article_dir):
                    article_dir = migrate_or_merge_article_dir(article_dir, new_dir)
                    print(f"  Renamed to: {os.path.abspath(article_dir)}")
    except Exception:
        pass

    print("\n  Checking for additional supplements via page scrape...")
    scraped_dir = _try_page_scrape_supplements(url, article_dir, config, args, files, bsk_runtime=bsk_runtime)
    if scraped_dir:
        article_dir = scraped_dir

    if getattr(args, "convert_markdown", False):
        print("\n  [Post-Processing] Converting downloaded supplements to Markdown via Microsoft MarkItDown...")
        converted = convert_directory_supplements(article_dir)
        print(f"  Converted {len(converted)} file(s) to Markdown sidecars.")


def _quick_title_lookup(doi, url, allow_oa=True):
    """Try to get article title from Unpaywall / OpenAlex / CrossRef APIs (fast, no auth)."""
    try:
        doi_clean = extract_doi(doi) or extract_doi(url)
        if doi_clean:
            # First check Unpaywall & OpenAlex
            oa_res = resolve_oa_info(doi_clean, timeout=6) if allow_oa else None
            if oa_res and oa_res.title:
                clean_title = html.unescape(oa_res.title)
                clean_title = re.sub(r'[\\/*?:"<>|]', "_", clean_title)
                clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                if len(clean_title) > 120:
                    clean_title = clean_title[:120]
                return clean_title

            # Fallback to CrossRef API
            resp = requests.get(
                f"https://api.crossref.org/works/{doi_clean}",
                timeout=10,
                headers={"User-Agent": "ScanSci-Supp-Downloader/2.0 (mailto:scansci@example.com)"},
            )
            if resp.status_code == 200:
                data = resp.json()
                titles = data.get("message", {}).get("title", [])
                if titles:
                    title = html.unescape(titles[0])
                    title = re.sub(r'[\\/*?:"<>|]', "_", title)
                    title = re.sub(r'\s+', ' ', title).strip()
                    if len(title) > 120:
                        title = title[:120]
                    return title
    except Exception:
        pass
    return None


def _try_page_scrape_supplements(url, article_dir, config, args, existing_files, bsk_runtime=None):
    """
    After CDN success, scrape the page for non-CDN supplement links.
    Falls back to browser rendering if raw HTML is blocked or incomplete (Fixing Issue #4).
    """
    try:
        cdp_target = getattr(args, "cdp_url", None)
        # P1-D: proxy / tunnel breaker for this run's browser attempts.
        _scrape_proxy = getattr(args, "proxy", None)
        # P1 H-1: same 30s-goto / 60s-total fallback budget as the main
        # run (fresh here: this scrape is a separate post-success pass).
        _scrape_budget = browser_budget_from_args(args)
        session, play_cookies = _setup_authenticated_session_and_cookies(config, args, cdp_target=cdp_target)

        html_content, status, final_url = fetch_page_html(
            url,
            session,
            play_cookies,
            args.headful,
            use_bsk=bool(bsk_runtime and getattr(args, "browserskill", False)),
            bsk_runtime=bsk_runtime,
            proxy=_scrape_proxy,
            tunnel_tracker=_TUNNEL_TRACKER,
            browser_budget=_scrape_budget,
        )

        url_to_use = final_url or url
        effective_base = extract_effective_base_url(html_content, url_to_use)
        diag = find_all_candidates(html_content, effective_base, original_url=url)

        # Fallback to browser rendering if raw HTML yields no candidates or is incomplete
        # H3: also force the render when the static page shows dynamic
        # signals (diag.needs_browser) but the candidate set is empty or
        # the evidence check still reports missing files. The evidence
        # verdict is used here, not diag.is_incomplete alone: the two can
        # disagree (e.g. a singular "Additional file (7)" declaration is
        # seen by the legacy evidence file count but not by the H5 count).
        # When the supplements are already complete, do not force.
        _scrape_evidence = DiscoveryEvidence()
        _scrape_evidence.observe("publisher", html_content, effective_base)
        _scrape_inc, _ = _scrape_evidence.check(diag.candidates)
        _needs_browser_retry = bool(diag.needs_browser) and (not diag.candidates or _scrape_inc)
        if (not diag.candidates or diag.is_incomplete or _needs_browser_retry) and (HAS_SCRAPLING or (bsk_runtime and bsk_runtime.enabled)):
            print("    Raw HTML incomplete or blocked. Retrying with browser rendering for page scrape...")
            html_content_b, status_b, final_url_b = fetch_page_html(
                url,
                session,
                play_cookies,
                args.headful,
                force_browser=True,
                use_bsk=bool(bsk_runtime and bsk_runtime.enabled),
                bsk_runtime=bsk_runtime,
                proxy=_scrape_proxy,
                tunnel_tracker=_TUNNEL_TRACKER,
                browser_budget=_scrape_budget,
            )
            if html_content_b:
                html_content = html_content_b
                url_to_use = final_url_b or url_to_use
                effective_base = extract_effective_base_url(html_content, url_to_use)
                diag = find_all_candidates(html_content, effective_base, original_url=url)

        if not html_content:
            print("    Could not fetch page for additional links.")
            return article_dir

        # If folder is still a placeholder (e.g. "supplements" when input was a PII URL without DOI),
        # upgrade to the article title discovered from the scraped HTML.
        cur_basename = os.path.basename(os.path.normpath(article_dir))
        if cur_basename.lower() == "supplements" or is_placeholder_or_blocked_title(cur_basename):
            scraped_title = extract_article_title(html_content, default="")
            if scraped_title and not is_placeholder_or_blocked_title(scraped_title):
                new_dir = os.path.join(args.output_dir, scraped_title)
                if os.path.abspath(new_dir) != os.path.abspath(article_dir):
                    article_dir = migrate_or_merge_article_dir(article_dir, new_dir)
                    print(f"    Renamed folder from HTML title to: {os.path.abspath(article_dir)}")

        manifest = load_download_manifest(article_dir)
        downloaded_urls = {manifest_entry_url(v) for v in manifest.values()}
        existing_basenames = {os.path.basename(f) for f in existing_files}
        # P1-B: resume set by normalized filename, not just URL: a file
        # already downloaded via another mirror URL -- or left behind as a
        # _2 copy by an earlier run -- counts as already downloaded.
        downloaded_fnames = {
            k for k in (
                filename_dedup_key(name)
                for name in list(manifest)
                + [os.path.basename(f) for f in existing_files]
            ) if k is not None
        }
        if os.path.exists(article_dir):
            for f in os.listdir(article_dir):
                if f.startswith("."):
                    continue
                key = filename_dedup_key(f)
                if key is not None:
                    downloaded_fnames.add(key)
        downloaded_mmcs = set()
        for k, v in manifest.items():
            for s in (k, manifest_entry_url(v)):
                m = MMC_NUM_REGEX.search(s)
                if m:
                    downloaded_mmcs.add(int(m.group(1)))
        for f in existing_files:
            m = MMC_NUM_REGEX.search(os.path.basename(f))
            if m:
                downloaded_mmcs.add(int(m.group(1)))
        if os.path.exists(article_dir):
            for f in os.listdir(article_dir):
                # Skip dotfiles: H9 temp files (and the manifest) are
                # never downloaded files, even if a hard kill left one.
                if f.startswith("."):
                    continue
                m_f = MMC_NUM_REGEX.search(f)
                if m_f:
                    downloaded_mmcs.add(int(m_f.group(1)))

        seen_cand_urls = set()
        seen_cand_fnames = set()
        new_candidates = []
        for cand in diag.candidates:
            if cand.url in downloaded_urls or cand.url in seen_cand_urls:
                continue
            cand_fname = cand.filename or extract_filename_from_url(cand.url)
            if cand_fname in existing_basenames:
                continue
            # P1-B: same filename (normalized, _2 suffix ignored) already
            # downloaded in this run / a previous run, or seen twice in
            # this batch under different URLs -- download it once.
            cand_fkey = filename_dedup_key(cand_fname)
            if cand_fkey is not None and (cand_fkey in downloaded_fnames or cand_fkey in seen_cand_fnames):
                continue
            # Skip if candidate refers to an MMC file that was already downloaded
            m_cand = MMC_NUM_REGEX.search(cand.url + " " + cand_fname)
            if m_cand and int(m_cand.group(1)) in downloaded_mmcs:
                continue
            seen_cand_urls.add(cand.url)
            if cand_fkey is not None:
                seen_cand_fnames.add(cand_fkey)
            new_candidates.append(cand)

        if new_candidates:
            print(f"    Found {len(new_candidates)} additional supplement link(s):")
            for cand in new_candidates:
                print(f"      • {cand.filename}: {cand.url} ({cand.match_rule})")
            for cand in new_candidates:
                download_file(
                    cand,
                    article_dir,
                    session,
                    play_cookies,
                    bsk_runtime=bsk_runtime,
                    referer=url_to_use,
                    proxy=_scrape_proxy,
                    tunnel_tracker=_TUNNEL_TRACKER,
                )
        else:
            print("    No additional supplements found beyond CDN files.")

        # Check if page mentions MMC files that were not caught by HTML links or CDN
        pii = extract_pii_from_url(url_to_use) or extract_pii_from_url(url)
        if pii and not getattr(args, "skip_cdn", False):
            page_mmcs = {int(m.group(1)) for m in MMC_NUM_REGEX.finditer(html_content)}
            downloaded_mmcs = set()
            manifest = load_download_manifest(article_dir)
            for k, v in manifest.items():
                for s in (k, manifest_entry_url(v)):
                    m = MMC_NUM_REGEX.search(s)
                    if m:
                        downloaded_mmcs.add(int(m.group(1)))
            for f in existing_files:
                m = MMC_NUM_REGEX.search(os.path.basename(f))
                if m:
                    downloaded_mmcs.add(int(m.group(1)))
            if os.path.exists(article_dir):
                for f in os.listdir(article_dir):
                    # Skip dotfiles: H9 temp files (and the manifest) are
                    # never downloaded files, even if a hard kill left one.
                    if f.startswith("."):
                        continue
                    m_f = MMC_NUM_REGEX.search(f)
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
    except BrowserSkillError:
        raise
    except Exception as e:
        print(f"    Page scrape error: {e}")
    return article_dir


# ── Main Logic ───────────────────────────────────────────────────────────────

def print_tier_preserved_files(api_files, cdn_files, article_dir):
    """Confirm Tier A / Tier 0 files by name (P1-F).

    Used when no candidate list was found: the old line (``Preserved N
    API/CDN file(s); page completeness remains unverified``) hid which
    files those were -- #47's 3 mmc files were only visible by counting
    files on disk. Each file is listed with its size; a path that no
    longer resolves on disk (e.g. the folder was renamed after the
    download) is reported as such instead of being silently counted.

    Returns the number of listed files verified on disk (P1-G2).
    """
    seen = set()
    on_disk = 0
    for tier, files in (("Tier A", api_files or []), ("Tier 0", cdn_files or [])):
        for path in files:
            name = os.path.basename(path)
            if name in seen:
                continue
            seen.add(name)
            resolved = path if os.path.exists(path) else os.path.join(article_dir, name)
            if os.path.exists(resolved):
                on_disk += 1
                size_kb = os.path.getsize(resolved) / 1024
                print(f"  [{tier}] \u2713 {name} ({size_kb:.1f} KB, downloaded via {tier}, validated on download)")
            else:
                print(f"  [{tier}] ? {name} (listed as downloaded but not found on disk)")
    total = len(seen)
    print(f"  [Info] {total} API/CDN file(s) preserved; no candidate list was found, "
          f"so page completeness cannot be verified from the page.")
    return on_disk


def preserved_done_line(count, article_dir):
    """Countable Done line for the preserved-files path (P1-G2).

    Round-3 #13 (11/11) and #47 (3/3) downloaded via Tier 0 yet were
    scored failed-timeout downstream because this path printed no
    "✓ Done:" line at all. The count is real (files verified on disk)
    but there is no honest denominator -- page completeness is
    unverified -- so the line deliberately carries no N/M fraction.
    """
    return (f"\u2713 Done: {count} file(s) downloaded via Tier A/CDN "
            f"(page completeness unverified) to {os.path.abspath(article_dir)}")


# ── P1-E2: Phase B seeds from the Phase A candidate list ──────────────────

SEED_CANDIDATES_FILENAMES = (
    "supplementary_candidates.json",
    "supplementary_candidates.partial.json",
)


def _seed_search_dirs(article_dir):
    """Directories to search for a Phase A seed list, in order.

    article_dir itself first, then each immediate subdirectory of
    its parent (the -o output dir), sorted by name for determinism.
    Phase A writes the list into <output>/<Phase A title>/ while
    Phase B's article_dir can be a different sibling --
    <output>/supplements, or a title extracted from a different page
    source (publisher HTML vs OA mirror title, or a failed fetch
    leaving the placeholder) -- and run_download only ever
    migrates/merges <output>/supplements, never a sibling title dir,
    so the file is often one directory over.

    Only one level down: the seed payload carries no DOI/title to
    attribute a file with, so a recursive walk could pick up another
    article's list when -o is shared across papers. With the usual
    per-article -o (one output dir per paper, as the test runner
    uses), every sibling here is a title variant of the same article.
    """
    root = os.path.abspath(article_dir)
    dirs = [root]
    parent = os.path.dirname(root)
    if parent and parent != root:
        try:
            entries = sorted(os.scandir(parent), key=lambda e: e.name)
        except OSError:
            entries = []
        for entry in entries:
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir and os.path.abspath(entry.path) != root:
                dirs.append(os.path.abspath(entry.path))
    return dirs


def load_seed_candidates(article_dir):
    """Load the Phase A candidate list for this article (P1-E2).

    Phase A (journal_downloader.py) streams every discovered candidate
    to supplementary_candidates(.partial).json in the article directory
    so a timeout leaves them readable on disk. Phase B used to
    re-discover from scratch and never read that file: round 3
    preserved 348 candidates across 15 papers that nobody downloaded
    from.

    The list is searched for in article_dir itself first, then in
    each immediate subdirectory of the output dir (see
    _seed_search_dirs): Phase A and Phase B can resolve different
    titles for the same article, so the file often sits in a sibling
    title directory. The final list wins over the partial one across
    locations; within one filename kind, article_dir wins, then
    siblings in name order. A corrupt file falls through to the next
    candidate location instead of aborting the run; a parseable file
    -- even one with zero candidates -- is authoritative and stops
    the search, as before. Entries without a URL are skipped.

    Returns (candidates, source) -- ([], None) when nothing usable
    is on disk. source is the bare filename for a file in article_dir
    itself, otherwise "<subdir>/<filename>" so the log line shows
    which title directory the seed came from.
    """
    search_dirs = _seed_search_dirs(article_dir)
    root_abs = os.path.abspath(article_dir)
    for name in SEED_CANDIDATES_FILENAMES:
        for directory in search_dirs:
            path = os.path.join(directory, name)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                continue
            entries = payload.get("candidates", []) if isinstance(payload, dict) else []
            cands = []
            for entry in entries:
                cand = Candidate.from_dict(entry)
                if cand is not None:
                    cands.append(cand)
            if os.path.abspath(directory) == root_abs:
                source = name
            else:
                source = os.path.join(os.path.basename(directory), name)
            return cands, source
    return [], None


def merge_seed_candidates(candidates, seeds):
    """Append seed candidates whose URL is new; return (merged, added).

    Lossless and input-preserving: existing candidates keep their
    order, seeds follow in file order, duplicates by URL are skipped
    (the later dedupe_candidates pass still handles filename-level
    duplicates across tiers).
    """
    known = {c.url for c in candidates}
    added = [c for c in seeds if c.url not in known]
    return list(candidates) + added, len(added)


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
    # P1-D: same --proxy syntax as journal_downloader.py; plumbed to the
    # StealthyFetcher / StealthySession browser layer.
    parser.add_argument("--proxy", default=None, help="Proxy URL (e.g. http://user:pass@host:port)")
    parser.add_argument("--tunnel-fail-threshold", type=int, default=DEFAULT_TUNNEL_FAILURE_THRESHOLD,
                        help="Consecutive ERR_TUNNEL_CONNECTION_FAILED failures per host before fast-failing "
                             "that host's browser attempts (default: 3)")
    # P1-CE: same single-phase budget as journal_downloader.py (default
    # 150s); the download loop stops cleanly at the deadline instead of
    # being killed mid-list.
    parser.add_argument("--phase-timeout", type=float, default=DEFAULT_PHASE_TIMEOUT_SECONDS,
                        help="Overall budget in seconds for this phase (discovery + download). "
                             "On expiry, downloading stops and already-downloaded files are kept. "
                             "Every page load is additionally capped at the remaining phase budget, "
                             "so one load can never run past the deadline (default: 150)")
    # P1 H-1: the browser fallback gets its own quotas -- one page goto
    # at most 30s, 60s of browser time in total per phase. Either quota
    # running out degrades to the OA mirror round instead of killing
    # the phase inside one page load (round 3: #1 hung 120s+ on
    # nature.com, mirrors never ran, 0 files).
    # P1 C: both quotas are additionally capped at the remaining phase
    # budget -- a single goto runs at most min(goto, total remaining,
    # phase remaining), and the fallback budget counts as exhausted
    # once the phase deadline passes.
    parser.add_argument("--browser-goto-timeout", type=float, default=DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS,
                        help="Max seconds for one browser page load (goto) in the "
                             "fallback path, capped at the remaining phase budget "
                             "(default: 30)")
    parser.add_argument("--browser-fallback-budget", type=float, default=DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS,
                        help="Max total seconds of browser fallback time per phase, "
                             "capped at the remaining phase budget; "
                             "once spent, the run degrades to the OA mirror round "
                             "instead of launching another browser (default: 60)")
    parser.add_argument("--list-only", action="store_true", help="Only list found links with diagnostic evidence, don't download")
    parser.add_argument("--oa", action="store_true", help="Prioritize / prefer Open Access repository mirrors (PMC, Europe PMC, etc.) via Unpaywall & OpenAlex")
    parser.add_argument("--no-oa", action="store_true", help="Disable automatic Open Access repository mirror query and penetration")
    parser.add_argument("--cdp", action="store_true", help="Connect to running Chromium browser via CDP (inherits live session, cookies, Cloudflare bypass)")
    parser.add_argument("--cdp-url", default=None, help="Custom CDP endpoint URL or port (e.g. http://127.0.0.1:9222, ws://... or 9222)")
    add_browserskill_arguments(parser)
    parser.add_argument("--convert-markdown", "--markitdown", dest="convert_markdown", action="store_true", help="Convert downloaded supplementary documents (.docx, .xlsx, .pdf) to Markdown via Microsoft MarkItDown")
    args = parser.parse_args()

    # P1-D: fail fast on a malformed --proxy instead of silently going
    # direct (run_download re-validates for direct callers).
    if args.proxy:
        try:
            playwright_proxy_config(args.proxy)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

    with runtime_from_args(args, parser) as bsk_runtime:
        try:
            return run_download(args, bsk_runtime)
        except BrowserSkillError as exc:
            print(f"BrowserSkill: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc


def run_download(args, bsk_runtime):

    # P1-CE: the phase budget (default 150s, --phase-timeout), shared by
    # discovery and the download loop below.
    phase_timeout = resolve_phase_timeout(getattr(args, "phase_timeout", None))
    phase_deadline = time.monotonic() + phase_timeout
    print(f"  Phase timeout: {phase_timeout:g}s")

    # P1 H-1: the browser fallback's own quota (default: one goto 30s,
    # 60s total per phase), shared by every browser attempt below.
    # P1 C: the run's phase deadline is passed in as well, so the
    # fallback quota can never outlive the phase: one attempt runs at
    # most min(goto, total remaining, phase remaining), and the budget
    # counts as exhausted once the phase deadline passes.
    browser_budget = browser_budget_from_args(args, phase_deadline=phase_deadline)
    print(f"  Browser fallback budget: {browser_budget.goto_timeout:g}s per page, "
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

    # P1-D: validate the proxy up front (fail fast on a typo'd value
    # instead of silently going direct) and arm the per-host tunnel
    # breaker for this run with the CLI threshold.
    proxy = getattr(args, "proxy", None)
    if proxy:
        playwright_proxy_config(proxy)
        print(f"  Proxy: {redact_proxy_url(proxy)}")
    try:
        _TUNNEL_TRACKER.threshold = max(
            1, int(getattr(args, "tunnel_fail_threshold", DEFAULT_TUNNEL_FAILURE_THRESHOLD)
                   or DEFAULT_TUNNEL_FAILURE_THRESHOLD))
    except (TypeError, ValueError):
        _TUNNEL_TRACKER.threshold = DEFAULT_TUNNEL_FAILURE_THRESHOLD
    _TUNNEL_TRACKER.reset()

    config = load_config() if HAS_SCANSCI else {}
    if os.environ.get("ELSEVIER_API_KEY"):
        config["elsevier_api_key"] = os.environ["ELSEVIER_API_KEY"].strip()
    if os.environ.get("IS_CAMPUS_NETWORK"):
        val = os.environ["IS_CAMPUS_NETWORK"].strip().lower()
        config["is_campus_network"] = val in ("1", "true", "yes", "y")
    if os.environ.get("ELSEVIER_INSTTOKEN"):
        config["elsevier_insttoken"] = os.environ["ELSEVIER_INSTTOKEN"].strip()
    os.makedirs(args.output_dir, exist_ok=True)

    if getattr(args, "browserskill", False):
        print("  [BrowserSkill] Tencent BrowserSkill mode active")

    cdp_target = args.cdp_url
    if args.cdp and not cdp_target:
        detected = discover_active_cdp_port()
        if detected:
            print(f"  [CDP] Discovered running browser at {detected[0]} (ws: {detected[1]})")
            cdp_target = detected[0]
        else:
            print("  [CDP] No active browser CDP port detected.")

    ws_cdp_url = resolve_cdp_ws_url(cdp_target) if cdp_target else None
    if ws_cdp_url:
        print(f"  [CDP] Live browser session active: {ws_cdp_url}")

    # ── Step 1: Resolve DOI / URL ─────────────────────────────────────────
    doi = args.url_or_doi
    url = resolve_doi_url(doi)
    url = normalize_url(url)
    print(f"  Normalized:  {url}")

    # Extract PII for Elsevier CDN (before WebVPN transformation)
    pii = extract_pii_from_url(url) or extract_pii_from_url(doi)

    # WebVPN URL conversion if configured and not disabled
    if not args.no_vpn and HAS_SCANSCI and vpnsci_is_configured(config):
        webvpn_base = _get_webvpn_base(config)
        vpn_url = convert_url(url, webvpn_base, config)
        if vpn_url and vpn_url != url:
            print(f"  WebVPN URL:  {vpn_url}")
            url = vpn_url

    if pii:
        print(f"  Elsevier PII: {pii}")

    # ── Step 2: Tier A — Elsevier API Campus-IP (fastest if on campus) ────
    article_dir = os.path.join(args.output_dir, "supplements")

    api_files = []
    known_mmcs = set()
    is_elsevier_target = (
        "10.1016" in str(doi)
        or "10.1016" in str(url)
        or bool(pii)
        or any(d in str(url).lower() for d in ("sciencedirect.com", "elsevier.com", "cell.com", "thelancet.com"))
    )
    if not args.no_api and not args.list_only and is_elsevier_target:
        api_files, detected_mmcs = try_elsevier_api_campus(doi, pii, article_dir, config)
        known_mmcs.update(detected_mmcs)

    # Partial API success must still pass through CDN, page and OA discovery.

    # ── Step 3: Tier 0 — CDN Brute-Force (fast, no auth) ─────────────────
    cdn_files = []
    api_mmc_ids = {int(match.group(1)) for path in api_files
                   if (match := MMC_NUM_REGEX.search(os.path.basename(path)))}
    api_missing_mmcs = known_mmcs - api_mmc_ids
    if pii and not args.skip_cdn and not args.list_only and (not api_files or api_missing_mmcs):
        cdn_files = try_elsevier_cdn_brute_force(
            pii, article_dir,
            max_mmc=args.max_mmc,
            max_consecutive_misses=args.max_misses,
            known_mmcs=known_mmcs,
        )

    # Keep successful artifacts while discovering remaining sources.

    # ── Step 4: Tier 1/2 — Page Scrape + Link Download ───────────────────
    print(f"\n[Tier 1/2] Fetching article page for supplement links...")

    session, play_cookies = _setup_authenticated_session_and_cookies(config, args, cdp_target=cdp_target)

    # Fetch page with redirect resolution (Fixing Issue #1)
    # P1 C: the phase deadline rides along, so a load that starts
    # after the deadline never starts, and a load that returns late
    # stops the phase instead of starting the next stage.
    html_content, status, final_url = fetch_page_html(
        url,
        session,
        play_cookies,
        args.headful,
        cdp_url=ws_cdp_url,
        use_bsk=getattr(args, "browserskill", False),
        bsk_browser=getattr(args, "bsk_browser", None),
        bsk_runtime=bsk_runtime,
        bsk_tab=getattr(args, "bsk_tab", None),
        proxy=proxy,
        tunnel_tracker=_TUNNEL_TRACKER,
        browser_budget=browser_budget,
        deadline=phase_deadline,
    )
    if (not html_content or (status is not None and status >= 400)) and bsk_runtime.enabled and not args.browserskill and _browser_allowed():
        try:
            html_content, status, final_url = fetch_page_via_browserskill(
                url, timeout=browser_budget.attempt_timeout(), runtime=bsk_runtime)
        except BrowserSkillError as exc:
            if bsk_runtime.browser or bsk_runtime.tab_id:
                raise
            print(f"  [BrowserSkill] Recovery failed: {exc}")
    if not html_content:
        print("  ERROR: Failed to fetch article page.")
        print("  Continuing with OA discovery where a DOI is available.")
        html_content = ""
        final_url = final_url or url

    url = final_url
    effective_base = extract_effective_base_url(html_content, url)
    print(f"  Status:    {status}")
    if url != args.url_or_doi:
        print(f"  Final URL: {url}")
    if effective_base != url:
        print(f"  Base URL:  {effective_base}")

    article_dirname = extract_article_title(html_content, default="supplements")
    target_dir = os.path.join(args.output_dir, article_dirname)
    article_dir = migrate_or_merge_article_dir(article_dir, target_dir) if os.path.exists(article_dir) else target_dir
    print(f"  Title:     {article_dirname}")
    print(f"  Folder:    {article_dir}")

    def _refresh_dir_from_html_or_title(new_html=None, raw_title=None):
        nonlocal article_dirname, article_dir
        new_t = ""
        if raw_title and not is_placeholder_or_blocked_title(raw_title):
            new_t = sanitize_filename(raw_title)
        elif new_html:
            extracted = extract_article_title(new_html, default="")
            if extracted and not is_placeholder_or_blocked_title(extracted):
                new_t = extracted
        if not new_t or is_placeholder_or_blocked_title(new_t):
            return
        if is_placeholder_or_blocked_title(article_dirname) or new_t != article_dirname:
            target_dir = os.path.join(args.output_dir, new_t)
            if os.path.abspath(target_dir) != os.path.abspath(article_dir):
                if os.path.exists(article_dir):
                    article_dir = migrate_or_merge_article_dir(article_dir, target_dir)
                else:
                    article_dir = target_dir
                article_dirname = os.path.basename(article_dir)
                print(f"  Updated Title:  {article_dirname}")
                print(f"  Updated Folder: {article_dir}")

    # Scan for candidates with unified rules and diagnostics
    print("\n  Scanning for supplementary data links...")
    diag = find_all_candidates(html_content, effective_base, original_url=args.url_or_doi)
    candidates = diag.candidates
    evidence = DiscoveryEvidence()
    evidence.observe("publisher", html_content, effective_base)

    # Check completeness: trigger browser rendering on partial hits (Fixing Issue #4)
    # H3: also force a browser render when the static page shows dynamic
    # signals (diag.needs_browser) but the candidate set is empty or the
    # evidence check still reports missing files. When the supplements
    # are already complete, do not force.
    is_inc, inc_reason = evidence.check(candidates)

    # P1 H-1: the OA mirror round runs BEFORE any browser fallback.
    # Mirrors are fetched over requests (cheap, seconds), while one
    # browser page load over the proxy takes 60-90s -- in round 3 the
    # browser retry ran first, hung, and the mirrors never ran at all
    # (#1: 13 files in R2, 0 in R3). Files reachable without a browser
    # are therefore collected first; the browser retry below only runs
    # for what the mirrors did not already cover, and only while its
    # own budget lasts.
    doi_val = (
        extract_doi(args.url_or_doi)
        or extract_doi(url)
        or extract_doi(effective_base)
        or extract_doi_from_html(html_content)
        or config.get("_last_api_doi")
    )
    def fetch_mirror(oa_url):
        return fetch_page_html(oa_url, session, play_cookies, args.headful,
                               proxy=proxy, tunnel_tracker=_TUNNEL_TRACKER,
                               browser_budget=browser_budget,
                               deadline=phase_deadline)

    mirrors = MirrorDiscovery(doi_val, not args.no_oa, resolve_oa_info, fetch_mirror, evidence)
    # P1 C: the mirror round is a page-load round -- it must not start
    # once the phase deadline has passed inside the publisher fetch
    # above (round 3: the phase died in a load and the next rounds
    # still launched loads until the runner killed the process). The
    # candidates found so far still reach the download loop below,
    # where download_with_recovery prints the graceful-stop line.
    if (not candidates or is_inc or args.oa) and time.monotonic() < phase_deadline:
        known = {c.url for c in candidates}
        additions = [c for c in mirrors.discover() if c.url not in known]
        candidates = additions + candidates if args.oa else candidates + additions
    if mirrors.info and mirrors.info.title and is_placeholder_or_blocked_title(article_dirname):
        _refresh_dir_from_html_or_title(raw_title=mirrors.info.title)

    # P1-E2: seed from the Phase A candidate list left in this article
    # directory (separate Phase A process, same -o). Without this, a
    # Phase A timeout preserved candidates on disk that Phase B never
    # downloaded from (round 3: 348 candidates across 15 papers).
    # P1-E remainder: the loader also checks the sibling title
    # directories under the same output dir -- Phase A and Phase B can
    # resolve different titles, so the list often sits one directory
    # over from article_dir (final still wins over partial, corrupt
    # files still fall through to the next location).
    seed_candidates, seed_source = load_seed_candidates(article_dir)
    if seed_candidates:
        candidates, seeds_added = merge_seed_candidates(candidates, seed_candidates)
        if seeds_added:
            print(f"  [Info] Seeded {seeds_added} candidate(s) from Phase A "
                  f"candidate list ({seed_source}).")

    is_inc, inc_reason = evidence.check(candidates)
    needs_browser_retry = bool(diag.needs_browser) and (not candidates or is_inc)
    if (not candidates or is_inc or needs_browser_retry) and HAS_SCRAPLING and session and not args.browserskill and _browser_allowed():
        print(f"  Incomplete candidate set ({inc_reason}). Falling back to browser rendering (JS execution)...")
        html_content_browser, status_browser, final_url_browser = fetch_page_html(
            url, session, play_cookies, args.headful, force_browser=True, cdp_url=ws_cdp_url,
            proxy=proxy, tunnel_tracker=_TUNNEL_TRACKER,
            browser_budget=browser_budget,
        )
        if html_content_browser:
            url = final_url_browser or url
            effective_base_b = extract_effective_base_url(html_content_browser, url)
            evidence.observe("publisher", html_content_browser, effective_base_b)
            diag_b = find_all_candidates(html_content_browser, effective_base_b, original_url=args.url_or_doi)
            seen_urls = {c.url for c in candidates}
            new_candidates = [c for c in diag_b.candidates if c.url not in seen_urls]
            if new_candidates:
                candidates.extend(new_candidates)
                html_content = html_content_browser
                diag = diag_b
                print(f"  Found {len(new_candidates)} additional candidate(s) after browser rendering (total: {len(candidates)}).")
                _refresh_dir_from_html_or_title(new_html=html_content)

    is_inc, inc_reason = evidence.check(candidates)

    # Tier 3 (CDP Live Browser Probe): Only when CDP is explicitly requested via --cdp / --cdp-url
    # H3: needs_browser also forces the probe (same rule as above).
    needs_browser_retry = bool(diag.needs_browser) and (not candidates or is_inc)
    if (args.cdp or args.cdp_url) and (not candidates or is_inc or needs_browser_retry) and not ws_cdp_url and HAS_SCRAPLING and not bsk_runtime.enabled and _browser_allowed():
        detected = discover_active_cdp_port()
        if detected:
            print(f"\n[Tier 3] Probing via running browser CDP session ({detected[0]})...")
            try:
                live_auto_c = extract_cdp_cookies(detected[0])
                if live_auto_c:
                    print(f"  [Tier 3] Injected {len(live_auto_c)} session cookies from browser into downloader.")
                    for c in live_auto_c:
                        session.cookies.set(
                            c.get("name", ""), c.get("value", ""),
                            domain=c.get("domain") or None, path=c.get("path", "/"),
                        )
                    play_cookies = merge_cookies(play_cookies, live_auto_c)
            except Exception:
                pass

            cdp_html, cdp_status, cdp_final = fetch_page_html(
                url, session, play_cookies, args.headful, force_browser=True, cdp_url=detected[0],
                proxy=proxy, tunnel_tracker=_TUNNEL_TRACKER,
                browser_budget=browser_budget,
            )
            if cdp_html:
                cdp_base = extract_effective_base_url(cdp_html, cdp_final or url)
                evidence.observe("publisher", cdp_html, cdp_base)
                cdp_diag = find_all_candidates(cdp_html, cdp_base, original_url=args.url_or_doi)
                if cdp_diag.candidates:
                    print(f"  ✓ Tier 3 CDP success! Found {len(cdp_diag.candidates)} supplementary file(s) via live browser.")
                    seen_urls = {c.url for c in candidates}
                    new_cands = [c for c in cdp_diag.candidates if c.url not in seen_urls]
                    candidates.extend(new_cands)
                    if not candidates:
                        candidates = list(cdp_diag.candidates)
                    diag = cdp_diag
                    html_content = cdp_html
                    effective_base = cdp_base
                    _refresh_dir_from_html_or_title(new_html=cdp_html)

    is_inc, inc_reason = evidence.check(candidates)

    # Tier 3-BSK (BrowserSkill Probe): If still no candidates/incomplete, probe via Tencent BrowserSkill
    # H3: needs_browser also forces the probe (same rule as above).
    needs_browser_retry = bool(diag.needs_browser) and (not candidates or is_inc)
    if (not candidates or is_inc or needs_browser_retry) and bsk_runtime.enabled and _browser_allowed():
        print(f"\n[Tier 3-BSK] Probing via Tencent BrowserSkill (bsk)...")
        try:
            bsk_html, bsk_status, bsk_final = fetch_page_via_browserskill(
                url,
                timeout=browser_budget.attempt_timeout(),
                browser=getattr(args, "bsk_browser", None),
                tab_id=getattr(args, "bsk_tab", None),
                runtime=bsk_runtime,
            )
            if bsk_html and not page_fetch_is_failure(bsk_html, bsk_status):
                bsk_base = extract_effective_base_url(bsk_html, bsk_final or url)
                evidence.observe("publisher", bsk_html, bsk_base)
                bsk_diag = find_all_candidates(bsk_html, bsk_base, original_url=args.url_or_doi)
                if bsk_diag.candidates:
                    print(f"  ✓ Tier 3-BSK success! Found {len(bsk_diag.candidates)} supplementary file(s) via BrowserSkill.")
                    seen_urls = {c.url for c in candidates}
                    new_cands = [c for c in bsk_diag.candidates if c.url not in seen_urls]
                    candidates.extend(new_cands)
                    if not candidates:
                        candidates = list(bsk_diag.candidates)
                    diag = bsk_diag
                    html_content = bsk_html
                    effective_base = bsk_base
                    url = bsk_final or url
                    _refresh_dir_from_html_or_title(new_html=bsk_html)
        except Exception as e_bsk:
            if bsk_runtime.browser or bsk_runtime.tab_id:
                raise
            print(f"  [Tier 3-BSK] Notice: {e_bsk}")

    is_inc, inc_reason = evidence.check(candidates)

    if not candidates:
        if pii and not args.skip_cdn and not args.list_only:
            page_mmcs = {int(m.group(1)) for m in MMC_NUM_REGEX.finditer(html_content)}
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
                        title = ((mirrors.info.title if mirrors.info else None) or config.get("_last_api_title")
                             or _quick_title_lookup(doi, url, allow_oa=False))
                        if title:
                            _refresh_dir_from_html_or_title(raw_title=title)
                    except Exception:
                        pass
                    print(f"\n✓ Done: {len(cdn_extras)} files downloaded via targeted CDN probe to {os.path.abspath(article_dir)}")
                    return

        if api_files or cdn_files:
            # P1-F: confirm each Tier A / Tier 0 file by name instead
            # of the vague "Preserved ..." line.
            preserved_count = print_tier_preserved_files(api_files, cdn_files, article_dir)
            # P1-G2: countable Done line so downstream status parsing
            # sees the download (round-3 #13/#47 scored failed-timeout
            # with dl_done empty despite 11/11 and 3/3 on disk).
            if preserved_count:
                print(preserved_done_line(preserved_count, article_dir))
            if args.convert_markdown:
                convert_directory_supplements(article_dir)
        print("  No supplementary links found in HTML.")
        # P1 C: say so explicitly when the phase died inside a page
        # load with nothing found -- otherwise a run killed by the
        # budget looks identical to a paper with no supplements, and
        # the graceful-stop line never appears (round 3: 0 times).
        if time.monotonic() >= phase_deadline:
            print(f"  [Warning] Phase timeout ({phase_timeout:g}s budget) reached; "
                  f"no candidates were found before the budget ran out.")
        if diag.incomplete_reason:
            print(f"  [Diagnostic] {diag.incomplete_reason}")
        if diag.excluded_links:
            print(f"  [Diagnostic] Evaluated and excluded {len(diag.excluded_links)} links (figures/references/anchors).")
        if pii and not (api_files or cdn_files):
            print("  (CDN brute-force also found nothing — this paper may have no supplements.)")
        return

    candidates = expand_repository_candidates(candidates)
    # P1-B: tiers / mirrors can re-discover the same file under another URL
    # (Springer vs PMC, :443 vs plain); download it once.
    candidates = dedupe_candidates(candidates)
    # P1-CE: download in priority order (real supplement files first,
    # external/repository pages last); the sort is lossless, so the list
    # below is the order downloads will be attempted in.
    discovery_order = list(candidates)
    candidates = sort_candidates_by_priority(candidates)
    diag.candidates = candidates
    diag.is_incomplete, diag.incomplete_reason = evidence.check(candidates)

    print(f"  Found {len(candidates)} potential supplementary file(s):")
    for i, cand in enumerate(candidates, 1):
        print(f"    [{i}] {cand.filename}")
        print(f"        URL:     {cand.url}")
        if cand.link_text:
            print(f"        Text:    {cand.link_text}")
        print(f"        Section: {cand.section} | Rule: {cand.match_rule}")
    if candidates != discovery_order:
        print("  [Info] Candidates reordered by priority for download "
              "(real supplement files first, external pages last).")

    if evidence.describe():
        print(f"  [Info] {evidence.describe()}")
    if diag.is_incomplete:
        print(f"  [Warning] Result may be incomplete: {diag.incomplete_reason}")

    if args.list_only:
        return

    # Download files
    os.makedirs(article_dir, exist_ok=True)
    print(f"\n  Downloading {len(candidates)} file(s)...")
    # H8: no separate transport exists at this call site -- download_file
    # already cascades requests -> Scrapling -> BrowserSkill ->
    # StealthySession -> curl internally, so there is no distinct
    # retry_download to wire in. A transient failure therefore retries
    # the same download once, with backoff (max_retries=1); a bare
    # None/False with no reason is not retried.
    successful, attempted = download_with_recovery(
        candidates,
        lambda cand: download_file(cand, article_dir, session, play_cookies,
                                   bsk_runtime=bsk_runtime, referer=cand.source_url or url,
                                   proxy=proxy, tunnel_tracker=_TUNNEL_TRACKER),
        mirrors, expand_repository_candidates, max_retries=1, deadline=phase_deadline)
    success_count = len(successful)
    incomplete, reason = evidence.check(successful)
    if incomplete:
        print(f"  [Warning] Downloaded set remains incomplete: {reason}")

    # Check if page text references additional MMCs not in candidate URLs
    extra_cdn_files = []
    # P1-CE: the extra CDN probe is a new stage, not part of the download
    # loop; skip it once the phase budget is gone.
    if time.monotonic() >= phase_deadline:
        print(f"  [Warning] Phase timeout ({phase_timeout:g}s budget) reached; "
              f"skipping extra CDN probe.")
    elif pii and not args.skip_cdn and not args.list_only:
        page_mmcs = {int(m.group(1)) for m in MMC_NUM_REGEX.finditer(html_content)}
        downloaded_mmcs = set()
        manifest = load_download_manifest(article_dir)
        for k, v in manifest.items():
            for s in (k, manifest_entry_url(v)):
                m = MMC_NUM_REGEX.search(s)
                if m:
                    downloaded_mmcs.add(int(m.group(1)))
        if os.path.exists(article_dir):
            for f in os.listdir(article_dir):
                # Skip dotfiles: H9 temp files (and the manifest) are
                # never downloaded files, even if a hard kill left one.
                if f.startswith("."):
                    continue
                m_f = MMC_NUM_REGEX.search(f)
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
        if is_placeholder_or_blocked_title(article_dirname):
            title = ((mirrors.info.title if mirrors.info else None) or config.get("_last_api_title")
                             or _quick_title_lookup(doi, url, allow_oa=False))
            if title:
                _refresh_dir_from_html_or_title(raw_title=title)
    except Exception:
        pass

    total_target = attempted + len(extra_cdn_files)
    print(f"\n✓ Done: {success_count}/{total_target} files downloaded to {os.path.abspath(article_dir)}")

    # Step 5: Optional MarkItDown conversion
    if getattr(args, "convert_markdown", False):
        print("\n  [Post-Processing] Converting downloaded supplements to Markdown via Microsoft MarkItDown...")
        converted = convert_directory_supplements(article_dir)
        print(f"  Converted {len(converted)} file(s) to Markdown sidecars.")



if __name__ == "__main__":
    main()
