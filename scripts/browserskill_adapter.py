#!/usr/bin/env python3
"""
Tencent BrowserSkill (bsk) Adapter Module for journal-supp-downloader.

Connects AI agents and journal downloaders to the user's running Chrome or Edge browser
via Tencent/BrowserSkill (https://github.com/Tencent/BrowserSkill).
Inherits authenticated university/institution subscriptions, CARSI SSO, publisher cookies,
and naturally passes Cloudflare Turnstile without requiring manual remote debugging flags.

Key Features:
- Auto-discovery of bsk CLI binary across macOS, Linux, and Windows (supporting BSK_HOME,
  BSK_INSTALL_DIR, ~/.local/bin, ~/.cargo/bin, and system PATH)
- Structured status diagnostics (daemon health, extension connectivity, browser sessions)
- Safe session lifecycle management (with automatic cleanup on error or exit)
- DOM extraction via native 'bsk get-html' with accordion expansion & evaluate fallback
- Visual Object Model inspection ('bsk observe') and interaction ('bsk click')
- User tab borrowing & return ('bsk tab borrow' / 'bsk tab return') for zero-friction handoff
- Authenticated in-session file download with Base64 transfer & 'bsk download' fallback
- Seamless persistent session reuse across page fetch and supplementary downloads
- Standalone CLI interface (status, doctor, sessions, tabs, fetch, borrow-fetch, observe, download, stop-all)
"""

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple, Union


class BrowserSkillError(Exception):
    """Base exception for BrowserSkill operations."""
    pass


class BrowserSkillUnavailableError(BrowserSkillError):
    """Raised when bsk CLI binary is not installed or not found."""
    pass


class BrowserSkillDaemonError(BrowserSkillError):
    """Raised when BrowserSkill daemon is not running or extension is disconnected."""
    pass


class BrowserSkillSessionError(BrowserSkillError):
    """Raised when starting, operating, or closing a BrowserSkill session fails."""
    pass


class BrowserSkillExecutionError(BrowserSkillError):
    """Raised when an operation (navigation, evaluate, download, observe) fails inside a session."""
    pass


def find_bsk_executable() -> Optional[str]:
    """
    Locates the BrowserSkill (bsk) CLI executable path across macOS, Linux, and Windows.
    Checks:
      1. BSK_BIN or BSK_PATH environment variables
      2. BSK_INSTALL_DIR environment variable
      3. BSK_HOME environment variable ($BSK_HOME/bin/bsk)
      4. System PATH via shutil.which
      5. Common user installation paths (~/.local/bin, ~/.cargo/bin, ~/.bsk/bin, etc.)
    Returns absolute path or None.
    """
    for env_key in ("BSK_BIN", "BSK_PATH"):
        env_val = os.environ.get(env_key)
        if env_val and os.path.isfile(env_val) and os.access(env_val, os.X_OK):
            return os.path.abspath(env_val)

    bsk_install = os.environ.get("BSK_INSTALL_DIR")
    if bsk_install:
        for fname in ("bsk", "bsk.exe"):
            p = os.path.join(bsk_install, fname)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return os.path.abspath(p)

    bsk_home = os.environ.get("BSK_HOME")
    if bsk_home:
        for fname in ("bsk", "bsk.exe"):
            p = os.path.join(bsk_home, "bin", fname)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return os.path.abspath(p)

    # Standard PATH check
    which_bin = shutil.which("bsk") or shutil.which("bsk.exe")
    if which_bin and os.path.isfile(which_bin) and os.access(which_bin, os.X_OK):
        return os.path.abspath(which_bin)

    # Standard per-platform install locations
    home = os.path.expanduser("~")
    candidates = [
        os.path.join(home, ".local", "bin", "bsk"),
        os.path.join(home, ".local", "bin", "bsk.exe"),
        os.path.join(home, ".cargo", "bin", "bsk"),
        os.path.join(home, ".cargo", "bin", "bsk.exe"),
        os.path.join(home, ".bsk", "bin", "bsk"),
        os.path.join(home, ".bsk", "bin", "bsk.exe"),
        "/usr/local/bin/bsk",
        "/opt/homebrew/bin/bsk",
        os.path.join(home, "bin", "bsk"),
    ]

    if sys.platform.startswith("win"):
        local_app = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        user_prof = os.environ.get("USERPROFILE", home)
        candidates.extend([
            os.path.join(local_app, "BrowserSkill", "bsk.exe"),
            os.path.join(local_app, "bsk", "bsk.exe"),
            os.path.join(prog_files, "BrowserSkill", "bsk.exe"),
            os.path.join(user_prof, ".bsk", "bin", "bsk.exe"),
        ])

    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return os.path.abspath(c)

    return None


def is_browserskill_available(timeout: float = 2.0) -> bool:
    """
    Checks if BrowserSkill (bsk) CLI is installed and responsive.
    Runs 'bsk --version' with a short timeout.
    """
    exe = find_bsk_executable()
    if not exe:
        return False
    try:
        res = subprocess.run(
            [exe, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return res.returncode == 0
    except Exception:
        return False


def _parse_json_safely(raw_text: str) -> Optional[Any]:
    """
    Attempts to parse JSON from text safely without catastrophic regex backtracking.
    Handles exact JSON, JSON wrapped in terminal logs, and JSON Lines output streams.
    """
    if not raw_text or not raw_text.strip():
        return None
    text = raw_text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass

    # Try finding outermost {...} using index bounds (safe O(N) scan)
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # Try finding outermost [...] using index bounds
    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        candidate = text[first_bracket : last_bracket + 1]
        try:
            return json.loads(candidate)
        except Exception:
            pass

    # Try parsing line-by-line from bottom (common in bsk JSON Lines stream)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in reversed(lines[-50:]):
        if (line.startswith("{") and line.endswith("}")) or (line.startswith("[") and line.endswith("]")):
            try:
                return json.loads(line)
            except Exception:
                continue

    return None


def check_browserskill_status(
    bsk_bin: Optional[str] = None, timeout: float = 4.0
) -> Dict[str, Any]:
    """
    Queries BrowserSkill daemon and extension status via 'bsk status'.
    Returns structured status dict:
      - available: bool
      - bsk_path: Optional[str]
      - version: Optional[str]
      - daemon_running: bool
      - browser_connected: bool
      - browsers: List[Any]
      - message: str
      - raw: Any
    """
    exe = bsk_bin or find_bsk_executable()
    if not exe:
        return {
            "available": False,
            "bsk_path": None,
            "version": None,
            "daemon_running": False,
            "browser_connected": False,
            "browsers": [],
            "message": (
                "BrowserSkill (bsk) executable not found. "
                "Install via: https://github.com/Tencent/BrowserSkill"
            ),
            "raw": None,
        }

    version_str = None
    try:
        v_res = subprocess.run(
            [exe, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if v_res.returncode == 0:
            version_str = v_res.stdout.strip()
    except Exception:
        pass

    try:
        s_res = subprocess.run(
            [exe, "status", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        raw_out = s_res.stdout
        parsed = _parse_json_safely(raw_out)

        daemon_running = False
        browser_connected = False
        browsers = []

        if isinstance(parsed, dict):
            # Check daemon status
            daemon_val = parsed.get("daemon") or parsed.get("daemon_running") or parsed.get("daemon_status")
            if isinstance(daemon_val, dict):
                daemon_running = bool(daemon_val.get("running") or daemon_val.get("ok"))
            elif isinstance(daemon_val, bool):
                daemon_running = daemon_val
            elif parsed.get("status") in ("ok", "ready", "running") or parsed.get("ok") is True:
                daemon_running = True

            # Check browser/extension connectivity
            browsers = parsed.get("browsers") or parsed.get("connected_browsers") or []
            if browsers or parsed.get("browser_connected") or parsed.get("extension_connected"):
                browser_connected = True
        elif s_res.returncode == 0:
            daemon_running = "daemon" in raw_out.lower() or "running" in raw_out.lower()
            browser_connected = "connected" in raw_out.lower()

        # Check 'bsk browsers' as well if status output didn't list any
        if daemon_running and not browsers:
            try:
                b_res = subprocess.run(
                    [exe, "browsers", "--json"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout,
                )
                b_parsed = _parse_json_safely(b_res.stdout)
                if isinstance(b_parsed, list):
                    browsers = b_parsed
                elif isinstance(b_parsed, dict) and "browsers" in b_parsed:
                    browsers = b_parsed["browsers"]
                if browsers:
                    browser_connected = True
            except Exception:
                pass

        msg = "BrowserSkill is ready." if (daemon_running and browser_connected) else (
            "Daemon is running, but no browser extension connected." if daemon_running else (
                "BrowserSkill daemon is stopped. Run 'bsk daemon start' to start it."
            )
        )

        return {
            "available": True,
            "bsk_path": exe,
            "version": version_str,
            "daemon_running": daemon_running,
            "browser_connected": browser_connected,
            "browsers": browsers,
            "message": msg,
            "raw": parsed or raw_out,
        }
    except Exception as e:
        return {
            "available": True,
            "bsk_path": exe,
            "version": version_str,
            "daemon_running": False,
            "browser_connected": False,
            "browsers": [],
            "message": f"Failed to query BrowserSkill daemon status: {e}",
            "raw": None,
        }


class BrowserSkillSession:
    """
    Context manager for an isolated BrowserSkill agent session.
    Manages 'bsk session start' and guarantees 'bsk session stop' on exit.
    Supports navigation, native 'bsk get-html', JavaScript evaluate,
    'bsk observe', 'bsk click', user tab borrowing/returning, and authenticated file downloads.
    """

    def __init__(
        self,
        browser: Optional[str] = None,
        no_focus: bool = True,
        bsk_bin: Optional[str] = None,
        timeout: float = 45.0,
    ):
        self.browser = browser
        self.no_focus = no_focus
        self.bsk_bin = bsk_bin or find_bsk_executable()
        self.timeout = timeout
        self.session_id: Optional[str] = None

        if not self.bsk_bin:
            raise BrowserSkillUnavailableError(
                "BrowserSkill CLI (bsk) is not installed or not found in PATH."
            )

    def start(self) -> str:
        """
        Starts a new isolated Agent Window session and returns the session_id.
        """
        if self.session_id:
            return self.session_id

        cmd = [self.bsk_bin, "session", "start", "--json"]
        if self.no_focus:
            cmd.append("--no-focus")
        if self.browser:
            cmd.extend(["--browser", str(self.browser)])

        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout,
            )
        except Exception as e:
            raise BrowserSkillSessionError(f"Failed to execute 'bsk session start': {e}") from e

        if res.returncode != 0:
            err_msg = res.stderr.strip() or res.stdout.strip() or f"exit code {res.returncode}"
            raise BrowserSkillSessionError(f"BrowserSkill failed to start session: {err_msg}")

        parsed = _parse_json_safely(res.stdout)
        sid = None
        if isinstance(parsed, dict):
            sid = (
                parsed.get("session_id")
                or parsed.get("sessionId")
                or parsed.get("id")
            )
            if not sid and "reply" in parsed and isinstance(parsed["reply"], dict):
                sid = parsed["reply"].get("session_id") or parsed["reply"].get("sessionId")
            if not sid and "data" in parsed and isinstance(parsed["data"], dict):
                sid = parsed["data"].get("session_id") or parsed["data"].get("sessionId")

        if not sid:
            # Fallback regex search on stdout:
            # Matches "session_id: abc", "session: abc", "session started: abc", "started session abc"
            patterns = [
                r'(?:session[_-]?id|session\s+id|session\s+started|session)\s*[:=]\s*["\']?([A-Za-z0-9_-]+)',
                r'(?:session\s+started|started\s+session)\s+["\']?([A-Za-z0-9_-]+)',
            ]
            for pat in patterns:
                m = re.search(pat, res.stdout, re.IGNORECASE)
                if m:
                    sid = m.group(1)
                    break

        if not sid:
            raise BrowserSkillSessionError(
                f"Could not extract session_id from BrowserSkill response: {res.stdout.strip()}"
            )

        self.session_id = str(sid)
        return self.session_id

    def stop(self) -> bool:
        """
        Stops the active session and releases browser tabs. Suppresses errors to prevent masking.
        """
        if not self.session_id:
            return True
        sid = self.session_id
        self.session_id = None
        try:
            res = subprocess.run(
                [self.bsk_bin, "session", "stop", sid],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=15.0,
            )
            return res.returncode == 0
        except Exception:
            return False

    def __enter__(self) -> "BrowserSkillSession":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    def navigate(self, url: str, timeout: Optional[float] = None) -> bool:
        """
        Navigates the active session tab to target URL.
        """
        if not self.session_id:
            raise BrowserSkillSessionError("Session is not active.")
        to = timeout or self.timeout
        cmd = [self.bsk_bin, "navigate", url, "--session", self.session_id]
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=to,
            )
            if res.returncode != 0:
                err_msg = res.stderr.strip() or res.stdout.strip()
                raise BrowserSkillExecutionError(f"Navigation to {url} failed: {err_msg}")
            return True
        except subprocess.TimeoutExpired:
            raise BrowserSkillExecutionError(f"Navigation to {url} timed out after {to}s.")
        except Exception as e:
            if isinstance(e, BrowserSkillExecutionError):
                raise
            raise BrowserSkillExecutionError(f"Navigation error: {e}") from e

    def get_html(self, out_path: Optional[str] = None, timeout: Optional[float] = None) -> str:
        """
        Retrieves page HTML using the native 'bsk get-html' command.
        Falls back to evaluate("document.documentElement.outerHTML") if get-html is unavailable.
        """
        if not self.session_id:
            raise BrowserSkillSessionError("Session is not active.")
        to = timeout or 30.0

        # Try native bsk get-html
        cmd = [self.bsk_bin, "get-html", "--session", self.session_id]
        if out_path:
            cmd.extend(["--out", out_path])

        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=to,
            )
            if res.returncode == 0:
                if out_path and os.path.exists(out_path):
                    with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                        if content and content.strip():
                            return content
                elif res.stdout and res.stdout.strip():
                    return res.stdout
        except Exception:
            pass

        # Fallback to evaluate document.documentElement.outerHTML
        try:
            html = str(self.evaluate("document.documentElement.outerHTML", timeout=to))
            if out_path and html:
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(html)
            return html
        except Exception as e:
            raise BrowserSkillExecutionError(f"Failed to extract HTML DOM: {e}") from e

    def observe(self, timeout: Optional[float] = None) -> Any:
        """
        Runs 'bsk observe --session <id> --json' to inspect the Visual Object Model / interactive element references (@e1, @e2, etc.).
        """
        if not self.session_id:
            raise BrowserSkillSessionError("Session is not active.")
        to = timeout or 15.0
        cmd = [self.bsk_bin, "observe", "--session", self.session_id, "--json"]
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=to,
            )
            if res.returncode == 0:
                parsed = _parse_json_safely(res.stdout)
                return parsed if parsed is not None else res.stdout.strip()
            # Try without --json
            res2 = subprocess.run(
                [self.bsk_bin, "observe", "--session", self.session_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=to,
            )
            if res2.returncode == 0:
                return res2.stdout.strip()
            err_msg = res.stderr.strip() or res.stdout.strip()
            raise BrowserSkillExecutionError(f"Observe failed: {err_msg}")
        except subprocess.TimeoutExpired:
            raise BrowserSkillExecutionError(f"Observe timed out after {to}s.")
        except Exception as e:
            if isinstance(e, BrowserSkillExecutionError):
                raise
            raise BrowserSkillExecutionError(f"Observe error: {e}") from e

    def click(self, ref: str, timeout: Optional[float] = None) -> bool:
        """
        Runs 'bsk click <ref> --session <id>' to click an element reference.
        """
        if not self.session_id:
            raise BrowserSkillSessionError("Session is not active.")
        to = timeout or 15.0
        cmd = [self.bsk_bin, "click", ref, "--session", self.session_id]
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=to,
            )
            if res.returncode != 0:
                err = res.stderr.strip() or res.stdout.strip()
                raise BrowserSkillExecutionError(f"Click on {ref} failed: {err}")
            return True
        except subprocess.TimeoutExpired:
            raise BrowserSkillExecutionError(f"Click on {ref} timed out after {to}s.")
        except Exception as e:
            if isinstance(e, BrowserSkillExecutionError):
                raise
            raise BrowserSkillExecutionError(f"Click error: {e}") from e

    def evaluate(self, script: str, timeout: Optional[float] = None) -> Any:
        """
        Executes JavaScript snippet in the target session tab and returns parsed result.
        """
        if not self.session_id:
            raise BrowserSkillSessionError("Session is not active.")
        to = timeout or 15.0
        cmd = [self.bsk_bin, "evaluate", script, "--session", self.session_id, "--json"]
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=to,
            )
            if res.returncode != 0:
                # Try without --json flag
                cmd_nojson = [self.bsk_bin, "evaluate", script, "--session", self.session_id]
                res_nojson = subprocess.run(
                    cmd_nojson,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=to,
                )
                if res_nojson.returncode != 0:
                    err_msg = res.stderr.strip() or res.stdout.strip()
                    raise BrowserSkillExecutionError(f"Evaluate failed: {err_msg}")
                res = res_nojson

            parsed = _parse_json_safely(res.stdout)
            if isinstance(parsed, dict):
                # Check for .ok status or .result field
                if "result" in parsed:
                    return parsed["result"]
                if "value" in parsed:
                    return parsed["value"]
                if "data" in parsed:
                    return parsed["data"]
                return parsed
            elif parsed is not None:
                return parsed

            return res.stdout.strip()
        except subprocess.TimeoutExpired:
            raise BrowserSkillExecutionError(f"JavaScript evaluation timed out after {to}s.")
        except Exception as e:
            if isinstance(e, BrowserSkillExecutionError):
                raise
            raise BrowserSkillExecutionError(f"Evaluate error: {e}") from e

    def tab_list(self, scope: Optional[str] = None, timeout: Optional[float] = None) -> List[Dict[str, Any]]:
        """
        Lists tabs in current session or user browser tabs.
        scope can be 'user' to list user tabs available for borrowing.
        """
        if not self.session_id:
            raise BrowserSkillSessionError("Session is not active.")
        to = timeout or 10.0
        cmd = [self.bsk_bin, "tab", "list", "--session", self.session_id, "--json"]
        if scope:
            cmd.extend(["--scope", scope])
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=to)
            parsed = _parse_json_safely(res.stdout)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and "tabs" in parsed and isinstance(parsed["tabs"], list):
                return parsed["tabs"]
            return []
        except Exception:
            return []

    def tab_borrow(self, tab_id: str, timeout: Optional[float] = None) -> bool:
        """
        Borrows a user tab into the active agent session context.
        """
        if not self.session_id:
            raise BrowserSkillSessionError("Session is not active.")
        to = timeout or 30.0
        cmd = [self.bsk_bin, "tab", "borrow", str(tab_id), "--session", self.session_id]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=to)
            if res.returncode != 0:
                err = res.stderr.strip() or res.stdout.strip()
                raise BrowserSkillExecutionError(f"Tab borrow failed for tab {tab_id}: {err}")
            return True
        except subprocess.TimeoutExpired:
            raise BrowserSkillExecutionError(f"Tab borrow timed out for tab {tab_id}.")
        except Exception as e:
            if isinstance(e, BrowserSkillExecutionError):
                raise
            raise BrowserSkillExecutionError(f"Tab borrow error: {e}") from e

    def tab_return(self, tab_id: str, timeout: Optional[float] = None) -> bool:
        """
        Returns a borrowed user tab back to its original window context.
        """
        if not self.session_id:
            return False
        to = timeout or 15.0
        cmd = [self.bsk_bin, "tab", "return", str(tab_id), "--session", self.session_id]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=to)
            return res.returncode == 0
        except Exception:
            return False

    def download_url_in_session(self, url: str, out_path: str, timeout: Optional[float] = None) -> str:
        """
        Downloads a file URL inside the authenticated session tab using in-page fetch()
        and base64 transfer, preserving all session cookies, CARSI tokens, and login headers.
        """
        to = timeout or 30.0
        script = f"""
        (async () => {{
            try {{
                const resp = await window.fetch({json.dumps(url)}, {{ credentials: 'include' }});
                if (!resp.ok) {{
                    return {{ ok: false, status: resp.status, error: "HTTP " + resp.status }};
                }}
                const blob = await resp.blob();
                return new Promise((resolve) => {{
                    const reader = new FileReader();
                    reader.onloadend = () => {{
                        const res = reader.result;
                        const b64 = (typeof res === 'string') ? (res.split(',')[1] || '') : '';
                        resolve({{ ok: true, status: resp.status, base64: b64, size: blob.size }});
                    }};
                    reader.onerror = () => resolve({{ ok: false, error: 'FileReader error' }});
                    reader.readAsDataURL(blob);
                }});
            }} catch(err) {{
                return {{ ok: false, error: String(err) }};
            }}
        }})()
        """
        res = self.evaluate(script, timeout=to)
        parsed = None
        if isinstance(res, dict):
            parsed = res
        elif isinstance(res, str):
            parsed = _parse_json_safely(res)

        if isinstance(parsed, dict) and parsed.get("ok") and parsed.get("base64"):
            data = base64.b64decode(parsed["base64"])
            with open(out_path, "wb") as f:
                f.write(data)
            return out_path

        err = (parsed.get("error") if isinstance(parsed, dict) else None) or "Failed to transfer file data via browser tab"
        raise BrowserSkillExecutionError(f"In-session URL download failed: {err}")

    def download(
        self,
        target: str,
        out_path: str,
        overwrite: bool = True,
        timeout: Optional[float] = None,
    ) -> str:
        """
        Downloads a file or target element via BrowserSkill.
        - If target starts with '@' (e.g. '@e3'): invokes native 'bsk download <ref>'.
        - If target is an HTTP/HTTPS URL: executes authenticated in-session download preserving
          session cookies and institutional credentials, falling back to 'bsk download'.
        """
        if not self.session_id:
            raise BrowserSkillSessionError("Session is not active.")
        to = timeout or 60.0
        out_abs = os.path.abspath(out_path)
        os.makedirs(os.path.dirname(out_abs), exist_ok=True)

        # Case 1: Target is a URL -> attempt in-page authenticated fetch first
        if target.startswith(("http://", "https://")):
            try:
                self.download_url_in_session(target, out_abs, timeout=to)
                if os.path.exists(out_abs) and os.path.getsize(out_abs) > 0:
                    return out_abs
            except Exception:
                pass

        # Case 2: Element ref (@eN) or URL fallback via bsk download
        cmd = [self.bsk_bin, "download", target, "--session", self.session_id, "--out", out_abs]
        if overwrite:
            cmd.append("--overwrite")

        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=to,
            )
            if res.returncode == 0:
                return out_abs
            err_msg = res.stderr.strip() or res.stdout.strip()
            raise BrowserSkillExecutionError(f"Download failed: {err_msg or f'exit code {res.returncode}'}")
        except subprocess.TimeoutExpired:
            raise BrowserSkillExecutionError(f"Download timed out after {to}s.")
        except Exception as e:
            if isinstance(e, BrowserSkillExecutionError):
                raise
            raise BrowserSkillExecutionError(f"Download error: {e}") from e

    @classmethod
    def stop_all(cls, bsk_bin: Optional[str] = None) -> bool:
        """Emergency cleanup to terminate all active sessions."""
        exe = bsk_bin or find_bsk_executable()
        if not exe:
            return False
        try:
            res = subprocess.run(
                [exe, "session", "stop", "--all"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10.0,
            )
            return res.returncode == 0
        except Exception:
            return False

    @classmethod
    def list_sessions(cls, bsk_bin: Optional[str] = None, timeout: float = 5.0) -> List[Dict[str, Any]]:
        """Lists active browser sessions via 'bsk session list --json'."""
        exe = bsk_bin or find_bsk_executable()
        if not exe:
            return []
        try:
            res = subprocess.run(
                [exe, "session", "list", "--json"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout,
            )
            parsed = _parse_json_safely(res.stdout)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and "sessions" in parsed and isinstance(parsed["sessions"], list):
                return parsed["sessions"]
            return []
        except Exception:
            return []


ACCORDION_EXPANSION_SCRIPT = """
(() => {
    try {
        // Expand all details elements
        document.querySelectorAll("details:not([open])").forEach(d => d.open = true);
        // Click collapsed accordion / supplementary buttons
        document.querySelectorAll("button[aria-expanded='false'], [role='button'][aria-expanded='false']").forEach(b => {
            const txt = (b.innerText || b.getAttribute("aria-label") || "").toLowerCase();
            if (
                txt.includes("supp") ||
                txt.includes("material") ||
                txt.includes("data") ||
                txt.includes("table") ||
                txt.includes("appendix") ||
                txt.includes("附") ||
                txt.includes("补充")
            ) {
                try { b.click(); } catch(e) {}
            }
        });
    } catch(e) {}
})()
"""

PAGE_EXTRACTION_SCRIPT = """
(() => {
    return JSON.stringify({
        html: document.documentElement.outerHTML,
        url: window.location.href,
        title: document.title
    });
})()
"""

PAGE_META_SCRIPT = """
(() => {
    return JSON.stringify({
        url: window.location.href,
        title: document.title
    });
})()
"""


def fetch_page_via_browserskill(
    url_or_doi: str,
    wait_selector: Optional[str] = None,
    timeout: float = 45.0,
    browser: Optional[str] = None,
    tab_id: Optional[str] = None,
    expand_accordions: bool = True,
    session: Optional[BrowserSkillSession] = None,
    bsk_bin: Optional[str] = None,
) -> Tuple[str, int, str]:
    """
    Fetches an article page using Tencent BrowserSkill:
    1. Normalizes DOI if given as '10.xxxx/...'.
    2. Opens an isolated Agent Window session in the user's running browser (--no-focus)
       or borrows an existing user tab if tab_id is provided.
    3. Follows redirects naturally (e.g. doi.org -> sciencedirect.com).
    4. Automatically expands collapsed supplementary sections.
    5. Retrieves fully rendered HTML DOM via native 'bsk get-html' and final landing page URL.
    6. Safely cleans up the session (if created internally).

    Returns: (html_content, status_code, final_url)
    """
    exe = bsk_bin or (session.bsk_bin if session else None) or find_bsk_executable()
    if not exe:
        raise BrowserSkillUnavailableError(
            "Tencent BrowserSkill (bsk) executable not found. "
            "Please install BrowserSkill from https://github.com/Tencent/BrowserSkill"
        )

    # Normalize DOI input
    target = url_or_doi.strip()
    if target.startswith("10."):
        fetch_url = f"https://doi.org/{target}"
    else:
        fetch_url = target

    should_close_session = False
    sess = session
    if not sess or not sess.session_id:
        sess = BrowserSkillSession(browser=browser, no_focus=True, bsk_bin=exe, timeout=timeout)
        sess.start()
        should_close_session = True

    try:
        # If tab_id provided, borrow that existing user tab
        borrowed = False
        if tab_id:
            try:
                sess.tab_borrow(str(tab_id), timeout=min(timeout, 30.0))
                borrowed = True
            except Exception as e_borrow:
                # Fall back to standard navigation if borrow fails
                pass

        if not borrowed:
            sess.navigate(fetch_url, timeout=timeout)

        # Wait for selector if specified
        if wait_selector:
            check_script = f"!!document.querySelector({json.dumps(wait_selector)})"
            start_t = time.time()
            wait_limit = min(timeout, 15.0)
            while time.time() - start_t < wait_limit:
                try:
                    exists = sess.evaluate(check_script, timeout=3.0)
                    if exists is True or str(exists).lower() == "true":
                        break
                except Exception:
                    pass
                time.sleep(0.5)

        # Expand collapsed supplementary accordions if requested
        if expand_accordions:
            try:
                sess.evaluate(ACCORDION_EXPANSION_SCRIPT, timeout=5.0)
            except Exception:
                pass

        # Extract DOM, title, and final landing page URL
        final_html = ""
        final_url = fetch_url
        try:
            extracted_raw = sess.evaluate(PAGE_EXTRACTION_SCRIPT, timeout=15.0)
            if isinstance(extracted_raw, str):
                parsed_data = _parse_json_safely(extracted_raw)
                if isinstance(parsed_data, dict):
                    final_html = parsed_data.get("html") or ""
                    final_url = parsed_data.get("url") or fetch_url
                else:
                    final_html = extracted_raw
            elif isinstance(extracted_raw, dict):
                final_html = extracted_raw.get("html") or ""
                final_url = extracted_raw.get("url") or fetch_url
        except Exception:
            pass

        # Fallback to get_html
        if not final_html or not final_html.strip():
            try:
                final_html = sess.get_html(timeout=15.0)
            except Exception:
                pass

        if not final_html or not final_html.strip():
            # Second fallback: direct outerHTML evaluate
            try:
                final_html = str(sess.evaluate("document.documentElement.outerHTML", timeout=10.0))
            except Exception:
                pass

        if not final_html or not final_html.strip():
            raise BrowserSkillExecutionError("Failed to extract HTML DOM from BrowserSkill session.")

        # Return borrowed tab if applicable
        if borrowed and tab_id:
            try:
                sess.tab_return(str(tab_id), timeout=5.0)
            except Exception:
                pass

        return final_html, 200, final_url

    finally:
        if should_close_session and sess:
            sess.stop()


def download_file_via_browserskill(
    url_or_ref: str,
    output_path: str,
    session: Optional[BrowserSkillSession] = None,
    referer: Optional[str] = None,
    browser: Optional[str] = None,
    bsk_bin: Optional[str] = None,
    timeout: float = 60.0,
) -> Optional[str]:
    """
    Downloads a supplementary file using BrowserSkill within an authenticated session.
    - If an active session is provided, reuses its existing page cookies and session state.
    - If no session is active, opens a session, navigates to referer (or file origin)
      to establish cookies/session context, downloads, and closes.
    Returns the saved file path if successful, or None on failure.
    """
    exe = bsk_bin or (session.bsk_bin if session else None) or find_bsk_executable()
    if not exe:
        return None

    try:
        if session and session.session_id:
            return session.download(url_or_ref, output_path, overwrite=True, timeout=timeout)
        else:
            with BrowserSkillSession(browser=browser, no_focus=True, bsk_bin=exe, timeout=timeout) as sess:
                if referer and str(referer).startswith(("http://", "https://")):
                    try:
                        sess.navigate(referer, timeout=min(timeout, 20.0))
                    except Exception:
                        pass
                return sess.download(url_or_ref, output_path, overwrite=True, timeout=timeout)
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Tencent BrowserSkill (bsk) Adapter Interface for journal-supp-downloader",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Commands:
  status                   Check BrowserSkill installation, daemon, and connected browsers
  doctor                   Run 'bsk doctor' diagnostics
  sessions                 List active browser sessions ('bsk session list')
  tabs                     List open browser tabs ('bsk tab list')
  fetch <url_or_doi>       Fetch rendered page DOM via BrowserSkill Agent Window
  borrow-fetch <tab_id>    Borrow existing user tab, extract HTML DOM, and return tab
  observe <url_or_doi>     Inspect interactive page elements and visual refs (@e1, @e2)
  download <ref_or_url>    Download file or attachment using authenticated browser session
  stop-all                 Emergency cleanup of all BrowserSkill sessions
        """,
    )
    subparsers = parser.add_subparsers(dest="subcommand", help="Subcommand to execute")

    # Subcommand: status
    subparsers.add_parser("status", help="Check BrowserSkill health & connected browsers")

    # Subcommand: doctor
    subparsers.add_parser("doctor", help="Run bsk doctor")

    # Subcommand: sessions
    subparsers.add_parser("sessions", help="List active browser sessions")

    # Subcommand: tabs
    tab_p = subparsers.add_parser("tabs", help="List browser tabs available for borrowing")
    tab_p.add_argument("--scope", default="user", choices=["user", "all"], help="Scope of tabs (user or all)")

    # Subcommand: stop-all
    subparsers.add_parser("stop-all", help="Stop all running browser sessions")

    # Subcommand: fetch
    fetch_parser = subparsers.add_parser("fetch", help="Fetch rendered article page HTML via BrowserSkill")
    fetch_parser.add_argument("url_or_doi", help="Article URL or DOI (e.g. 10.1016/...)")
    fetch_parser.add_argument("-o", "--output", default=None, help="Save HTML to output file")
    fetch_parser.add_argument("--timeout", type=float, default=45.0, help="Timeout in seconds")
    fetch_parser.add_argument("--browser", default=None, help="Specific browser instance name or id")
    fetch_parser.add_argument("--wait-selector", default=None, help="Wait for specific CSS selector")

    # Subcommand: borrow-fetch
    borrow_p = subparsers.add_parser("borrow-fetch", help="Borrow existing user tab, extract DOM, and return")
    borrow_p.add_argument("tab_id", help="User tab ID to borrow")
    borrow_p.add_argument("-o", "--output", default=None, help="Save HTML to output file")
    borrow_p.add_argument("--timeout", type=float, default=45.0, help="Timeout in seconds")

    # Subcommand: observe
    obs_p = subparsers.add_parser("observe", help="Inspect interactive elements and refs on article page")
    obs_p.add_argument("url_or_doi", help="Article URL or DOI")
    obs_p.add_argument("--timeout", type=float, default=45.0, help="Timeout in seconds")

    # Subcommand: download
    dl_p = subparsers.add_parser("download", help="Download file attachment via authenticated session")
    dl_p.add_argument("target", help="Element reference (@eN) or download URL")
    dl_p.add_argument("-o", "--output", required=True, help="Local output file path")
    dl_p.add_argument("--referer", default=None, help="Article referer page URL")
    dl_p.add_argument("--timeout", type=float, default=60.0, help="Timeout in seconds")

    args = parser.parse_args()

    if not args.subcommand or args.subcommand == "status":
        print("Checking Tencent BrowserSkill status...")
        info = check_browserskill_status()
        print(f"  Installed:          {info['available']}")
        print(f"  Executable:         {info['bsk_path'] or 'Not found'}")
        print(f"  Version:            {info['version'] or 'N/A'}")
        print(f"  Daemon Running:     {info['daemon_running']}")
        print(f"  Browser Connected:  {info['browser_connected']}")
        if info["browsers"]:
            print(f"  Connected Browsers: {info['browsers']}")
        print(f"  Status Message:     {info['message']}")
        sys.exit(0 if (info["available"] and info["daemon_running"]) else 1)

    elif args.subcommand == "doctor":
        exe = find_bsk_executable()
        if not exe:
            print("Error: BrowserSkill (bsk) CLI is not installed.")
            sys.exit(1)
        res = subprocess.run([exe, "doctor"])
        sys.exit(res.returncode)

    elif args.subcommand == "sessions":
        sessions = BrowserSkillSession.list_sessions()
        if sessions:
            print(f"Active BrowserSkill sessions ({len(sessions)}):")
            for s in sessions:
                sid = s.get("id") or s.get("session_id") or "unknown"
                browser = s.get("browser") or s.get("browser_id") or "default"
                print(f"  - Session ID: {sid} (Browser: {browser})")
        else:
            print("No active BrowserSkill sessions found.")
        sys.exit(0)

    elif args.subcommand == "tabs":
        with BrowserSkillSession(no_focus=True) as sess:
            tabs = sess.tab_list(scope=args.scope)
            if tabs:
                print(f"Available {args.scope} tabs ({len(tabs)}):")
                for t in tabs:
                    tid = t.get("id") or t.get("tab_id")
                    title = t.get("title") or "Untitled"
                    url = t.get("url") or ""
                    print(f"  - [{tid}] {title} ({url})")
            else:
                print(f"No {args.scope} tabs found.")
        sys.exit(0)

    elif args.subcommand == "stop-all":
        success = BrowserSkillSession.stop_all()
        print("Stopped all sessions." if success else "Failed or no sessions running.")
        sys.exit(0 if success else 1)

    elif args.subcommand == "fetch":
        print(f"Fetching {args.url_or_doi} via Tencent BrowserSkill...")
        try:
            html, status, final_url = fetch_page_via_browserskill(
                args.url_or_doi,
                wait_selector=args.wait_selector,
                timeout=args.timeout,
                browser=args.browser,
            )
            print(f"  Status:    {status}")
            print(f"  Final URL: {final_url}")
            print(f"  DOM Size:  {len(html)} bytes")
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  Saved to:  {args.output}")
        except Exception as e:
            print(f"Error fetching page via BrowserSkill: {e}")
            sys.exit(1)

    elif args.subcommand == "borrow-fetch":
        print(f"Borrowing tab {args.tab_id} via Tencent BrowserSkill...")
        try:
            html, status, final_url = fetch_page_via_browserskill(
                url_or_doi="",
                tab_id=args.tab_id,
                timeout=args.timeout,
            )
            print(f"  Status:    {status}")
            print(f"  Final URL: {final_url}")
            print(f"  DOM Size:  {len(html)} bytes")
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    f.write(html)
                print(f"  Saved to:  {args.output}")
        except Exception as e:
            print(f"Error borrowing tab via BrowserSkill: {e}")
            sys.exit(1)

    elif args.subcommand == "observe":
        print(f"Observing elements on {args.url_or_doi}...")
        with BrowserSkillSession(no_focus=True, timeout=args.timeout) as sess:
            sess.navigate(args.url_or_doi, timeout=args.timeout)
            obs = sess.observe(timeout=args.timeout)
            print(json.dumps(obs, indent=2, ensure_ascii=False) if isinstance(obs, (dict, list)) else obs)
        sys.exit(0)

    elif args.subcommand == "download":
        print(f"Downloading {args.target} via BrowserSkill...")
        res = download_file_via_browserskill(
            args.target,
            args.output,
            referer=args.referer,
            timeout=args.timeout,
        )
        if res and os.path.exists(res):
            print(f"  Downloaded: {res} ({os.path.getsize(res)} bytes)")
            sys.exit(0)
        else:
            print(f"  Failed to download {args.target}")
            sys.exit(1)


if __name__ == "__main__":
    main()
