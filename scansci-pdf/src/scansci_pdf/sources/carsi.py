"""CARSI (Shibboleth/SAML) federated authentication for publisher access.

Provides institutional login through CARSI federation, supporting
publishers like Elsevier, Springer Nature, Wiley, ACS, etc.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from ..config import DATA_DIR
from ..log import get_logger
from ..publisher_strategies import (
    _IDP_MAP,
    _AUTH_KEYWORDS,
    _AUTH_TITLES,
    _INSTITUTION_SEARCH_SELECTORS,
    _SSO_LINK_FINDER_JS,
    _INSTITUTION_CLICK_JS,
)

log = get_logger()

_PUBLISHER_CONFIGS_FILE = DATA_DIR / "publisher_carsi.json"
_PKG_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_PKG_PUBLISHER_CONFIGS_FILE = _PKG_DATA_DIR / "publisher_carsi.json"


@dataclass
class PublisherCARSIConfig:
    name: str
    domains: list[str]
    login_url: str
    search_selector: str
    result_selector: str
    success_url_pattern: str
    pdf_pattern: str


def _load_publisher_configs() -> dict[str, PublisherCARSIConfig]:
    # Try package data first, then user data dir
    config_file = _PKG_PUBLISHER_CONFIGS_FILE if _PKG_PUBLISHER_CONFIGS_FILE.exists() else _PUBLISHER_CONFIGS_FILE
    if not config_file.exists():
        return {}
    data = json.loads(config_file.read_text(encoding="utf-8"))
    configs = {}
    for key, val in data.items():
        configs[key] = PublisherCARSIConfig(**val)
    return configs


def detect_publisher(url: str) -> str | None:
    """Detect publisher key from a URL."""
    hostname = urlparse(url).hostname or ""
    configs = _load_publisher_configs()
    for key, cfg in configs.items():
        for domain in cfg.domains:
            if domain in hostname:
                return key
    return None


class CARSIClient:
    """Manages CARSI/Shibboleth federated authentication with academic publishers."""

    _login_lock = threading.Lock()

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._sessions: dict[str, requests.Session] = {}
        self._publisher_configs = _load_publisher_configs()
        self._cookie_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / "carsi_cookies"
        self._cookie_dir.mkdir(parents=True, exist_ok=True)

    def _cookie_path(self, publisher: str) -> Path:
        return self._cookie_dir / f"{publisher}.json"

    def _get_session(self, publisher: str) -> requests.Session:
        if publisher not in self._sessions:
            sess = requests.Session()
            sess.trust_env = False
            sess.headers.update({
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            })
            self._sessions[publisher] = sess
        return self._sessions[publisher]

    def login(self, publisher: str, force: bool = False) -> bool:
        """Ensure we have a valid CARSI session for the given publisher."""
        with self._login_lock:
            if not force and self._try_load_cookies(publisher):
                log.info(f"   [CARSI] Loaded saved cookies for {publisher}")
                return True
            log.info(f"   [CARSI] No valid session for {publisher}. Opening browser...")
            return self._browser_login(publisher)

    def fetch(self, url: str, **kwargs) -> requests.Response | None:
        """Fetch a URL using CARSI-authenticated session."""
        publisher = detect_publisher(url)
        if not publisher:
            return None

        if not self.login(publisher):
            return None

        sess = self._get_session(publisher)
        kwargs.setdefault("timeout", 30)
        kwargs.setdefault("allow_redirects", True)
        try:
            return sess.get(url, **kwargs)
        except requests.RequestException as e:
            log.warning(f"   [CARSI] Fetch failed: {e}")
            return None

    def download_via_camofox(self, doi: str, article_url: str, output_path: Path) -> dict[str, Any] | None:
        """Download PDF via stealth browser browser with CARSI auth. Single session: login + download."""
        publisher = detect_publisher(article_url)
        if not publisher:
            return None
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return None

        try:
            from cloakbrowser import launch  # noqa: F401
        except ImportError:
            log.info("   [CARSI-Camofox] cloakbrowser not installed")
            return None

        idp_name = self.config.get("carsi_idp_name", "")
        if not idp_name:
            log.info("   [CARSI-Camofox] No carsi_idp_name configured")
            return None

        idp_en = _IDP_MAP.get(idp_name, idp_name)

        from ..pdf_utils import is_pdf_file, success as _success

        # Serialize browser opens across threads — only one browser at a time
        with self._login_lock:
            log.info(f"   [CARSI-Camofox] Opening browser for {publisher}...")
            try:
                from ..publisher_strategies import _visible_camofox, _save_all_cookie_formats

                with _visible_camofox(self.config, publisher, viewport=None) as (context, page):

                    # Restore saved cookies if any (supplements persistent profile)
                    cookie_file = self._cookie_path(publisher)
                    if cookie_file.exists():
                        try:
                            saved = json.loads(cookie_file.read_text(encoding="utf-8"))
                            pw_cookies = []
                            for c in saved:
                                pw_c = {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
                                if pw_c["domain"]:
                                    pw_cookies.append(pw_c)
                            if pw_cookies:
                                context.add_cookies(pw_cookies)
                                log.info(f"   [CARSI-Camofox] Restored {len(pw_cookies)} cookies from file")
                        except Exception:
                            pass

                    # Capture PDF from network
                    captured_pdf = []
                    def on_response(response):
                        try:
                            ct = response.headers.get("content-type", "")
                            url = response.url
                            is_pdf_ct = "pdf" in ct.lower() or "octet-stream" in ct.lower()
                            is_pdf_url = url.lower().endswith(".pdf") or "/pdfdirect/" in url or "/doi/pdf/" in url
                            if not (is_pdf_ct or is_pdf_url):
                                return
                            if response.status >= 400:
                                return
                            body = response.body()
                            if len(body) > 5000 and body[:4] == b"%PDF-":
                                captured_pdf.append(body)
                                log.info(f"   [CARSI-Camofox] PDF captured: {len(body)} bytes")
                        except Exception:
                            pass
                    page.on("response", on_response)

                    # Step 1: Navigate to article page first (gets Cloudflare clearance)
                    log.info(f"   [CARSI-Camofox] Loading article: {article_url[:60]}")
                    try:
                        page.goto(article_url, wait_until="domcontentloaded", timeout=60000)
                        time.sleep(5)
                    except Exception:
                        pass

                    title = page.title()
                    url = page.url
                    log.info(f"   [CARSI-Camofox] Page: '{title[:40]}' {url[:60]}")

                    # Wait for Cloudflare challenge to resolve (visible stealth browser can pass it)
                    for _cf_wait in range(6):
                        _cf_title = (page.title() or "").lower()
                        if any(_sig in _cf_title for _sig in ("just a moment", "attention required", "verify", "security check")):
                            log.info(f"   [CARSI-Camofox] Cloudflare challenge detected, waiting... ({_cf_wait+1}/6)")
                            time.sleep(5)
                        else:
                            break
                    else:
                        log.info("   [CARSI-Camofox] Cloudflare challenge did not resolve")

                    # Step 1b: Check if restored cookies already grant access
                    has_cookies = cookie_file.exists()
                    needs_login = False
                    try:
                        needs_login = page.evaluate("""
                            () => {
                                if (!document.body) return true;
                                const body = (document.body.innerText || '').toLowerCase();
                                const hasPaywall = body.includes('purchase') || body.includes('subscribe')
                                    || body.includes('access through your institution')
                                    || body.includes('sign in to access')
                                    || body.includes('buy this article');
                                const hasPdf = !!document.querySelector('a[href*="pdf"], a[href*="download"], iframe[src*="pdf"]');
                                return hasPaywall && !hasPdf;
                            }
                        """)
                    except Exception as _e:
                        log.info(f"   [CARSI-Camofox] paywall check error (likely Cloudflare): {_e}")
                        needs_login = True

                    # Even if page looks accessible, verify cookies work by
                    # trying a quick pdfft probe. ScienceDirect may accept
                    # expired cookies without showing a paywall, but return
                    # HTML instead of PDF for /pdfft requests.
                    cookies_valid = False
                    if has_cookies and not needs_login:
                        pii_from_url = ""
                        import re as _re
                        _pm = _re.search(r"pii/([A-Z0-9]+)", page.url)
                        if _pm:
                            pii_from_url = _pm.group(1)
                        if pii_from_url:
                            try:
                                probe_ok = page.evaluate(f"""
                                    (async () => {{
                                        try {{
                                            const r = await fetch('/science/article/pii/{pii_from_url}/pdfft',
                                                {{credentials: 'include', headers: {{'Accept': 'application/pdf'}}}});
                                            const ct = r.headers.get('content-type') || '';
                                            return r.ok && ct.includes('pdf');
                                        }} catch(e) {{ return false; }}
                                    }})()
                                """)
                                cookies_valid = bool(probe_ok)
                                if cookies_valid:
                                    log.info("   [CARSI-Camofox] Cookie probe OK, skipping login")
                                else:
                                    log.info("   [CARSI-Camofox] Cookie probe failed, re-login needed")
                            except Exception:
                                log.info("   [CARSI-Camofox] Cookie probe error, will re-login")

                    if not cookies_valid:
                        # Step 2: Click "Institutional login" link on article page
                        sso_clicked = page.evaluate(_SSO_LINK_FINDER_JS)
                        if not sso_clicked:
                            log.info("   [CARSI-Camofox] No SSO link found, trying direct login URL...")
                            page.goto(cfg.login_url, wait_until="domcontentloaded", timeout=30000)
                            time.sleep(5)

                        time.sleep(8)

                        # Step 3: Search for institution in the WAYF page
                        search_input = page.query_selector('#searchInstitution')
                        if not search_input:
                            for sel in _INSTITUTION_SEARCH_SELECTORS[1:]:  # skip #searchInstitution (already tried)
                                search_input = page.query_selector(sel)
                                if search_input:
                                    break

                        if search_input:
                            search_input.fill(idp_en)
                            time.sleep(3)
                            log.info(f"   [CARSI-Camofox] Searched for '{idp_en}'")

                            # Click matching institution
                            clicked = page.evaluate(_INSTITUTION_CLICK_JS, idp_en)
                            if clicked:
                                log.info(f"   [CARSI-Camofox] Selected: {clicked}")
                                time.sleep(5)
                            else:
                                search_input.press("Enter")
                                time.sleep(3)
                        else:
                            log.info("   [CARSI-Camofox] No institution search box found")

                        # Step 4: Wait for CAS login
                        _ak = _AUTH_KEYWORDS
                        _at = _AUTH_TITLES

                        url = page.url
                        title = page.title()
                        if any(x in url.lower() for x in _ak) or any(x in title for x in _at):
                            log.info("   [CARSI-Camofox] CAS login required. Please log in...")
                            for i in range(100):
                                time.sleep(3)
                                try:
                                    title = page.title()
                                    url = page.url
                                except Exception:
                                    return None
                                is_auth = any(x in title for x in _at)
                                is_auth_url = any(x in url.lower() for x in _ak)
                                if not is_auth and not is_auth_url:
                                    # Login success - save cookies in all formats + bridge to camofox-browser
                                    try:
                                        cookies = context.cookies()
                                        _save_all_cookie_formats(cookies, publisher, self.config)
                                    except Exception:
                                        pass
                                    break
                            else:
                                log.info("   [CARSI-Camofox] Login timed out")
                                return None
                        else:
                            log.info("   [CARSI-Camofox] Already authenticated")

                    # Step 5: Navigate to article (with CARSI auth now)
                    time.sleep(2)
                    log.info(f"   [CARSI-Camofox] Navigating to article: {article_url[:60]}")
                    try:
                        page.goto(article_url, wait_until="domcontentloaded", timeout=30000)
                        time.sleep(5)
                    except Exception:
                        pass

                    # Check for PDF via network capture
                    if captured_pdf:
                        output_path.parent.mkdir(parents=True, exist_ok=True)
                        output_path.write_bytes(captured_pdf[-1])
                        if is_pdf_file(output_path):
                            return _success(doi, output_path, "CARSI-Camofox")

                    # Step 6: Try direct PDF URL
                    import re
                    pii_match = re.search(r"pii/([A-Z0-9]+)", page.url)
                    pii_value = pii_match.group(1) if pii_match else ""
                    pdf_pattern = cfg.pdf_pattern.replace("{doi}", doi).replace("{pii}", pii_value)
                    if pdf_pattern and not pdf_pattern.startswith("http"):
                        pdf_url = f"https://{cfg.domains[0]}{pdf_pattern}"
                    else:
                        pdf_url = pdf_pattern

                    if pdf_url and "{pii}" not in pdf_url:
                        log.info(f"   [CARSI-Camofox] Trying PDF: {pdf_url[:80]}")
                        captured_pdf.clear()
                        try:
                            page.goto(pdf_url, wait_until="commit", timeout=30000)
                            time.sleep(5)
                        except Exception:
                            pass
                        if captured_pdf:
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            output_path.write_bytes(captured_pdf[-1])
                            if is_pdf_file(output_path):
                                return _success(doi, output_path, "CARSI-Camofox")

                    # Step 7: Find PDF link in HTML
                    from ..pdf_utils import extract_pdf_url_from_html
                    html = page.content()
                    found_pdf = extract_pdf_url_from_html(html, page.url)
                    if found_pdf:
                        log.info(f"   [CARSI-Camofox] Found PDF link: {found_pdf[:80]}")
                        captured_pdf.clear()
                        try:
                            page.goto(found_pdf, wait_until="commit", timeout=30000)
                            time.sleep(5)
                        except Exception:
                            pass
                        if captured_pdf:
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            output_path.write_bytes(captured_pdf[-1])
                            if is_pdf_file(output_path):
                                return _success(doi, output_path, "CARSI-Camofox")

                    # Step 8: Click PDF button
                    click_result = page.evaluate("""
                        () => {
                            const links = document.querySelectorAll('a');
                            for (const a of links) {
                                const href = (a.getAttribute('href') || '').toLowerCase();
                                const text = (a.innerText || '').toLowerCase();
                                if ((href.includes('pdf') || href.includes('download')) && !href.includes('supplement')) {
                                    if (text.includes('pdf') || text.includes('download')) {
                                        a.click();
                                        return a.href;
                                    }
                                }
                            }
                            return null;
                        }
                    """)
                    if click_result:
                        log.info(f"   [CARSI-Camofox] Clicked: {str(click_result)[:80]}")
                        time.sleep(8)
                        if captured_pdf:
                            output_path.parent.mkdir(parents=True, exist_ok=True)
                            output_path.write_bytes(captured_pdf[-1])
                            if is_pdf_file(output_path):
                                return _success(doi, output_path, "CARSI-Camofox")

                    log.info(f"   [CARSI-Camofox] No PDF found. Title: {page.title()[:40]} URL: {page.url[:60]}")
                    return None

            except Exception as e:
                log.info(f"   [CARSI-Camofox] Error: {e}")
                return None

    def download_via_browser(self, doi: str, article_url: str, output_path: Path) -> dict[str, Any] | None:
        """Download PDF via browser in a single session (login + download).

        This avoids Cloudflare TLS fingerprinting issues by keeping everything
        in one browser session.
        """
        publisher = detect_publisher(article_url)
        if not publisher:
            return None

        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return None

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.by import By
        except ImportError:
            log.info("   [CARSI-Browser] selenium not installed")
            return None

        download_dir = str(output_path.parent)
        options = Options()
        options.add_argument("--no-first-run")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-allow-origins=*")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        prefs = {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "plugins.always_open_pdf_externally": True,
        }
        options.add_experimental_option("prefs", prefs)

        try:
            driver = webdriver.Chrome(options=options)
        except Exception as e:
            log.info(f"   [CARSI-Browser] Chrome launch failed: {e}")
            return None

        try:
            # Step 1: Navigate to institutional login
            driver.get(cfg.login_url)
            time.sleep(5)

            # Step 2: Search for institution
            idp_name = self.config.get("carsi_idp_name", "")
            if idp_name:
                search = driver.find_element(By.ID, "bdd-email")
                search.send_keys(idp_name[:10])  # Use first few chars
                time.sleep(3)
                # Click on institution
                driver.execute_script('''
                    var buttons = document.querySelectorAll("button");
                    for (var i = 0; i < buttons.length; i++) {
                        if (buttons[i].textContent.includes("''' + idp_name[:4] + '''")) {
                            buttons[i].click();
                            return true;
                        }
                    }
                    return false;
                ''')
                time.sleep(5)

            # Step 3: Wait for CAS login (user interaction)
            _login_keywords = ("cas", "login", "idp", "saml", "wayf", "auth", "sso", "passport", "accounts")
            url = driver.current_url
            if any(x in url.lower() for x in _login_keywords):
                log.info(f"   [CARSI-Browser] Please log in via CAS in the browser...")
                max_wait = 180
                elapsed = 0
                while elapsed < max_wait:
                    time.sleep(3)
                    elapsed += 3
                    try:
                        current = driver.current_url
                    except Exception:
                        return None
                    if not any(x in current.lower() for x in _login_keywords):
                        break
                else:
                    log.info("   [CARSI-Browser] Login timed out.")
                    return None

            # Step 4: Navigate to article
            time.sleep(2)
            driver.get(article_url)
            time.sleep(8)

            # Step 5: Check for PDF access
            body = driver.execute_script("return document.body.innerText")
            if "robot" in body.lower() or "captcha" in body.lower():
                log.info("   [CARSI-Browser] Bot detection triggered.")
                return None

            # Look for PDF download link
            links = driver.find_elements(By.CSS_SELECTOR, "a")
            for link in links:
                href = link.get_attribute("href") or ""
                text = link.text.strip().lower()
                if "pdf" in text and "purchase" not in text:
                    log.info(f"   [CARSI-Browser] Found PDF link: {href[:80]}")
                    driver.get(href)
                    time.sleep(5)
                    # Check if downloaded
                    from ..pdf_utils import is_pdf_file
                    downloaded = self._find_downloaded_pdf(download_dir, doi)
                    if downloaded:
                        return {"success": True, "path": str(downloaded), "source": "CARSI-Browser"}
                    break

            # Try pdfft pattern
            if publisher == "sciencedirect":
                import re
                pii_match = re.search(r"pii/([A-Z0-9]+)", article_url)
                if pii_match:
                    pdfft_url = f"https://www.sciencedirect.com/science/article/pii/{pii_match.group(1)}/pdfft"
                    driver.get(pdfft_url)
                    time.sleep(5)
                    from ..pdf_utils import is_pdf_file
                    downloaded = self._find_downloaded_pdf(download_dir, doi)
                    if downloaded:
                        return {"success": True, "path": str(downloaded), "source": "CARSI-Browser"}

        except Exception as e:
            log.info(f"   [CARSI-Browser] Error: {e}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass
        return None

    def _find_downloaded_pdf(self, download_dir: str, doi: str) -> Path | None:
        """Check download directory for recently downloaded PDF files."""
        dir_path = Path(download_dir)
        if not dir_path.exists():
            return None
        now = time.time()
        for f in dir_path.iterdir():
            if f.suffix.lower() == ".pdf" and (now - f.stat().st_mtime) < 30:
                try:
                    if f.stat().st_size > 1000:
                        return f
                except OSError:
                    pass
        return None

    def _try_load_cookies(self, publisher: str) -> bool:
        cookie_file = self._cookie_path(publisher)
        if not cookie_file.exists():
            return False
        try:
            cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False

        sess = self._get_session(publisher)
        for cookie in cookies:
            sess.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain", ""),
                path=cookie.get("path", "/"),
            )
        return self._validate_session(publisher)

    def _validate_session(self, publisher: str) -> bool:
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return False
        sess = self._get_session(publisher)

        # Check cookie file freshness — accept if < 24h old
        cookie_file = self._cookie_path(publisher)
        try:
            import os
            age_hours = (time.time() - os.path.getmtime(cookie_file)) / 3600
            if age_hours > 24:
                log.info(f"   [CARSI] Cookies for {publisher} expired ({age_hours:.1f}h old)")
                return False
        except OSError:
            return False

        # Validate by hitting a publisher page that requires auth
        # Use the main domain, not login_url (which always contains "login")
        try:
            test_url = f"https://{cfg.domains[0]}/"
            resp = sess.get(test_url, timeout=15, allow_redirects=True)
            # If we get redirected to a SSO/CAS/WAYF page, session is invalid
            url_lower = resp.url.lower()
            sso_keywords = ("wayf", "shibboleth", "saml", "idp.bayern", "passport")
            if any(k in url_lower for k in sso_keywords):
                return False
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _browser_login(self, publisher: str) -> bool:
        """Login via CARSI by opening the publisher's institutional login page. Tries stealth browser first, falls back to Selenium."""
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            log.error(f"   [CARSI] Unknown publisher: {publisher}")
            return False

        # Try stealth browser (stealth browser) first
        try:
            from ..camofox_login import carsi_login
            if carsi_login(publisher, self.config, login_url=cfg.login_url, domains=cfg.domains):
                return True
        except Exception as exc:
            log.info(f"   [CARSI] stealth browser login failed: {exc}")

        # Fallback to Selenium
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
        except ImportError:
            log.error("   [CARSI] selenium not installed")
            return False

        idp_name = self.config.get("carsi_idp_name", "")
        log.info(f"   [CARSI] Opening {cfg.name} institutional login...")

        options = Options()
        options.add_argument("--no-first-run")
        options.add_argument("--no-default-browser-check")
        options.add_argument("--disable-gpu")
        options.add_argument("--remote-allow-origins=*")
        options.add_experimental_option("excludeSwitches", ["enable-automation"])

        try:
            driver = webdriver.Chrome(options=options)
        except Exception as e:
            log.error(f"   [CARSI] Chrome launch failed: {e}")
            return False

        try:
            driver.get(cfg.login_url)
            log.info(f"   [CARSI] Please log in via {cfg.name} institutional access in the browser...")

            # Wait for user to complete login (up to 180 seconds)
            max_wait = 180
            elapsed = 0
            while elapsed < max_wait:
                time.sleep(3)
                elapsed += 3
                try:
                    url = driver.current_url
                except Exception:
                    log.info("   [CARSI] Browser closed by user.")
                    return False

                # Check if we're back on the publisher page (login successful)
                on_publisher = any(d in url for d in cfg.domains)
                on_login_page = any(x in url.lower() for x in ("login", "institutional", "wayf", "saml", "cas", "idp"))

                if on_publisher and not on_login_page:
                    # Save cookies
                    cookies = driver.get_cookies()
                    cookie_file = self._cookie_path(publisher)
                    cookie_data = [
                        {"name": c["name"], "value": c["value"], "domain": c.get("domain", ""), "path": c.get("path", "/")}
                        for c in cookies
                    ]
                    cookie_file.write_text(
                        json.dumps(cookie_data, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    log.info(f"   [CARSI] Login successful! Saved {len(cookie_data)} cookies.")
                    return True

            log.info("   [CARSI] Login timed out.")
            return False

        except Exception as e:
            log.error(f"   [CARSI] Login error: {e}")
            return False
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    def _extract_chrome_cookies(self, publisher: str) -> None:
        """Try to extract cookies from Chrome's cookie database."""
        cfg = self._publisher_configs.get(publisher)
        if not cfg:
            return

        cookie_paths = [
            Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Cookies",
            Path.home() / "AppData/Local/Google/Chrome/User Data/Default/Network/Cookies",
        ]

        for cookie_path in cookie_paths:
            if not cookie_path.exists():
                continue
            try:
                import shutil
                import sqlite3
                tmp_cookie = self._cookie_dir / "chrome_cookies_tmp.db"
                shutil.copy2(cookie_path, tmp_cookie)

                conn = sqlite3.connect(str(tmp_cookie))
                cursor = conn.cursor()

                cookies = []
                for domain in cfg.domains:
                    cursor.execute(
                        "SELECT name, value, host_key, path FROM cookies WHERE host_key LIKE ?",
                        (f"%{domain}%",),
                    )
                    cookies.extend(cursor.fetchall())
                conn.close()
                tmp_cookie.unlink(missing_ok=True)

                if cookies:
                    cookie_file = self._cookie_path(publisher)
                    cookie_data = [
                        {"name": n, "value": v, "domain": h, "path": p}
                        for n, v, h, p in cookies
                    ]
                    cookie_file.write_text(
                        json.dumps(cookie_data, indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    log.info(f"   [CARSI] Extracted {len(cookie_data)} cookies from Chrome")
                    return
            except Exception as e:
                log.warning(f"   [CARSI] Chrome cookie extraction failed: {e}")

    def close(self):
        for sess in self._sessions.values():
            sess.close()
        self._sessions.clear()
