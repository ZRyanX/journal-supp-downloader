import sys
import os
import glob


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
