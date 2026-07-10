#!/usr/bin/env python3
"""
Publisher Login & Cookie Configuration Wizard
============================================
Interactive wizard that opens browser windows for logging into publisher sites
and saves cookies for use with journal_downloader.py / scansci_supp_downloader.py.

Run this when:
  - You use Safari/Firefox and the scripts can't auto-clone your Chrome session
  - You're on a headless server and need to generate a portable cookies.json
  - Auto-cloning of Chrome/Edge cookies failed

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
    from playwright_utils import find_playwright_chromium
except ImportError:
    find_playwright_chromium = lambda: None

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


def launch_chrome_debug_mode(profile_dir):
    """Launches the user's everyday Google Chrome in debugging mode on port 9222."""
    import platform
    system = platform.system()

    if system == "Darwin":
        cmd = f'"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --remote-debugging-port=9222 --user-data-dir="{profile_dir}"'
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif system == "Windows":
        cmd = f'start "" "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" --remote-debugging-port=9222 --user-data-dir="{profile_dir}"'
        subprocess.Popen(cmd, shell=True)
    else:  # Linux
        cmd = f'google-chrome --remote-debugging-port=9222 --user-data-dir="{profile_dir}"'
        subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("Launching Chrome in debug mode ...")
    for _ in range(10):
        time.sleep(0.5)
        try:
            import urllib.request
            with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1.0) as response:
                if response.status == 200:
                    print("Chrome debug port 9222 is ready!")
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
    print("  If you use Chrome/Edge as your daily browser, the downloader scripts")
    print("  automatically clone your session — no login needed.")
    print("  This wizard is only needed when:")
    print("    - You use Safari/Firefox (Chrome session isn't available)")
    print("    - System security blocks Chrome profile access")
    print("    - Running in a headless/server environment")
    print("=" * 50)
    print()
    print("Choose operation:")
    print("  1. Launch Chrome debug mode + open ALL publisher sites (recommended for first login)")
    print("  2. Import cookies from Chrome already running on port 9222")
    print("  3. Open ALL publisher sites in Playwright Chrome Testing (sequential)")
    print("  4. Custom URL (Playwright Chrome Testing)")
    print()
    print("  Or open a single publisher in Playwright Chrome Testing:")
    for key, (name, url) in PUBLISHERS.items():
        print(f"    {key}. {name}")

    choice = input("\nEnter number or letter (default 1): ").strip().upper() or "1"

    exec_path = find_playwright_chromium()

    # --- Choice 1: Launch Chrome debug body and open all publisher sites ---
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
                os.makedirs(default_profile_dir, exist_ok=True)
                cookies_path = os.path.join(default_profile_dir, "cookies.json")
                with open(cookies_path, "w", encoding="utf-8") as f:
                    json.dump(last_valid_cookies, f, indent=2, ensure_ascii=False)
                print(f"Saved {len(last_valid_cookies)} cookies to: {cookies_path}")
                print("Login session saved successfully!")
            except Exception as e:
                print(f"CDP connection error: {e}")
        return

    # --- Choice 2: Import cookies from already running Chrome ---
    elif choice == "2":
        print("\nConnecting to Chrome on port 9222 ...")
        with sync_playwright() as p:
            try:
                browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                cookies = context.cookies()
                if not cookies:
                    print("Error: No cookies captured. Make sure sites are open in Chrome.")
                    return

                os.makedirs(default_profile_dir, exist_ok=True)
                cookies_path = os.path.join(default_profile_dir, "cookies.json")
                with open(cookies_path, "w", encoding="utf-8") as f:
                    json.dump(cookies, f, indent=2, ensure_ascii=False)
                print(f"Exported {len(cookies)} cookies to: {cookies_path}")
                print("Scripts will now reuse your login session automatically!")
            except Exception as e:
                print(f"Connection failed: {e}")
                print()
                print("To start Chrome in debug mode, run:")
                print('  macOS:  /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome '
                      '--remote-debugging-port=9222 --user-data-dir="~/.journal_supp_chrome_debug_profile"')
                print('  Windows: "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" '
                      '--remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\\.journal_supp_chrome_debug_profile"')
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
        cookies_json = os.path.join(default_profile_dir, "cookies.json")
        if os.path.exists(cookies_json):
            try:
                with open(cookies_json, "r", encoding="utf-8") as f:
                    c_list = json.load(f)
                if c_list:
                    for c in c_list:
                        try:
                            context.add_cookies([c])
                        except Exception:
                            pass
                    print(f"Restored {len(c_list)} session cookies from cookies.json")
            except Exception as e:
                print(f"Warning: Failed to load cookies: {e}")

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

        print(f"\nCaptured {len(last_valid_cookies)} cookies.")
        print("Login session saved. You can now run journal_downloader.py / scansci_supp_downloader.py.")


if __name__ == "__main__":
    main()
