import sys
import os
import glob
import urllib.parse


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
