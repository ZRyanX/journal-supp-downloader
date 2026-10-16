#!/usr/bin/env python3
"""
Publisher Login & Cookie Configuration Wizard
============================================
Interactive wizard that opens browser windows for logging into publisher sites
and saves cookies for use with journal_downloader.py / scansci_supp_downloader.py.

Run this when:
  - You need to log into publisher sites (Elsevier, Springer, Nature, Wiley) to access restricted supplements
  - You're on a headless server or outside campus and need to generate a saved cookies.json
  - You need to capture an authenticated session for subscription or paywalled downloads

Usage:
  python scripts/login_publishers.py
"""

import sys
import os
import json
import subprocess
import time
from playwright.sync_api import sync_playwright

scripts_dir = os.path.dirname(os.path.abspath(__file__))
if scripts_dir not in sys.path:
    sys.path.append(scripts_dir)

try:
    from playwright_utils import (
        find_playwright_chromium,
        find_system_chromium,
        find_any_chromium,
        discover_active_cdp_port,
        resolve_cdp_ws_url,
        extract_cdp_cookies,
        merge_cookies,
        normalize_playwright_cookies,
    )
except ImportError:
    find_playwright_chromium = lambda: None
    find_system_chromium = lambda: None
    find_any_chromium = lambda: None
    discover_active_cdp_port = lambda profile_paths=None: None
    resolve_cdp_ws_url = lambda ep, timeout=1.0: None
    extract_cdp_cookies = lambda ep, timeout=4.0: []
    merge_cookies = lambda a, b: list(a or []) + list(b or [])
    normalize_playwright_cookies = lambda c, default_url=None: c


def save_merged_cookies(new_cookies, output_path):
    """
    Merge newly captured cookies with existing cookies by (domain, name, path),
    then save the result to output_path (and local cookies.json if present).
    """
    existing_cookies = []
    # Check output_path and optionally local cookies.json
    paths_to_read = [output_path]
    local_path = os.path.abspath("cookies.json")
    if os.path.exists(local_path) and os.path.abspath(output_path) != local_path:
        paths_to_read.append(local_path)

    for p in paths_to_read:
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        existing_cookies.extend(data)
                    elif isinstance(data, dict):
                        existing_cookies.extend([{"name": k, "value": v, "path": "/"} for k, v in data.items()])
            except Exception:
                pass

    cookie_dict = {}
    for c in existing_cookies:
        if isinstance(c, dict) and c.get("name"):
            key = (c.get("domain", ""), c.get("name", ""), c.get("path", "/"))
            cookie_dict[key] = c

    for c in (new_cookies or []):
        if isinstance(c, dict) and c.get("name"):
            key = (c.get("domain", ""), c.get("name", ""), c.get("path", "/"))
            cookie_dict[key] = c

    merged = list(cookie_dict.values())
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)

    if os.path.exists(local_path) and os.path.abspath(output_path) != local_path:
        try:
            with open(local_path, "w", encoding="utf-8") as f:
                json.dump(merged, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    return merged

PUBLISHERS = {
    "A": ("Elsevier / ScienceDirect", "https://www.sciencedirect.com/"),
    "B": ("Springer Link", "https://link.springer.com/"),
    "C": ("Nature", "https://www.nature.com/"),
    "D": ("Wiley Online Library", "https://onlinelibrary.wiley.com/"),
    "E": ("IEEE Xplore", "https://ieeexplore.ieee.org/"),
    "F": ("GeoSciWorld", "https://pubs.geoscienceworld.org/"),
    "G": ("Taylor & Francis", "https://www.tandfonline.com/"),
    "H": ("MDPI", "https://www.mdpi.com/"),
}

_STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]

_STEALTH_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
    "window.chrome = {runtime: {}};"
)


def launch_chrome_debug_mode(profile_dir, port=9222):
    """Launches the user's everyday Chromium browser in debugging mode."""
    import platform
    system = platform.system()
    chrome_bin = find_system_chromium()

    if chrome_bin:
        cmd = f'"{chrome_bin}" --remote-debugging-port={port} --user-data-dir="{profile_dir}"'
        if system == "Windows":
            subprocess.Popen(f'start "" {cmd}', shell=True)
        else:
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        if system == "Darwin":
            cmd = f'"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port={port} --user-data-dir="{profile_dir}"'
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Windows":
            cmd = f'start "" "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port={port} --user-data-dir="{profile_dir}"'
            subprocess.Popen(cmd, shell=True)
        else:  # Linux
            cmd = f'google-chrome --remote-debugging-port={port} --user-data-dir="{profile_dir}"'
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"Launching Chrome in debug mode on port {port} ...")
    for _ in range(12):
        time.sleep(0.5)
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1.0) as response:
                if response.status == 200:
                    print(f"Chrome debug port {port} is ready!")
                    return True
        except Exception:
            pass
    print("Warning: Chrome startup timed out. Make sure Chrome is installed and no other instance "
          "with this profile is running.")
    return False


def main():
    home = os.path.expanduser("~")
    default_profile_dir = os.path.join(home, ".journal_supp_downloader_profile")
    default_chrome_debug_profile = os.path.join(home, ".journal_supp_chrome_debug_profile")

    print("=" * 50)
    print("    Journal Publisher Login & Cookie Wizard")
    print("=" * 50)
    print()
    print("[IMPORTANT]")
    print("  The downloader scripts read saved session cookies from ~/.journal_supp_downloader_profile/cookies.json")
    print("  or scansci-pdf credentials. They also support live browser connection via --cdp.")
    print("  This wizard lets you log into publisher websites interactively and save credentials.")
    print("=" * 50)
    print()

    active_cdp = discover_active_cdp_port()
    if active_cdp:
        print(f"  ★ [DETECTED] Active Chromium browser running with CDP at {active_cdp[0]} (ws: {active_cdp[1]})")
        print("  0. Quick Import: Capture cookies from running browser instantly (recommended)")
        print()

    print("Choose operation:")
    print("  1. Launch Chrome debug mode + open ALL publisher sites (recommended for first login)")
    print("  2. Import cookies from Chrome already running via CDP")
    print("  3. Open ALL publisher sites in Playwright Chrome Testing (sequential)")
    print("  4. Custom URL (Playwright Chrome Testing)")
    print()
    print("  Or open a single publisher in Playwright Chrome Testing:")
    for key, (name, url) in PUBLISHERS.items():
        print(f"    {key}. {name}")

    default_choice = "0" if active_cdp else "1"
    choice = input(f"\nEnter number or letter (default {default_choice}): ").strip().upper() or default_choice

    exec_path = find_any_chromium()

    # --- Choice 0: Quick Import from detected active browser ---
    if choice == "0":
        target_endpoint = active_cdp[0] if active_cdp else "http://127.0.0.1:9222"
        print(f"\nConnecting to active browser at {target_endpoint} ...")
        cookies = extract_cdp_cookies(target_endpoint)
        if not cookies:
            print("Warning: No cookies found or could not connect to browser via CDP. Make sure browser is open.")
            return

        cookies_path = os.path.join(default_profile_dir, "cookies.json")
        merged = save_merged_cookies(cookies, cookies_path)
        print(f"Successfully captured {len(cookies)} cookies (total {len(merged)} stored) to: {cookies_path}")
        print("Downloaders will now reuse your authentic browser session automatically!")
        return

    # --- Choice 1: Launch Chrome debug mode and open all publisher sites ---
    if choice == "1":
        success = launch_chrome_debug_mode(default_chrome_debug_profile)
        if not success:
            return

        print("\nConnecting via CDP and opening publisher sites ...")
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0] if browser.contexts else browser.new_context()

                urls = [url for name, url in PUBLISHERS.values()]
                for i, url in enumerate(urls):
                    try:
                        if i == 0:
                            page = context.pages[0] if context.pages else context.new_page()
                            page.goto(url)
                        else:
                            page = context.new_page()
                            page.goto(url)
                    except Exception as e:
                        print(f"Warning: Failed to open {url}: {e}")

                print()
                print("=" * 50)
                print("  Publisher sites opened in Chrome tabs.")
                print("  1. Log in / authorize on each site.")
                print("  2. Close the ENTIRE Chrome window when done.")
                print("  Cookies will be saved automatically.")
                print("=" * 50)
                print()

                closed = [False]
                def on_close(ctx):
                    closed[0] = True
                context.on("close", on_close)

                last_valid_cookies = []
                while not closed[0]:
                    try:
                        cookies = context.cookies()
                        if cookies:
                            last_valid_cookies = cookies
                        time.sleep(0.5)
                    except Exception:
                        break

                # Save cookies
                cookies_path = os.path.join(default_profile_dir, "cookies.json")
                merged = save_merged_cookies(last_valid_cookies, cookies_path)
                print(f"Saved {len(last_valid_cookies)} captured cookies (total {len(merged)} stored) to: {cookies_path}")
                print("Login session saved successfully!")
            except Exception as e:
                print(f"CDP connection error: {e}")
        return

    # --- Choice 2: Import cookies from already running Chrome ---
    elif choice == "2":
        default_port = active_cdp[0] if active_cdp else "http://127.0.0.1:9222"
        prompt_endpoint = input(f"CDP endpoint URL or port (default {default_port}): ").strip() or default_port
        if prompt_endpoint.isdigit():
            prompt_endpoint = f"http://127.0.0.1:{prompt_endpoint}"

        print(f"\nConnecting to browser on {prompt_endpoint} ...")
        cookies = extract_cdp_cookies(prompt_endpoint)
        if not cookies:
            print("Error: No cookies captured. Make sure Chrome is running with remote debugging.")
            print()
            print("To start Chrome in debug mode, run:")
            print('  macOS:  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
                  '--remote-debugging-port=9222 --user-data-dir="~/.journal_supp_chrome_debug_profile"')
            print('  Windows: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                  '--remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\\.journal_supp_chrome_debug_profile"')
            return

        cookies_path = os.path.join(default_profile_dir, "cookies.json")
        merged = save_merged_cookies(cookies, cookies_path)
        print(f"Exported {len(cookies)} cookies (total {len(merged)} stored) to: {cookies_path}")
        print("Scripts will now reuse your login session automatically!")
        return

    # --- Regular Playwright Chrome Testing ---
    urls = []
    if choice in PUBLISHERS:
        name, url = PUBLISHERS[choice]
        urls = [url]

        print(f"\nOpening: {name}")
    elif choice == "3":
        urls = [url for name, url in PUBLISHERS.values()]
        print("\nOpening ALL publisher sites sequentially.")
    elif choice == "4":
        custom_url = input("Enter custom URL: ").strip()
        if not custom_url.startswith(("http://", "https://")):
            custom_url = "https://" + custom_url
        urls = [custom_url]
    else:
        print("Invalid choice. Defaulting to Elsevier.")
        urls = ["https://www.sciencedirect.com/"]

    print()
    print("=" * 50)
    print("  1. Launching visible Chrome Testing window ...")
    print("  2. Please log in / authorize on each site.")
    print("  3. Close the browser window when done.")
    print("=" * 50)
    print()

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                default_profile_dir,
                headless=False,
                executable_path=exec_path,
                args=_STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36",
            )
        except Exception as e:
            print(f"Launch failed: {e}")
            print("Trying without profile directory ...")
            context = p.chromium.launch_persistent_context(
                "",
                headless=False,
                executable_path=exec_path,
                args=_STEALTH_ARGS,
                ignore_default_args=["--enable-automation"],
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/125.0.0.0 Safari/537.36",
            )

        # Inject saved cookies if present
        restore_paths = [
            os.path.abspath("cookies.json"),
            os.path.join(default_profile_dir, "cookies.json"),
        ]
        all_to_restore = []
        for c_path in restore_paths:
            if os.path.exists(c_path):
                try:
                    with open(c_path, "r", encoding="utf-8") as f:
                        c_list = json.load(f)
                    if isinstance(c_list, list):
                        all_to_restore.extend(c_list)
                    elif isinstance(c_list, dict):
                        all_to_restore.extend([{"name": k, "value": v, "path": "/"} for k, v in c_list.items()])
                except Exception as e:
                    print(f"Warning: Failed to load cookies from {c_path}: {e}")

        if all_to_restore:
            norm_list = normalize_playwright_cookies(all_to_restore)
            for c in norm_list:
                try:
                    context.add_cookies([c])
                except Exception:
                    pass
            print(f"Restored {len(norm_list)} session cookies from cookies.json")

        # Open pages
        for i, url in enumerate(urls):
            try:
                if i == 0:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.add_init_script(_STEALTH_INIT_SCRIPT)
                    page.goto(url)
                else:
                    page = context.new_page()
                    page.add_init_script(_STEALTH_INIT_SCRIPT)
                    page.goto(url)
            except Exception as e:
                print(f"Warning: Failed to load {url}: {e}")

        # Wait for context close
        closed = [False]
        def on_close(ctx):
            closed[0] = True
        context.on("close", on_close)

        last_valid_cookies = []
        while not closed[0]:
            try:
                cookies = context.cookies()
                if cookies:
                    last_valid_cookies = cookies
                time.sleep(0.5)
            except Exception:
                break

        cookies_path = os.path.join(default_profile_dir, "cookies.json")
        merged = save_merged_cookies(last_valid_cookies, cookies_path)
        print(f"\nCaptured {len(last_valid_cookies)} cookies (total {len(merged)} stored in {cookies_path}).")
        print("Login session saved. You can now run journal_downloader.py / scansci_supp_downloader.py.")


if __name__ == "__main__":
    main()
