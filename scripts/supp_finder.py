"""
Unified Supplementary Material & Table Candidate Finder
========================================================
Shared candidate extraction, pattern matching, diagnostics, and file validation
for both journal_downloader.py (lightweight) and scansci_supp_downloader.py (integrated).
"""

import hashlib
import html
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union
from bs4 import BeautifulSoup


# ── Parser Selection (Prefers fast C-based lxml over pure Python html.parser)
try:
    import lxml  # noqa: F401
    DEFAULT_PARSER = "lxml"
except ImportError:
    DEFAULT_PARSER = "html.parser"


# ── Generic Data File Extensions ─────────────────────────────────────────────
# Note: .pdf and .html are handled contextually (tables/supplementary sections)
# rather than as generic blind extensions to prevent grabbing main article PDFs/pages.
DATA_EXTENSIONS = {
    ".xlsx", ".xls", ".csv", ".tsv", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".txz", ".tsv.gz", ".csv.gz", ".txt.gz",
    ".txt", ".json", ".xml", ".r", ".py", ".ipynb", ".m", ".nb",
    ".cif", ".pdb", ".mol", ".sdf", ".xyz", ".fasta", ".fa", ".gb",
    ".nii", ".nii.gz", ".dcm", ".h5", ".hdf5", ".mat", ".pkl", ".rda", ".sav",
    ".docx", ".doc", ".pptx", ".ppt",
    ".parquet", ".feather", ".arrow", ".yaml", ".yml", ".sas7bdat", ".dta",
    ".bz2", ".xz", ".db", ".sqlite", ".sqlite3",
    ".vcf", ".vcf.gz", ".bed", ".gff", ".gff3", ".gtf", ".zarr",
}
# Sort extensions by length descending so multi-part extensions like .nii.gz match before .gz (F3)
SORTED_DATA_EXTENSIONS = tuple(sorted(DATA_EXTENSIONS, key=len, reverse=True))

# ── Publisher Patterns ───────────────────────────────────────────────────────
MMC_PATTERN = re.compile(r"mmc\d+", re.IGNORECASE)

# Keywords indicating supplementary material
SUPP_KEYWORD_PATTERN = re.compile(
    r"(?:"
    r"suppl?e?m?e?n?t?(?:ary|al)?"
    r"|supporting[-_ ]?info(?:rmation)?"
    r"|supporting[-_ ]?material(?:s)?"
    r"|supporting[-_ ]?document(?:s)?"
    r"|supporting[-_ ]?note(?:s)?"
    r"|supporting[-_ ]?file(?:s)?"
    r"|supplementary[-_ ]?material(?:s)?"
    r"|supplemental[-_ ]?material(?:s)?"
    r"|supplementary[-_ ]?file(?:s)?"
    r"|supplemental[-_ ]?file(?:s)?"
    r"|supplementary[-_ ]?data"
    r"|supplemental[-_ ]?data"
    r"|supplementary[-_ ]?note(?:s)?"
    r"|supplementary[-_ ]?protocol(?:s)?"
    r"|supplementary[-_ ]?method(?:s)?"
    r"|supplementary[-_ ]?code"
    r"|supplementary[-_ ]?script(?:s)?"
    r"|additional[-_ ]?file(?:s)?"
    r"|additional[-_ ]?info(?:rmation)?"
    r"|source[-_ ]?data(?:[-_ ]?(?:tables?|tabs?\.?|tbls?\.?|figs?\.?|figures?|files?))?"
    r"|appendix"
    r"|esm"
    r"|esm[-_]?\d+"
    r"|si[-_ ]?file(?:s)?"
    r"|extended[-_ ]?data"
    r"|online[-_ ]?suppl?e?m?e?n?t?(?:ary|al)?"
    r"|附录|补充材料|补充文件|附加文件|补充信息|补充数据"
    r")",
    re.IGNORECASE,
)

# ── Broadened Table & Supplementary Patterns ────────────────────────────────
# Matches labels in anchor text, aria-label, title, caption, nearby headings, etc.
# Covers Table S1, Tab. S1, S1 Table, Supplementary Table 1, Supplementary Tables S1-S4,
# Source Data Fig. 1, Extended Data Table 1, 补充表 S1, 附表 1, 附表一, Table SI, etc.
TABLE_LABEL_PATTERN = re.compile(
    r"(?:"
    # Branch 1: Explicit supplementary prefix with table/data (number optional, supports Roman numerals and ranges)
    r"(?:(?:supplementary|supporting|extended\s*data|ext\s*data|additional|appendix|online|suppl?\.?)\s+"
    r"(?:(?:figures?|figs?\.?)\s+(?:and|&)\s+)?(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|表格?|数据)(?:\s+(?:and|&)\s+(?:figures?|figs?\.?))?(?:\s*(?:[a-zA-Z]?[-_.]?\d+[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*[a-zA-Z]?[-_.]?\d+[a-zA-Z]?)?|[sS]?[-_.]?[ivxldcmIVXLDCM]+|[a-zA-Z]\b))?)"
    r"|"
    # Branch 1a2: Explicit supplementary prefix with file/文件 (number is required, e.g. Additional file 1, Supplementary File S1)
    r"(?:(?:supplementary|supporting|extended\s*data|ext\s*data|additional|appendix|online|suppl?\.?)\s+"
    r"(?:files?|文件)\s+(?:[a-zA-Z]?[-_.]?\d+[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*[a-zA-Z]?[-_.]?\d+[a-zA-Z]?)?|[sS]?[-_.]?[ivxldcmIVXLDCM]+|[a-zA-Z]\b))"
    r"|"
    # Branch 1b: Standalone Appendix with identifier (e.g. Appendix 1, Appendix-1, Appendix A, Appendix 1-3)
    r"(?:\bappendix[-_\s]*(?:tables?|tabs?\.?|tbls?\.?|[a-zA-Z0-9]+(?:\s*(?:[-–至]|to)\s*[a-zA-Z0-9]+)?)\b)"
    r"|"
    # Branch 1c: eTable format (e.g. eTable 1, eTable1, e-Table S1)
    r"(?:\be[-_]?(?:tables?|tabs?\.?|tbls?\.?)(?:\s*[a-zA-Z]?[-_.]?\d+[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*[a-zA-Z]?[-_.]?\d+[a-zA-Z]?)?)?\b)"
    r"|"
    # Branch 1d: Source Data format (e.g. Source Data, Source Data Fig. 1, Source Data Table 1)
    r"(?:\bsource[-_ ]?data(?:\s+(?:tables?|tabs?\.?|tbls?\.?|figs?\.?|figures?|files?)?(?:\s*[a-zA-Z]?[-_.]?\d+[a-zA-Z]?)?)?\b)"
    r"|"
    # Branch 1e: Pre-numbered S format (e.g. S1 Table, S2 Tables, S1 Data, S1 Dataset, S1 File)
    r"(?:\bs\d+[a-zA-Z]?[-_.\s]+(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?|表格?|数据|文件)\b)"
    r"|"
    # Branch 2: Chinese supplementary table/data terms (supports Chinese digits 一二三四五六七八九十)
    r"(?:(?:补充|附录|附加)?(?:附表|补充表|附录表|附加表|补充数据|附录数据|补充文件|表格)(?:\s*(?:[a-zA-Z]?[-_.]?[0-9一二三四五六七八九十]+[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*[a-zA-Z]?[-_.]?[0-9一二三四五六七八九十]+[a-zA-Z]?)?))?)"
    r"|"
    r"(?:附录\s*[a-zA-Z0-9一二三四五六七八九十]+(?:\s*(?:[-–至]|to)\s*[a-zA-Z0-9一二三四五六七八九十]+)?)"
    r"|"
    # Branch 3: Standard Table/Tab with identifier (S1, 1, A1, A.1, SI, SII, etc.) or range (S1-S4, 1-5)
    r"(?:(?:tables?|tabs?\.?|tbls?\.?)\s+(?:[a-zA-Z]?[-_.]?\d+[a-zA-Z]?|[sS]?[-_.]?[ivxldcmIVXLDCM]+)(?:\s*(?:[-–至]|to)\s*(?:[a-zA-Z]?[-_.]?\d+[a-zA-Z]?|[sS]?[-_.]?[ivxldcmIVXLDCM]+))?)"
    r"|"
    # Branch 4: Data S1, Dataset S1, File S1, Data SI, etc.
    r"(?:(?:datasets?|data|files?)\s+(?:s\d+|s[-_.]?[ivxldcm]+)[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*(?:s?\d+|s?[-_.]?[ivxldcm]+)[a-zA-Z]?)?\b)"
    r")",
    re.IGNORECASE,
)

# Matches table filenames (including hyphens, ranges, eTable, appendix, and Chinese)
# e.g., table-s1.pdf, s1-table.pdf, table_s1.xlsx, supp_table_1.csv, supp_tables.pdf,
# extended-data-table-2.pdf, tbl_s2.pdf, source-data-fig1.xlsx, 附表一.xlsx
TABLE_FILENAME_PATTERN = re.compile(
    r"(?:^|[\W_])"
    r"(?:"
    r"(?:(?:supplementary|supporting|extended[-_]?data|ext[-_]?data|appendix|online|suppl?)[-_.]*(?:tables?|tbls?|tabs?|附表|补充表|附录表|附加表|表格)(?:[-_.]*[a-z]?[-_.]?\d+(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+)?)?)"
    r"|"
    r"(?:e[-_]?(?:tables?|tbls?|tabs?)[-_.]*[a-z]?[-_.]?\d+(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+)?)"
    r"|"
    r"(?:appendix[-_.]*(?:tables?|tbls?|tabs?)?[-_.]*[a-z0-9]+(?:[-_.]*(?:to|[-–至])[-_.]*[a-z0-9]+)?)"
    r"|"
    r"(?:\bs\d+[a-z]?[-_.]*(?:tables?|tbls?|tabs?|datasets?|data|files?))"
    r"|"
    r"(?:source[-_.]?data(?:[-_.](?:tables?|tabs?|tbls?|figs?|figures?|files?)?[-_.]*[a-z0-9]+)?)"
    r"|"
    r"(?:(?:附表|补充表|附录表|附加表|附录)(?:[-_.]*[a-z]?[-_.]?[0-9一二三四五六七八九十]+(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?[0-9一二三四五六七八九十]+)?)?)"
    r"|"
    r"(?:(?:tables?|tbls?|tabs?)[-_.]*(?:[a-z]?[-_.]?\d+|[sS]?[-_.]*[ivxldcm]+)(?:[-_.]*(?:to|[-–至])[-_.]*(?:[a-z]?[-_.]?\d+|[sS]?[-_.]*[ivxldcm]+))?)"
    r"|"
    r"(?:(?:datasets?|data|files?)[-_.]*s(?:\d+|[-_.]*[ivxldcm]+))"
    r")"
    r"(?:[\W_]|$)",
    re.IGNORECASE,
)


def is_explicit_supp_table(label: str) -> bool:
    """
    Check if a table or data label explicitly indicates a supplementary or extended item.
    e.g. 'Extended Data Table 1', 'Supplementary Table S1', 'Supporting Table 1',
         'Table S1', 'Tab. S2', 'S1 Table', 'S1 Data', 'Source Data', '附表 1',
         '附表一', '补充表 S1', 'Appendix 1', '附录 1', 'Table SI',
         'Table A1', 'Table A.1', 'Table A-1', 'Table B.2',
         'eTable 1', 'Supplementary Data S1', 'Data S1', 'Dataset S1', etc.
    Excludes bare primary article table labels like 'Table 1', 'Table 2', 'Tab 1'.
    """
    if not label:
        return False
    # Check for supplementary/extended prefix with table/data
    if re.search(
        r'(?:supplementary|supporting|extended\s*data|ext\s*data|additional|online|suppl?\.?)\s+(?:(?:figures?|figs?\.?)\s+(?:and|&)\s+)?(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|表格?|数据)(?:\s+(?:and|&)\s+(?:figures?|figs?\.?))?',
        label,
        re.IGNORECASE,
    ):
        return True
    # Check for supplementary/extended prefix with file/文件 (requires number/identifier)
    if re.search(
        r'(?:supplementary|supporting|extended\s*data|ext\s*data|additional|online|suppl?\.?)\s+(?:files?|文件)\s+(?:[a-z]?[-_.]?\d+|[sS]?[-_.]*[ivxldcm]+|[a-z]\b)',
        label,
        re.IGNORECASE,
    ):
        return True
    # Check Source Data
    if re.search(r'\bsource[-_ ]?data\b', label, re.IGNORECASE):
        return True
    # Check S-prefixed table/data numbering (e.g. S1 Table, S2 Table, S1 Data, S1 Dataset, S1 File)
    if re.search(r'\bs\d+[a-zA-Z]?[-_.\s]+(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?|表格?|数据|文件)\b', label, re.IGNORECASE):
        return True
    # Check Appendix Table or standalone Appendix with identifier (Appendix 1, Appendix-1, Appendix A)
    if re.search(r'\bappendix[-_\s]*(?:tables?|tabs?\.?|tbls?\.?|[a-z0-9])', label, re.IGNORECASE):
        return True
    # Check eTable / e-Table
    if re.search(r'\be[-_]?(?:tables?|tabs?\.?|tbls?\.?)', label, re.IGNORECASE):
        return True
    # Check Chinese terms (附表, 补充表, 附录表, 附加表, 附录 1, 补充数据 1, etc.)
    if re.search(r'(?:(?:补充|附录|附加)?(?:附表|补充表|附录表|附加表|补充数据|附录数据|补充文件|附加文件))', label, re.IGNORECASE):
        return True
    if re.search(r'附录\s*[a-z0-9一二三四五六七八九十]', label, re.IGNORECASE):
        return True
    # Check if table/data/dataset has 'S' numbering (e.g. Table S1, Tab. S2, Data S1, Dataset S1, File S1, Table SI)
    if re.search(r'(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?)\s+s[-_.]*(?:\d+|[ivxldcm]+)', label, re.IGNORECASE):
        return True
    return False


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

# Article figures to exclude (gr1, ga1, fx1, fig1, figure_1, etc.) (C2)
ARTICLE_FIGURE_PATTERN = re.compile(
    r"(?:^|[\W_])(?:gr|ga|fx|fig|figure)[-_]?\d+[a-z]?(?:[-_](?:lrg|sml|large|small|preview|highres|html)|\.(?:jpe?g|png|gif|tif|tiff|webp|svg)|$|\?)",
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

    # Immunity (C1): Never exclude explicit MMC URLs or genuine data/document file downloads
    # (e.g. api.elsevier.com/.../mmc2.xlsx, www.elsevier.com/__data/assets/excel/.../table-s1.xlsx,
    # or doi.org/10.../table-s1.xlsx, or supplementary-tables.pdf)
    is_data_or_supp_file = bool(
        check_data_extension(url)
        or MMC_PATTERN.search(url)
        or TABLE_FILENAME_PATTERN.search(url)
        or re.search(r'\.(?:xlsx|xls|csv|tsv|zip|gz|tar|tgz|tar\.gz|7z|rar|pdf|docx|doc|bin|nii|nii\.gz|h5)(?:[?#]|$)', url, re.IGNORECASE)
    )

    for pattern in EXCLUDE_URL_PATTERNS:
        if pattern == r"doi\.org/10\.\d+/":
            # Only exclude DOI links when they are article reference links without a data/file extension
            if is_data_or_supp_file:
                continue
        elif "elsevier" in pattern:
            # Do not exclude elsevier download endpoints that point to data files or MMC
            if is_data_or_supp_file:
                continue
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False


def is_article_figure_url(url: str) -> bool:
    """Check if a URL is an article figure (gr1, ga1, fx1, fig1, etc.). Never flags data files or supps (C2)."""
    if not url:
        return False
    # Supplementary data files, MMCs, table files, or explicit supp keywords are never article figures
    if (
        check_data_extension(url)
        or MMC_PATTERN.search(url)
        or TABLE_FILENAME_PATTERN.search(url)
        or SUPP_KEYWORD_PATTERN.search(url)
    ):
        return False
    return bool(ARTICLE_FIGURE_PATTERN.search(url))


def sanitize_filename(name: str) -> str:
    """Remove or replace problematic filename characters, unquoting URL encoding (F4)."""
    if not name:
        return "download"
    name = urllib.parse.unquote(name)
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = name.strip(". ")
    return name or "download"


GENERIC_PATH_BASENAMES = {
    "file", "download", "downloadsupplement", "asset", "assets", "get", "attachment",
    "content", "supp", "supplement", "supplementary", "fetch", "view", "stream", "att",
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

    # 2. Check path basename (F4: unquote path)
    path = urllib.parse.unquote(parsed.path)
    fname = os.path.basename(path.rstrip("/"))
    fname_lower = fname.lower()

    # Handle MDPI extensionless path /s1, /s2
    m = re.search(r'/(s\d+)/?$', path)
    if m:
        return f"{m.group(1)}.zip"

    # If path basename has a non-generic extension like .xlsx, .pdf, .zip, etc.
    if fname and "." in fname and fname_lower not in {"file.php", "download.php", "index.html", "index.php"}:
        return sanitize_filename(fname)

    # 2.5 If default_name has a valid data or document extension (e.g. Table S1.xlsx, supp.pdf),
    # prioritize it over extensionless query-parameter disambiguation (attachment_1001)
    if default_name:
        clean_def = re.sub(r'\s*\((pdf|xlsx|xls|docx|doc|csv|zip|tar\.gz|gz|tsv)\)\s*$', r'.\1', default_name, flags=re.IGNORECASE)
        def_ext = check_data_extension(clean_def) or (".pdf" if clean_def.lower().endswith(".pdf") else None)
        if def_ext:
            return sanitize_filename(clean_def)

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

    # 6. Fallback (F2: stable deterministic hash across runs)
    h = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
    return f"download_{h}.bin"


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
        if "spreadsheet" in ct or "excel" in ct or b"xl/" in content_bytes[:2048] or b"worksheets" in content_bytes[:2048]:
            return ".xlsx"
        if "word" in ct or "document" in ct or b"word/" in content_bytes[:2048]:
            return ".docx"
        if "presentation" in ct or "powerpoint" in ct or b"ppt/" in content_bytes[:2048]:
            return ".pptx"
        return ".zip"
    if content_bytes.startswith(b"\x1f\x8b"):
        try:
            import zlib
            decomp = zlib.decompressobj(31).decompress(content_bytes[:4096])
            if len(decomp) >= 262 and decomp[257:262] == b"ustar":
                return ".tar.gz"
            if "tsv" in ct or (b"\t" in decomp and b"\n" in decomp):
                return ".tsv.gz"
            if "csv" in ct or (b"," in decomp and b"\n" in decomp):
                return ".csv.gz"
        except Exception:
            pass
        return ".gz"
    # Office 97-2003 compound document (OLE2: .xls, .doc, .ppt)
    if content_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        if "excel" in ct or "spreadsheet" in ct or b"Workbook" in content_bytes[:4096] or b"Book" in content_bytes[:4096]:
            return ".xls"
        if "word" in ct or "msword" in ct or b"WordDocument" in content_bytes[:4096]:
            return ".doc"
        if "powerpoint" in ct or "presentation" in ct or b"PowerPoint Document" in content_bytes[:4096]:
            return ".ppt"
        return ".xls"  # Default for academic supplementary tables
    if content_bytes.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z"
    if content_bytes.startswith(b"Rar!\x1a\x07"):
        return ".rar"
    if content_bytes.startswith(b"\x89HDF\r\n\x1a\n"):
        return ".h5"
    if content_bytes.startswith(b"PAR1"):
        return ".parquet"
    if content_bytes.startswith(b"ARROW1"):
        return ".feather"
    if len(content_bytes) >= 262 and content_bytes[257:262] == b"ustar":
        return ".tar"
    if content_bytes.startswith(b"BZh"):
        return ".bz2"
    if content_bytes.startswith(b"\xfd7zXZ\x00"):
        return ".xz"
    if content_bytes.startswith(b"SQLite format 3\x00"):
        return ".db"
    if "csv" in ct:
        return ".csv"
    if "tab-separated" in ct or "tsv" in ct:
        return ".tsv"
    # Infer .html if HTML content with table structure is returned (D1)
    if "text/html" in ct or b"<html" in content_bytes[:200].lower() or b"<!doctype html" in content_bytes[:200].lower():
        if b"<table" in content_bytes[:4096].lower() or b"<tr" in content_bytes[:4096].lower() or b"<th" in content_bytes[:4096].lower():
            return ".html"
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
    """Check if the string ends with or contains a known data extension. Checks longer extensions first (F3)."""
    parsed = urllib.parse.urlparse(path_or_url)
    path = parsed.path.lower()
    for ext in SORTED_DATA_EXTENSIONS:
        if path.endswith(ext):
            return ext

    # Check query params for extension
    if parsed.query:
        query_lower = parsed.query.lower()
        for ext in SORTED_DATA_EXTENSIONS:
            pattern = re.escape(ext) + r'(?:[&;#"\'\s]|$)'
            if re.search(pattern, query_lower):
                return ext

    # Strict boundary check on full URL
    url_lower = path_or_url.lower()
    for ext in SORTED_DATA_EXTENSIONS:
        pattern = re.escape(ext) + r'(?:[^a-z0-9]|$)'
        if re.search(pattern, url_lower):
            return ext
    return None


def get_nearby_context_text(tag) -> Tuple[str, str]:
    """
    Search nearby headings, captions, or descriptive text enclosing or preceding the tag.
    Returns (heading_or_label_text, source_type).
    Prioritizes text matching TABLE_LABEL_PATTERN without short-circuiting on irrelevant labels.
    Ensures multiple items in the same container do not falsely share an earlier item's title.
    """
    if not tag or not hasattr(tag, "parent"):
        return "", ""

    tag_text = tag.get_text(separator=" ", strip=True)

    # 1. Immediate preceding siblings of the tag itself (dt, td, strong, b, p, headings, div.info)
    if hasattr(tag, "find_previous_siblings"):
        for prev in tag.find_previous_siblings(["h1", "h2", "h3", "h4", "h5", "h6", "p", "dt", "td", "th", "caption", "figcaption", "strong", "b", "div"], limit=5):
            p_text = prev.get_text(separator=" ", strip=True)
            if p_text and p_text != tag_text:
                if TABLE_LABEL_PATTERN.search(p_text):
                    return p_text, f"tag_prev_sibling_{prev.name}"

    # 2. Check closest previous heading in document order that precedes tag
    if hasattr(tag, "find_all_previous"):
        for prev in tag.find_all_previous(["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption", "dt", "th"], limit=4):
            prev_text = prev.get_text(separator=" ", strip=True)
            if prev_text and prev_text != tag_text:
                if TABLE_LABEL_PATTERN.search(prev_text):
                    return prev_text, f"doc_prev_{prev.name}"
                break

    # 3. Upward traversal up to 4 parent layers
    curr = tag.parent
    fallback_text = ""
    fallback_source = ""
    for _ in range(4):
        if not curr or curr.name in ["[document]", "html", "body"]:
            break

        # Check headings inside this container that precede or enclose tag
        headings = curr.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption", "dt", "th"], limit=5)
        for h in headings:
            htext = h.get_text(separator=" ", strip=True)
            if htext and htext != tag_text:
                if TABLE_LABEL_PATTERN.search(htext):
                    # If multiple headings in container, check if h precedes tag
                    if len(headings) > 1 and hasattr(tag, "find_all_previous"):
                        if h in tag.find_all_previous(limit=15):
                            return htext, h.name
                    else:
                        return htext, h.name
                if not fallback_text:
                    fallback_text, fallback_source = htext, h.name

        # Check elements with class indicating title, label, caption, info, or desc
        for el in curr.find_all(lambda e: e != tag and any(c in " ".join(e.get("class", [])).lower() for c in ["title", "caption", "label", "heading", "name", "desc", "info"]), limit=5):
            el_text = el.get_text(separator=" ", strip=True)
            if el_text and el_text != tag_text:
                if TABLE_LABEL_PATTERN.search(el_text):
                    return el_text, "nearby_label"

        # Check previous siblings of container
        if hasattr(curr, "find_previous_siblings"):
            for prev in curr.find_previous_siblings(["h1", "h2", "h3", "h4", "h5", "h6", "p", "dt", "td", "th", "caption", "figcaption", "strong", "b", "div"], limit=5):
                p_text = prev.get_text(separator=" ", strip=True)
                if p_text and p_text != tag_text:
                    if TABLE_LABEL_PATTERN.search(p_text):
                        return p_text, f"container_prev_sibling_{prev.name}"
                    if not fallback_text and prev.name in ["h1", "h2", "h3", "h4", "h5", "h6", "caption", "dt"]:
                        fallback_text, fallback_source = p_text, f"container_prev_sibling_{prev.name}"

        curr = curr.parent

    return fallback_text, fallback_source


def is_table_text_or_attribute(text: str, attrs: Optional[Dict[str, str]] = None, tag=None) -> Tuple[bool, str]:
    """Check if link text, aria-label, title, download attributes, or nearby context indicate a table."""
    candidates_to_check = [text]
    if attrs:
        for attr_key in ["aria-label", "title", "download", "data-filename", "data-title", "data-caption", "value"]:
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
    heading_keywords = [
        "supplement", "suppl", "appendix", "supporting information",
        "supporting info", "additional file", "additional material",
        "additional data", "extended data", "extended table",
        "extended data table", "extended data tables", "extended tables",
        "supplementary table", "supplementary tables",
        "supplemental table", "补充材料", "附表", "附录", "附加材料",
        "支撑材料", "补充表格"
    ]
    supp_attr_keywords = [
        "supplement", "suppl", "appendix", "supporting-info", "supporting_info",
        "esm", "extended-data", "extended_data", "extendeddata",
        "additional-file", "additional-material", "additional-data",
        "data-availability", "supp-table", "supplementary-table",
        "补充", "附表", "附录", "附加", "支撑材料"
    ]

    curr = tag
    steps = 0
    while curr and steps < 6:
        # Check all tag attributes (id, class, data-*, role, aria-label, etc.)
        if hasattr(curr, "attrs") and isinstance(curr.attrs, dict):
            for k, v in curr.attrs.items():
                val_str = " ".join(v) if isinstance(v, list) else str(v)
                combined = f"{k} {val_str}".lower()
                if any(kw in combined for kw in supp_attr_keywords):
                    return True

        # Check headings inside curr if it's a sectioning container
        if hasattr(curr, "name") and curr.name in ["section", "div", "aside", "details", "figure"]:
            for h in curr.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption"], limit=3):
                if any(kw in h.get_text().lower() for kw in heading_keywords):
                    return True

        # Check previous sibling heading
        if hasattr(curr, "find_previous_sibling"):
            prev_h = curr.find_previous_sibling(["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption"])
            if prev_h and any(kw in prev_h.get_text().lower() for kw in heading_keywords):
                return True

        # Check nearby headings in parent
        if curr.parent:
            for sibling in curr.parent.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption"], limit=5):
                heading_text = sibling.get_text().lower()
                if any(kw in heading_text for kw in heading_keywords):
                    return True

        curr = curr.parent
        steps += 1
    return False


# ── Extractor from Controls (<a>, <button>, <meta>, etc.) ───────────────────

def extract_target_url_from_tag(tag) -> Optional[str]:
    """
    Extract target URL from <a>, <button>, or other controls.
    Covers href, data-*, and onclick handlers (Issue #6, B1).
    Excludes in-page hash anchors like #tbl1, #m0001, etc.
    """
    # 1. Standard attributes
    for attr in [
        "href",
        "data",     # <object data="...">
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

    # 2. Inspect onclick handlers (B1)
    onclick = tag.get("onclick")
    if onclick and isinstance(onclick, str):
        # Match location.href, window.location, window.open, dl(...), downloadFile(...)
        # Allow relative paths without leading slash!
        m = re.search(
            r'''(?:location\.href|window\.location(?:\.href)?|window\.open|location|open|dl|download\w*)\s*[=(]\s*['"]([^'"]+)['"]''',
            onclick,
            re.IGNORECASE,
        )
        if m:
            val = m.group(1).strip()
            if val and not val.startswith("#") and not val.startswith("javascript:"):
                return val

        # Fallback: scan any quoted URL/path inside onclick with download/query/data/table characteristics
        for qm in re.finditer(r'''['"]([^'"]+)['"]''', onclick):
            val = qm.group(1).strip()
            if val and not val.startswith("#") and not val.startswith("javascript:"):
                val_lower = val.lower()
                if (
                    "?" in val or
                    check_data_extension(val) or
                    TABLE_FILENAME_PATTERN.search(val) or
                    val_lower.endswith(".pdf") or
                    any(k in val_lower for k in ["/dl", "/download", "/get", "/att/", "/fetch", "table", "supp", "mmc"])
                ):
                    return val

    # 3. Form action if button, input, or container is within a <form action="...">
    form = tag if getattr(tag, "name", "") == "form" else (tag.find_parent("form") if hasattr(tag, "find_parent") else None)
    if form:
        form_action = form.get("action")
        if form_action and isinstance(form_action, str) and not form_action.startswith("javascript:"):
            act_clean = form_action.strip()
            if act_clean and not act_clean.startswith("#"):
                return act_clean

    return None


# ── Manifest & Collision Management (D2) ────────────────────────────────────

def load_download_manifest(output_dir: str) -> Dict[str, str]:
    """Load download manifest mapping filename -> URL (D2)."""
    manifest_path = os.path.join(output_dir, ".download_manifest.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_download_manifest(output_dir: str, manifest: Dict[str, str]):
    """Save download manifest mapping filename -> URL (D2)."""
    manifest_path = os.path.join(output_dir, ".download_manifest.json")
    try:
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def disambiguate_target_filename(output_dir: str, fname: str, url: str, manifest: Dict[str, str]) -> str:
    """
    Ensure fname does not collide with an existing file on disk downloaded for a different URL (D2).
    """
    target = fname
    base, ext = os.path.splitext(target)
    idx = 2
    while os.path.exists(os.path.join(output_dir, target)):
        # If this exact file on disk was downloaded for this exact URL, no rename needed
        if manifest.get(target) == url:
            break
        target = f"{base}_{idx}{ext}"
        idx += 1
    return target


# ── Unified Candidate Matcher (F1) ──────────────────────────────────────────

def should_match_candidate(
    url: str,
    text: str = "",
    attrs: Optional[Dict[str, str]] = None,
    is_supp_section: bool = False,
    tag_name: str = "a",
    tag=None,
) -> Tuple[bool, str]:
    """
    Unified candidate matching logic (F1).
    Evaluates whether a given URL, link text, attributes, and context match
    a supplementary data/table file.
    Returns (matched: bool, rule: str).
    """
    if not url:
        return False, ""

    attrs = attrs or {}
    url_lower = url.lower()
    parsed = urllib.parse.urlparse(url)
    path_lower = parsed.path.lower()
    fname = attrs.get("download") or extract_filename_from_url(url)
    data_ext = check_data_extension(url)
    is_table, table_rule = is_table_text_or_attribute(text, attrs, tag=tag)
    is_table_filename = bool(TABLE_FILENAME_PATTERN.search(fname)) or bool(TABLE_FILENAME_PATTERN.search(path_lower))

    # Case A: Elsevier MMC link
    if MMC_PATTERN.search(url_lower):
        return True, "mmc_pattern"

    # Case B: Standard data extension (.xlsx, .csv, .zip, etc.)
    if data_ext:
        return True, f"data_extension:{data_ext}"

    # Case C: Table label detected in text, attributes, or nearby heading (Issue #2, #3, A3, A4)
    if is_table:
        is_explicit_supp = is_explicit_supp_table(text) or any(is_explicit_supp_table(v) for v in attrs.values())
        if not is_explicit_supp and ":" in table_rule:
            is_explicit_supp = is_explicit_supp_table(table_rule.split(":", 1)[1])

        check_label = (text or "") + " " + (table_rule or "")
        is_letter_table = bool(re.search(r'\b(?:tables?|tabs?\.?|tbls?\.?)\s+[A-Z][-_.]?\d+\b', check_label, re.IGNORECASE))
        has_appendix_context = "appendix" in url_lower or "supp" in url_lower or "appendix" in check_label.lower() or "supp" in check_label.lower()

        path_basename = os.path.basename(path_lower.rstrip("/"))
        has_query_or_endpoint = (
            bool(parsed.query) or
            path_basename in GENERIC_PATH_BASENAMES or
            any(seg in path_lower for seg in ["/fetch", "/stream", "/export", "/api/", "/assets/", "/files/", "/download", "/att/", "/get/", "/attachment/"]) or
            tag_name in ["button", "input"] or
            (tag is not None and (tag.has_attr("data-url") or tag.has_attr("onclick")))
        )

        is_html = any(path_lower.endswith(h_ext) for h_ext in HTML_EXTENSIONS)
        if is_html:
            if is_supp_section or is_explicit_supp or is_table_filename or "download" in attrs:
                return True, f"html_table_attachment ({table_rule})"
        elif data_ext or path_lower.endswith(".pdf") or is_supp_section or "download" in attrs or is_table_filename:
            if not is_explicit_supp and not is_supp_section and not data_ext and not is_table_filename:
                if is_letter_table and (has_appendix_context or path_lower.endswith(".pdf") or data_ext):
                    return True, table_rule
            else:
                return True, table_rule
        elif not any(path_lower.endswith(h) for h in HTML_EXTENSIONS):
            if is_explicit_supp or is_supp_section:
                return True, f"table_endpoint ({table_rule})"
            elif is_letter_table and has_appendix_context and has_query_or_endpoint:
                return True, f"table_endpoint ({table_rule})"

    # Case D: Table filename pattern (e.g. table-s1.pdf, tbl_s2.xlsx)
    if is_table_filename:
        return True, "table_filename_pattern"

    # Case E: PDF in supplementary context or with supp keywords (Issue #3)
    # Also support Wiley direct download links (even without .pdf extension) via anchor text or supp keywords
    is_pdf = path_lower.endswith(".pdf")
    is_wiley_dl = (
        "downloadsupplement" in path_lower
        or "downloadsupplement" in url_lower
        or ("wiley.com" in url_lower and not any(path_lower.endswith(h) for h in HTML_EXTENSIONS))
    )

    if is_pdf or is_wiley_dl:
        attrs_text = " ".join(str(v) for v in attrs.values() if isinstance(v, (str, list)))
        check_supp_text = f"{text} {attrs_text}"
        if SUPP_KEYWORD_PATTERN.search(url_lower):
            return True, "pdf_with_supplementary_keyword" if is_pdf else "wiley_supp_link"
        if SUPP_KEYWORD_PATTERN.search(check_supp_text):
            return True, "pdf_with_supplementary_keyword" if is_pdf else "wiley_supp_link"
        if is_supp_section:
            return True, "pdf_in_supplementary_section" if is_pdf else "wiley_supp_link"

    # Case F: Control inside supplementary section with download indicator
    if is_supp_section:
        if "download" in attrs or any(kw in text.lower() for kw in ["download", "pdf", "file", "table"]):
            if not any(path_lower.endswith(h_ext) for h_ext in HTML_EXTENSIONS) or "table" in text.lower():
                return True, "download_control_in_supplementary_section"

    # Case G: MDPI style /s1, /s2
    if re.search(r'/s\d+/?$', path_lower):
        return True, "mdpi_supp_path"

    # Case H: NCBI / PMC / Europe PMC binary attachment link (/bin/ or click_feat_suppl)
    if ("/bin/" in path_lower or "/bin/" in url_lower) and any(d in url_lower for d in ["ncbi.nlm.nih.gov", "pmc", "europepmc"]):
        if is_article_figure_url(url):
            return False, ""
        return True, "pmc_bin_attachment"

    return False, ""


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

    soup = BeautifulSoup(html_content, DEFAULT_PARSER)
    seen_urls: Set[str] = set()
    candidates: List[Candidate] = []

    def add_candidate(cand: Candidate):
        norm_url = cand.url.split("#")[0].strip()
        if not norm_url:
            return
        if is_excluded_url(norm_url, base_url=effective_base):
            diag.excluded_links.append((norm_url, "excluded_url_pattern"))
            return
        if is_article_figure_url(norm_url):
            diag.excluded_links.append((norm_url, "article_figure_pattern"))
            return
        if norm_url in seen_urls:
            # Upgrade existing candidate if this one has a more specific filename
            for i, existing in enumerate(candidates):
                if existing.url.split("#")[0].strip() == norm_url:
                    is_existing_generic = (
                        existing.filename.endswith(".bin")
                        or existing.filename.startswith("download_")
                        or existing.filename.startswith("attachment_")
                    )
                    is_cand_specific = not (
                        cand.filename.endswith(".bin")
                        or cand.filename.startswith("download_")
                        or cand.filename.startswith("attachment_")
                    )
                    if is_existing_generic and is_cand_specific:
                        candidates[i] = cand
                    break
            return
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
                fname = extract_filename_from_url(full_url)
                add_candidate(Candidate(
                    url=full_url,
                    filename=fname,
                    link_text=name,
                    section="meta_tags",
                    match_rule="meta_supplementary_tag",
                    source_tag=meta.name,
                ))

    # ── Strategy 2: Scan all clickable controls (<a>, <button>, <input>, etc.) (B2) ──
    controls = soup.find_all(["a", "button", "form", "div", "span", "input", "li", "td", "th", "iframe", "embed", "object", "area"])
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

        link_text = tag.get("value", "") if tag.name == "input" else tag.get_text(separator=" ", strip=True)
        attrs = {k: tag[k] for k in ["aria-label", "title", "download", "data-filename", "data-title", "data-caption", "value"] if tag.has_attr(k)}
        default_candidate_name = attrs.get("data-filename") or attrs.get("title") or link_text
        fname = attrs.get("download") or extract_filename_from_url(full_url, default_name=default_candidate_name)
        parsed = urllib.parse.urlparse(full_url)
        path_lower = parsed.path.lower()
        url_lower = full_url.lower()

        is_supp_section = is_supplementary_container(tag)
        is_table, table_rule = is_table_text_or_attribute(link_text, attrs, tag=tag)

        matched, rule = should_match_candidate(
            full_url,
            text=link_text,
            attrs=attrs,
            is_supp_section=is_supp_section,
            tag_name=tag.name,
            tag=tag,
        )

        # If matched via table rule and fname is generic, derive a clean name from the table rule
        if matched and is_table:
            matched_label = table_rule.split(":", 1)[1] if ":" in table_rule else table_rule
            if (
                fname.startswith(("download", "attachment", "endpoint", "asset", "data-asset", "file", "item", "stream"))
                or fname.endswith(".bin")
                or fname in GENERIC_PATH_BASENAMES
                or "." not in fname
            ):
                clean_def = re.sub(r'\s*\((pdf|xlsx|xls|docx|doc|csv|zip|tar\.gz|gz|tsv)\)\s*$', r'.\1', default_candidate_name, flags=re.IGNORECASE)
                inferred_ext = check_data_extension(clean_def) or (
                    ".pdf" if clean_def.lower().endswith(".pdf") else ""
                )
                ext_part = os.path.splitext(path_lower)[1] or inferred_ext or ".bin"
                fname = sanitize_filename(f"{matched_label}{ext_part}")

        section = "supplementary_section" if is_supp_section else "page_body"

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

    # NCBI / PMC / Europe PMC supplementary files
    if any(d in effective_base for d in ["ncbi.nlm.nih.gov", "pmc.ncbi.nlm.nih.gov", "europepmc.org"]):
        _extract_pmc_candidates(soup, effective_base, add_candidate)

    # Embedded JSON data structures (Next.js / Nuxt / Publisher JSON payloads)
    _extract_embedded_json_candidates(soup, effective_base, add_candidate)

    # Disambiguate identical filenames across different candidates (D2)
    # Use case-insensitive tracking to prevent collisions on case-insensitive filesystems (macOS/Windows)
    seen_fnames_lower: Set[str] = set()
    for cand in candidates:
        fn = cand.filename
        fn_lower = fn.lower()
        if fn_lower in seen_fnames_lower:
            base_fn, ext_fn = os.path.splitext(fn)
            idx = 2
            while f"{base_fn}_{idx}{ext_fn}".lower() in seen_fnames_lower:
                idx += 1
            cand.filename = f"{base_fn}_{idx}{ext_fn}"
            seen_fnames_lower.add(cand.filename.lower())
        else:
            seen_fnames_lower.add(fn_lower)

    # ── Diagnostics & Completeness Check (Issue #4) ────────────────────────
    diag.candidates = candidates
    diag.declared_count = detect_declared_supplement_count(html_content)
    is_inc, inc_reason = is_result_obviously_incomplete(candidates, soup, diag.declared_count)
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
    """Springer, Nature, and EMBO supplementary & source data file extraction."""
    for a in soup.find_all("a", attrs={"data-track-action": "download"}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            url_lower = full_url.lower()
            link_text = a.get_text(strip=True)
            text_lower = link_text.lower()
            # Exclude main article PDF: /content/pdf/ without /esm/, or text containing 'download article pdf'
            if "/content/pdf/" in url_lower and "/esm/" not in url_lower:
                continue
            if "download article pdf" in text_lower or text_lower == "download pdf":
                continue
            add_func(Candidate(
                url=full_url,
                filename=extract_filename_from_url(full_url, default_name=link_text),
                link_text=link_text,
                section="springer_nature_download",
                match_rule="springer_data_track_download",
            ))

    # Nature supplementary file links
    for a in soup.find_all("a", attrs={"data-test": "supplementary-file-link"}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            add_func(Candidate(
                url=full_url,
                filename=extract_filename_from_url(full_url),
                link_text=a.get_text(strip=True),
                section="nature_supplementary",
                match_rule="nature_data_test_supp_link",
            ))

    # Nature / EMBO Source Data links (data-test="source-data-link" or "source-data")
    for a in soup.find_all(["a", "button"], attrs={"data-test": re.compile(r"source[-_]?data(?:[-_]?link)?", re.IGNORECASE)}):
        target = extract_target_url_from_tag(a)
        if target:
            full_url = urllib.parse.urljoin(base_url, target)
            link_text = a.get_text(separator=" ", strip=True) or "Source Data"
            add_func(Candidate(
                url=full_url,
                filename=extract_filename_from_url(full_url, default_name=link_text),
                link_text=link_text,
                section="nature_source_data",
                match_rule="nature_data_test_source_data_link",
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
        # Exclude printable/main article PDF
        if "type=printable" in full_url.lower():
            continue
        link_text = a.get_text(strip=True)
        fname = extract_filename_from_url(full_url, default_name=link_text)
        add_func(Candidate(
            url=full_url,
            filename=fname,
            link_text=link_text,
            section="plos_supplement",
            match_rule="plos_article_file_action",
        ))


def _extract_pmc_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """PMC (PubMed Central) and Europe PMC supplementary material extraction."""
    # 1. Links inside PMC supplementary sections
    for sec in soup.find_all(
        lambda t: t.name in ["section", "div"]
        and any(
            c in " ".join(t.get("class", [])).lower()
            for c in ["supplementary-materials", "supplementary-material", "suppl-data", "suppl-mat", "sm xbox"]
        )
    ):
        for a in sec.find_all(["a", "button"]):
            target = extract_target_url_from_tag(a)
            if not target:
                continue
            full_url = urllib.parse.urljoin(base_url, target)
            if is_excluded_url(full_url, base_url=base_url) or is_article_figure_url(full_url):
                continue
            link_text = a.get_text(separator=" ", strip=True)
            fname = a.get("download") or extract_filename_from_url(full_url, default_name=link_text)
            add_func(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=link_text,
                section="pmc_supplementary_section",
                match_rule="pmc_supp_section_link",
                source_tag=a.name,
            ))

    # 2. Direct binary links (/bin/ or click_feat_suppl)
    for a in soup.find_all("a", href=re.compile(r"/bin/", re.IGNORECASE)):
        href = a.get("href")
        full_url = urllib.parse.urljoin(base_url, href)
        if is_excluded_url(full_url, base_url=base_url) or is_article_figure_url(full_url):
            continue
        link_text = a.get_text(separator=" ", strip=True)
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=link_text)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=link_text,
            section="pmc_bin_attachment",
            match_rule="pmc_bin_link",
            source_tag=a.name,
        ))

    # 3. Attributes specific to PMC
    for a in soup.find_all("a", attrs={"data-ga-action": re.compile(r"click_(?:feat_)?suppl", re.IGNORECASE)}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            if is_excluded_url(full_url, base_url=base_url) or is_article_figure_url(full_url):
                continue
            link_text = a.get_text(separator=" ", strip=True)
            fname = a.get("download") or extract_filename_from_url(full_url, default_name=link_text)
            add_func(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=link_text,
                section="pmc_featured_suppl",
                match_rule="pmc_feat_suppl_link",
                source_tag=a.name,
            ))


def _extract_embedded_json_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Extract supplementary URLs embedded inside <script> JSON, LD-JSON, or inline scripts (B3)."""
    valid_data_exts = "|".join(re.escape(ext.lstrip(".")) for ext in SORTED_DATA_EXTENSIONS)
    pattern = re.compile(
        r'''["']('''
        # 1. Scientific data file (allowing spaces in filename, e.g. "/files/Supplementary Table S1.xlsx")
        r'''[^"'\r\n<>]+\.(?:''' + valid_data_exts + r''')'''
        r'''|'''
        # 2. Supplementary PDF (allowing spaces in filename)
        r'''[^"'\r\n<>]*(?:supp|table|appendix|附表|附录|esm|mmc|source[-_]?data)[^"'\r\n<>]+\.pdf'''
        r'''|'''
        # 3. Explicit supplementary download endpoints (MMC, ESM, or download paths with supp keywords)
        r'''(?:https?://|/|\./)[^"'\r\n<>]*(?:mmc[-_]?\d+|esm[-_]?\d+|\b(?:suppl?|supplementary|appendix|source[-_]?data)\b)[^"'\r\n<>]*'''
        r''')["']''',
        re.IGNORECASE,
    )

    for script in soup.find_all("script"):
        stype = (script.get("type") or "").lower()
        if stype and not any(t in stype for t in ["json", "javascript", "ecmascript"]):
            continue

        content = script.get_text() if hasattr(script, "get_text") else (script.string or "")
        if not content or len(content) > 5000000:
            continue

        # Unescape slashes for JSON strings (\/ -> /)
        content_unescaped = content.replace(r"\/", "/")

        if any(kw in content_unescaped.lower() for kw in ["mmc", "supplement", "supp", "table", "dataset", "attached", "asset", "附表", "附录", "source data", "source_data", "sourcedata"]):
            # Match data files, supplementary PDFs, or paths with mmc/table/supp indicators
            for m in pattern.finditer(content_unescaped):
                cand_raw = m.group(1).strip()
                cand_url = urllib.parse.urljoin(base_url, cand_raw)
                cand_path = urllib.parse.urlsplit(cand_url).path.lower()
                # Exclude frontend static web assets
                if any(cand_path.endswith(ext) for ext in [
                    ".js", ".css", ".map", ".svg", ".json", ".html", ".htm",
                    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf"
                ]):
                    continue
                # Exclude navigation/UI routes
                if any(kw in cand_path for kw in ["table-of-contents", "table_of_contents", "/support", "customer-support", "/toc"]):
                    continue
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
    Detect whether the article page declares a specific count of supplementary files (E1).
    e.g., '4 Supplementary Files', 'Supplementary Tables S1-S5', 'Supporting Information (3)',
    '共 4 个补充文件', '附表 1-5', '附表1至附表6', '附录 1-4', 'Supplementary Table S1 and S2',
    'Tables S1-S3 in Supplementary Information', 'see Supplementary Tables 2-5',
    'Appendix 1-4', 'Data S1-S3', 'Tables S1, S2, and S3'.
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

    # Pattern 3a: English Table Range: 'Tables S1-S3', 'Supplementary Tables 2-5', 'Tables S1 to S4' (E1)
    # Modifier and 'S' cannot both be omitted (e.g. 'Tables 1 to 3' is main text, not supplementary)
    m = re.search(
        r'(?:(?:supplementary|supporting|extended\s*data|appendix)\s+(?:(?:figures?|figs?\.?)\s+(?:and|&)\s+)?tables?(?:\s+(?:and|&)\s+(?:figures?|figs?\.?))?\s+s?0?(\d{1,2})|tables?\s+s0?(\d{1,2}))\s*(?:[-–至~到]|to)\s*s?0?(\d{1,2})\b',
        html_content,
        re.IGNORECASE,
    )
    if m:
        try:
            start_num = int(m.group(1) or m.group(2))
            end_num = int(m.group(3))
            if end_num >= start_num:
                return end_num - start_num + 1
        except ValueError:
            pass

    # Pattern 3b: Standalone Appendix range: 'Appendix 1-4', 'Appendix 1 to 5' (A1, E1)
    m = re.search(
        r'\bappendix\s+0?(\d{1,2})\s*(?:[-–至~到]|to)\s*0?(\d{1,2})\b',
        html_content,
        re.IGNORECASE,
    )
    if m:
        try:
            start_num = int(m.group(1))
            end_num = int(m.group(2))
            if end_num >= start_num:
                return end_num - start_num + 1
        except ValueError:
            pass

    # Pattern 3c: Data/Dataset range: 'Data S1-S3', 'Supplementary Data S1-S4', 'Dataset 1 to 4' (A3, E1)
    m = re.search(
        r'(?:(?:supplementary|supporting)\s+(?:datasets?|data|files?)\s+s?0?(\d{1,2})|(?:datasets?|data|files?)\s+s0?(\d{1,2}))\s*(?:[-–至~到]|to)\s*s?0?(\d{1,2})\b',
        html_content,
        re.IGNORECASE,
    )
    if m:
        try:
            start_num = int(m.group(1) or m.group(2))
            end_num = int(m.group(3))
            if end_num >= start_num:
                return end_num - start_num + 1
        except ValueError:
            pass

    # Helper for converting Arabic or Chinese numerals (一二三四五六七八九十)
    cn_digits = {
        '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
        '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
    }
    def _parse_count_val(s: str) -> Optional[int]:
        if not s:
            return None
        s = s.strip()
        if s.isdigit():
            return int(s)
        if s in cn_digits:
            return cn_digits[s]
        if s.startswith('十') and len(s) == 2 and s[1] in cn_digits:
            return 10 + cn_digits[s[1]]
        return None

    # Pattern 4: Chinese range: '附表 1-5', '附表1至附表6', '补充表 S1-S6', '附录 1-4', '附表一至附表四'
    m = re.search(
        r'(?:附表|补充表|附录表|补充表格|附录)\s*s?0?([0-9一二两三四五六七八九十]+)\s*(?:[-–至~到]|to)\s*(?:附表|补充表|附录表|补充表格|附录)?\s*s?0?([0-9一二两三四五六七八九十]+)',
        html_content,
        re.IGNORECASE,
    )
    if m:
        s_num = _parse_count_val(m.group(1))
        e_num = _parse_count_val(m.group(2))
        if s_num is not None and e_num is not None and e_num >= s_num:
            return e_num - s_num + 1

    # Pattern 5: Chinese counts: '共 4 个补充文件', '4 个附表', '3个附件', '含 5 个附表', '共四个补充文件'
    m = re.search(r'(?:共|含|包括)?\s*([0-9一二两三四五六七八九十]+)\s*个?(?:补充|附录|附加)?(?:文件|材料|表格|数据|附件|附表|补充表)', html_content)
    if m:
        c_num = _parse_count_val(m.group(1))
        if c_num is not None and c_num >= 1:
            return c_num

    # Pattern 6a: List of tables: 'Supplementary Table S1 and S2', 'Tables S1, S2 and S3', 'Tables S1, S2, and S3' (E1)
    m_list = re.search(
        r'(?:(?:supplementary|supporting|extended\s*data|appendix)\s+tables?\s+s?0?\d{1,2}(?:\s*,\s*s?0?\d{1,2})*(?:\s*,)?\s+(?:and|&)\s+s?0?\d{1,2}\b'
        r'|tables?\s+s0?\d{1,2}(?:\s*,\s*s?0?\d{1,2})*(?:\s*,)?\s+(?:and|&)\s+s?0?\d{1,2}\b)',
        html_content,
        re.IGNORECASE,
    )
    if m_list:
        nums = re.findall(r'\d{1,2}', m_list.group(0))
        if nums:
            return len(nums)

    # Pattern 6b: List of data files: 'Data S1 and S2', 'Supplementary Data S1, S2, and S3' (E1)
    m_data_list = re.search(
        r'(?:(?:supplementary|supporting)\s+(?:datasets?|data|files?)\s+s?0?\d{1,2}(?:\s*,\s*s?0?\d{1,2})*(?:\s*,)?\s+(?:and|&)\s+s?0?\d{1,2}\b'
        r'|(?:datasets?|data|files?)\s+s0?\d{1,2}(?:\s*,\s*s?0?\d{1,2})*(?:\s*,)?\s+(?:and|&)\s+s?0?\d{1,2}\b)',
        html_content,
        re.IGNORECASE,
    )
    if m_data_list:
        nums = re.findall(r'\d{1,2}', m_data_list.group(0))
        if nums:
            return len(nums)

    # Pattern 7: Chinese table/appendix list: '附表1、附表2和附表3', '附录1、附录2及附录3' (E1)
    m_cn_list = re.search(
        r'(?:附表|补充表|附录)\s*\d{1,2}(?:[、,，]\s*(?:附表|补充表|附录)?\s*\d{1,2})*\s*(?:和|及|与)\s*(?:附表|补充表|附录)?\s*(\d{1,2})',
        html_content,
    )
    if m_cn_list:
        nums = re.findall(r'\d{1,2}', m_cn_list.group(0))
        if nums:
            return len(nums)

    # Pattern 8: Highest explicit supplementary table mention
    # e.g. "see Supplementary Table S4", "Table S5", "S4 Table", "附表4", "附表四"
    # Requires explicit supplementary prefix or 'S' numbering to avoid matching bare primary 'Table 1-3'.
    m_high = re.findall(
        r'(?:(?:supplementary|supporting|extended\s*data)\s+tables?\s+s?0?(\d{1,2})'
        r'|tables?\s+s0?(\d{1,2})'
        r'|\bs0?(\d{1,2})\s+(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?)'
        r'|附表\s*s?0?([0-9一二两三四五六七八九十]+))\b',
        html_content,
        re.IGNORECASE,
    )
    if m_high:
        nums = []
        for g in m_high:
            for val in g:
                if val:
                    n = _parse_count_val(val)
                    if n is not None and 1 <= n <= 99:
                        nums.append(n)
        if nums:
            max_val = max(nums)
            if max_val >= 2:
                return max_val

    return None


def is_result_obviously_incomplete(
    candidates: List[Candidate],
    html_content: Union[str, BeautifulSoup],
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

    # Check if a supplementary section exists, but no candidates came from it (E2)
    if isinstance(html_content, BeautifulSoup):
        soup = html_content
    else:
        soup = BeautifulSoup(html_content, DEFAULT_PARSER)
    supp_keywords = [
        "supplement", "suppl", "appendix", "supporting-info", "supporting_info",
        "esm", "additional-file", "supplementary-material", "supplementary-data",
        "extended-data", "extended_data", "补充", "附表", "附录", "支撑材料",
    ]
    supp_sections = soup.find_all(lambda el: el.name in ["section", "div"] and any(
        kw in (el.get("id", "") + " " + " ".join(el.get("class", []))).lower()
        for kw in supp_keywords
    ))
    if supp_sections:
        cands_in_section = [c for c in candidates if c.section not in ["page_body", ""]]
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

    # Legacy Office 97-2003 (OLE2: .xls, .doc, .ppt): \xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1
    if header_bytes.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return True, f"Valid legacy Office 97-2003 (OLE2 .xls/.doc/.ppt) signature ({file_size} bytes)"

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

        # If it's an HTML file and NOT an error page, check if it contains a table structure (D1)
        if ext in HTML_EXTENSIONS or ext == ".bin" or not ext:
            html_sample = content_bytes[:65536] if content_bytes is not None else b""
            if not html_sample and os.path.exists(filepath):
                try:
                    with open(filepath, "rb") as f:
                        html_sample = f.read(65536)
                except Exception:
                    html_sample = header_bytes
            sample_lower = html_sample.lower()
            if b"<table" in sample_lower or b"<tr" in sample_lower or b"<th" in sample_lower:
                return True, f"Valid standalone HTML table attachment ({file_size} bytes)"
            if ext in HTML_EXTENSIONS:
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


# ── Title Extraction & Sanitization ──────────────────────────────────────────

def extract_article_title(page_or_html, default: str = "unknown_article") -> str:
    """Extract article title, unescape HTML entities, and sanitize for use as folder name."""
    if page_or_html is None:
        return default

    title = None
    if hasattr(page_or_html, "css"):
        try:
            title = page_or_html.css('title::text').get()
        except Exception:
            title = None

    if not title:
        m = re.search(r'<title[^>]*>(.*?)</title>', str(page_or_html), re.IGNORECASE | re.DOTALL)
        title = m.group(1) if m else None

    if not title:
        return default

    # Unescape HTML entities (e.g. &amp; -> &, &quot; -> ", &#39; -> ')
    title = html.unescape(title)

    for suffix in [
        " - ScienceDirect", " - SpringerLink", " - Springer",
        " | Nature", " | PNAS", " - Wiley Online Library",
        " - PubMed", " - PubMed Central", " - PMC",
        " | Oxford Academic", " - IEEE Xplore",
    ]:
        title = title.replace(suffix, "")

    title = title.strip()
    title = re.sub(r'[\\/*?:"<>|]', "_", title)
    title = re.sub(r'\s+', ' ', title)
    if len(title) > 120:
        title = title[:120]
    title = title.rstrip(". ")
    return title or default


def expand_page_accordions_action(page):
    """Playwright page action for Scrapling: expand details, tabs, and accordion panels."""
    try:
        if hasattr(page, "evaluate"):
            page.evaluate("""() => {
                // 1. Open all <details> elements
                document.querySelectorAll('details').forEach(d => { d.open = true; });

                // 2. Click buttons or tabs with aria-expanded="false" or supp-related classes/ids
                const suppKeywords = ['supp', 'appendi', 'extended', 'additional', 'data', 'table', 'material'];
                const elements = document.querySelectorAll('button[aria-expanded="false"], [role="tab"][aria-expanded="false"], summary, [data-toggle="collapse"], .accordion-header, .accordion-button');
                elements.forEach(el => {
                    const text = (el.innerText || el.textContent || '').toLowerCase();
                    const idClass = ((el.id || '') + ' ' + (el.className || '')).toLowerCase();
                    if (suppKeywords.some(kw => text.includes(kw) || idClass.includes(kw)) || el.tagName.toLowerCase() === 'summary') {
                        try { el.click(); } catch (e) {}
                    }
                });

                // 3. Set aria-expanded="true" on any remaining collapsed sections
                document.querySelectorAll('[aria-expanded="false"]').forEach(el => {
                    const text = (el.innerText || el.textContent || '').toLowerCase();
                    if (suppKeywords.some(kw => text.includes(kw))) {
                        el.setAttribute('aria-expanded', 'true');
                    }
                });

                // 4. Scroll down to trigger lazy loading
                window.scrollTo(0, document.body.scrollHeight / 2);
                window.scrollTo(0, document.body.scrollHeight);
            }""")
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(1000)
    except Exception:
        pass

