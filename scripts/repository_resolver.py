"""Bounded expansion of repository landing pages, GSA Crossref->figshare, and DiVA Portal."""
import json
import os
import re
from urllib.parse import quote, unquote, urljoin, urlparse, urlunparse
from bs4 import BeautifulSoup
import requests
from supp_finder import (
    Candidate,
    REPO_OR_COMPONENT_DOI_PATTERN,
    check_data_extension,
    configure_session_direct,
    find_all_candidates,
    is_blocked_page,
    normalize_bare_doi_url,
    sanitize_filename,
    supplementary_table_ids,
    validate_title_similarity,
)

FIGSHARE_ARTICLE_PATTERN = re.compile(r"figshare\.com/articles/[^/]+/[^/]+/(\d+)", re.IGNORECASE)
FIGSHARE_ARTICLE_DOI_PATTERN = re.compile(r"10\.6084/m9\.figshare\.(\d+)(?:\.v\d+)?\b", re.IGNORECASE)
FIGSHARE_COLLECTION_PATTERN = re.compile(
    r"(?:figshare\.com/collections/[^/]+/(\d+)|10\.6084/m9\.figshare\.c\.(\d+)(?:\.v\d+)?)",
    re.IGNORECASE,
)
GSA_SUPP_DOI_PATTERN = re.compile(r"10\.1130/[^\s\"'<>?#]*\.s\.\d+", re.IGNORECASE)
_GSA_CODE_PATTERN = re.compile(r"(?:^|[^A-Za-z0-9])([BG]\d{4,6})(?=$|[^A-Za-z0-9])", re.IGNORECASE)
_CROSSREF_CHOOSER_HOST = "chooser.crossref.org"
_SUPP_SUBPAGE_PATH_RE = re.compile(
    r"/(?:supplementary(?:-?materials?|s)?|supporting-?info(?:rmation)?|suppl(?:ement)?|appendices|appendix)(?:/[^/]+)?/?$",
    re.IGNORECASE,
)
DIVA_TARGET_PATTERN = re.compile(
    r"(?:urn:nbn:se:|urn\.kb\.se|diva-portal\.org|avhandlingar\.se|record\.jsf\?pid=diva|diva2:\d+)",
    re.IGNORECASE,
)

_TRANSLATE_MIRROR_HOSTS = {
    "api.figshare.com": "api-figshare-com.translate.goog",
    "ndownloader.figshare.com": "ndownloader-figshare-com.translate.goog",
    "ndownloader.figstatic.com": "ndownloader-figstatic-com.translate.goog",
    "www.diva-portal.org": "www-diva--portal-org.translate.goog",
}

_DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def translate_goog_mirror_url(url: str) -> str:
    """Return a Google Translate proxy URL for hosts frequently WAF-blocked on academic networks, else ''."""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
    except Exception:
        return ""
    mirror = _TRANSLATE_MIRROR_HOSTS.get((parsed.hostname or "").lower())
    if not mirror:
        return ""
    query = parsed.query
    sep = "&" if query else ""
    query += f"{sep}_x_tr_sl=en&_x_tr_tl=zh-CN&_x_tr_hl=en"
    return urlunparse((parsed.scheme or "https", mirror, parsed.path, parsed.params, query, parsed.fragment))


def iter_download_fallback_routes(url: str, proxy=None):
    """Yield ordered alternate (route_url, route_proxy) pairs for WAF-blocked figshare/DiVA hosts."""
    mirror_url = translate_goog_mirror_url(url)
    host = (urlparse(url).hostname or "").lower()
    is_waf_repo_host = bool(mirror_url) or host.endswith(("figshare.com", "figstatic.com", "diva-portal.org"))
    if not is_waf_repo_host or not mirror_url or mirror_url == url:
        return
    yield mirror_url, proxy


def clone_candidate_with_url(cand: Candidate, new_url: str) -> Candidate:
    """Return a shallow clone of Candidate targeting *new_url* for mirror retry."""
    return Candidate(
        url=new_url,
        filename=cand.filename,
        link_text=cand.link_text,
        section=cand.section,
        match_rule=cand.match_rule,
        source_tag=cand.source_tag,
        source_url=cand.source_url,
        extra_info=dict(cand.extra_info or {}),
        covered_table_ids=set(cand.covered_table_ids or set()),
    )


def _iter_route_attempts(url: str, proxy: str = ""):
    """Yield ordered (target_url, proxy_dict_or_None, timeout_s) attempts with direct + translate.goog."""
    mirror = translate_goog_mirror_url(url)
    seen = set()
    candidates = []
    if proxy:
        px = {"http": proxy, "https": proxy}
        candidates.append((url, px, proxy, 15))
        if mirror:
            candidates.append((mirror, px, proxy, 15))
    candidates.append((url, None, "", 12))
    if mirror:
        candidates.append((mirror, None, "", 12))
    for target_url, px_dict, px_key, timeout_s in candidates:
        key = (target_url, px_key)
        if key not in seen:
            seen.add(key)
            yield target_url, px_dict, timeout_s


def _resolve_http_session(session=None, proxy=None):
    """Return (http_session, owned_bool), bypassing env proxies unless *proxy* is explicitly passed."""
    if session is not None:
        return session, False
    return configure_session_direct(requests.Session(), proxy=proxy), True


def _request_with_routes(http, url: str, proxy: str = "", timeout: int = 15,
                         headers=None, allow_redirects: bool = True, verify: bool = True,
                         method: str = "GET", json_body=None):
    """Execute GET/POST along direct/mirror fallback routes; returns last successful Response or None."""
    req_headers = {"User-Agent": _DEFAULT_UA}
    if headers:
        req_headers.update(headers)
    is_post = (method or "GET").upper() == "POST" or json_body is not None
    for target_url, px_dict, route_tmo in _iter_route_attempts(url, proxy=proxy):
        if is_post and target_url != url and "translate.goog" in target_url:
            continue
        tmo = min(timeout, route_tmo) if timeout else route_tmo
        try:
            kwargs = {
                "headers": req_headers,
                "timeout": tmo,
                "allow_redirects": allow_redirects,
                "verify": verify,
            }
            if px_dict:
                kwargs["proxies"] = px_dict
            if is_post:
                resp = http.post(target_url, json=json_body, **kwargs)
            else:
                resp = http.get(target_url, **kwargs)
            if allow_redirects and resp.status_code == 200:
                return resp
            if not allow_redirects and resp.status_code in (200, 301, 302, 303, 307, 308):
                return resp
        except requests.RequestException:
            continue
    return None


def fetch_figshare_article_files(article_id: str, session=None, proxy: str = "",
                                 source_url: str = "", timeout: int = 15):
    """Fetch file list for a figshare article ID via api.figshare.com (+ translate.goog mirror fallback)."""
    if not article_id:
        return "", []
    http, owned = _resolve_http_session(session, proxy=proxy)
    api_url = f"https://api.figshare.com/v2/articles/{article_id}"
    try:
        resp = _request_with_routes(
            http, api_url, proxy=proxy or "",
            timeout=timeout, headers={"Accept": "application/json, */*"},
        )
        if resp is None:
            return "", []
        data = resp.json()
        title = str(data.get("title") or "").strip()
        files = data.get("files") or []
        out = []
        for idx, finfo in enumerate(files, start=1):
            if not isinstance(finfo, dict):
                continue
            dl_url = str(finfo.get("download_url") or "").strip()
            if not dl_url:
                continue
            raw_name = str(finfo.get("name") or f"figshare_{finfo.get('id', idx)}").strip()
            fname = sanitize_filename(raw_name)
            extra = {"structural_supp": True, "figshare_article_id": str(article_id)}
            if finfo.get("computed_md5"):
                extra["computed_md5"] = str(finfo["computed_md5"])
            if finfo.get("size") is not None:
                extra["expected_size"] = finfo["size"]
            out.append(Candidate(
                url=dl_url,
                filename=fname,
                link_text=raw_name,
                section="figshare_api",
                match_rule="figshare_api_v2",
                source_tag="api",
                source_url=source_url or api_url,
                extra_info=extra,
                covered_table_ids=supplementary_table_ids(raw_name, in_supplementary_context=True),
            ))
        return title, out
    except Exception:
        return "", []
    finally:
        if owned:
            http.close()


def _normalize_doi(raw: str) -> str:
    s = unquote((raw or "").strip())
    s = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^doi:\s*", "", s, flags=re.IGNORECASE)
    m = re.search(r"(?:^|[^0-9A-Za-z])(10\.\d{4,9}/[^\s\"'<>?#]+)", s)
    if not m:
        return ""
    doi = m.group(1).strip().rstrip(".,;)")
    doi = re.sub(r"/(?:full|abstract|html|epdf|pdfdirect|pdf|suppl|meta)$", "", doi, flags=re.IGNORECASE)
    return doi.lower()


def is_crossref_chooser_url(url: str) -> bool:
    """True when URL points to Crossref's multi-publisher resolver chooser page."""
    if not url:
        return False
    try:
        return (urlparse(url).hostname or "").lower() == _CROSSREF_CHOOSER_HOST
    except Exception:
        return False


def extract_crossref_chooser_urls(html_str: str = "", doi: str = "", session=None,
                                  proxy: str = "",
                                  timeout: int = 5, crossref_email: str = "") -> list[str]:
    """Extract publisher landing URLs from a chooser.crossref.org HTML page and/or Crossref works API."""
    urls: list[str] = []
    seen: set[str] = set()

    def _add(raw_u: str):
        u = (raw_u or "").strip()
        if not u or not u.lower().startswith(("http://", "https://")):
            return
        host = (urlparse(u).hostname or "").lower()
        if host in {"crossref.org", "www.crossref.org", "chooser.crossref.org", "doi.org", "dx.doi.org"}:
            return
        if u not in seen:
            seen.add(u)
            urls.append(u)

    if html_str:
        try:
            soup = BeautifulSoup(html_str, "html.parser")
            lines = soup.select(".resource-line a[href], .resource-list a[href]")
            if not lines:
                html_low = html_str.lower()
                is_publisher_page = (
                    len(html_str) > 25000
                    or any(marker in html_low for marker in (
                        "<article",
                        "<footer",
                        "article-body",
                        "article__body",
                        "site-footer",
                        "cookie-banner",
                        "onetrust",
                    ))
                )
                if not is_publisher_page or "chooser.crossref.org" in html_low:
                    lines = soup.find_all("a", href=True)
            for a in lines:
                _add(a.get("href", ""))
        except Exception:
            pass

    doi_clean = _normalize_doi(doi)
    if not doi_clean and not urls and html_str:
        m_doi = re.search(r"(10\.\d{4,9}/[^\s\"'<>?#]+)", html_str, re.IGNORECASE)
        if m_doi:
            doi_clean = _normalize_doi(m_doi.group(1))

    is_synthetic_test_doi = bool(
        doi_clean
        and (
            doi_clean.startswith("10.1234/")
            or doi_clean.startswith("10.1000/")
            or any(tag in doi_clean.lower() for tag in (".fake", ".example", ".test", ".invalid"))
        )
    )
    if doi_clean and (doi or not urls) and not (is_synthetic_test_doi and session is None):
        http, owned = _resolve_http_session(session, proxy=proxy)
        cr_headers = {"Accept": "application/json"}
        if crossref_email:
            cr_headers["User-Agent"] = f"journal-supp-downloader/1.0 (mailto:{crossref_email})"
        try:
            resp = _request_with_routes(
                http,
                f"https://api.crossref.org/works/{quote(doi_clean, safe='/')}",
                proxy=proxy or "",
                timeout=timeout,
                headers=cr_headers,
            )
            if resp is not None:
                msg = (resp.json() or {}).get("message") or {}
                res = msg.get("resource") or {}
                primary = (res.get("primary") or {}).get("URL")
                if primary:
                    _add(str(primary))
                for sec in res.get("secondary") or []:
                    if isinstance(sec, dict) and sec.get("URL"):
                        _add(str(sec["URL"]))
                if msg.get("URL"):
                    _add(str(msg["URL"]))
        except Exception:
            pass
        finally:
            if owned:
                http.close()
    return urls


_IGNORED_CHOOSER_EXCLUDE_HOSTS = frozenset({
    "doi.org",
    "dx.doi.org",
    "chooser.crossref.org",
    "crossref.org",
    "www.crossref.org",
})


def get_alternate_chooser_urls(
    doi: str = "",
    html_str: str = "",
    exclude_urls=None,
    seed_urls=None,
    session=None,
    proxy: str = "",
    crossref_email: str = "",
) -> list[str]:
    """Return ordered Crossref chooser publisher URLs whose host has not been tried yet."""
    seen_urls = {_norm_landing_url(u) for u in (exclude_urls or []) if u}
    seen_hosts = set()
    for u in (exclude_urls or []):
        h = (urlparse(u or "").hostname or "").lower()
        if h and h not in _IGNORED_CHOOSER_EXCLUDE_HOSTS:
            seen_hosts.add(h)

    pool: list[str] = []
    for u in (seed_urls or []):
        if u and u not in pool:
            pool.append(u)
    if doi or html_str:
        for u in extract_crossref_chooser_urls(
            html_str=html_str,
            doi=doi,
            session=session,
            proxy=proxy,
            crossref_email=crossref_email,
        ):
            if u not in pool:
                pool.append(u)

    alts: list[str] = []
    for u in pool:
        h = (urlparse(u).hostname or "").lower()
        if _norm_landing_url(u) in seen_urls or (h and h in seen_hosts):
            continue
        if h:
            seen_hosts.add(h)
        alts.append(u)
    return alts


def fetch_figshare_collection_files(collection_id: str, session=None, proxy: str = "",
                                     source_url: str = "", timeout: int = 20):
    """Fetch all files across articles inside a Figshare collection ID."""
    if not collection_id:
        return "", []
    http, owned = _resolve_http_session(session, proxy=proxy)
    col_url = f"https://api.figshare.com/v2/collections/{collection_id}"
    arts_url = f"{col_url}/articles?page_size=100"
    try:
        col_title = ""
        resp_meta = _request_with_routes(
            http, col_url, proxy=proxy or "",
            timeout=timeout, headers={"Accept": "application/json, */*"},
        )
        if resp_meta is not None:
            try:
                col_title = str((resp_meta.json() or {}).get("title") or "").strip()
            except Exception:
                pass
        resp_arts = _request_with_routes(
            http, arts_url, proxy=proxy or "",
            timeout=timeout, headers={"Accept": "application/json, */*"},
        )
        if resp_arts is None:
            return col_title, []
        articles = resp_arts.json() or []
        if not isinstance(articles, list):
            return col_title, []
        out = []
        seen_urls = set()
        for art in articles[:25]:
            aid = str((art or {}).get("id") or "").strip()
            if not aid:
                continue
            _, fcands = fetch_figshare_article_files(
                aid,
                session=http,
                proxy=proxy or "",
                source_url=source_url or col_url,
                timeout=timeout,
            )
            for c in fcands:
                if c.url not in seen_urls:
                    seen_urls.add(c.url)
                    c.extra_info["figshare_collection_id"] = str(collection_id)
                    out.append(c)
        return col_title, out
    except Exception:
        return "", []
    finally:
        if owned:
            http.close()


def _figshare_item_mentions_doi(detail: dict, doi_norm: str) -> bool:
    if not isinstance(detail, dict) or not doi_norm:
        return False
    for ref in detail.get("references") or []:
        if _normalize_doi(str(ref)) == doi_norm:
            return True
    for rel in detail.get("related_materials") or []:
        if isinstance(rel, dict):
            ident = _normalize_doi(str(rel.get("identifier") or ""))
            if ident == doi_norm:
                return True
    for key in ("citation", "description"):
        val = str(detail.get(key) or "").lower()
        if doi_norm in val:
            return True
    return False


def discover_figshare_by_doi(doi: str, session=None, proxy: str = "",
                             timeout: int = 20):
    """Search Figshare collections & articles referencing an article DOI and return verified files."""
    doi_norm = _normalize_doi(doi)
    if not doi_norm:
        return [], ""
    http, owned = _resolve_http_session(session, proxy=proxy)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        # 1. Check Figshare collections referencing this DOI (e.g. Lyell/GeolSoc 10.6084/m9.figshare.c.*)
        resp_cs = _request_with_routes(
            http,
            "https://api.figshare.com/v2/collections/search",
            proxy=proxy or "",
            timeout=timeout,
            headers=headers,
            method="POST",
            json_body={"search_for": doi_norm, "page_size": 5},
        )
        if resp_cs is not None:
            try:
                cols = resp_cs.json() or []
            except Exception:
                cols = []
            for col in (cols if isinstance(cols, list) else []):
                cid = str((col or {}).get("id") or "").strip()
                if not cid:
                    continue
                resp_det = _request_with_routes(
                    http,
                    f"https://api.figshare.com/v2/collections/{cid}",
                    proxy=proxy or "",
                    timeout=timeout,
                    headers={"Accept": "application/json"},
                )
                if resp_det is None:
                    continue
                try:
                    det = resp_det.json() or {}
                except Exception:
                    continue
                if _figshare_item_mentions_doi(det, doi_norm):
                    title, cands = fetch_figshare_collection_files(
                        cid,
                        session=http,
                        proxy=proxy or "",
                        source_url=str(det.get("url_public_html") or col.get("url") or f"https://api.figshare.com/v2/collections/{cid}"),
                        timeout=timeout,
                    )
                    if cands:
                        return cands, title

        # 2. Check standalone Figshare articles referencing this DOI
        resp_as = _request_with_routes(
            http,
            "https://api.figshare.com/v2/articles/search",
            proxy=proxy or "",
            timeout=timeout,
            headers=headers,
            method="POST",
            json_body={"search_for": doi_norm, "page_size": 5},
        )
        if resp_as is not None:
            try:
                arts = resp_as.json() or []
            except Exception:
                arts = []
            for art in (arts if isinstance(arts, list) else []):
                aid = str((art or {}).get("id") or "").strip()
                if not aid:
                    continue
                resp_det = _request_with_routes(
                    http,
                    f"https://api.figshare.com/v2/articles/{aid}",
                    proxy=proxy or "",
                    timeout=timeout,
                    headers={"Accept": "application/json"},
                )
                if resp_det is None:
                    continue
                try:
                    det = resp_det.json() or {}
                except Exception:
                    continue
                if _figshare_item_mentions_doi(det, doi_norm):
                    title, cands = fetch_figshare_article_files(
                        aid,
                        session=http,
                        proxy=proxy or "",
                        source_url=str(det.get("url_public_html") or art.get("url") or f"https://api.figshare.com/v2/articles/{aid}"),
                        timeout=timeout,
                    )
                    if cands:
                        return cands, title
        return [], ""
    except Exception:
        return [], ""
    finally:
        if owned:
            http.close()


def is_gsa_or_gsw_target(url_or_doi: str) -> bool:
    """True when the target belongs to GSA (10.1130/) or GeoScienceWorld."""
    low = (url_or_doi or "").lower()
    return ("10.1130/" in low) or ("geoscienceworld.org" in low)


def is_diva_target(url_or_id: str) -> bool:
    """True when the target points to Swedish DiVA Portal / urn:nbn:se / diva2."""
    return bool(url_or_id and DIVA_TARGET_PATTERN.search(url_or_id))


def discover_gsa_figshare_candidates(url_or_doi: str, session=None, proxy: str = "",
                                     crossref_email: str = ""):
    """Resolve GSA/GeoScienceWorld article -> Crossref relations -> 10.1130/*.s.* -> figshare API files."""
    http, owned = _resolve_http_session(session, proxy=proxy)
    cr_headers = {"Accept": "application/json"}
    if crossref_email:
        cr_headers["User-Agent"] = f"journal-supp-downloader/1.0 (mailto:{crossref_email})"
    try:
        m_doi = re.search(r"(10\.1130/[^\s?#\"'<>]+)", url_or_doi or "", re.IGNORECASE)
        doi = m_doi.group(1).rstrip(".,;)") if m_doi else ""
        expected_slug = ""
        if not doi and "geoscienceworld.org" in (url_or_doi or "").lower():
            expected_slug = urlparse(url_or_doi).path.rstrip("/").split("/")[-1].replace("-", " ")
            if expected_slug:
                resp = _request_with_routes(
                    http,
                    f"https://api.crossref.org/works?query.bibliographic={quote(expected_slug)}&rows=5",
                    proxy=proxy or "",
                    timeout=20, headers=cr_headers,
                )
                if resp is not None:
                    try:
                        items = resp.json().get("message", {}).get("items") or []
                        for item in items:
                            item_doi = str(item.get("DOI") or "")
                            item_title = str((item.get("title") or [""])[0] or "")
                            if (
                                item_doi.lower().startswith("10.1130/")
                                and not GSA_SUPP_DOI_PATTERN.search(item_doi)
                                and item.get("type") == "journal-article"
                                and validate_title_similarity(expected_slug, item_title, min_similarity=0.35)
                            ):
                                doi = item_doi
                                break
                        if not doi:
                            for item in items:
                                item_doi = str(item.get("DOI") or "")
                                item_title = str((item.get("title") or [""])[0] or "")
                                if item_doi.lower().startswith("10.1130/") and validate_title_similarity(expected_slug, item_title, min_similarity=0.35):
                                    doi = item_doi
                                    break
                    except Exception:
                        pass
        if not doi:
            return [], ""

        title = ""
        supp_dois = []
        if GSA_SUPP_DOI_PATTERN.search(doi):
            supp_dois = [doi]
            resp = _request_with_routes(
                http, f"https://api.crossref.org/works/{quote(doi, safe='/')}",
                proxy=proxy or "", timeout=20, headers=cr_headers,
            )
            if resp is not None:
                try:
                    raw_title = str((resp.json().get("message", {}).get("title") or [""])[0] or "")
                    title = re.sub(r"^supplement\w*\s*material\s*:\s*", "", raw_title, flags=re.IGNORECASE).strip()
                except Exception:
                    pass
        else:
            resp = _request_with_routes(
                http, f"https://api.crossref.org/works/{quote(doi, safe='/')}",
                proxy=proxy or "", timeout=20, headers=cr_headers,
            )
            if resp is not None:
                try:
                    msg = resp.json().get("message", {})
                    title = str((msg.get("title") or [""])[0] or "").strip()
                    for rel_list in (msg.get("relation") or {}).values():
                        if not isinstance(rel_list, list):
                            continue
                        for rel in rel_list:
                            rid = str((rel or {}).get("id") or "").strip()
                            if GSA_SUPP_DOI_PATTERN.search(rid) and rid not in supp_dois:
                                supp_dois.append(rid)
                except Exception:
                    pass
            if not supp_dois and title:
                q = quote(f"Supplemental Material: {title}")
                resp_q = _request_with_routes(
                    http, f"https://api.crossref.org/works?query.bibliographic={q}&rows=5",
                    proxy=proxy or "", timeout=20, headers=cr_headers,
                )
                if resp_q is not None:
                    try:
                        doi_norm = _normalize_doi(doi)
                        for item in resp_q.json().get("message", {}).get("items") or []:
                            sdoi = str(item.get("DOI") or "").strip()
                            raw_stitle = str((item.get("title") or [""])[0] or "").strip()
                            stitle = raw_stitle.lower()
                            if not (GSA_SUPP_DOI_PATTERN.search(sdoi) and "supplement" in stitle):
                                continue
                            clean_stitle = re.sub(
                                r"^supplement\w*\s*material\s*:\s*", "", raw_stitle, flags=re.IGNORECASE
                            ).strip()
                            if not validate_title_similarity(title, clean_stitle, min_similarity=0.75):
                                continue
                            rels = item.get("relation") or {}
                            parent_rel_dois = []
                            for rel_key in ("is-supplement-to", "is-part-of"):
                                for rel in (rels.get(rel_key) or []):
                                    rid = _normalize_doi(str((rel or {}).get("id") or ""))
                                    if rid:
                                        parent_rel_dois.append(rid)
                            if parent_rel_dois and doi_norm not in parent_rel_dois:
                                continue
                            supp_dois.append(sdoi)
                            break
                    except Exception:
                        pass

        m_parent_code = None if GSA_SUPP_DOI_PATTERN.search(doi) else _GSA_CODE_PATTERN.search(doi)
        parent_code = m_parent_code.group(1).upper() if m_parent_code else ""
        all_cands = []
        seen_urls = set()
        for supp_doi in supp_dois:
            loc_resp = _request_with_routes(
                http, f"https://doi.org/{supp_doi}",
                proxy=proxy or "",
                timeout=20, allow_redirects=False,
            )
            loc = ""
            if loc_resp is not None:
                for k, v in loc_resp.headers.items():
                    if k.lower() == "location":
                        loc = v
                        break
            m_fid = FIGSHARE_ARTICLE_PATTERN.search(loc or "")
            if not m_fid:
                continue
            ftitle, fcands = fetch_figshare_article_files(
                m_fid.group(1),
                session=http,
                proxy=proxy or "",
                source_url=loc or f"https://doi.org/{supp_doi}",
            )
            if not title and ftitle:
                title = ftitle
            for c in fcands:
                if parent_code:
                    cand_codes = {
                        m.upper()
                        for m in _GSA_CODE_PATTERN.findall(f"{c.filename} {c.link_text} {ftitle}")
                    }
                    if cand_codes and parent_code not in cand_codes:
                        continue
                if c.url not in seen_urls:
                    seen_urls.add(c.url)
                    all_cands.append(c)
        return all_cands, title
    except Exception:
        return [], ""
    finally:
        if owned:
            http.close()


def discover_diva_candidates(url_or_id: str, session=None, proxy: str = ""):
    """Discover DiVA Portal thesis/repository PDFs via OAI-PMH (Anubis bypass) or smash record fallback."""
    http, owned = _resolve_http_session(session, proxy=proxy)
    try:
        org = ""
        pid = ""
        nbn_id = ""
        m_urn = re.search(r"urn:nbn:se:([\w-]+):diva-(\d+)", url_or_id or "", re.IGNORECASE)
        if m_urn:
            org, nbn_id = m_urn.group(1).lower(), m_urn.group(2)
            r_loc = _request_with_routes(
                http, f"http://urn.kb.se/resolve?urn=urn:nbn:se:{org}:diva-{nbn_id}",
                proxy=proxy or "", timeout=25, allow_redirects=False, verify=False,
            )
            if r_loc is not None:
                loc = r_loc.headers.get("Location") or r_loc.headers.get("location") or ""
                m_pid = re.search(r"(diva2:\d+)", loc, re.IGNORECASE)
                if m_pid:
                    pid = m_pid.group(1)
        else:
            m_diva = re.search(r"(diva2:\d+)", url_or_id or "", re.IGNORECASE)
            if m_diva:
                pid = m_diva.group(1)
                host = (urlparse(url_or_id).hostname or "").lower() if "://" in (url_or_id or "") else ""
                if host.endswith("diva-portal.org") and host != "www.diva-portal.org":
                    org = host.split(".")[0]

        if not pid:
            return [], ""

        title = ""
        if org and nbn_id:
            oai_url = (
                f"https://{org}.diva-portal.org/dice/oai?verb=GetRecord&metadataPrefix=oai_dc"
                f"&identifier={quote(f'oai:DiVA.org:{org}-{nbn_id}')}"
            )
            r_oai = _request_with_routes(
                http, oai_url, proxy=proxy or "", timeout=25, verify=False,
            )
            if r_oai is not None and "<error" not in (r_oai.text or "")[:400].lower():
                m_t = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", r_oai.text or "", re.DOTALL | re.IGNORECASE)
                if m_t:
                    title = re.sub(r"\s+", " ", m_t.group(1)).strip()

        base = f"https://{org}.diva-portal.org" if org else "https://www.diva-portal.org"
        cands = []
        seen = set()

        # 1. Try direct smash FULLTEXT01..07 endpoints when org/pid are known (OAI route avoids Anubis wall)
        if org or "urn:nbn:" in (url_or_id or "").lower():
            for n in range(1, 8):
                fname = f"FULLTEXT{n:02d}.pdf"
                cand_url = f"{base}/smash/get/{pid}/{fname}"
                try:
                    probe = None
                    for target_u, px_dict, tmo in _iter_route_attempts(cand_url, proxy=proxy or ""):
                        try:
                            kw = {"headers": {"User-Agent": _DEFAULT_UA}, "timeout": tmo, "stream": True, "verify": False}
                            if px_dict:
                                kw["proxies"] = px_dict
                            probe = http.get(target_u, **kw)
                            head_bytes = next(probe.iter_content(8), b"") if probe.status_code == 200 else b""
                            if probe.status_code == 200 and head_bytes.startswith(b"%PDF-"):
                                if cand_url not in seen:
                                    seen.add(cand_url)
                                    cands.append(Candidate(
                                        url=cand_url,
                                        filename=fname,
                                        link_text=fname,
                                        section="diva_repository",
                                        match_rule="diva_oai_fulltext",
                                        source_tag="api",
                                        source_url=url_or_id,
                                        extra_info={"structural_supp": True},
                                    ))
                                break
                            elif probe.status_code == 404:
                                break
                        except requests.RequestException:
                            continue
                        finally:
                            if probe is not None:
                                probe.close()
                    if n == 1 and not cands:
                        break
                    if cand_url not in seen and n > 1:
                        break
                except Exception:
                    break

        # 2. Fallback: scrape record page directly or via translate.goog mirror
        if not cands:
            rec_url = f"https://www.diva-portal.org/smash/record.jsf?pid={pid}"
            r_rec = _request_with_routes(
                http, rec_url, proxy=proxy or "", timeout=25, verify=False,
            )
            if r_rec is not None and r_rec.text:
                if not title:
                    m_h1 = re.search(r"<h1[^>]*>(.*?)</h1>", r_rec.text, re.DOTALL | re.IGNORECASE)
                    if m_h1:
                        title = re.sub(r"<[^>]+>", "", m_h1.group(1)).strip()
                for rel in re.findall(r'href="([^"]*smash/get/diva2:\d+/[^"]*)"', r_rec.text, re.IGNORECASE):
                    full_u = urljoin("https://www.diva-portal.org", rel)
                    if full_u in seen:
                        continue
                    seen.add(full_u)
                    fname = sanitize_filename(os.path.basename(urlparse(full_u).path) or f"{pid}.pdf")
                    cands.append(Candidate(
                        url=full_u,
                        filename=fname,
                        link_text=fname,
                        section="diva_repository",
                        match_rule="diva_record_link",
                        source_tag="a",
                        source_url=rec_url,
                        extra_info={"structural_supp": True},
                    ))
        return cands, title
    except Exception:
        return [], ""
    finally:
        if owned:
            http.close()


def is_supplementary_subpage_url(url: str) -> bool:
    """True when URL looks like an HTML supplementary-landing subpage (e.g. .../supplementary)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    path = parsed.path or ""
    if check_data_extension(path) or path.lower().endswith(".pdf"):
        return False
    return bool(_SUPP_SUBPAGE_PATH_RE.search(path))


def is_repository_landing(url):
    parsed = urlparse(url)
    if check_data_extension(parsed.path) or parsed.path.lower().endswith('.pdf'):
        return False
    host = (parsed.hostname or '').lower()
    if host in {'ndownloader.figshare.com', 'ndownloader.figstatic.com'}:
        return False
    return (
        bool(REPO_OR_COMPONENT_DOI_PATTERN.search(url))
        or bool(FIGSHARE_COLLECTION_PATTERN.search(url))
        or bool(FIGSHARE_ARTICLE_DOI_PATTERN.search(url))
        or bool(FIGSHARE_ARTICLE_PATTERN.search(url))
        or is_supplementary_subpage_url(url)
        or (
            (
                host in {
                    'zenodo.org', 'figshare.com', 'osf.io', 'datadryad.org',
                    'data.mendeley.com', 'doi.pangaea.de', 'pangaea.de',
                    'www.pangaea.de', 'www.scidb.cn', 'scidb.cn',
                    'www.sciencebase.gov', 'sciencebase.gov',
                }
                or host.endswith('.figshare.com')
            )
            and not any(part in parsed.path for part in ('/files/', '/download', '/ndownloader/', '/file/get/'))
        )
    )


def _norm_landing_url(url: str) -> str:
    try:
        p = urlparse((url or "").strip())
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", p.query, ""))
    except Exception:
        return (url or "").strip()


_SCIENCEBASE_ITEM_RE = re.compile(
    r"https?://(?:www\.)?sciencebase\.gov/catalog/item/([a-f0-9]{16,32})",
    re.IGNORECASE,
)
_FIGSHARE_ITEM_URL_RE = re.compile(
    r"https?://[^/\s\"'<>]+\.?figshare\.com/articles/[^/\s\"'<>]+/[^/\s\"'<>]+/(\d+)",
    re.IGNORECASE,
)


def _extract_sciencebase_children_from_json_text(raw_text: str, item_url: str, source_url: str) -> list[Candidate]:
    """Parse ScienceBase item JSON payload (`?format=json`) into downloadable file candidates."""
    if not raw_text:
        return []
    m_json = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not m_json:
        return []
    try:
        data = json.loads(m_json.group(0))
    except Exception:
        return []
    files = data.get("files") or []
    out: list[Candidate] = []
    for idx, f in enumerate(files, start=1):
        if not isinstance(f, dict):
            continue
        dl_url = str(f.get("url") or "").strip()
        if not dl_url:
            continue
        fname = sanitize_filename(str(f.get("name") or f"sciencebase_file_{idx}").strip())
        out.append(Candidate(
            url=dl_url,
            filename=fname,
            link_text=fname,
            section="sciencebase_repository",
            match_rule="sciencebase_json_file",
            source_tag="api",
            source_url=source_url or item_url,
            extra_info={"structural_supp": True},
        ))
    return out


def _extract_figshare_children_from_html(html_str: str, page_url: str, source_url: str):
    """Extract Figshare direct ``ndownloader/files/<id>`` files or collection item URLs from rendered HTML."""
    if not html_str:
        return [], []
    decoded = html_str.replace("\\u002F", "/").replace("\\/", "/")

    # 1. Direct file metadata inside __APOLLO_STATE__ or ndownloader links
    file_cands: list[Candidate] = []
    seen_files: set[str] = set()
    m_slug = re.search(r"/articles/[^/]+/([^/?#]+)/\d+", page_url or "")
    slug_base = m_slug.group(1) if m_slug else ""
    file_block_re = re.compile(
        r'"File:(\d+)"\s*:\s*\{[^{}]*"downloadUrl"\s*:\s*"(https?://[^"]+/ndownloader/files/\1[^"]*)"[^{}]*\}',
        re.IGNORECASE,
    )
    for m_fb in file_block_re.finditer(decoded):
        blk = m_fb.group(0)
        fid = m_fb.group(1)
        dl_url = m_fb.group(2)
        if dl_url in seen_files:
            continue
        seen_files.add(dl_url)
        m_name = re.search(r'"name"\s*:\s*"([^"]+)"', blk)
        m_ext = re.search(r'"extension"\s*:\s*"([^"]+)"', blk)
        raw_name = (m_name.group(1) if m_name else "").strip()
        ext = (m_ext.group(1) if m_ext else "").strip().lstrip(".")
        if not raw_name:
            prefix = slug_base[:80] if slug_base else f"figshare_file_{fid}"
            raw_name = f"{prefix}_{fid}.{ext}" if ext else f"{prefix}_{fid}"
        elif ext and "." not in os.path.basename(raw_name):
            raw_name = f"{raw_name}.{ext}"
        fname = sanitize_filename(raw_name)
        file_cands.append(Candidate(
            url=dl_url,
            filename=fname,
            link_text=fname,
            section="figshare_repository",
            match_rule="figshare_html_apollo_file",
            source_tag="a",
            source_url=page_url or source_url,
            extra_info={"structural_supp": True},
        ))

    if not file_cands:
        for fid_url in re.findall(r"https?://[^\s\"'<>\\]+/ndownloader/files/(\d+)", decoded):
            full_dl = urljoin(page_url or source_url, f"/ndownloader/files/{fid_url}")
            if full_dl in seen_files:
                continue
            seen_files.add(full_dl)
            fname = sanitize_filename(f"{slug_base[:80]}_{fid_url}" if slug_base else f"figshare_file_{fid_url}")
            file_cands.append(Candidate(
                url=full_dl,
                filename=fname,
                link_text=fname,
                section="figshare_repository",
                match_rule="figshare_html_ndownloader",
                source_tag="a",
                source_url=page_url or source_url,
                extra_info={"structural_supp": True},
            ))
    if file_cands:
        return file_cands, []

    # 2. Collection page: collect linked dataset/item pages
    item_cands: list[Candidate] = []
    seen_items: set[str] = set()
    for m_item in _FIGSHARE_ITEM_URL_RE.finditer(decoded):
        clean_item = m_item.group(0).split("?", 1)[0]
        if _norm_landing_url(clean_item) == _norm_landing_url(page_url):
            continue
        if clean_item in seen_items:
            continue
        seen_items.add(clean_item)
        item_cands.append(Candidate(
            url=clean_item,
            filename=f"figshare_item_{m_item.group(1)}",
            link_text=f"figshare item {m_item.group(1)}",
            section="figshare_repository",
            match_rule="figshare_collection_item_html",
            source_tag="a",
            source_url=page_url or source_url,
            extra_info={"structural_supp": True},
        ))
        if len(item_cands) >= 20:
            break
    return [], item_cands


def expand_repository_candidates(candidates, session=None, proxy=None, browser_fetch=None):
    """Keep unresolved originals, expand figshare/ScienceBase/supplementary subpages, and prevent cyclic crawls."""
    http, owned = _resolve_http_session(session, proxy=proxy)
    visited = set()

    def _attach_parent(parent_cand, children_list):
        parent = parent_cand.to_dict()
        parent['required_urls'] = [child.url for child in children_list]
        for child in children_list:
            child.extra_info['repository_parent'] = parent_cand.url
            child.extra_info.setdefault('parent_evidence', []).append(parent)
            child.extra_info['parent_evidence'].extend(parent_cand.extra_info.get('parent_evidence', []))
        return children_list

    figshare_api_unavailable = {"val": False}

    def _try_figshare_expand(url_or_html: str, source_url: str, parent_cand):
        if figshare_api_unavailable["val"]:
            return None
        m_fcol = FIGSHARE_COLLECTION_PATTERN.search(url_or_html or "")
        if m_fcol:
            col_id = m_fcol.group(1) or m_fcol.group(2)
            _, api_children = fetch_figshare_collection_files(
                col_id, session=http, proxy=proxy or "",
                source_url=source_url, timeout=6,
            )
            children = [c for c in api_children if c.url not in visited]
            if children:
                return _attach_parent(parent_cand, children)
            figshare_api_unavailable["val"] = True
            return None
        m_fid = FIGSHARE_ARTICLE_PATTERN.search(url_or_html or "") or FIGSHARE_ARTICLE_DOI_PATTERN.search(url_or_html or "")
        if m_fid:
            _, api_children = fetch_figshare_article_files(
                m_fid.group(1), session=http, proxy=proxy or "",
                source_url=source_url, timeout=6,
            )
            children = [c for c in api_children if c.url not in visited]
            if children:
                return _attach_parent(parent_cand, children)
            figshare_api_unavailable["val"] = True
        return None

    def _is_browser_expandable_repo(target_url: str, resolved_url: str = "") -> bool:
        for u in (resolved_url, target_url):
            if not u:
                continue
            ul = u.lower()
            h = (urlparse(ul).hostname or "").lower()
            if (
                h.endswith(("figshare.com", "sciencebase.gov", "zenodo.org", "datadryad.org", "osf.io", "pangaea.de", "scidb.cn"))
                or h == "data.mendeley.com"
                or any(p in ul for p in ("10.6084/", "10.5066/", "10.5281/", "10.5061/", "10.17632/", "10.17605/"))
            ):
                return True
        return False

    def _try_browser_html(target_url: str, resolved_url: str = ""):
        if browser_fetch is None or not _is_browser_expandable_repo(target_url, resolved_url):
            return "", resolved_url or target_url
        try:
            res = browser_fetch(target_url)
            if isinstance(res, tuple):
                b_html = res[0] or ""
                b_final = (res[2] if len(res) > 2 else None) or target_url
                return b_html, b_final
        except Exception:
            pass
        return "", target_url

    def _try_sciencebase_expand(landing_url: str, parent_cand):
        m_sb = _SCIENCEBASE_ITEM_RE.search(landing_url or "")
        if not m_sb:
            return None
        json_url = f"https://www.sciencebase.gov/catalog/item/{m_sb.group(1)}?format=json"
        sb_text = ""
        try:
            get_kw = {"timeout": 15, "headers": {"User-Agent": _DEFAULT_UA, "Accept": "application/json"}}
            if proxy:
                get_kw["proxies"] = {"http": proxy, "https": proxy}
            r_sb = http.get(json_url, **get_kw)
            if r_sb.status_code == 200 and not is_blocked_page(r_sb.text or ""):
                sb_text = r_sb.text or ""
        except Exception:
            sb_text = ""
        if not sb_text and browser_fetch is not None:
            sb_text, _ = _try_browser_html(json_url)
        sb_cands = _extract_sciencebase_children_from_json_text(sb_text, landing_url, parent_cand.source_url)
        sb_cands = [c for c in sb_cands if c.url not in visited]
        if sb_cands:
            return _attach_parent(parent_cand, sb_cands)
        return None

    def expand(candidate, depth):
        norm_url = normalize_bare_doi_url(candidate.url)
        if norm_url != candidate.url:
            candidate = clone_candidate_with_url(candidate, norm_url)
        if depth >= 2 or len(visited) >= 16 or candidate.url in visited or not is_repository_landing(candidate.url):
            return [candidate]
        visited.add(candidate.url)

        direct_figshare = _try_figshare_expand(candidate.url, candidate.url, candidate)
        if direct_figshare:
            return direct_figshare
        direct_sb = _try_sciencebase_expand(candidate.url, candidate)
        if direct_sb:
            return direct_sb

        response = None
        html_str = ""
        landing_url = candidate.url
        try:
            get_kw = {
                'timeout': 25,
                'stream': True,
                'headers': {
                    'User-Agent': _DEFAULT_UA,
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Referer': candidate.source_url or candidate.url,
                },
            }
            if proxy:
                get_kw['proxies'] = {'http': proxy, 'https': proxy}
            response = http.get(candidate.url, **get_kw)
            response.raise_for_status()
            landing_url = response.url or candidate.url
            if 'html' not in response.headers.get('Content-Type', '').lower():
                return [candidate]
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError('Repository page exceeds 2 MiB discovery limit')
            html_str = bytes(body).decode(response.encoding or 'utf-8', errors='replace')
            if is_blocked_page(html_str):
                html_str = ""
        except (requests.RequestException, ValueError) as exc:
            if response is not None and getattr(response, "url", None):
                landing_url = response.url
            if browser_fetch is None:
                print(f'  [Repository] Unresolved {candidate.url}: {exc}')
                return [candidate]
        finally:
            if response is not None:
                response.close()

        sb_from_landing = _try_sciencebase_expand(landing_url, candidate)
        if sb_from_landing:
            return sb_from_landing

        if not html_str and browser_fetch is not None:
            html_str, landing_url = _try_browser_html(candidate.url, resolved_url=landing_url)
            sb_after_browser = _try_sciencebase_expand(landing_url, candidate)
            if sb_after_browser:
                return sb_after_browser

        if not html_str or is_blocked_page(html_str):
            print(f'  [Repository] Blocked/challenge page at {candidate.url}')
            return [candidate]

        fig_files, fig_items = _extract_figshare_children_from_html(html_str, landing_url, candidate.url)
        if fig_files:
            fig_files = [c for c in fig_files if c.url not in visited]
            if fig_files:
                return _attach_parent(candidate, fig_files)
        if fig_items:
            fig_result = []
            for item in fig_items:
                if len(visited) >= 24:
                    break
                item.extra_info['repository_parent'] = candidate.url
                fig_result.extend(expand(item, depth + 1))
            if fig_result:
                return _attach_parent(candidate, fig_result)

        landing_figshare = (
            _try_figshare_expand(landing_url, landing_url, candidate)
            or _try_figshare_expand(html_str[:8000], landing_url, candidate)
        )
        if landing_figshare:
            return landing_figshare

        found = find_all_candidates(html_str, landing_url)
        self_norms = {_norm_landing_url(candidate.url), _norm_landing_url(landing_url)}
        children = [
            c for c in found.candidates
            if c.url not in visited and _norm_landing_url(c.url) not in self_norms
        ]
        if not children:
            print(f'  [Repository] No file links resolved: {candidate.url}')
            return [candidate]
        result = []
        for child in children:
            child.extra_info['repository_parent'] = candidate.url
            result.extend(expand(child, depth + 1))
        return _attach_parent(candidate, result)
    try:
        result = {}
        for candidate in candidates:
            for resolved in expand(candidate, 0):
                if resolved.url in result:
                    result[resolved.url].extra_info.setdefault('parent_evidence', []).extend(resolved.extra_info.get('parent_evidence', []))
                else:
                    result[resolved.url] = resolved
        return list(result.values())
    finally:
        if owned:
            http.close()
