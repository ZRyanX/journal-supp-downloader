import sys
import os
import re
import glob
import json
import shutil
import socket
import urllib.parse
import urllib.request
from typing import List, Dict, Any, Optional, Tuple


def find_playwright_chromium():
    """
    Automatically detects the Playwright Chromium executable path on macOS, Windows, and Linux.
    Returns the absolute path to the executable, or None if not found.

    Checks in order:
      1. PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH environment variable
      2. PLAYWRIGHT_BROWSERS_PATH environment variable
      3. Default ms-playwright cache directories per platform
    """
    # 1. Check custom env var
    env_path = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    # Determine default ms-playwright directories
    home = os.path.expanduser("~")
    possible_roots = []

    if sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            possible_roots.append(os.path.join(local_app_data, "ms-playwright"))
        possible_roots.append(os.path.join(home, "AppData", "Local", "ms-playwright"))
    elif sys.platform == "darwin":
        possible_roots.append(os.path.join(home, "Library", "Caches", "ms-playwright"))
    else:
        possible_roots.append(os.path.join(home, ".cache", "ms-playwright"))

    # Also check PLAYWRIGHT_BROWSERS_PATH
    browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_path:
        possible_roots.insert(0, browsers_path)

    for root in possible_roots:
        if not os.path.isdir(root):
            continue

        # Look for chromium-* directories
        chromium_dirs = glob.glob(os.path.join(root, "chromium-*"))
        if not chromium_dirs:
            continue

        # Sort to prioritize newer revisions
        def extract_rev(path):
            name = os.path.basename(path)
            parts = name.split("-")
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
            return 0

        chromium_dirs.sort(key=extract_rev, reverse=True)

        for chrom_dir in chromium_dirs:
            if sys.platform.startswith("win"):
                exe_path = os.path.join(chrom_dir, "chrome-win", "chrome.exe")
                if os.path.exists(exe_path):
                    return exe_path
            elif sys.platform == "darwin":
                # macOS .app bundle
                app_glob = os.path.join(
                    chrom_dir,
                    "chrome-mac*",
                    "Google Chrome for Testing.app",
                    "Contents",
                    "MacOS",
                    "Google Chrome for Testing",
                )
                matches = glob.glob(app_glob)
                if matches and os.path.exists(matches[0]):
                    return matches[0]

                # Fallback walk
                for r, d, f in os.walk(chrom_dir):
                    if "Google Chrome for Testing" in f:
                        test_path = os.path.join(r, "Google Chrome for Testing")
                        if os.path.exists(test_path) and os.access(test_path, os.X_OK):
                            if "Contents/MacOS" in test_path:
                                return test_path
            else:
                exe_path = os.path.join(chrom_dir, "chrome-linux", "chrome")
                if os.path.exists(exe_path):
                    return exe_path

    return None


def find_system_chromium():
    """
    Detects locally installed system Chromium-based browsers (Google Chrome, Microsoft Edge, Brave, Chromium).
    Checks standard application paths and system PATH across macOS, Windows, and Linux.
    Returns absolute path to the executable, or None if not found.
    """
    home = os.path.expanduser("~")
    candidates = []

    if sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.join(home, "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            os.path.join(home, "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            os.path.join(home, "Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            os.path.join(home, "Applications/Chromium.app/Contents/MacOS/Chromium"),
            "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
            os.path.join(home, "Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
        ]
    elif sys.platform.startswith("win"):
        local_app = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        candidates = [
            os.path.join(prog_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(prog_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local_app, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(prog_files, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(prog_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(local_app, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(prog_files, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(prog_files_x86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(local_app, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        ]
    else:  # Linux / BSD
        for name in [
            "google-chrome", "google-chrome-stable", "chromium",
            "chromium-browser", "microsoft-edge", "microsoft-edge-stable", "brave-browser",
        ]:
            which_p = shutil.which(name)
            if which_p:
                candidates.append(which_p)
        candidates.extend([
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/usr/bin/microsoft-edge",
            "/usr/bin/brave-browser",
            "/snap/bin/chromium",
            "/snap/bin/brave",
        ])

    for p in candidates:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p

    return None


def find_any_chromium():
    """
    Returns the best available Chromium executable path (Playwright first, then system).
    Returns None if no browser is found.
    """
    return find_playwright_chromium() or find_system_chromium()


def is_tcp_port_open(host: str = "127.0.0.1", port: int = 9222, timeout: float = 0.2) -> bool:
    """Non-blocking TCP socket check to verify if a port is actively listening."""
    if not port or port <= 0 or port > 65535:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, int(port))) == 0
    except Exception:
        return False


def get_common_user_data_dirs():
    """Returns a list of common user data directories across Chromium browsers."""
    home = os.path.expanduser("~")
    dirs = [
        os.path.join(home, ".journal_supp_chrome_debug_profile"),
        os.path.join(home, ".journal_supp_downloader_profile"),
        os.path.join(home, ".web-access"),
        os.path.join(home, ".web-access", "chrome-profile"),
        os.path.join(home, ".web-access", "browser-profile"),
    ]

    if sys.platform == "darwin":
        app_sup = os.path.join(home, "Library", "Application Support")
        dirs.extend([
            os.path.join(app_sup, "Google", "Chrome"),
            os.path.join(app_sup, "Google", "Chrome Canary"),
            os.path.join(app_sup, "Microsoft Edge"),
            os.path.join(app_sup, "Chromium"),
            os.path.join(app_sup, "BraveSoftware", "Brave-Browser"),
            os.path.join(app_sup, "Arc", "User Data"),
            os.path.join(app_sup, "Dia", "User Data"),
        ])
    elif sys.platform.startswith("win"):
        local_app = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        dirs.extend([
            os.path.join(local_app, "Google", "Chrome", "User Data"),
            os.path.join(local_app, "Google", "Chrome SxS", "User Data"),
            os.path.join(local_app, "Microsoft", "Edge", "User Data"),
            os.path.join(local_app, "Chromium", "User Data"),
            os.path.join(local_app, "BraveSoftware", "Brave-Browser", "User Data"),
        ])
    else:
        config_dir = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
        dirs.extend([
            os.path.join(config_dir, "google-chrome"),
            os.path.join(config_dir, "google-chrome-stable"),
            os.path.join(config_dir, "google-chrome-canary"),
            os.path.join(config_dir, "microsoft-edge"),
            os.path.join(config_dir, "microsoft-edge-stable"),
            os.path.join(config_dir, "chromium"),
            os.path.join(config_dir, "BraveSoftware", "Brave-Browser"),
        ])

    return [d for d in dirs if os.path.isdir(d)]


def discover_active_cdp_port(profile_paths=None):
    """
    Automatically discovers an active Chrome DevTools Protocol (CDP) port.
    Checks:
      1. Environment variables (CDP_URL, CDP_PORT, WEB_ACCESS_CDP_PORT, CHROME_DEBUG_PORT)
      2. DevToolsActivePort in Chromium user data directories (macOS, Windows, Linux)
      3. Common debug ports (9222, 9223)

    Returns a tuple of (http_url, ws_url) if an active browser is listening, or None.
    """
    # 1. Check environment variables
    for env_key in ["CDP_URL", "WEB_ACCESS_CDP_URL"]:
        env_val = os.environ.get(env_key)
        if env_val:
            ws = resolve_cdp_ws_url(env_val)
            if ws:
                if env_val.startswith(("http://", "https://")):
                    http_base = env_val.rstrip("/")
                elif env_val.isdigit():
                    http_base = f"http://127.0.0.1:{env_val}"
                else:
                    netloc = urllib.parse.urlsplit(ws).netloc or f"127.0.0.1:{env_val}"
                    http_base = f"http://{netloc}"
                return (http_base, ws)

    for env_port_key in ["CDP_PORT", "WEB_ACCESS_CDP_PORT", "CHROME_DEBUG_PORT"]:
        port_val = os.environ.get(env_port_key)
        if port_val and port_val.isdigit():
            p = int(port_val)
            if is_tcp_port_open("127.0.0.1", p):
                ws = resolve_cdp_ws_url(p)
                if ws:
                    return (f"http://127.0.0.1:{p}", ws)

    # 2. Inspect DevToolsActivePort in user data directories
    dirs_to_check = list(profile_paths) if profile_paths else get_common_user_data_dirs()
    for d in dirs_to_check:
        active_port_file = os.path.join(d, "DevToolsActivePort")
        if os.path.isfile(active_port_file):
            try:
                with open(active_port_file, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f if line.strip()]
                if lines and lines[0].isdigit():
                    port = int(lines[0])
                    ws_path = lines[1] if len(lines) > 1 else ""
                    if ws_path and not ws_path.startswith("/"):
                        ws_path = "/" + ws_path
                    # First verify active CDP via /json/version
                    ws_url = resolve_cdp_ws_url(port)
                    if ws_url:
                        return (f"http://127.0.0.1:{port}", ws_url)
                    # Fallback to tcp check + written ws_path
                    if is_tcp_port_open("127.0.0.1", port) and ws_path:
                        ws_url = f"ws://127.0.0.1:{port}{ws_path}"
                        return (f"http://127.0.0.1:{port}", ws_url)
            except Exception:
                continue

    # 3. Check default debug ports (9222, 9223)
    for p in [9222, 9223]:
        if is_tcp_port_open("127.0.0.1", p):
            ws = resolve_cdp_ws_url(p)
            if ws:
                return (f"http://127.0.0.1:{p}", ws)

    return None


def resolve_cdp_ws_url(endpoint_or_port, timeout=1.0):
    """
    Resolves an HTTP endpoint (http://127.0.0.1:9222), port number (9222), /json endpoint,
    or ws:// URL to the exact WebSocket debugger URL required by Scrapling / Playwright.
    Returns WebSocket URL string, or None if resolution fails.
    """
    if not endpoint_or_port:
        return None

    target = str(endpoint_or_port).strip()
    if target.startswith(("ws://", "wss://")):
        return target

    # Handle numeric port (e.g. 9222 or "9222")
    if target.isdigit():
        http_url = f"http://127.0.0.1:{target}/json/version"
    elif target.startswith("http://") or target.startswith("https://"):
        base = re.sub(r"/json(?:/(?:version|list))?/?$", "", target.rstrip("/"))
        http_url = f"{base}/json/version"
    else:
        # e.g. "127.0.0.1:9222" or "localhost:9222"
        base = re.sub(r"/json(?:/(?:version|list))?/?$", "", target.rstrip("/"))
        http_url = f"http://{base}/json/version"

    try:
        req = urllib.request.Request(http_url, headers={"User-Agent": "journal-supp-downloader/cdp"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if isinstance(data, dict):
                    ws_url = data.get("webSocketDebuggerUrl")
                    if ws_url:
                        return ws_url
                elif isinstance(data, list) and len(data) > 0:
                    for item in data:
                        if isinstance(item, dict) and item.get("webSocketDebuggerUrl"):
                            return item["webSocketDebuggerUrl"]
    except Exception:
        pass

    return None


def extract_cdp_cookies(endpoint_or_port, timeout: float = 4.0) -> List[Dict[str, Any]]:
    """
    Extracts all active cookies from the running Chromium browser's default context via CDP.
    Connects non-destructively to the user's running session.
    """
    if not endpoint_or_port:
        return []
    target = str(endpoint_or_port).strip()
    if target.isdigit():
        target = f"http://127.0.0.1:{target}"

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(target, timeout=int(timeout * 1000))
            cookies = []
            if browser.contexts:
                cookies = browser.contexts[0].cookies()
            browser.close()
            return cookies
    except Exception:
        return []


def merge_cookies(existing_cookies, new_cookies) -> List[Dict[str, Any]]:
    """
    Merges two cookie lists or dicts, keying on (domain, name, path).
    New cookies overwrite older ones. Returns a list of cookie dicts.
    """
    cookie_dict = {}
    for c_list in (existing_cookies, new_cookies):
        if not c_list:
            continue
        if isinstance(c_list, dict):
            c_items = [{"name": k, "value": v, "path": "/"} for k, v in c_list.items()]
        elif isinstance(c_list, (list, tuple)):
            c_items = c_list
        else:
            continue

        for c in c_items:
            if isinstance(c, dict) and c.get("name"):
                domain = (c.get("domain") or "").lstrip(".").lower()
                name = c.get("name", "")
                path = c.get("path") or "/"
                key = (domain, name, path)
                cookie_dict[key] = dict(c)

    return list(cookie_dict.values())


def fetch_page_via_cdp(endpoint_or_port, target_url: str, wait_selector: Optional[str] = None, timeout: float = 30.0) -> Tuple[str, int, str, List[Dict[str, Any]]]:
    """
    Fetches or attaches to a page via a live CDP connection to the user's browser:
    1. Inspects existing tabs in browser.contexts[0]. If target_url (or its DOI/PII)
       is already open, reuses that exact tab without making duplicate network requests.
    2. If not already open, opens a new tab in the authenticated default context, navigates
       to target_url, waits for domcontentloaded / wait_selector, and closes only that tab.
    3. Runs in-page expansion for accordions/supplementary details if available.
    4. Captures HTML content, effective URL, and retrieves live session cookies.
    Returns a tuple: (html_content, status_code, final_url, live_cookies).
    """
    if not endpoint_or_port:
        raise ValueError("CDP endpoint or port must be specified.")

    target = str(endpoint_or_port).strip()
    if target.isdigit():
        target = f"http://127.0.0.1:{target}"

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(target, timeout=int(timeout * 1000))
        if not browser.contexts:
            browser.close()
            raise RuntimeError("No browser contexts found in CDP session.")

        context = browser.contexts[0]
        live_cookies = context.cookies()

        matched_page = None
        target_norm = target_url.lower().rstrip("/")
        doi_token = ""
        m_doi = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", target_url)
        if m_doi:
            doi_token = m_doi.group(0).lower()
        m_pii = re.search(r"[sS]\d{16}[0-9a-zA-Z]", target_url)
        pii_token = m_pii.group(0).lower() if m_pii else ""

        for pg in context.pages:
            try:
                pg_url = (pg.url or "").lower().rstrip("/")
                if not pg_url or pg_url == "about:blank" or pg_url.startswith("chrome://"):
                    continue
                if target_norm in pg_url or pg_url in target_norm:
                    matched_page = pg
                    break
                if doi_token and doi_token in pg_url:
                    matched_page = pg
                    break
                if pii_token and pii_token in pg_url:
                    matched_page = pg
                    break
                if doi_token:
                    try:
                        meta_doi = pg.evaluate("() => document.querySelector('meta[name*=\"doi\" i], meta[name=\"citation_doi\" i]')?.content || ''")
                        if meta_doi and doi_token in str(meta_doi).lower():
                            matched_page = pg
                            break
                    except Exception:
                        pass
            except Exception:
                continue

        created_new_page = False
        page = matched_page
        if not page:
            page = context.new_page()
            created_new_page = True
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            except Exception:
                pass

        try:
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=min(int(timeout * 500), 10000))
                except Exception:
                    pass

            try:
                page.evaluate("""
                    () => {
                        document.querySelectorAll("details:not([open])").forEach(d => d.open = true);
                        document.querySelectorAll("button[aria-expanded='false']").forEach(b => {
                            const txt = (b.innerText || "").toLowerCase();
                            if (txt.includes("supp") || txt.includes("material") || txt.includes("data") || txt.includes("table")) {
                                try { b.click(); } catch(e) {}
                            }
                        });
                    }
                """)
            except Exception:
                pass

            html_content = page.content()
            final_url = page.url or target_url
            status_code = 200
        finally:
            if created_new_page and page:
                try:
                    page.close()
                except Exception:
                    pass
            browser.close()

        return html_content, status_code, final_url, live_cookies




def normalize_playwright_cookies(cookies, default_url=None):
    """
    Normalizes a list of cookies (or dicts) for Playwright:
    - Supports list of cookie dicts or key-value mapping dict {name: value}.
    - Fixes or removes invalid sameSite values (must be 'Strict', 'Lax', or 'None').
    - Removes empty string domains (domain: ""). If default_url is provided and cookie
      lacks a domain and url, derives domain or url from default_url.
    - If cookie has url, removes path and domain to avoid Playwright 'either url or path' error.
    - Drops cookies that lack both domain and url (when no default_url is provided).
    - Ensures each cookie has required fields (name, value).
    """
    if not cookies:
        return []
    if isinstance(cookies, dict):
        if "name" in cookies and "value" in cookies:
            cookies = [cookies]
        else:
            cookies = [{"name": k, "value": str(v)} for k, v in cookies.items()]

    normalized = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        c = dict(item)
        if not c.get("name") or c.get("value") is None:
            continue

        c["name"] = str(c["name"])
        c["value"] = str(c["value"])

        # Handle domain: Playwright throws error if domain is empty string
        domain = c.get("domain")
        if domain == "" or domain is None:
            c.pop("domain", None)
            if not c.get("url"):
                if default_url:
                    host = urllib.parse.urlsplit(default_url).hostname
                    if host:
                        c["domain"] = host
                        c["path"] = c.get("path") or "/"
                    else:
                        c["url"] = default_url
                else:
                    continue

        # Playwright rule: Cookie should have either url or domain/path pair, not both
        if c.get("url"):
            c.pop("domain", None)
            c.pop("path", None)
        else:
            if not c.get("path"):
                c["path"] = "/"

        # Handle sameSite: Playwright only accepts "Strict", "Lax", or "None"
        if "sameSite" in c:
            ss = c["sameSite"]
            if isinstance(ss, str):
                ss_norm = ss.strip().lower()
                if ss_norm == "strict":
                    c["sameSite"] = "Strict"
                elif ss_norm == "lax":
                    c["sameSite"] = "Lax"
                elif ss_norm in ("none", "no_restriction"):
                    c["sameSite"] = "None"
                else:
                    c.pop("sameSite", None)
            else:
                c.pop("sameSite", None)

        normalized.append(c)
    return normalized
