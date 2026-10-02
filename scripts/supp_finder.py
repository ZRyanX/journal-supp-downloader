"""
Unified Supplementary Material & Table Candidate Finder
========================================================
Shared candidate extraction, pattern matching, diagnostics, and file validation
for both journal_downloader.py (lightweight) and scansci_supp_downloader.py (integrated).
"""

import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from bs4 import BeautifulSoup


# ── Generic Data File Extensions ─────────────────────────────────────────────
# Note: .pdf and .html are handled contextually (tables/supplementary sections)
# rather than as generic blind extensions to prevent grabbing main article PDFs/pages.
DATA_EXTENSIONS = {
    ".xlsx", ".xls", ".csv", ".tsv", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".txt", ".json", ".xml", ".r", ".py", ".ipynb", ".m", ".nb",
    ".cif", ".pdb", ".mol", ".sdf", ".xyz", ".fasta", ".fa", ".gb",
    ".nii", ".nii.gz", ".dcm", ".h5", ".hdf5", ".mat", ".pkl", ".rda", ".sav",
    ".docx", ".doc", ".pptx", ".ppt",
}

# ── Publisher Patterns ───────────────────────────────────────────────────────
MMC_PATTERN = re.compile(r"mmc\d+", re.IGNORECASE)

# Keywords indicating supplementary material
SUPP_KEYWORD_PATTERN = re.compile(
    r"(?:suppl?e?m?e?n?t?(?:ary)?|supporting[-_ ]?info(?:rmation)?|additional[-_ ]?file|appendix|esm|esm[-_]?\d+|si[-_ ]?file)",
    re.IGNORECASE,
)

# ── Broadened Table Patterns (Fixing Issue #2) ──────────────────────────────
# Matches labels in anchor text, aria-label, title, caption, nearby headings, etc.
# Covers Table S1, Tab. S1, Supplementary Table 1, Supplementary Tables S1-S4, Extended Data Table 1, 补充表 S1, 附表 1, etc.
TABLE_LABEL_PATTERN = re.compile(
    r"(?:"
    # Branch 1: Explicit supplementary prefix (number is optional, e.g. Supplementary Table, Supplementary Tables S1-S4)
    r"(?:(?:supplementary|supporting|extended\s*data|ext\s*data|additional|appendix|online|suppl?\.?)\s+"
    r"(?:tables?|tabs?\.?|tbls?\.?|表格?)(?:\s*(?:[a-zA-Z]?[-_.]?\d+[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*[a-zA-Z]?[-_.]?\d+[a-zA-Z]?)?|[a-zA-Z]\b))?)"
    r"|"
    # Branch 2: Chinese supplementary table terms (number optional, e.g. 附表 1, 补充表格 1, 附表 1-5)
    r"(?:(?:补充|附录|附加)?(?:附表|补充表|附录表|附加表|表格)(?:\s*(?:[a-zA-Z]?[-_.]?\d+[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*[a-zA-Z]?[-_.]?\d+[a-zA-Z]?)?))?)"
    r"|"
    # Branch 3: Standard Table/Tab with identifier (S1, 1, A1, etc.) or range (S1-S4, 1-5)
    r"(?:(?:tables?|tabs?\.?|tbls?\.?)\s+(?:[a-zA-Z]?[-_.]?\d+[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*[a-zA-Z]?[-_.]?\d+[a-zA-Z]?)?))"
    r")",
    re.IGNORECASE,
)

# Matches table filenames (including hyphens and ranges)
# e.g., table-s1.pdf, table_s1.xlsx, supp_table_1.csv, supp_tables.pdf, extended-data-table-2.pdf, tbl_s2.pdf
TABLE_FILENAME_PATTERN = re.compile(
    r"(?:^|[\W_])"
    r"(?:"
    r"(?:(?:supplementary|supporting|extended[-_]?data|ext[-_]?data|appendix|online|suppl?)[-_.]*(?:tables?|tbls?|tabs?|附表|补充表|附录表|附加表|表格)(?:[-_.]*[a-z]?[-_.]?\d+(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+)?)?)"
    r"|"
    r"(?:(?:附表|补充表|附录表|附加表)(?:[-_.]*[a-z]?[-_.]?\d+(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+)?)?)"
    r"|"
    r"(?:(?:tables?|tbls?|tabs?)[-_.]*[a-z]?[-_.]?\d+(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+)?)"
    r")"
    r"(?:[\W_]|$)",
    re.IGNORECASE,
)

# ── URL Patterns to EXCLUDE ──────────────────────────────────────────────────
EXCLUDE_URL_PATTERNS = [
    r"scholar\.google\.com",
    r"scholar_lookup",
    r"plu\.mx",
    r"relx\.com",
    r"elsevier\.com/(?!cdn)",
    r"#(m\d{4}|s\d{4}|!)",  # in-page anchor links
    r"/journal/.*/vol/",
    r"doi\.org/journal/",
    r"service\.elsevier\.com",
    r"linkedin\.com",
    r"facebook\.com",
    r"twitter\.com",
    r"/article/pii/\w+/pdfft\?md5=",
    r"/science/article/pii/\w+/pdf\?",
    r"hub\.elsevier\.com",
    r"mendeley\.com",
    r"crossmark",
    r"crossref\.org",
    r"orcid\.org",
    r"doi\.org/10\.\d+/",  # DOI links to other articles (references)
]

# Article figures to exclude (gr1, fx1, ga1, etc.)
ARTICLE_FIGURE_PATTERN = re.compile(
    r"-(?:gr|ga|fx)\d+[a-z]?_(?:lrg|sml)?",
    re.IGNORECASE,
)

# Known HTML file extensions
HTML_EXTENSIONS = {".html", ".htm", ".php", ".asp", ".jsp"}


# ── Data Structures ──────────────────────────────────────────────────────────

@dataclass
class Candidate:
    """Represents a discovered supplementary candidate with full evidence."""
    url: str
    filename: str = ""
    link_text: str = ""
    section: str = ""
    match_rule: str = ""
    source_tag: str = "a"
    extra_info: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "filename": self.filename,
            "link_text": self.link_text,
            "section": self.section,
            "match_rule": self.match_rule,
            "source_tag": self.source_tag,
            "extra_info": self.extra_info,
        }


@dataclass
class FindingDiagnostics:
    """Diagnostic details about candidate extraction."""
    original_url: str = ""
    final_url: str = ""
    effective_base_url: str = ""
    declared_count: Optional[int] = None
    is_incomplete: bool = False
    incomplete_reason: str = ""
    candidates: List[Candidate] = field(default_factory=list)
    excluded_links: List[Tuple[str, str]] = field(default_factory=list)
    strategy_counts: Dict[str, int] = field(default_factory=dict)


# ── URL & Filename Utilities ─────────────────────────────────────────────────

def is_same_page_fragment(url: str, base_url: str) -> bool:
    """Check if a URL is merely an in-page anchor targeting the current page."""
    if not url or not base_url:
        return False
    u = urllib.parse.urlsplit(url)
    b = urllib.parse.urlsplit(base_url)
    if u.fragment and u.netloc.lower() == b.netloc.lower() and u.path.rstrip('/') == b.path.rstrip('/'):
        # If no query differences, it's just an anchor on the same page
        if u.query == b.query:
            return True
    return False


def is_excluded_url(url: str, base_url: str = "") -> bool:
    """Check if a URL should be excluded (references, external sites, anchors, etc.)."""
    if not url or url.startswith("javascript:") or url.startswith("mailto:") or url.startswith("tel:"):
        return True
    if url.startswith("#"):
        return True
    if base_url and is_same_page_fragment(url, base_url):
        return True
    for pattern in EXCLUDE_URL_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False


def is_article_figure_url(url: str) -> bool:
    """Check if a URL is an article figure (gr1, ga1, fx1, etc.)."""
    return bool(ARTICLE_FIGURE_PATTERN.search(url))


def sanitize_filename(name: str) -> str:
    """Remove or replace problematic filename characters."""
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = name.strip(". ")
    return name or "download"


GENERIC_PATH_BASENAMES = {
    "file", "download", "downloadsupplement", "asset", "get", "attachment",
    "content", "supp", "supplement", "supplementary",
}


def extract_filename_from_url(url: str, default_name: str = "") -> str:
    """
    Derive a readable and distinct filename from URL path, query parameters, or default_name.
    Handles generic endpoints (Wiley downloadSupplement, PLOS article/file) so distinct
    files never collide or overwrite each other.
    """
    parsed = urllib.parse.urlparse(url)
    
    # 1. Check query parameters first for explicit filenames
    if parsed.query:
        qs = urllib.parse.parse_qs(parsed.query)
        for key in ["file", "filename", "fileName", "attachment", "name", "download"]:
            if key in qs and qs[key]:
                val = qs[key][0].strip()
                if val and "." in val:
                    return sanitize_filename(os.path.basename(val))

    # 2. Check path basename
    path = parsed.path
    fname = os.path.basename(path.rstrip("/"))
    fname_lower = fname.lower()

    # Handle MDPI extensionless path /s1, /s2
    m = re.search(r'/(s\d+)/?$', path)
    if m:
        return f"{m.group(1)}.zip"

    # If path basename has a non-generic extension like .xlsx, .pdf, .zip, etc.
    if fname and "." in fname and fname_lower not in {"file.php", "download.php", "index.html", "index.php"}:
        return sanitize_filename(fname)

    # 3. Disambiguate generic endpoints with query parameters (Wiley, PLOS, etc.)
    if parsed.query:
        qs = urllib.parse.parse_qs(parsed.query)
        # PLOS: id=10.1371/journal.pone.0298123.s001
        if "id" in qs and qs["id"]:
            id_val = qs["id"][0].strip().split("/")[-1]
            if id_val:
                if "." in id_val:
                    return sanitize_filename(id_val)
                prefix = "attachment" if fname_lower in GENERIC_PATH_BASENAMES else (fname or "attachment")
                return sanitize_filename(f"{prefix}_{id_val}")
        # Wiley / Springer: attachmentId=1001, suppId, seq, part
        for id_key in ["attachmentId", "attachmentid", "attachment_id", "suppId", "seq", "part"]:
            if id_key in qs and qs[id_key]:
                val = qs[id_key][0].strip()
                if val:
                    prefix = "attachment" if fname_lower in GENERIC_PATH_BASENAMES else (fname or "attachment")
                    return sanitize_filename(f"{prefix}_{val}")

    # 4. Use suggested default name if provided
    if default_name:
        return sanitize_filename(default_name)

    # 5. Non-generic path basename
    if fname and fname_lower not in GENERIC_PATH_BASENAMES:
        return sanitize_filename(fname)

    # 6. Fallback
    return f"download_{abs(hash(url)) % 100000}.bin"


def extract_filename_from_content_disposition(cd_header: str) -> Optional[str]:
    """
    Parse filename from Content-Disposition header conforming to RFC 6266 / RFC 5987.
    Handles filename*=UTF-8''... as well as standard filename="...".
    """
    if not cd_header:
        return None
    # RFC 5987 / RFC 6266: filename*=UTF-8''filename
    m_star = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)?''([^;\n]+)", cd_header)
    if m_star:
        val = urllib.parse.unquote(m_star.group(1).strip('"\' '))
        if val:
            return sanitize_filename(os.path.basename(val))

    # Standard filename="..."
    m_std = re.search(r'filename\s*=\s*(["\'])(.*?)\1', cd_header)
    if m_std:
        val = m_std.group(2).strip()
        if val:
            return sanitize_filename(os.path.basename(val))

    # Unquoted filename=foo.ext
    m_unq = re.search(r'filename\s*=\s*([^;\s\n]+)', cd_header)
    if m_unq:
        val = m_unq.group(1).strip('"\' ')
        if val:
            return sanitize_filename(os.path.basename(val))

    return None


def infer_file_extension(content_bytes: bytes, content_type: str = "") -> str:
    """Infer file extension from magic bytes and/or Content-Type header."""
    ct = (content_type or "").lower()
    if content_bytes.startswith(b"%PDF"):
        return ".pdf"
    if content_bytes.startswith(b"PK\x03\x04") or content_bytes.startswith(b"PK\x05\x06"):
        if "spreadsheet" in ct or "excel" in ct:
            return ".xlsx"
        if "word" in ct or "document" in ct:
            return ".docx"
        if "presentation" in ct or "powerpoint" in ct:
            return ".pptx"
        return ".zip"
    if content_bytes.startswith(b"\x1f\x8b"):
        return ".gz"
    if content_bytes.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if content_bytes.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if "csv" in ct:
        return ".csv"
    if "tab-separated" in ct or "tsv" in ct:
        return ".tsv"
    return ""


def cookies_to_dict(cookies, target_url: Optional[str] = None) -> Dict[str, str]:
    """
    Convert a list of Playwright/browser cookie dictionaries to a standard {name: value}
    dictionary accepted by requests and Fetcher.get, avoiding unpacking crashes.
    """
    if not cookies:
        return {}
    if isinstance(cookies, dict):
        return cookies

    cookie_dict = {}
    target_domain = ""
    if target_url:
        target_domain = urllib.parse.urlparse(target_url).netloc.lower()

    if isinstance(cookies, list):
        for c in cookies:
            if isinstance(c, dict) and "name" in c and "value" in c:
                domain = (c.get("domain") or "").lstrip(".").lower()
                if target_domain and domain:
                    if domain not in target_domain and target_domain not in domain:
                        continue
                cookie_dict[c["name"]] = str(c["value"])
    return cookie_dict


def extract_effective_base_url(html_content: str, response_url: str) -> str:
    """
    Determine the effective base URL for relative link resolution.
    Respects <base href="..."> if present in HTML, otherwise uses the response URL.
    This fixes Issue #1 where DOI inputs resolved relative URLs against doi.org!
    """
    if not html_content:
        return response_url

    # Fast regex search for <base href="...">
    m = re.search(r'<base\s+[^>]*href=["\']([^"\']+)["\']', html_content, re.IGNORECASE)
    if m:
        base_href = m.group(1).strip()
        if base_href:
            return urllib.parse.urljoin(response_url, base_href)

    return response_url


# ── Pattern Matchers ─────────────────────────────────────────────────────────

def check_data_extension(path_or_url: str) -> Optional[str]:
    """Check if the string ends with or contains a known data extension."""
    parsed = urllib.parse.urlparse(path_or_url)
    path = parsed.path.lower()
    for ext in DATA_EXTENSIONS:
        if path.endswith(ext):
            return ext

    # Check query params for extension
    if parsed.query:
        query_lower = parsed.query.lower()
        for ext in DATA_EXTENSIONS:
            pattern = re.escape(ext) + r'(?:[&;#"\'\s]|$)'
            if re.search(pattern, query_lower):
                return ext

    # Strict boundary check on full URL
    url_lower = path_or_url.lower()
    for ext in DATA_EXTENSIONS:
        pattern = re.escape(ext) + r'(?:[^a-z0-9]|$)'
        if re.search(pattern, url_lower):
            return ext
    return None


def get_nearby_context_text(tag) -> Tuple[str, str]:
    """
    Search nearby headings, captions, or descriptive text enclosing or preceding the tag.
    Returns (heading_or_label_text, source_type).
    """
    if not tag or not hasattr(tag, "parent"):
        return "", ""

    curr = tag.parent
    for _ in range(3):
        if not curr:
            break
        # Check headings inside this container
        for h in curr.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption"], limit=3):
            htext = h.get_text(separator=" ", strip=True)
            if htext and htext != tag.get_text(separator=" ", strip=True):
                return htext, h.name

        # Check elements with class indicating title, label, or caption
        for el in curr.find_all(lambda e: e != tag and any(c in " ".join(e.get("class", [])).lower() for c in ["title", "caption", "label", "heading", "name", "desc"]), limit=3):
            el_text = el.get_text(separator=" ", strip=True)
            if el_text and el_text != tag.get_text(separator=" ", strip=True):
                return el_text, "nearby_label"

        # Check previous sibling of container
        prev = curr.find_previous_sibling(["h1", "h2", "h3", "h4", "h5", "h6", "p", "caption"])
        if prev:
            p_text = prev.get_text(separator=" ", strip=True)
            if p_text:
                return p_text, "previous_sibling_heading"

        curr = curr.parent
    return "", ""


def is_table_text_or_attribute(text: str, attrs: Optional[Dict[str, str]] = None, tag=None) -> Tuple[bool, str]:
    """Check if link text, aria-label, title, download attributes, or nearby context indicate a table."""
    candidates_to_check = [text]
    if attrs:
        for attr_key in ["aria-label", "title", "download", "data-filename", "data-title", "data-caption"]:
            val = attrs.get(attr_key)
            if val:
                candidates_to_check.append(val)

    for item in candidates_to_check:
        if not item:
            continue
        cleaned = item.strip()
        m = TABLE_LABEL_PATTERN.search(cleaned)
        if m:
            return True, f"table_label:{m.group(0)}"
        
        # Check filename pattern in attributes like download="table-s1.pdf"
        m_file = TABLE_FILENAME_PATTERN.search(cleaned)
        if m_file:
            return True, f"table_filename:{m_file.group(0).strip()}"

    # Check nearby heading and description context (Issue #2 requirement)
    if tag:
        nearby_text, source = get_nearby_context_text(tag)
        if nearby_text:
            m_ctx = TABLE_LABEL_PATTERN.search(nearby_text)
            if m_ctx:
                return True, f"nearby_context:{m_ctx.group(0)}"

    return False, ""


def is_supplementary_container(tag) -> bool:
    """Check if an HTML element is or is within a supplementary material container."""
    curr = tag
    steps = 0
    while curr and steps < 6:
        # Check tag name or attributes
        tag_id = (curr.get("id") or "").lower() if hasattr(curr, "get") else ""
        tag_cls = " ".join(curr.get("class", [])).lower() if hasattr(curr, "get") else ""
        tag_data = (curr.get("data-component") or "").lower() if hasattr(curr, "get") else ""

        for s in [tag_id, tag_cls, tag_data]:
            if any(kw in s for kw in ["supplement", "suppl", "appendix", "supporting-info", "esm"]):
                return True

        # Check nearby heading if curr has parent
        if curr.parent:
            for sibling in curr.parent.find_all(["h2", "h3", "h4", "h5", "caption", "figcaption"], limit=5):
                heading_text = sibling.get_text().lower()
                if any(kw in heading_text for kw in ["supplement", "suppl", "appendix", "supporting information", "additional file"]):
                    return True

        curr = curr.parent
        steps += 1
    return False


# ── Extractor from Controls (<a>, <button>, <meta>, etc.) ───────────────────

def extract_target_url_from_tag(tag) -> Optional[str]:
    """
    Extract target URL from <a>, <button>, or other controls.
    Covers href, data-*, and onclick handlers (Issue #6).
    Excludes in-page hash anchors like #tbl1, #m0001, etc.
    """
    # 1. Standard attributes
    for attr in [
        "href",
        "data-url",
        "data-href",
        "data-download-url",
        "data-file-url",
        "data-asset-url",
        "data-file",
        "data-src",
        "data-target-url",
        "data-download",
        "data-link",
        "data-uri",
        "content",  # <meta>
        "src",      # <iframe> / <embed>
    ]:
        val = tag.get(attr)
        if val and isinstance(val, str) and not val.startswith("javascript:"):
            val_clean = val.strip()
            # In-page anchors are not downloadable attachment URLs
            if val_clean and not val_clean.startswith("#"):
                return val_clean

    # 2. Inspect onclick handlers
    onclick = tag.get("onclick")
    if onclick and isinstance(onclick, str):
        # Look for window.open('...'), location.href='...', download('...')
        m = re.search(r'''(?:open|href|download)\s*\(?\s*['"](https?://[^'"]+|/[^'"]+)['"]''', onclick, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if not val.startswith("#"):
                return val

    return None


# ── Core Candidate Scanner ───────────────────────────────────────────────────

def find_all_candidates(
    html_content: str,
    base_url: str,
    original_url: str = "",
) -> FindingDiagnostics:
    """
    Scan HTML content for supplementary material candidates.
    Unifies logic between lightweight and advanced versions, records evidence,
    and returns rich diagnostics.
    """
    html_content = html_content or ""
    effective_base = extract_effective_base_url(html_content, base_url)
    diag = FindingDiagnostics(
        original_url=original_url or base_url,
        final_url=base_url,
        effective_base_url=effective_base,
    )

    if not html_content.strip():
        return diag

    soup = BeautifulSoup(html_content, "html.parser")
    seen_urls: Set[str] = set()
    candidates: List[Candidate] = []

    def add_candidate(cand: Candidate):
        norm_url = cand.url.split("#")[0].strip()
        if norm_url and norm_url not in seen_urls:
            seen_urls.add(norm_url)
            candidates.append(cand)

    # ── Strategy 1: Targeted Meta & Link Tags (Highwire, Nature, etc.) ────
    for meta in soup.find_all(["meta", "link"]):
        name = (meta.get("name") or meta.get("property") or meta.get("rel") or "")
        if isinstance(name, list):
            name = " ".join(name)
        name_lower = name.lower()

        if "supplementary_material" in name_lower or "supplement" in name_lower:
            target = meta.get("content") or meta.get("href")
            if target:
                full_url = urllib.parse.urljoin(effective_base, target.strip())
                if not is_excluded_url(full_url, base_url=effective_base) and not is_article_figure_url(full_url):
                    fname = extract_filename_from_url(full_url)
                    add_candidate(Candidate(
                        url=full_url,
                        filename=fname,
                        link_text=name,
                        section="meta_tags",
                        match_rule="meta_supplementary_tag",
                        source_tag=meta.name,
                    ))

    # ── Strategy 2: Scan all clickable controls (<a>, <button>, etc.) ─────
    controls = soup.find_all(["a", "button", "div", "span"])
    for tag in controls:
        target_raw = extract_target_url_from_tag(tag)
        if not target_raw:
            continue

        full_url = urllib.parse.urljoin(effective_base, target_raw)
        if is_excluded_url(full_url, base_url=effective_base):
            diag.excluded_links.append((full_url, "excluded_url_pattern"))
            continue
        if is_article_figure_url(full_url):
            diag.excluded_links.append((full_url, "article_figure_pattern"))
            continue

        link_text = tag.get_text(separator=" ", strip=True)
        attrs = {k: tag[k] for k in ["aria-label", "title", "download", "data-filename", "data-title", "data-caption"] if tag.has_attr(k)}
        fname = attrs.get("download") or extract_filename_from_url(full_url)
        parsed = urllib.parse.urlparse(full_url)
        path_lower = parsed.path.lower()
        url_lower = full_url.lower()

        is_supp_section = is_supplementary_container(tag)
        data_ext = check_data_extension(full_url)
        is_table, table_rule = is_table_text_or_attribute(link_text, attrs, tag=tag)
        is_table_filename = bool(TABLE_FILENAME_PATTERN.search(fname)) or bool(TABLE_FILENAME_PATTERN.search(path_lower))

        # If matched via nearby heading context and fname is generic, derive a clean name from the table rule
        if is_table and table_rule.startswith("nearby_context:"):
            if fname.startswith("download_") or fname in GENERIC_PATH_BASENAMES or "." not in fname or fname.startswith("attachment_") or fname.startswith("endpoint_"):
                matched_label = table_rule.split("nearby_context:", 1)[1]
                ext_part = os.path.splitext(path_lower)[1] or ".bin"
                fname = sanitize_filename(f"{matched_label}{ext_part}")

        matched = False
        rule = ""
        section = "supplementary_section" if is_supp_section else "page_body"

        # Case A: Elsevier MMC link
        if MMC_PATTERN.search(url_lower):
            matched = True
            rule = "mmc_pattern"

        # Case B: Standard data extension (.xlsx, .csv, .zip, etc.)
        elif data_ext:
            matched = True
            rule = f"data_extension:{data_ext}"

        # Case C: Table label detected in text, attributes, or nearby heading (Issue #2 & #3)
        elif is_table:
            # Check if it's an HTML table attachment vs normal webpage
            is_html = any(path_lower.endswith(h_ext) for h_ext in HTML_EXTENSIONS)
            if is_html:
                # Valid standalone HTML table attachment!
                matched = True
                rule = f"html_table_attachment ({table_rule})"
            elif data_ext or path_lower.endswith(".pdf") or is_supp_section or tag.has_attr("download") or is_table_filename:
                matched = True
                rule = table_rule
            elif not any(path_lower.endswith(h) for h in HTML_EXTENSIONS) and ("download" in full_url.lower() or "attachment" in full_url.lower() or "supp" in full_url.lower() or "table" in full_url.lower()):
                matched = True
                rule = f"download_table_endpoint ({table_rule})"

        # Case D: Table filename pattern (e.g. table-s1.pdf, tbl_s2.xlsx)
        elif is_table_filename:
            matched = True
            rule = "table_filename_pattern"

        # Case E: PDF in supplementary context or with supp keywords (Issue #3)
        elif path_lower.endswith(".pdf"):
            if SUPP_KEYWORD_PATTERN.search(url_lower):
                matched = True
                rule = "pdf_with_supplementary_keyword"
            elif is_supp_section:
                # Inside supplementary section and is PDF (e.g. S1.pdf, button "Download PDF")
                matched = True
                rule = "pdf_in_supplementary_section"

        # Case F: Control inside supplementary section with download indicator
        elif is_supp_section:
            if tag.has_attr("download") or any(kw in link_text.lower() for kw in ["download", "pdf", "file", "table"]):
                # Don't grab non-file HTML navigation links
                if not any(path_lower.endswith(h_ext) for h_ext in HTML_EXTENSIONS) or "table" in link_text.lower():
                    matched = True
                    rule = "download_control_in_supplementary_section"

        # Case G: MDPI style /s1, /s2
        elif re.search(r'/s\d+/?$', path_lower):
            matched = True
            rule = "mdpi_supp_path"

        if matched:
            add_candidate(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=link_text[:80],
                section=section,
                match_rule=rule,
                source_tag=tag.name,
                extra_info={"attributes": str(attrs)},
            ))

    # ── Strategy 3: Publisher-Specific Extractors (Issue #6) ───────────────
    # Elsevier ScienceDirect / Cell / Lancet reader asset links
    if any(d in effective_base for d in ["sciencedirect.com", "els-cdn.com", "elsevier.com", "cell.com", "thelancet.com"]):
        _extract_elsevier_candidates(soup, effective_base, add_candidate)

    # Springer / Nature / BMC supplementary assets
    if any(d in effective_base for d in ["springer.com", "nature.com", "biomedcentral.com", "springeropen.com", "palgrave.com"]):
        _extract_springer_nature_candidates(soup, effective_base, add_candidate)

    # Wiley supplementary assets
    if "wiley.com" in effective_base:
        _extract_wiley_candidates(soup, effective_base, add_candidate)

    # PLOS supplementary files
    if "plos.org" in effective_base:
        _extract_plos_candidates(soup, effective_base, add_candidate)

    # Embedded JSON data structures (Next.js / Nuxt / Publisher JSON payloads)
    _extract_embedded_json_candidates(soup, effective_base, add_candidate)

    # ── Diagnostics & Completeness Check (Issue #4) ────────────────────────
    diag.candidates = candidates
    diag.declared_count = detect_declared_supplement_count(html_content)
    is_inc, inc_reason = is_result_obviously_incomplete(candidates, html_content, diag.declared_count)
    diag.is_incomplete = is_inc
    diag.incomplete_reason = inc_reason

    return diag


# ── Publisher-Specific Extractors ────────────────────────────────────────────

def _extract_elsevier_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Elsevier-specific asset and MMC extraction."""
    for el in soup.find_all(["a", "button"], attrs={"data-component": True}):
        comp = el.get("data-component", "").lower()
        if "attachment" in comp or "mmc" in comp:
            url = extract_target_url_from_tag(el)
            if url:
                full_url = urllib.parse.urljoin(base_url, url)
                add_func(Candidate(
                    url=full_url,
                    filename=extract_filename_from_url(full_url),
                    link_text=el.get_text(strip=True),
                    section="elsevier_component",
                    match_rule="elsevier_attachment_component",
                ))


def _extract_springer_nature_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Springer and Nature supplementary file extraction."""
    for a in soup.find_all("a", attrs={"data-track-action": "download"}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            if not is_excluded_url(full_url, base_url=base_url):
                add_func(Candidate(
                    url=full_url,
                    filename=extract_filename_from_url(full_url),
                    link_text=a.get_text(strip=True),
                    section="springer_nature_download",
                    match_rule="springer_data_track_download",
                ))

    # Nature supplementary file links
    for a in soup.find_all("a", attrs={"data-test": "supplementary-file-link"}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            if not is_excluded_url(full_url, base_url=base_url):
                add_func(Candidate(
                    url=full_url,
                    filename=extract_filename_from_url(full_url),
                    link_text=a.get_text(strip=True),
                    section="nature_supplementary",
                    match_rule="nature_data_test_supp_link",
                ))


def _extract_wiley_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Wiley Online Library supplementary extraction."""
    for a in soup.find_all("a", href=re.compile(r"/action/downloadSupplement", re.IGNORECASE)):
        href = a.get("href")
        full_url = urllib.parse.urljoin(base_url, href)
        link_text = a.get_text(strip=True)
        fname = extract_filename_from_url(full_url, default_name=link_text)
        add_func(Candidate(
            url=full_url,
            filename=fname,
            link_text=link_text,
            section="wiley_supplement",
            match_rule="wiley_download_supplement_action",
        ))


def _extract_plos_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """PLOS (Public Library of Science) supplementary file extraction."""
    for a in soup.find_all("a", href=re.compile(r"/article/file\?id=", re.IGNORECASE)):
        href = a.get("href")
        full_url = urllib.parse.urljoin(base_url, href)
        link_text = a.get_text(strip=True)
        fname = extract_filename_from_url(full_url, default_name=link_text)
        add_func(Candidate(
            url=full_url,
            filename=fname,
            link_text=link_text,
            section="plos_supplement",
            match_rule="plos_article_file_action",
        ))


def _extract_embedded_json_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Extract supplementary URLs embedded inside <script> JSON or LD-JSON."""
    for script in soup.find_all("script", attrs={"type": re.compile(r"json", re.IGNORECASE)}):
        content = script.string or ""
        if not content or len(content) > 5000000:
            continue
        
        if "mmc" in content or "supplement" in content or ".xlsx" in content or "table" in content:
            for m in re.finditer(r'''https?://[^\s"'<>]+(?:\.xlsx|\.xls|\.csv|\.tsv|\.zip|mmc\d+\.\w+)''', content, re.IGNORECASE):
                cand_url = m.group(0)
                if not is_excluded_url(cand_url, base_url=base_url) and not is_article_figure_url(cand_url):
                    add_func(Candidate(
                        url=cand_url,
                        filename=extract_filename_from_url(cand_url),
                        link_text="embedded_json_link",
                        section="embedded_json",
                        match_rule="embedded_script_json_url",
                        source_tag="script",
                    ))


# ── Completeness & Incomplete Result Detection (Issue #4) ────────────────────

def detect_declared_supplement_count(html_content: str) -> Optional[int]:
    """
    Detect whether the article page declares a specific count of supplementary files.
    e.g., '4 Supplementary Files', 'Supplementary Tables 1–5', 'Supporting Information (3)',
    '共 4 个补充文件', '附表 1-5', '4 个附表'.
    """
    if not html_content:
        return None

    # Pattern 1: 'N Supplementary Files' / 'N Supplementary Tables'
    m = re.search(r'\b(\d{1,2})\s+(?:supplementary|supporting|additional)\s+(?:files?|tables?|datasets?|materials?|items?)\b', html_content, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # Pattern 2: 'Supporting Information (N)' or 'Supplementary Material (4 files)'
    m = re.search(
        r'(?:Supplementary|Supporting|Additional)\s+(?:Information|Material|Data|Files|Tables)\s*\(\s*([1-9]\d?)(?:\s+(?:files?|tables?|items?|datasets?))?\s*\)',
        html_content,
        re.IGNORECASE,
    )
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # Pattern 3: 'Supplementary Tables S?1-S?(\d+)' / 'Supplementary Tables 1 to 5'
    m = re.search(r'(?:Supplementary|Supporting|Extended Data)\s+Tables?\s+S?0?1\s*(?:[-–至]|to)\s*S?0?(\d{1,2})', html_content, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # Pattern 4: Chinese counts: '共 4 个补充文件', '4 个附表', '3个附件'
    m = re.search(r'(?:共|含|包括)?\s*(\d{1,2})\s*个?(?:补充|附录|附加)?(?:文件|材料|表格|数据|附件|附表|补充表)', html_content)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    # Pattern 5: Chinese range: '附表 1-5', '附表 1 至 5', '补充表 S1-S6'
    m = re.search(r'(?:附表|补充表|补充表格)\s*S?0?1\s*(?:[-–至]|to)\s*S?0?(\d{1,2})', html_content, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass

    return None


def is_result_obviously_incomplete(
    candidates: List[Candidate],
    html_content: str,
    declared_count: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    Determine if the extraction result is obviously incomplete, requiring browser rendering.
    Covers:
      1. Supplementary container found in HTML but 0 candidates extracted from it.
      2. Zero candidates found overall.
      3. Candidate count is less than declared count in page text.
    """
    if not html_content:
        return bool(not candidates), "Empty page content"

    # Check if a supplementary section exists, but no candidates came from it
    soup = BeautifulSoup(html_content, "html.parser")
    supp_keywords = ["supplement", "suppl", "appendix", "supporting-info", "supporting_info", "esm", "additional-file", "supplementary-material", "supplementary-data"]
    supp_sections = soup.find_all(lambda el: el.name in ["section", "div"] and any(
        kw in (el.get("id", "") + " " + " ".join(el.get("class", []))).lower()
        for kw in supp_keywords
    ))
    if supp_sections:
        cands_in_section = [c for c in candidates if c.section in ["supplementary_section", "table_caption"]]
        if not cands_in_section:
            return True, "Supplementary container found in HTML but 0 candidates extracted from it"

    if not candidates:
        return True, "Zero supplementary candidates found"

    if declared_count is not None and len(candidates) < declared_count:
        return True, f"Candidate count ({len(candidates)}) is less than declared count ({declared_count})"

    return False, ""


# ── File Validation (Issue #5: Removing Fixed 1 KB Threshold) ────────────────

NON_HTML_DATA_EXTS = {
    ".xlsx", ".xls", ".csv", ".tsv", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".txt", ".json", ".xml", ".r", ".py", ".ipynb", ".m", ".nb",
    ".cif", ".pdb", ".mol", ".sdf", ".xyz", ".fasta", ".fa", ".gb",
    ".nii", ".dcm", ".h5", ".hdf5", ".mat", ".pkl", ".rda", ".sav",
    ".docx", ".doc", ".pptx", ".ppt", ".pdf",
}


def validate_downloaded_file(
    filepath: str,
    content_bytes: Optional[bytes] = None,
    response_status: int = 200,
    headers: Optional[dict] = None,
) -> Tuple[bool, str]:
    """
    Validate that a downloaded file is genuine supplementary data and not an HTML
    error page, login wall, or Cloudflare challenge.
    Does NOT use an arbitrary 1 KB threshold.
    Properly verifies headers, signatures, and file extensions.
    """
    if not os.path.exists(filepath):
        return False, "File does not exist on disk"

    file_size = os.path.getsize(filepath)
    if file_size == 0:
        return False, "Downloaded file is empty (0 bytes)"

    if response_status >= 400:
        return False, f"Server returned HTTP error status {response_status}"

    # Read initial bytes for signature checking
    if content_bytes is None:
        try:
            with open(filepath, "rb") as f:
                header_bytes = f.read(2048)
        except Exception as e:
            return False, f"Cannot read file header: {e}"
    else:
        header_bytes = content_bytes[:2048]

    # Normalize response headers
    hdr = {k.lower(): v for k, v in (headers or {}).items()}
    ct = (hdr.get("content-type") or "").lower()

    # 1. Binary Magic Bytes Verification
    # ZIP / XLSX / DOCX / PPTX: PK\x03\x04
    if header_bytes.startswith(b"PK\x03\x04") or header_bytes.startswith(b"PK\x05\x06"):
        return True, f"Valid ZIP/Office document signature ({file_size} bytes)"

    # PDF: %PDF
    if header_bytes.startswith(b"%PDF"):
        return True, f"Valid PDF signature ({file_size} bytes)"

    # GZIP: \x1f\x8b
    if header_bytes.startswith(b"\x1f\x8b"):
        return True, f"Valid GZIP signature ({file_size} bytes)"

    # 7z: 7z\xbc\xaf\x27\x1c
    if header_bytes.startswith(b"7z\xbc\xaf\x27\x1c"):
        return True, f"Valid 7-Zip signature ({file_size} bytes)"

    # RAR: Rar!\x1a\x07
    if header_bytes.startswith(b"Rar!\x1a\x07"):
        return True, f"Valid RAR signature ({file_size} bytes)"

    # HDF5: \x89HDF\r\n\x1a\n
    if header_bytes.startswith(b"\x89HDF\r\n\x1a\n"):
        return True, f"Valid HDF5 signature ({file_size} bytes)"

    # 2. Check for HTML content (either via HTML tags or text/html Content-Type)
    header_lower = header_bytes.lower()
    is_html_tag = any(tag in header_lower for tag in [b"<!doctype html", b"<html", b"<head", b"<body"])
    is_html_ct = "text/html" in ct

    if is_html_tag or is_html_ct:
        ext = os.path.splitext(filepath)[1].lower()
        # If the file was expected to be a binary/data file (e.g. .xlsx, .pdf, .zip, .csv)
        # but content is HTML, it is an error/login page disguised as data file!
        if ext in NON_HTML_DATA_EXTS:
            return False, f"Expected {ext} data file but received HTML document ({file_size} bytes)"

        # Check for error indicators in HTML
        error_indicators = [
            b"challenge-platform",
            b"cf-browser-verification",
            b"just a moment",
            b"access denied",
            b"403 forbidden",
            b"404 not found",
            b"page not found",
            b"sign in",
            b"login required",
            b"institutional login",
            b"purchase article",
            b"subscribe to",
        ]
        for indicator in error_indicators:
            if indicator in header_lower:
                return False, f"HTML error/challenge/login page detected: {indicator.decode('latin1')}"

        # If it's an HTML file and NOT an error page, check if it contains a table structure
        if ext in HTML_EXTENSIONS or not ext:
            if b"<table" in header_lower or b"<tr" in header_lower or b"<th" in header_lower:
                return True, f"Valid standalone HTML table attachment ({file_size} bytes)"
            return False, f"Received HTML document without table structure ({file_size} bytes)"

        return False, f"Expected data file but received HTML document ({file_size} bytes)"

    # 3. For text files (CSV, TSV, TXT, JSON, XML, scripts)
    ext = os.path.splitext(filepath)[1].lower()
    if ext in {".csv", ".tsv", ".txt", ".json", ".xml", ".r", ".py", ".m", ".cif"}:
        # Check if JSON payload is an API error response
        if ext == ".json":
            if b'"error"' in header_lower or b'"errors"' in header_lower or b'"status": 40' in header_lower:
                return False, f"JSON error response detected ({file_size} bytes)"
        if file_size < 10 and not header_bytes.strip():
            return False, "File contains only whitespace"
        return True, f"Valid text data file ({file_size} bytes)"

    # 4. General non-empty file
    return True, f"Non-empty binary/text file ({file_size} bytes)"
