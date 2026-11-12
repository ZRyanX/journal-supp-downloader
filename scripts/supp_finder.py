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
import sys
import tempfile
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union
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
# Supplementary Information Table S1, Supplemental Material Table 2, ESM Table 1, SI Table S1,
# Table S1.1, Source Data Fig. 1, Extended Data Table 1, 补充表 S1, 附表 1, 附表一, Table SI, etc.
_SUPP_PREFIX_RE = (
    r"(?:supplementary|supplemental|supporting|extended\s*data|ext\s*data|additional|appendix|online|suppl?\.?|esm|si|web)"
    r"(?:\s+(?:information|info|materials?|data))?"
)
_HIER_NUM_RE = r"[a-zA-Z]?[-_.]?\d+(?:[._-]\d+)*[a-zA-Z]?"

TABLE_LABEL_PATTERN = re.compile(
    r"(?:"
    # Branch 1: Explicit supplementary prefix with table/data (number optional, supports hierarchical numbers, Roman numerals, and ranges)
    r"(?:" + _SUPP_PREFIX_RE + r"\s+"
    r"(?:(?:figures?|figs?\.?)\s+(?:and|&)\s+)?(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|表格?|数据)(?:\s+(?:and|&)\s+(?:figures?|figs?\.?))?"
    r"(?:\s*(?:" + _HIER_NUM_RE + r"(?:\s*(?:[-–至]|to)\s*" + _HIER_NUM_RE + r")?|[sS]?[-_.]?[ivxldcmIVXLDCM]+|[a-zA-Z]\b))?)"
    r"|"
    # Branch 1a2: Explicit supplementary prefix with file/文件 (number is required, e.g. Additional file 1, Supplementary File S1, ESM File 1)
    r"(?:" + _SUPP_PREFIX_RE + r"\s+"
    r"(?:files?|文件)\s+(?:" + _HIER_NUM_RE + r"(?:\s*(?:[-–至]|to)\s*" + _HIER_NUM_RE + r")?|[sS]?[-_.]?[ivxldcmIVXLDCM]+|[a-zA-Z]\b))"
    r"|"
    # Branch 1b: Standalone Appendix with identifier (e.g. Appendix 1, Appendix-1, Appendix A, Appendix 1-3)
    r"(?:\bappendix[-_\s]*(?:tables?|tabs?\.?|tbls?\.?|[a-zA-Z0-9]+(?:[._-]\d+)*(?:\s*(?:[-–至]|to)\s*[a-zA-Z0-9]+(?:[._-]\d+)*)?)\b)"
    r"|"
    # Branch 1c: eTable / STable / S-Table format (e.g. eTable 1, eTable1, e-Table S1, STable 1, S-Table 2)
    r"(?:\b(?:e[-_]?|s[-_]?)(?:tables?|tabs?\.?|tbls?\.?)(?:\s*" + _HIER_NUM_RE + r"(?:\s*(?:[-–至]|to)\s*" + _HIER_NUM_RE + r")?)?\b)"
    r"|"
    # Branch 1d: Source Data format (e.g. Source Data, Source Data Fig. 1, Source Data Table 1)
    r"(?:\bsource[-_ ]?data(?:\s+(?:tables?|tabs?\.?|tbls?\.?|figs?\.?|figures?|files?)?(?:\s*" + _HIER_NUM_RE + r")?)?\b)"
    r"|"
    # Branch 1e: Pre-numbered S format (e.g. S1 Table, S1.1 Table, S2 Tables, S1 Data, S1 Dataset, S1 File)
    r"(?:\bs\d+(?:[._-]\d+)*[a-zA-Z]?[-_.\s]+(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?|表格?|数据|文件)\b)"
    r"|"
    # Branch 2: Chinese supplementary table/data terms (supports Chinese digits 一二三四五六七八九十)
    r"(?:(?:补充|附录|附加)?(?:附表|补充表|附录表|附加表|补充数据|附录数据|补充文件|表格)(?:\s*(?:[a-zA-Z]?[-_.]?[0-9一二三四五六七八九十]+(?:[._-]\d+)*[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*[a-zA-Z]?[-_.]?[0-9一二三四五六七八九十]+(?:[._-]\d+)*[a-zA-Z]?)?))?)"
    r"|"
    r"(?:附录\s*[a-zA-Z0-9一二三四五六七八九十]+(?:\s*(?:[-–至]|to)\s*[a-zA-Z0-9一二三四五六七八九十]+)?)"
    r"|"
    # Branch 3: Standard Table/Tab with identifier (S1, S1.1, 1, 1.1, A1, A.1, SI, SII, etc.) or range (S1-S4, 1-5)
    r"(?:(?:tables?|tabs?\.?|tbls?\.?)\s+(?:" + _HIER_NUM_RE + r"|[sS]?[-_.]?[ivxldcmIVXLDCM]+)(?:\s*(?:[-–至]|to)\s*(?:" + _HIER_NUM_RE + r"|[sS]?[-_.]?[ivxldcmIVXLDCM]+))?)"
    r"|"
    # Branch 4: Data S1, Dataset S1, File S1, Data SI, etc.
    r"(?:(?:datasets?|data|files?)\s+(?:s\d+(?:[._-]\d+)*|s[-_.]?[ivxldcm]+)[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*(?:s?\d+(?:[._-]\d+)*|s?[-_.]?[ivxldcm]+)[a-zA-Z]?)?\b)"
    r")",
    re.IGNORECASE,
)

# Matches table filenames (including hyphens, ranges, eTable, STable, appendix, intermediate words, hierarchical numbers, and Chinese)
# e.g., table-s1.pdf, s1-table.pdf, table_s1.1.xlsx, supp_info_table_1.csv, supp_tables.pdf,
# extended-data-table-2.pdf, tbl_s2.pdf, source-data-fig1.xlsx, 附表一.xlsx
TABLE_FILENAME_PATTERN = re.compile(
    r"(?:^|[\W_])"
    r"(?:"
    r"(?:(?:supplementary|supplemental|supporting|extended[-_]?data|ext[-_]?data|additional|appendix|online|suppl?|esm|si|web)"
    r"(?:[-_.]*(?:information|info|materials?|data))?[-_.]*(?:tables?|tbls?|tabs?|附表|补充表|附录表|附加表|表格)"
    r"(?:[-_.]*[a-z]?[-_.]?\d+(?:[._-]\d+)*(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+(?:[._-]\d+)*)?)?)"
    r"|"
    r"(?:(?:e|s)[-_]?(?:tables?|tbls?|tabs?)[-_.]*[a-z]?[-_.]?\d+(?:[._-]\d+)*(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+(?:[._-]\d+)*)?)"
    r"|"
    r"(?:appendix[-_.]*(?:tables?|tbls?|tabs?)?[-_.]*[a-z0-9]+(?:[._-]\d+)*(?:[-_.]*(?:to|[-–至])[-_.]*[a-z0-9]+(?:[._-]\d+)*)?)"
    r"|"
    r"(?:\bs\d+(?:[._-]\d+)*[a-z]?[-_.]*(?:tables?|tbls?|tabs?|datasets?|data|files?))"
    r"|"
    r"(?:source[-_.]?data(?:[-_.](?:tables?|tabs?|tbls?|figs?|figures?|files?)?[-_.]*[a-z0-9]+(?:[._-]\d+)*)?)"
    r"|"
    r"(?:(?:附表|补充表|附录表|附加表|附录)(?:[-_.]*[a-z]?[-_.]?[0-9一二三四五六七八九十]+(?:[._-]\d+)*(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?[0-9一二三四五六七八九十]+(?:[._-]\d+)*)?)?)"
    r"|"
    r"(?:(?:tables?|tbls?|tabs?)[-_.]*(?:[a-z]?[-_.]?\d+(?:[._-]\d+)*|[sS]?[-_.]*[ivxldcm]+)(?:[-_.]*(?:to|[-–至])[-_.]*(?:[a-z]?[-_.]?\d+(?:[._-]\d+)*|[sS]?[-_.]*[ivxldcm]+))?)"
    r"|"
    r"(?:(?:datasets?|data|files?)[-_.]*s(?:\d+(?:[._-]\d+)*|[-_.]*[ivxldcm]+))"
    r")"
    r"(?:[\W_]|$)",
    re.IGNORECASE,
)


def is_explicit_supp_table(label: str) -> bool:
    """
    Check if a table or data label explicitly indicates a supplementary or extended item.
    e.g. 'Extended Data Table 1', 'Supplementary Table S1', 'Supporting Information Table 1',
         'Supplemental Material Table S2', 'ESM Table 1', 'SI Table 1', 'Web Table 1',
         'Table S1', 'Table S1.1', 'Tab. S2', 'S1 Table', 'S1 Data', 'Source Data', '附表 1',
         '附表一', '补充表 S1', 'Appendix 1', '附录 1', 'Table SI',
         'Table A1', 'Table A.1', 'Table A-1', 'Table B.2',
         'eTable 1', 'STable 1', 'S-Table 1', 'Supplementary Data S1', 'Data S1', 'Dataset S1', etc.
    Excludes bare primary article table labels like 'Table 1', 'Table 2', 'Tab 1'.
    """
    if not label:
        return False
    # Check for supplementary/extended prefix (with optional intermediate info/material/data) + table/data
    if re.search(
        r'\b' + _SUPP_PREFIX_RE + r'\s+(?:(?:figures?|figs?\.?)\s+(?:and|&)\s+)?(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|表格?|数据)(?:\s+(?:and|&)\s+(?:figures?|figs?\.?))?',
        label,
        re.IGNORECASE,
    ):
        return True
    # Check for supplementary/extended prefix with file/文件 (requires number/identifier)
    if re.search(
        r'\b' + _SUPP_PREFIX_RE + r'\s+(?:files?|文件)\s+(?:[a-z]?[-_.]?\d+(?:[._-]\d+)*|[sS]?[-_.]*[ivxldcm]+|[a-z]\b)',
        label,
        re.IGNORECASE,
    ):
        return True
    # Check Source Data
    if re.search(r'\bsource[-_ ]?data\b', label, re.IGNORECASE):
        return True
    # Check S-prefixed table/data numbering (e.g. S1 Table, S1.1 Table, S2 Table, S1 Data, S1 Dataset, S1 File)
    if re.search(r'\bs\d+(?:[._-]\d+)*[a-zA-Z]?[-_.\s]+(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?|表格?|数据|文件)\b', label, re.IGNORECASE):
        return True
    # Check Appendix Table or standalone Appendix with identifier (Appendix 1, Appendix-1, Appendix A)
    if re.search(r'\bappendix[-_\s]*(?:tables?|tabs?\.?|tbls?\.?|[a-z0-9])', label, re.IGNORECASE):
        return True
    # Check eTable / e-Table / STable / S-Table
    if re.search(r'\b(?:e|s)[-_]?(?:tables?|tabs?\.?|tbls?\.?)(?:[-_.\s]*\d+|\b)', label, re.IGNORECASE):
        return True
    # Check Chinese terms (附表, 补充表, 附录表, 附加表, 附录 1, 补充数据 1, etc.)
    if re.search(r'(?:(?:补充|附录|附加)?(?:附表|补充表|附录表|附加表|补充数据|附录数据|补充文件|附加文件))', label, re.IGNORECASE):
        return True
    if re.search(r'附录\s*[a-z0-9一二三四五六七八九十]', label, re.IGNORECASE):
        return True
    # Check if table/data/dataset has 'S' numbering (e.g. Table S1, Table S1.1, Tab. S2, Data S1, Dataset S1, File S1, Table SI)
    if re.search(r'(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?)\s+s[-_.]*(?:\d+(?:[._-]\d+)*|[ivxldcm]+)', label, re.IGNORECASE):
        return True
    # Check letter-prefixed appendix tables (e.g. Table A1, Table A.1, Table A-1, Table B.2)
    if re.search(r'\b(?:tables?|tabs?\.?|tbls?\.?)\s+[a-rt-z][-_.]?\d+(?:[._-]\d+)*\b', label, re.IGNORECASE):
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
    r"(?<!data\.)mendeley\.com",
    r"crossmark",
    r"crossref\.org",
    r"orcid\.org",
    r"doi\.org/10\.\d+/",  # DOI links to other articles (references)
]

# Scientific data repository DOIs and publisher component supplement DOIs (immune from doi.org reference exclusion)
REPO_OR_COMPONENT_DOI_PATTERN = re.compile(
    r"(?:"
    r"10\.6084/m9\.figshare"
    r"|10\.5281/zenodo"
    r"|10\.5061/dryad"
    r"|10\.17632/"
    r"|10\.17605/osf\.io"
    r"|10\.7910/dvn"
    r"|10\.24433/co\."
    r"|10\.6073/pasta"
    r"|doi\.org/10\.\d+/[^\s\"'<>?#]*(?:[._/-]s\d{1,4}|[._/-]suppl(?:ement)?\d*|[._/-]esm\d*|[._/-]mmc\d+|[._/-]table[-_.]?s?\d+)(?:[/?#]|$)"
    r")",
    re.IGNORECASE,
)

# Article figures to exclude (gr1, ga1, fx1, fig1, figure_1, etc.) (C2)
ARTICLE_FIGURE_PATTERN = re.compile(
    r"(?:^|[\W_])(?:gr|ga|fx|fig|figure)[-_]?\d+[a-z]?(?:[-_](?:lrg|sml|large|small|preview|highres|html)|\.(?:jpe?g|png|gif|tif|tiff|webp|svg)|$|\?)",
    re.IGNORECASE,
)

# Known HTML file extensions
HTML_EXTENSIONS = {".html", ".htm", ".php", ".asp", ".jsp"}

# Selector for genuine supplementary assets during browser rendering (excludes bare containers or generic 'table' links)
SUPP_WAIT_SELECTOR = (
    "a[href*='mmc'], a[href*='supp'], a[href*='esm'], "
    "a[href*='.xlsx'], a[href*='.xls'], a[href*='.csv'], a[href*='.zip'], "
    "[data-component*='attachment'], [data-test*='supplementary'], [data-test*='source-data'], "
    "a[href*='downloadSupplement'], a[href*='/article/file?id=']"
)


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
    extra_info: dict = field(default_factory=dict)
    source_url: str = ""
    # Table ids this candidate is known to cover (H6). Filled during
    # extraction from the link label / filename / nearby item text; the
    # coverage check prefers this field and falls back to inferring ids
    # from link_text/filename when it is empty.
    covered_table_ids: Set[str] = field(default_factory=set)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "filename": self.filename,
            "link_text": self.link_text,
            "section": self.section,
            "match_rule": self.match_rule,
            "source_tag": self.source_tag,
            "extra_info": self.extra_info,
            "source_url": self.source_url,
            "covered_table_ids": sorted(self.covered_table_ids),
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
    # H3: static parsing could not settle the page; a browser render is
    # needed (supplementary heading/section with no candidates, or inline
    # JSON such as __NEXT_DATA__ from which no URL was parsed).
    needs_browser: bool = False


# ── URL & Filename Utilities ─────────────────────────────────────────────────

def _iter_base_urls(base_url: Union[str, List[str], Tuple[str, ...], Set[str]]) -> List[str]:
    if not base_url:
        return []
    if isinstance(base_url, str):
        return [base_url]
    return [u for u in base_url if isinstance(u, str) and u]


def is_same_page_fragment(url: str, base_url: Union[str, List[str], Tuple[str, ...], Set[str]]) -> bool:
    """Check if a URL is merely an in-page anchor targeting the current page."""
    if not url or not base_url:
        return False
    u = urllib.parse.urlsplit(url)
    if not u.fragment:
        return False
    for b_str in _iter_base_urls(base_url):
        b = urllib.parse.urlsplit(b_str)
        if u.netloc.lower() == b.netloc.lower() and u.path.rstrip("/") == b.path.rstrip("/"):
            # If query matches or the anchor link simply omits the page's tracking query string
            if u.query == b.query or not u.query:
                return True
    return False


def is_same_article_page_url(url: str, base_url: Union[str, List[str], Tuple[str, ...], Set[str]]) -> bool:
    """Check if a URL points to the article page itself rather than a downloadable attachment."""
    if not url or not base_url:
        return False
    u = urllib.parse.urlsplit(url.split("#")[0].strip())
    if not u.netloc or not u.path:
        return False
    for b_str in _iter_base_urls(base_url):
        b = urllib.parse.urlsplit(b_str.split("#")[0].strip())
        if u.netloc.lower() == b.netloc.lower() and u.path.rstrip("/") == b.path.rstrip("/"):
            if u.query == b.query:
                return True
    return False


def is_excluded_url(
    url: str,
    base_url: Union[str, List[str], Tuple[str, ...], Set[str]] = "",
    text: str = "",
    attrs: Optional[Dict[str, str]] = None,
) -> bool:
    """Check if a URL should be excluded (references, external sites, anchors, etc.)."""
    if not url or url.startswith("javascript:") or url.startswith("mailto:") or url.startswith("tel:"):
        return True
    if url.startswith("#"):
        return True
    if base_url and is_same_page_fragment(url, base_url):
        return True

    # Immunity (C1): Never exclude explicit MMC URLs, data repository/component DOIs, or genuine data/document file downloads
    is_data_or_supp_file = bool(
        check_data_extension(url)
        or MMC_PATTERN.search(url)
        or TABLE_FILENAME_PATTERN.search(url)
        or REPO_OR_COMPONENT_DOI_PATTERN.search(url)
        or re.search(r'\.(?:xlsx|xls|csv|tsv|zip|gz|tar|tgz|tar\.gz|7z|rar|pdf|docx|doc|bin|nii|nii\.gz|h5)(?:[?#]|$)', url, re.IGNORECASE)
    )
    is_explicit_supp_context = bool(
        (text and is_explicit_supp_table(text))
        or (attrs and any(is_explicit_supp_table(str(v)) for v in attrs.values() if v))
    )

    for pattern in EXCLUDE_URL_PATTERNS:
        if pattern == r"doi\.org/10\.\d+/":
            # Allow DOI links when they point to data files, data repositories, component DOIs, or explicit supplementary tables
            if is_data_or_supp_file or is_explicit_supp_context:
                continue
        elif "elsevier" in pattern:
            # Do not exclude elsevier download endpoints that point to data files or MMC
            if is_data_or_supp_file:
                continue
        if re.search(pattern, url, re.IGNORECASE):
            return True
    return False


def is_article_figure_url(url: str, text: str = "", attrs: Optional[Dict[str, str]] = None) -> bool:
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
    # If link text or attributes explicitly declare Source Data or a Supplementary Table, do not treat as a figure
    if text and (is_explicit_supp_table(text) or TABLE_FILENAME_PATTERN.search(text) or SUPP_KEYWORD_PATTERN.search(text)):
        return False
    if attrs:
        for v in attrs.values():
            if v and isinstance(v, str) and (is_explicit_supp_table(v) or TABLE_FILENAME_PATTERN.search(v) or SUPP_KEYWORD_PATTERN.search(v)):
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


def _node_has_other_control(node, target_tag) -> bool:
    """Return True if node is or contains a clickable link/button distinct from target_tag."""
    if not node or node == target_tag or not hasattr(node, "find_all"):
        return False
    # If target_tag is inside node, node is an ancestor, not a separate sibling control
    if target_tag in node.descendants:
        return False
    if getattr(node, "name", "") in ("a", "button") and extract_target_url_from_tag(node):
        return True
    for ctrl in node.find_all(["a", "button"], limit=5):
        if ctrl != target_tag and extract_target_url_from_tag(ctrl):
            return True
    return False


_WHOLE_PAGE_CONTAINER_TAGS = {"[document]", "html", "body", "main", "article", "header", "footer", "nav"}
_WHOLE_PAGE_ATTR_TOKENS = {
    "article-body", "article_body", "article-content", "article_content",
    "main-content", "main_content", "page-content", "page_content",
    "fulltext", "full-text", "manuscript-body", "journal-content",
}


def _is_whole_article_wrapper(el) -> bool:
    if not el or not hasattr(el, "name"):
        return True
    if el.name in _WHOLE_PAGE_CONTAINER_TAGS:
        return True
    if hasattr(el, "attrs") and isinstance(el.attrs, dict):
        id_val = str(el.attrs.get("id") or "").lower()
        cls_val = " ".join(el.attrs.get("class") or []).lower() if isinstance(el.attrs.get("class"), list) else str(el.attrs.get("class") or "").lower()
        role_val = str(el.attrs.get("role") or "").lower()
        if role_val in {"main", "article", "document", "navigation", "banner", "contentinfo"}:
            return True
        combined = f"{id_val} {cls_val}"
        if any(tok in combined for tok in _WHOLE_PAGE_ATTR_TOKENS):
            return True
    return False


def get_nearby_context_text(tag) -> Tuple[str, str]:
    """
    Search nearby headings, captions, or descriptive text enclosing or adjacent to the tag.
    Returns (heading_or_label_text, source_type).
    Prioritizes text matching TABLE_LABEL_PATTERN without short-circuiting on irrelevant labels.
    Ensures multiple items in the same container do not falsely share an earlier item's title,
    and never leaks across whole-article boundaries to steal main-text headings.
    """
    if not tag or not hasattr(tag, "parent"):
        return "", ""

    tag_text = tag.get_text(separator=" ", strip=True)
    sibling_tags = [
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "dt", "dd", "td", "th", "li",
        "caption", "figcaption", "strong", "b", "span", "label", "div", "summary",
    ]

    # 1. Immediate preceding siblings of the tag itself
    if hasattr(tag, "find_previous_siblings"):
        for prev in tag.find_previous_siblings(sibling_tags, limit=6):
            if _node_has_other_control(prev, tag):
                break
            p_text = prev.get_text(separator=" ", strip=True)
            if p_text and p_text != tag_text:
                if TABLE_LABEL_PATTERN.search(p_text):
                    return p_text, f"tag_prev_sibling_{prev.name}"

    # 2. Immediate following siblings of the tag itself (e.g. [Download Icon] <span>Table S1</span>)
    if hasattr(tag, "find_next_siblings"):
        for nxt in tag.find_next_siblings(sibling_tags, limit=4):
            if _node_has_other_control(nxt, tag):
                break
            n_text = nxt.get_text(separator=" ", strip=True)
            if n_text and n_text != tag_text:
                if TABLE_LABEL_PATTERN.search(n_text):
                    return n_text, f"tag_next_sibling_{nxt.name}"

    # 3. Bounded upward traversal up to 4 parent layers (stopping at article/main/body boundaries)
    curr = tag.parent
    prev_child = tag
    fallback_text = ""
    fallback_source = ""
    for _ in range(4):
        if not curr or _is_whole_article_wrapper(curr):
            break

        # Check preceding siblings of prev_child inside curr (stops if hitting another item's download control)
        if hasattr(prev_child, "find_previous_siblings"):
            for prev in prev_child.find_previous_siblings(sibling_tags, limit=6):
                if _node_has_other_control(prev, tag):
                    break
                p_text = prev.get_text(separator=" ", strip=True)
                if p_text and p_text != tag_text:
                    if TABLE_LABEL_PATTERN.search(p_text):
                        return p_text, f"container_prev_sibling_{prev.name}"
                    if not fallback_text and prev.name in ["h1", "h2", "h3", "h4", "h5", "h6", "caption", "dt", "summary"]:
                        fallback_text, fallback_source = p_text, f"container_prev_sibling_{prev.name}"

        # Check following siblings of prev_child inside curr when prev_child is a table cell or small action wrapper
        if getattr(prev_child, "name", "") in ("td", "th", "dt", "dd", "span", "div") and hasattr(prev_child, "find_next_siblings"):
            if not _node_has_other_control(prev_child, tag):
                for nxt in prev_child.find_next_siblings(sibling_tags, limit=4):
                    if _node_has_other_control(nxt, tag):
                        break
                    n_text = nxt.get_text(separator=" ", strip=True)
                    if n_text and n_text != tag_text:
                        if TABLE_LABEL_PATTERN.search(n_text):
                            return n_text, f"container_next_sibling_{nxt.name}"

        # Only scan descendant headings/labels across curr if curr does not contain competing item rows
        # (or if the heading precedes prev_child without any intervening download control)
        headings = curr.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption", "dt", "th", "summary"], limit=6)
        for h in headings:
            htext = h.get_text(separator=" ", strip=True)
            if not htext or htext == tag_text:
                continue
            # Ensure h is not inside a sibling branch that follows prev_child or after another download control
            if hasattr(tag, "find_all_previous"):
                prev_elements = tag.find_all_previous(limit=25)
                if h not in prev_elements and tag not in h.descendants:
                    continue
                # Check if another download control sits between h and tag
                intervening_control = False
                for pe in prev_elements:
                    if pe == h:
                        break
                    if pe != tag and getattr(pe, "name", "") in ("a", "button") and extract_target_url_from_tag(pe):
                        # Allow sibling format links inside the exact same action parent (e.g. [XLSX] [PDF])
                        if getattr(pe, "parent", None) != getattr(tag, "parent", None):
                            intervening_control = True
                            break
                if intervening_control:
                    continue
            if TABLE_LABEL_PATTERN.search(htext):
                return htext, h.name
            if not fallback_text:
                fallback_text, fallback_source = htext, h.name

        # Check elements with class indicating title, label, caption, info, or desc inside curr
        for el in curr.find_all(
            lambda e: e != tag
            and hasattr(e, "get")
            and any(c in " ".join(e.get("class", [])).lower() for c in ["title", "caption", "label", "heading", "name", "desc", "info"]),
            limit=6,
        ):
            if _node_has_other_control(el, tag):
                continue
            el_text = el.get_text(separator=" ", strip=True)
            if el_text and el_text != tag_text:
                if TABLE_LABEL_PATTERN.search(el_text):
                    return el_text, "nearby_label"

        prev_child = curr
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
    """
    Check if an HTML element is or is within a supplementary material container.
    Stops at whole-article layout boundaries and avoids scanning descendant headings
    of unrelated sibling sections.
    """
    heading_keywords = [
        "supplement", "suppl", "appendix", "supporting information",
        "supporting info", "additional file", "additional material",
        "additional data", "extended data", "extended table",
        "extended data table", "extended data tables", "extended tables",
        "supplementary table", "supplementary tables",
        "supplemental table", "source data", "补充材料", "附表", "附录", "附加材料",
        "支撑材料", "补充表格", "补充信息", "补充数据",
    ]
    supp_attr_keywords = [
        "supplement", "suppl", "appendix", "supporting-info", "supporting_info",
        "esm", "extended-data", "extended_data", "extendeddata",
        "additional-file", "additional-material", "additional-data",
        "data-availability", "supp-table", "supplementary-table", "source-data",
        "补充", "附表", "附录", "附加", "支撑材料",
    ]
    heading_tag_names = ["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption", "summary"]

    curr = tag
    prev_child = None
    steps = 0
    while curr and steps < 6:
        if _is_whole_article_wrapper(curr):
            break

        # 1. Check curr's own attributes (id, class, data-*, role, aria-label, etc.)
        if hasattr(curr, "attrs") and isinstance(curr.attrs, dict):
            for k, v in curr.attrs.items():
                val_str = " ".join(v) if isinstance(v, list) else str(v)
                combined = f"{k} {val_str}".lower()
                if any(kw in combined for kw in supp_attr_keywords):
                    return True

        # 2. Check direct child headings of curr (or direct header wrapper of curr) that precede prev_child
        if hasattr(curr, "name") and curr.name in ["section", "div", "aside", "details", "figure", "dl", "ul", "ol", "table"]:
            for h in curr.find_all(heading_tag_names, recursive=False, limit=3):
                if any(kw in h.get_text(" ", strip=True).lower() for kw in heading_keywords):
                    return True
            # Also check a direct child header wrapper (e.g. <div class="section-title"><h2>Supplementary</h2></div>)
            for child_hdr in curr.find_all(["header", "div"], recursive=False, limit=2):
                if child_hdr == prev_child:
                    break
                hdr_cls = " ".join(child_hdr.get("class", [])).lower() if hasattr(child_hdr, "get") else ""
                if child_hdr.name == "header" or any(k in hdr_cls for k in ["header", "heading", "title", "caption"]):
                    for h in child_hdr.find_all(heading_tag_names, limit=2):
                        if any(kw in h.get_text(" ", strip=True).lower() for kw in heading_keywords):
                            return True

        # 3. Check preceding sibling headings of curr (only when curr is the section content block immediately following a heading)
        if hasattr(curr, "find_previous_sibling"):
            prev_h = curr.find_previous_sibling(heading_tag_names)
            if prev_h:
                # Only treat prev_h as governing curr if there is no other non-heading section between prev_h and curr
                prev_any = curr.find_previous_sibling()
                if prev_any == prev_h and any(kw in prev_h.get_text(" ", strip=True).lower() for kw in heading_keywords):
                    return True

        prev_child = curr
        curr = curr.parent
        steps += 1
    return False


# ── Extractor from Controls (<a>, <button>, <meta>, etc.) ───────────────────

def extract_target_url_from_tag(tag) -> Optional[str]:
    """
    Extract target URL from <a>, <button>, or other controls.
    Covers href, formaction, data-*, and onclick handlers (Issue #6, B1).
    Excludes in-page hash anchors like #tbl1, #m0001, etc.
    """
    # 1. Standard attributes
    for attr in [
        "data-download-url",
        "data-file-url",
        "data-asset-url",
        "href",
        "formaction",
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

    # 3. Form action ONLY if tag is <form> itself or a submit/button <button>/<input>
    tag_name = getattr(tag, "name", "")
    is_form_submit_control = False
    if tag_name == "form":
        is_form_submit_control = True
    elif tag_name == "button":
        btn_type = str(tag.get("type") or "submit").lower()
        is_form_submit_control = btn_type in ("submit", "button")
    elif tag_name == "input":
        inp_type = str(tag.get("type") or "").lower()
        is_form_submit_control = inp_type in ("submit", "button", "image")

    if is_form_submit_control:
        form = tag if tag_name == "form" else (tag.find_parent("form") if hasattr(tag, "find_parent") else None)
        if form:
            form_action = form.get("action")
            if form_action and isinstance(form_action, str) and not form_action.startswith("javascript:"):
                act_clean = form_action.strip()
                if act_clean and not act_clean.startswith("#"):
                    return act_clean

    return None


# ── Manifest & Collision Management (D2, H6) ─────────────────────────────────

# Manifest entries are filename -> {url, source_url, table_ids, size, sha256, status}.
# Older manifests stored filename -> URL string; they are normalized on load/save.
_MANIFEST_ENTRY_DEFAULTS = {
    "url": "",
    "source_url": "",
    "table_ids": [],
    "size": None,
    "sha256": None,
    "status": "downloaded",
}


def manifest_entry_url(entry: Any) -> str:
    """Return the URL recorded in a manifest entry, old (str) or new (dict) format."""
    if isinstance(entry, dict):
        return str(entry.get("url") or "")
    if isinstance(entry, str):
        return entry
    return ""


def _normalize_manifest_entry(entry: Any) -> Optional[Dict[str, Any]]:
    """Coerce a manifest entry to the structured dict format; None when unusable."""
    if isinstance(entry, str):
        if not entry:
            return None
        normalized = dict(_MANIFEST_ENTRY_DEFAULTS)
        normalized["url"] = entry
        return normalized
    if isinstance(entry, dict):
        url = str(entry.get("url") or "")
        if not url:
            return None
        normalized = dict(_MANIFEST_ENTRY_DEFAULTS)
        normalized.update(entry)
        normalized["url"] = url
        normalized["source_url"] = str(entry.get("source_url") or "")
        table_ids = entry.get("table_ids") or []
        if isinstance(table_ids, str):
            table_ids = [table_ids]
        normalized["table_ids"] = sorted({str(t) for t in table_ids if t})
        return normalized
    return None


def record_manifest_entry(
    manifest: Dict[str, Any],
    filename: str,
    url: str,
    *,
    source_url: str = "",
    table_ids: Any = (),
    size: Optional[int] = None,
    sha256: Optional[str] = None,
    status: str = "downloaded",
) -> Dict[str, Any]:
    """Record one download in structured form (H6); returns the stored entry."""
    entry = {
        "url": url,
        "source_url": source_url or "",
        "table_ids": sorted({str(t) for t in (table_ids or ()) if t}),
        "size": size,
        "sha256": sha256,
        "status": status,
    }
    manifest[filename] = entry
    return entry


def load_download_manifest(output_dir: str) -> Dict[str, Dict[str, Any]]:
    """Load download manifest mapping filename -> structured entry dict (D2, H6).

    A corrupt manifest file prints a warning and returns {} instead of
    silently discarding the saved entries.
    """
    manifest_path = os.path.join(output_dir, ".download_manifest.json")
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            print(f"[Warning] Corrupt download manifest {manifest_path}: {exc}; ignoring saved entries", file=sys.stderr)
            return {}
        if not isinstance(data, dict):
            print(f"[Warning] Corrupt download manifest {manifest_path}: expected a JSON object; ignoring saved entries", file=sys.stderr)
            return {}
        normalized: Dict[str, Dict[str, Any]] = {}
        for fname, entry in data.items():
            coerced = _normalize_manifest_entry(entry)
            if coerced is None:
                print(f"[Warning] Skipping unusable manifest entry for {fname!r} in {manifest_path}", file=sys.stderr)
                continue
            normalized[fname] = coerced
        return normalized
    return {}


def save_download_manifest(output_dir: str, manifest: Dict[str, Any]):
    """Save download manifest atomically via temp file + os.replace (D2, H6)."""
    manifest_path = os.path.join(output_dir, ".download_manifest.json")
    normalized = {}
    for fname, entry in (manifest or {}).items():
        coerced = _normalize_manifest_entry(entry)
        if coerced is not None:
            normalized[fname] = coerced
    tmp_path = None
    try:
        os.makedirs(output_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".download_manifest.", suffix=".tmp", dir=output_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(normalized, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, manifest_path)
            tmp_path = None
        finally:
            if tmp_path is not None:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    except Exception as exc:
        print(f"[Warning] Could not save download manifest {manifest_path}: {exc}", file=sys.stderr)


# ── Atomic download landing (H9) ─────────────────────────────────────────────
# Every download path writes to a same-directory temporary file, validates
# it, then commits with os.replace and only afterwards updates the
# manifest. The temp name keeps the target extension (so
# validate_downloaded_file's extension-specific checks still apply) and
# is dot-prefixed with a random token, so it can never collide with the
# final file and is never registered in the manifest.

def temp_path_for(final_path: str) -> str:
    """Create an empty temp file next to *final_path* and return its path.

    Name shape: ``.<stem>.<random><ext>`` in the same directory, so the
    commit is a same-filesystem os.replace and the extension is preserved
    for validation. The caller owns the file and must commit or discard it.
    """
    directory = os.path.dirname(os.path.abspath(final_path))
    base = os.path.basename(final_path)
    stem, ext = os.path.splitext(base)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{stem}.", suffix=ext, dir=directory)
    os.close(fd)
    return tmp_path


def commit_temp_file(tmp_path: str, final_path: str) -> None:
    """Atomically publish a validated temp file at *final_path*."""
    os.replace(tmp_path, final_path)


def discard_temp_file(tmp_path: Optional[str]) -> None:
    """Best-effort removal of a temp file (failed / invalid download)."""
    if not tmp_path:
        return
    try:
        os.remove(tmp_path)
    except OSError:
        pass


def write_bytes_chunked(path: str, data: bytes, chunk_size: int = 65536) -> int:
    """Write *data* to *path* in bounded chunks instead of one shot.

    The bytes are already in memory (e.g. a Fetcher response body); the
    point is to avoid a single huge write call and to keep the write path
    uniform with the streaming downloaders.
    """
    written = 0
    view = memoryview(data)
    with open(path, "wb") as f:
        for offset in range(0, len(data), chunk_size):
            written += f.write(view[offset:offset + chunk_size])
    return written


def disambiguate_target_filename(output_dir: str, fname: str, url: str, manifest: Dict[str, Any]) -> str:
    """
    Ensure fname does not collide with an existing file on disk downloaded for a different URL (D2).
    """
    target = fname
    base, ext = os.path.splitext(target)
    idx = 2
    while os.path.exists(os.path.join(output_dir, target)):
        # If this exact file on disk was downloaded for this exact URL, no rename needed
        if manifest_entry_url(manifest.get(target)) == url:
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
    if not data_ext:
        for attr_k in ("download", "data-filename", "title", "data-title"):
            attr_v = attrs.get(attr_k)
            if attr_v and isinstance(attr_v, str):
                clean_v = re.sub(
                    r'\s*\((xlsx|xls|docx|doc|csv|tsv|zip|tar\.gz|tgz|gz|7z|rar|txt|json|xml|h5|hdf5|mat|rds|rda)\)\s*$',
                    r'.\1',
                    attr_v.strip(),
                    flags=re.IGNORECASE,
                )
                ext_from_attr = check_data_extension(clean_v)
                if ext_from_attr:
                    data_ext = ext_from_attr
                    break

    is_table, table_rule = is_table_text_or_attribute(text, attrs, tag=tag)
    is_table_filename = bool(TABLE_FILENAME_PATTERN.search(fname)) or bool(TABLE_FILENAME_PATTERN.search(path_lower))

    # Case A: Elsevier MMC link
    if MMC_PATTERN.search(url_lower):
        return True, "mmc_pattern"

    # Case B: Standard data extension (.xlsx, .csv, .zip, etc.)
    if data_ext:
        return True, f"data_extension:{data_ext}"

    path_basename = os.path.basename(path_lower.rstrip("/"))
    has_query_or_endpoint = (
        bool(parsed.query) or
        path_basename in GENERIC_PATH_BASENAMES or
        any(seg in path_lower for seg in [
            "/fetch", "/stream", "/export", "/api/", "/assets/", "/files/",
            "/download", "/att/", "/get/", "/attachment/", "/mediaobjects/", "/suppl",
        ]) or
        tag_name in ["button", "input"] or
        (tag is not None and (tag.has_attr("data-url") or tag.has_attr("onclick") or tag.has_attr("download")))
    )
    is_html = any(path_lower.endswith(h_ext) for h_ext in HTML_EXTENSIONS)

    # Case C: Table label detected in text, attributes, or nearby heading (Issue #2, #3, A3, A4)
    if is_table:
        is_explicit_supp = is_explicit_supp_table(text) or any(is_explicit_supp_table(str(v)) for v in attrs.values() if v)
        if not is_explicit_supp and ":" in table_rule:
            is_explicit_supp = is_explicit_supp_table(table_rule.split(":", 1)[1])

        check_label = (text or "") + " " + (table_rule or "")
        is_letter_table = bool(re.search(r'\b(?:tables?|tabs?\.?|tbls?\.?)\s+[A-RT-Z][-_.]?\d+(?:[._-]\d+)*\b', check_label, re.IGNORECASE))
        has_explicit_supp_prefix = bool(re.search(
            r'(?:supplementary|supplemental|supporting|extended\s*data|ext\s*data|additional|appendix|online|suppl?\.?|esm|si|web|source[-_ ]?data|附表|补充|附录)',
            check_label,
            re.IGNORECASE,
        ))
        has_appendix_context = (
            "appendix" in url_lower
            or "supp" in url_lower
            or "appendix" in check_label.lower()
            or "supp" in check_label.lower()
            or has_explicit_supp_prefix
        )

        # Unqualified letter tables (e.g. bare 'Table A1' in page_body) require appendix/supp context or a direct file/download indicator
        if is_letter_table and not has_explicit_supp_prefix:
            if is_html:
                if is_supp_section or has_appendix_context or is_table_filename or "download" in attrs:
                    return True, f"html_table_attachment ({table_rule})"
            elif data_ext or path_lower.endswith(".pdf") or is_supp_section or "download" in attrs or is_table_filename:
                return True, table_rule
            elif has_appendix_context and has_query_or_endpoint:
                return True, f"table_endpoint ({table_rule})"
        else:
            if is_html:
                if is_supp_section or is_explicit_supp or is_table_filename or "download" in attrs:
                    return True, f"html_table_attachment ({table_rule})"
            elif data_ext or path_lower.endswith(".pdf") or is_supp_section or "download" in attrs or is_table_filename:
                return True, table_rule
            elif not is_html:
                if is_explicit_supp or is_supp_section:
                    return True, f"table_endpoint ({table_rule})"

    # Case D: Table filename pattern (e.g. table-s1.pdf, tbl_s2.xlsx)
    if is_table_filename:
        return True, "table_filename_pattern"

    # Case E: PDF or extensionless supplementary endpoint with supp keywords (Issue #3)
    is_pdf = path_lower.endswith(".pdf")
    is_wiley_dl = (
        "downloadsupplement" in path_lower
        or "downloadsupplement" in url_lower
        or ("wiley.com" in url_lower and not is_html)
    )
    attrs_text = " ".join(str(v) for v in attrs.values() if isinstance(v, (str, list)))
    check_supp_text = f"{text} {attrs_text}"
    has_s_numbered_item = bool(re.search(
        r'\bs\d+(?:[._-]\d+)*\s+(?:text|note|notes|protocol|method|methods|code|file|files|material|materials|appendix)\b',
        check_supp_text,
        re.IGNORECASE,
    ))

    if is_pdf or is_wiley_dl:
        if SUPP_KEYWORD_PATTERN.search(url_lower):
            return True, "pdf_with_supplementary_keyword" if is_pdf else "wiley_supp_link"
        if SUPP_KEYWORD_PATTERN.search(check_supp_text) or has_s_numbered_item:
            return True, "pdf_with_supplementary_keyword" if is_pdf else "wiley_supp_link"
        if is_supp_section:
            return True, "pdf_in_supplementary_section" if is_pdf else "wiley_supp_link"

    # Extensionless download links with explicit supplementary keywords or data repository/component DOIs
    if not is_html:
        if REPO_OR_COMPONENT_DOI_PATTERN.search(url):
            return True, "supp_or_repo_doi_link"
        if (SUPP_KEYWORD_PATTERN.search(check_supp_text) or has_s_numbered_item) and (is_supp_section or has_query_or_endpoint or SUPP_KEYWORD_PATTERN.search(url_lower)):
            return True, "supp_keyword_endpoint"

    # Case F: Control inside supplementary section with download or format indicator
    if is_supp_section:
        supp_ctrl_keywords = [
            "download", "pdf", "file", "table", "xlsx", "xls", "csv", "tsv", "zip",
            "docx", "doc", "excel", "word", "spreadsheet", "archive", "data", "dataset",
            "supp", "esm", "mmc", "appendix", "source",
            "下载", "附件", "附表", "补充", "附录", "数据", "表格",
        ]
        if "download" in attrs or any(kw in check_supp_text.lower() for kw in supp_ctrl_keywords):
            if not is_html or "table" in check_supp_text.lower() or "附表" in check_supp_text.lower():
                return True, "download_control_in_supplementary_section"

    # Case G: MDPI style /s1, /s2
    if re.search(r'/s\d+/?$', path_lower):
        return True, "mdpi_supp_path"

    # Case H: NCBI / PMC / Europe PMC binary attachment link (/bin/ or click_feat_suppl)
    if ("/bin/" in path_lower or "/bin/" in url_lower) and any(d in url_lower for d in ["ncbi.nlm.nih.gov", "pmc", "europepmc"]):
        if is_article_figure_url(url, text=text, attrs=attrs):
            return False, ""
        return True, "pmc_bin_attachment"

    return False, ""


# ── Core Candidate Scanner ───────────────────────────────────────────────────

# Query parameters that only mark a link as a download request (or analytics
# tracking) and never identify a distinct file. Two URLs that differ ONLY in
# these parameters point at the same file and must dedup to one candidate.
# Normalization rule (H6): a marker parameter is dropped only when its value
# is a generic flag (empty, a bare number, true/yes/download/attachment);
# a value that names a file ('download=S2.xlsx') is kept, as are all other
# parameters (id, attachmentId, file, type, ...), which may identify
# distinct files. Remaining parameters are sorted so their order does not
# matter. Scheme/host are lowercased; the path is left untouched.
_DOWNLOAD_MARKER_PARAMS = {"download", "dl", "attachment", "force_download", "force-download"}
_DOWNLOAD_MARKER_VALUES = {"", "true", "yes", "download", "attachment"}


def _normalize_url_for_dedup(url: str) -> str:
    """Dedup key for a candidate URL: fragment stripped, marker params dropped (H6)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    params = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    kept = [
        (k, v) for k, v in params
        if not (
            k.lower() in _DOWNLOAD_MARKER_PARAMS
            and (v.lower() in _DOWNLOAD_MARKER_VALUES or v.isdigit())
        )
        and not k.lower().startswith("utm_")
    ]
    kept.sort()
    query = urllib.parse.urlencode(kept)
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def _infer_candidate_table_ids(link_text: str, filename: str, attrs: Optional[Dict[str, Any]] = None) -> Set[str]:
    """Table ids named by a candidate's own label, filename, or attributes (H6)."""
    ids: Set[str] = set()
    for value in (link_text, filename):
        ids |= supplementary_table_ids(value)
    for value in (attrs or {}).values():
        if isinstance(value, str):
            ids |= supplementary_table_ids(value)
        elif isinstance(value, list):
            for item in value:
                ids |= supplementary_table_ids(str(item))
    return ids


def _parent_direct_table_ids(tag) -> Set[str]:
    """Table ids in the immediate parent's own text nodes (H6).

    E.g. '<li>Table S2 <a>Download</a></li>' attributes S2 to the link.
    Only when the parent is a single item row (li/td/p/...) holding exactly
    one download control, so a section that lists several tables never
    donates its whole declaration ('Tables S1.1 and S1.2') to one link.
    """
    parent = getattr(tag, "parent", None)
    if parent is None or not hasattr(parent, "find_all"):
        return set()
    parent_name = getattr(parent, "name", "") or ""
    if parent_name in ("section", "article", "main", "body", "html", "ul", "ol", "table", "figure"):
        return set()
    if parent_name == "div":
        attrs = " ".join(
            [str(parent.get("id") or "")] + [str(c) for c in (parent.get("class") or [])]
        ).lower()
        if any(kw in attrs for kw in ("supplement", "suppl", "appendix", "extended-data", "extended_data", "附表", "附录")):
            return set()
    try:
        controls = [
            c for c in parent.find_all(["a", "button", "input"], recursive=False)
            if extract_target_url_from_tag(c)
        ]
        if len(controls) != 1:
            return set()
        direct_text = " ".join(
            s.strip() for s in parent.find_all(string=True, recursive=False) if s.strip()
        )
    except Exception:
        return set()
    if not direct_text:
        return set()
    return supplementary_table_ids(direct_text)


# Inline JSON payloads whose URLs static parsing may miss (H3).
_INLINE_JSON_MARKERS = ("__NEXT_DATA__", "__INITIAL_STATE__", "__NUXT__", "__PRELOADED_STATE__")

_SUPP_SIGNAL_KEYWORDS = (
    "supplement", "suppl", "appendix", "supporting-info", "supporting_info",
    "esm", "additional-file", "extended-data", "extended_data",
    "补充", "附表", "附录", "支撑材料",
)


def _page_has_supplementary_signal(soup) -> bool:
    """True when the page shows a supplementary heading, section, or collapsed control (H3)."""
    for el in soup.find_all(["section", "div", "aside", "details", "figure"]):
        ident = (el.get("id", "") + " " + " ".join(el.get("class", []))).lower()
        if any(kw in ident for kw in _SUPP_SIGNAL_KEYWORDS):
            return True
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if SUPP_KEYWORD_PATTERN.search(heading.get_text(" ", strip=True) or ""):
            return True
    for el in soup.find_all(["button", "a", "summary"]):
        text = " ".join([
            el.get_text(" ", strip=True) or "",
            str(el.get("aria-label") or ""),
            str(el.get("title") or ""),
        ])
        if SUPP_KEYWORD_PATTERN.search(text):
            return True
    return False


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
    page_urls = [u for u in (effective_base, base_url, original_url) if u]
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
        cand.source_url = cand.source_url or base_url
        evidence_attrs = cand.extra_info.get("attributes", {})
        if not isinstance(evidence_attrs, dict):
            evidence_attrs = {}
        raw_cand_url = (cand.url or "").strip()
        if not raw_cand_url:
            return
        # Check raw_cand_url BEFORE stripping '#' so in-page fragments are caught
        if is_excluded_url(raw_cand_url, base_url=page_urls, text=cand.link_text, attrs=evidence_attrs):
            diag.excluded_links.append((raw_cand_url, "excluded_url_pattern"))
            return
        norm_url = raw_cand_url.split("#")[0].strip()
        if not norm_url:
            return
        if is_same_article_page_url(norm_url, page_urls):
            diag.excluded_links.append((norm_url, "same_article_page_url"))
            return
        if is_excluded_url(norm_url, base_url=page_urls, text=cand.link_text, attrs=evidence_attrs):
            diag.excluded_links.append((norm_url, "excluded_url_pattern"))
            return
        if is_article_figure_url(norm_url, text=cand.link_text, attrs=evidence_attrs):
            diag.excluded_links.append((norm_url, "article_figure_pattern"))
            return
        # H6: record the table ids this link is known to cover, then dedup on
        # the query-normalized URL so download-marker differences do not
        # inflate the candidate count.
        cand.covered_table_ids = set(cand.covered_table_ids) | _infer_candidate_table_ids(
            cand.link_text, cand.filename, evidence_attrs
        )
        dedup_key = _normalize_url_for_dedup(norm_url)
        if dedup_key in seen_urls:
            # H6: same file reached twice (e.g. once as 'Table S1', once as
            # 'Table S2') keeps ONE candidate but merges both labels/table ids.
            for existing in candidates:
                if _normalize_url_for_dedup(existing.url.split("#")[0].strip()) == dedup_key:
                    existing.covered_table_ids |= cand.covered_table_ids
                    if not is_explicit_supp_table(existing.link_text) and is_explicit_supp_table(cand.link_text):
                        existing.link_text = cand.link_text
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
                        existing.filename = cand.filename
                    break
            return
        seen_urls.add(dedup_key)
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
            selector = tag.get("data-journal-download-selector")
            if selector:
                control_label = tag.get("value") or tag.get_text(" ", strip=True) or tag.get("aria-label") or tag.get("title") or "download"
                candidates.append(Candidate(
                    url=base_url + "#" + tag.get("id", "download"),
                    filename=sanitize_filename(control_label) + ".bin",
                    link_text=control_label, section="supplementary_section",
                    source_url=base_url, extra_info={"browser_selector": selector},
                    match_rule="browser_download_control",
                    covered_table_ids=supplementary_table_ids(control_label)))
            continue

        link_text = tag.get("value", "") if tag.name == "input" else tag.get_text(separator=" ", strip=True)
        attrs = {k: tag[k] for k in ["aria-label", "title", "download", "data-filename", "data-title", "data-caption", "value"] if tag.has_attr(k)}

        full_url = urllib.parse.urljoin(effective_base, target_raw)
        if is_excluded_url(full_url, base_url=page_urls, text=link_text, attrs=attrs):
            diag.excluded_links.append((full_url, "excluded_url_pattern"))
            continue
        if is_same_article_page_url(full_url, page_urls):
            diag.excluded_links.append((full_url, "same_article_page_url"))
            continue
        if is_article_figure_url(full_url, text=link_text, attrs=attrs):
            diag.excluded_links.append((full_url, "article_figure_pattern"))
            continue

        default_candidate_name = attrs.get("data-filename") or attrs.get("title") or link_text
        fname = attrs.get("download") or extract_filename_from_url(full_url, default_name=default_candidate_name)
        parsed = urllib.parse.urlparse(full_url)
        path_lower = parsed.path.lower()

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
                link_text=link_text,
                section=section,
                match_rule=rule,
                source_tag=tag.name,
                extra_info={"attributes": attrs},
                covered_table_ids=_parent_direct_table_ids(tag),
            ))

    # ── Strategy 3: Publisher-Specific Extractors (Issue #6 & WebVPN compatibility) ──
    combined_url = f"{effective_base} {base_url} {original_url}".lower()

    # Elsevier ScienceDirect / Cell / Lancet reader asset links
    if (
        any(d in combined_url for d in ["sciencedirect", "els-cdn", "elsevier", "cell.com", "thelancet"])
        or soup.find(attrs={"data-component": True}) is not None
    ):
        _extract_elsevier_candidates(soup, effective_base, add_candidate)

    # Springer / Nature / BMC supplementary assets
    if (
        any(d in combined_url for d in ["springer", "nature.com", "biomedcentral", "springeropen", "palgrave"])
        or soup.find("a", attrs={"data-track-action": re.compile(r"download", re.IGNORECASE)}) is not None
        or soup.find(attrs={"data-test": re.compile(r"supplementary|source[-_]?data", re.IGNORECASE)}) is not None
    ):
        _extract_springer_nature_candidates(soup, effective_base, add_candidate)

    # Wiley supplementary assets
    if "wiley" in combined_url or soup.find("a", href=re.compile(r"/action/downloadSupplement", re.IGNORECASE)) is not None:
        _extract_wiley_candidates(soup, effective_base, add_candidate)

    # PLOS supplementary files
    if "plos" in combined_url or soup.find("a", href=re.compile(r"/article/file\?id=", re.IGNORECASE)) is not None:
        _extract_plos_candidates(soup, effective_base, add_candidate)

    # NCBI / PMC / Europe PMC supplementary files
    if (
        any(d in combined_url for d in ["ncbi.nlm.nih.gov", "pmc", "europepmc"])
        or soup.find("a", attrs={"data-ga-action": re.compile(r"click_(?:feat_)?suppl", re.IGNORECASE)}) is not None
    ):
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

    # ── H3: dynamic 'not loaded' signal ───────────────────────────────────
    # Supplementary heading/section present but nothing extracted, or inline
    # JSON (__NEXT_DATA__ / __INITIAL_STATE__ / ...) present but no URL was
    # parsed out of it: static parsing cannot settle the page, so a browser
    # render is needed. Callers should treat this as a must-render signal.
    embedded_found = any(c.match_rule.startswith("embedded_") for c in candidates)
    has_inline_json = any(marker in html_content for marker in _INLINE_JSON_MARKERS)
    diag.needs_browser = (not candidates and _page_has_supplementary_signal(soup)) or (
        has_inline_json and not embedded_found
    )

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
    for a in soup.find_all("a", attrs={"data-track-action": re.compile(r"download", re.IGNORECASE)}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            url_lower = full_url.lower()
            link_text = a.get_text(strip=True)
            text_lower = link_text.lower()
            track_action = str(a.get("data-track-action") or "").lower()
            # Exclude citation downloads (.ris / citation)
            if "citation" in track_action or "citation" in text_lower or url_lower.endswith(".ris"):
                continue
            # Exclude main article PDF: /content/pdf/ without /esm/, or text containing 'download article pdf'
            if "/content/pdf/" in url_lower and "/esm/" not in url_lower:
                continue
            if "download article pdf" in text_lower or (
                text_lower in ("download pdf", "pdf")
                and "/esm/" not in url_lower
                and "/mediaobjects/" not in url_lower
                and "supp" not in track_action
                and not is_supplementary_container(a)
            ):
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
            link_text = a.get_text(separator=" ", strip=True)
            full_url = urllib.parse.urljoin(base_url, target)
            if is_excluded_url(full_url, base_url=base_url, text=link_text) or is_article_figure_url(full_url, text=link_text):
                continue
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
        link_text = a.get_text(separator=" ", strip=True)
        full_url = urllib.parse.urljoin(base_url, href)
        if is_excluded_url(full_url, base_url=base_url, text=link_text) or is_article_figure_url(full_url, text=link_text):
            continue
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
            link_text = a.get_text(separator=" ", strip=True)
            full_url = urllib.parse.urljoin(base_url, href)
            if is_excluded_url(full_url, base_url=base_url, text=link_text) or is_article_figure_url(full_url, text=link_text):
                continue
            fname = a.get("download") or extract_filename_from_url(full_url, default_name=link_text)
            add_func(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=link_text,
                section="pmc_featured_suppl",
                match_rule="pmc_feat_suppl_link",
                source_tag=a.name,
            ))


_SHORT_AMBIGUOUS_EXTS = {".r", ".m", ".fa", ".gb", ".nb", ".py", ".db", ".gz", ".xz"}


def _is_plausible_json_url_or_path(cand_raw: str) -> bool:
    """Return True if a string matched in a script block looks like a real URL/path rather than prose."""
    if not cand_raw or len(cand_raw) > 512:
        return False
    # Reject strings containing sentence punctuation or newlines/tabs
    if any(p in cand_raw for p in [". ", ", ", "; ", "! ", "? ", ": ", "\n", "\r", "\t"]):
        return False
    has_url_or_dir_prefix = bool(re.match(r"^(?:https?://|/|\.\.?/|[\w.-]+/)", cand_raw, re.IGNORECASE))
    # If the candidate contains spaces, it MUST start with an explicit URL or path prefix (e.g. "/files/Supplementary Table S1.xlsx")
    if " " in cand_raw and not has_url_or_dir_prefix:
        return False
    # Check if extension is a short 1-2 char extension like .r or .m that can collide with abbreviations
    lower_raw = cand_raw.lower()
    for s_ext in _SHORT_AMBIGUOUS_EXTS:
        if lower_raw.endswith(s_ext):
            if not has_url_or_dir_prefix and not (
                MMC_PATTERN.search(lower_raw)
                or TABLE_FILENAME_PATTERN.search(lower_raw)
                or SUPP_KEYWORD_PATTERN.search(lower_raw)
            ):
                return False
            break
    return True


def _walk_json_for_supp_objects(node, base_url: str, add_func, consumed_names: Optional[Set[str]] = None):
    """Recursively inspect parsed JSON objects for extensionless supplementary/table download entries."""
    if isinstance(node, list):
        for item in node:
            _walk_json_for_supp_objects(item, base_url, add_func, consumed_names)
        return
    if not isinstance(node, dict):
        return

    url_keys = ("downloadUrl", "download_url", "fileUrl", "file_url", "assetUrl", "asset_url", "contentUrl", "suppUrl", "url", "href", "uri")
    name_keys = ("fileName", "filename", "file_name", "name", "title", "label", "caption", "description")
    type_keys = ("mimeType", "mime_type", "contentType", "content_type", "type", "category", "section")

    raw_url = None
    for uk in url_keys:
        v = node.get(uk)
        if isinstance(v, str) and v.strip() and not v.strip().startswith(("#", "javascript:", "mailto:")):
            raw_url = v.strip()
            break

    if raw_url:
        name_vals = [str(node[nk]).strip() for nk in name_keys if isinstance(node.get(nk), str) and str(node[nk]).strip()]
        type_vals = [str(node[tk]).strip() for tk in type_keys if isinstance(node.get(tk), str) and str(node[tk]).strip()]
        combined_meta = " ".join(name_vals + type_vals)
        best_name = name_vals[0] if name_vals else ""

        if consumed_names is not None:
            for nv in name_vals:
                if nv != raw_url and "/" not in nv:
                    consumed_names.add(nv)

        has_data_name = any(check_data_extension(nv) for nv in name_vals)
        has_supp_meta = bool(
            has_data_name
            or is_explicit_supp_table(combined_meta)
            or TABLE_FILENAME_PATTERN.search(combined_meta)
            or SUPP_KEYWORD_PATTERN.search(combined_meta)
            or any(m in combined_meta.lower() for m in ["spreadsheet", "excel", "csv", "zip", "supplementary", "supplement"])
        )
        if has_supp_meta and _is_plausible_json_url_or_path(raw_url):
            cand_url = urllib.parse.urljoin(base_url, raw_url)
            cand_path = urllib.parse.urlsplit(cand_url).path.lower()
            if not any(cand_path.endswith(ext) for ext in [".js", ".css", ".map", ".svg", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf"]):
                if not any(kw in cand_path for kw in ["table-of-contents", "table_of_contents", "/support", "customer-support", "/toc"]):
                    if not is_excluded_url(cand_url, base_url=base_url, text=best_name) and not is_article_figure_url(cand_url, text=best_name):
                        fname = extract_filename_from_url(cand_url, default_name=best_name)
                        add_func(Candidate(
                            url=cand_url,
                            filename=sanitize_filename(fname),
                            link_text=(best_name or "embedded_json_link")[:80],
                            section="embedded_json",
                            match_rule="embedded_script_json_object",
                            source_tag="script",
                        ))

    for val in node.values():
        if isinstance(val, (dict, list)):
            _walk_json_for_supp_objects(val, base_url, add_func, consumed_names)


def _extract_embedded_json_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Extract supplementary URLs embedded inside <script> JSON, LD-JSON, or inline scripts (B3)."""
    valid_data_exts = "|".join(re.escape(ext.lstrip(".")) for ext in SORTED_DATA_EXTENSIONS)
    pattern = re.compile(
        r'''["']('''
        # 1. Scientific data file (allowing spaces in filename when prefixed by a path, e.g. "/files/Supplementary Table S1.xlsx")
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

        if any(kw in content_unescaped.lower() for kw in ["mmc", "supplement", "supp", "table", "dataset", "attached", "asset", "附表", "附录", "source data", "source_data", "sourcedata", "downloadurl", "fileurl"]):
            consumed_names: Set[str] = set()
            # 1. Structured JSON object walk (catches extensionless downloadUrl paired with fileName/title)
            str_stripped = content.strip()
            if str_stripped.startswith(("{", "[")):
                try:
                    parsed_json = json.loads(str_stripped)
                    _walk_json_for_supp_objects(parsed_json, base_url, add_func, consumed_names)
                except Exception:
                    pass
            else:
                decoder = json.JSONDecoder()
                for assign_m in re.finditer(r'(?:=|\bJSON\.parse\s*\()\s*([\[{])', content_unescaped):
                    start_idx = assign_m.start(1)
                    try:
                        parsed_sub, _ = decoder.raw_decode(content_unescaped, start_idx)
                        _walk_json_for_supp_objects(parsed_sub, base_url, add_func, consumed_names)
                    except Exception:
                        pass
            # Also scan inline JS object literals containing downloadUrl/fileUrl + fileName/title
            for obj_m in re.finditer(r'\{[^{}]{10,1000}\}', content_unescaped):
                try:
                    obj_data = json.loads(obj_m.group(0))
                    if isinstance(obj_data, dict):
                        _walk_json_for_supp_objects(obj_data, base_url, add_func, consumed_names)
                except Exception:
                    pass

            # 2. Regex match for direct file URLs/paths
            for m in pattern.finditer(content_unescaped):
                cand_raw = m.group(1).strip()
                if cand_raw in consumed_names:
                    continue
                if not _is_plausible_json_url_or_path(cand_raw):
                    continue
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

def _clean_text_for_declarations(html_content: str) -> str:
    """Strip <script>/<style> blocks and tags so declarations are matched on visible text."""
    cleaned = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html_content, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned)


_CN_DECLARATION_DIGITS = {
    '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}


def _parse_declared_numeral(s: str) -> Optional[int]:
    """Convert an Arabic or Chinese numeral (一二三四五六七八九十) to int, else None."""
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s in _CN_DECLARATION_DIGITS:
        return _CN_DECLARATION_DIGITS[s]
    if s.startswith('十') and len(s) == 2 and s[1] in _CN_DECLARATION_DIGITS:
        return 10 + _CN_DECLARATION_DIGITS[s[1]]
    return None


def detect_declared_file_count(html_content: str) -> Optional[int]:
    """
    Detect an explicit declared number of supplementary *files* (H5, file-count reading).

    Only statements that count files are recognised, e.g. '3 Supplementary Files',
    'Supplementary Material (4 files)', '共 4 个补充文件', '提供 3 个附件'.
    Table identifiers and table ranges ('Tables S1-S3', 'Supplementary Table S10',
    '5 个附表') are deliberately NOT counted here; see detect_declared_table_range.
    Returns None when no file-count statement is present.
    """
    if not html_content:
        return None
    cleaned = _clean_text_for_declarations(html_content)
    found_counts: List[int] = []

    # Pattern 1: 'N Supplementary Files' / 'N Supplementary Materials' (never 'N Supplementary Tables')
    for m in re.finditer(r'\b(\d{1,2})\s+(?:supplementary|supplemental|supporting|additional)\s+(?:files?|datasets?|materials?|items?)\b', cleaned, re.IGNORECASE):
        try:
            val = int(m.group(1))
            if 1 <= val <= 99:
                found_counts.append(val)
        except ValueError:
            pass

    # Pattern 1b: 'N attachments' / 'Total N files'
    for m in re.finditer(r'\b(\d{1,2})\s+attachments?\b|\btotal\s+(\d{1,2})\s+files?\b', cleaned, re.IGNORECASE):
        try:
            val = int(m.group(1) or m.group(2))
            if 1 <= val <= 99:
                found_counts.append(val)
        except ValueError:
            pass

    # Pattern 2: 'Supporting Information (N)' / 'Supplementary Material (4 files)'.
    # A bare parenthesised number after an Information/Material heading counts files;
    # an explicit '(N tables)' belongs to the table reading instead.
    for m in re.finditer(
        r'(?:Supplementary|Supplemental|Supporting|Additional)\s+(?:Information|Material|Materials|Data|Files)\s*\(\s*([1-9]\d?)(?:\s+files?)?\s*\)',
        cleaned,
        re.IGNORECASE,
    ):
        try:
            val = int(m.group(1))
            if 1 <= val <= 99:
                found_counts.append(val)
        except ValueError:
            pass

    # Pattern 5: Chinese file counts: '共 4 个补充文件', '3 个附件', '共四个补充文件'.
    # The supplement qualifier (补充/附录/附加) is mandatory: bare '本文包含 4 个表格'
    # is a main-text table count, not a supplementary declaration (H5).
    for m in re.finditer(
        r'(?:共|含|包括)?\s*([0-9一二两三四五六七八九十]+)\s*个?(?:补充|附录|附加)(?:文件|材料|附件|数据)'
        r'|([0-9一二两三四五六七八九十]+)\s*个?附件',
        cleaned,
    ):
        c_num = _parse_declared_numeral(m.group(1) or m.group(2))
        if c_num is not None and 1 <= c_num <= 99:
            found_counts.append(c_num)

    return max(found_counts) if found_counts else None


def detect_declared_table_range(html_content: str) -> Set[str]:
    """
    Detect the table identities a page declares (H5, table-range reading).

    Returns the set of namespaced table ids (e.g. {'S1', 'S2', 'S3'}) from
    ranges ('Tables S1-S3', 'Tables S1 to S3'), lists ('Tables S1, S2, and S3'),
    single mentions ('Supplementary Table S10' -> {'S10'} only, never S1..S10)
    and bare table counts ('5 个附表' / '3 Supplementary Tables' -> S1..S5 / S1..S3).
    A bare table count expands from 1 because such a statement declares that
    many tables without naming them; a single numbered mention does not imply
    the tables below it. Returns an empty set when nothing is declared.
    """
    if not html_content:
        return set()
    cleaned = _clean_text_for_declarations(html_content)
    declared = set(supplementary_table_ids(cleaned))

    # Bare table counts with no ids: '3 Supplementary Tables', 'Supplementary Tables (5)'.
    for m in re.finditer(r'\b(\d{1,2})\s+(?:supplementary|supplemental|supporting|additional)\s+tables?\b', cleaned, re.IGNORECASE):
        try:
            count = int(m.group(1))
            if 1 <= count <= 99:
                declared.update(f"S{i}" for i in range(1, count + 1))
        except ValueError:
            pass
    for m in re.finditer(
        r'(?:Supplementary|Supplemental|Supporting|Additional)\s+Tables?\s*\(\s*([1-9]\d?)(?:\s+tables?)?\s*\)',
        cleaned,
        re.IGNORECASE,
    ):
        try:
            count = int(m.group(1))
            if 1 <= count <= 99:
                declared.update(f"S{i}" for i in range(1, count + 1))
        except ValueError:
            pass

    # Bare Chinese table counts: '5 个附表' -> S1..S5, '5 个附录' -> A1..A5.
    for m in re.finditer(
        r'(?:共|含|包括)?\s*([0-9一二两三四五六七八九十]+)\s*个?(附表|补充表|附录表|附加表|附录)',
        cleaned,
    ):
        c_num = _parse_declared_numeral(m.group(1))
        if c_num is not None and 1 <= c_num <= 99:
            prefix = "A" if m.group(2) == "附录" else "S"
            declared.update(f"{prefix}{i}" for i in range(1, c_num + 1))

    return declared


def detect_declared_supplement_count(html_content: str) -> Optional[int]:
    """
    Compatibility entry point: now returns the *file-count* reading only.

    Historically this returned max() across file counts, table ranges and the
    highest table mention, which inflated the declared number (e.g. a page
    with '3 Supplementary Files' plus a mention of 'Table S10' reported 10).
    It is kept so existing callers keep working, but its semantics changed in
    the P0 H5 fix: it is exactly detect_declared_file_count(). For the
    table-range reading use detect_declared_table_range(), and for the
    completeness verdict use is_result_obviously_incomplete(), which
    consults both readings separately instead of mixing them.
    """
    return detect_declared_file_count(html_content)


_DOM_SUPP_SECTIONS = {
    "supplementary_section",
    "elsevier_component",
    "springer_nature_download",
    "nature_supplementary",
    "nature_source_data",
    "wiley_supplement",
    "plos_supplement",
    "pmc_supplementary_section",
    "pmc_bin_attachment",
    "pmc_featured_suppl",
}


_CN_TABLE_DIGITS = {
    '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
    '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}


def _parse_table_number(raw: str) -> Optional[str]:
    """Normalize a table number to its canonical digit form ('01' -> '1', '一' -> '1').

    Hierarchical numbers keep their dotted parts ('1.1' stays '1.1').
    Returns None for values that are not a plain or Chinese numeral.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw[0].isdigit():
        parts = raw.split(".")
        try:
            return ".".join(str(int(p)) for p in parts)
        except ValueError:
            return None
    if all(ch in _CN_TABLE_DIGITS for ch in raw):
        if '十' in raw:
            head, _, tail = raw.partition('十')
            tens = _CN_TABLE_DIGITS.get(head, 1) if head else 1
            ones = _CN_TABLE_DIGITS.get(tail, 0) if tail else 0
            return str(tens * 10 + ones)
        if len(raw) == 1:
            return str(_CN_TABLE_DIGITS[raw])
    return None


# One table-id token: an optional table-word prefix, an optional S/A/E letter,
# and a (possibly hierarchical or Chinese) number. A trailing table word
# ('S4 Table') also counts as explicit table context.
_TABLE_TOKEN_RE = re.compile(
    r"(?:(?P<pw>(?:supplementary|supplemental|supporting|additional|extended\s+data|appendix)"
    r"\b(?:\s+(?:information|info|materials?|data))?(?:\s+tables?)?"
    r"|附表|附录|tables?|tabs?\.?|tbls?\.?)\s*)?"
    r"(?:(?P<letter>[SAE])[-_.\s]*)?(?P<num>\d+(?:\.\d+)*|[一二两三四五六七八九十]+)"
    r"(?P<post>\s+(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?|表格|数据|文件))?",
    re.IGNORECASE,
)
_TABLE_RANGE_SEP_RE = re.compile(r"^\s*(?:-|\bto\b|至|~|到)\s*$", re.IGNORECASE)
_TABLE_LIST_SEP_RE = re.compile(r"^\s*(?:[,、，/]\s*)?(?:and|&|和|及|与)?\s*$", re.IGNORECASE)
# URLs and DOIs must never be read for table ids: a DOI path segment such as
# 's41467-025-61134-0' otherwise parses as S41467/S25/S61134/S0 (50-paper test,
# Cause A). Masked with same-length spaces so token offsets stay valid.
_URL_DOI_MASK_RE = re.compile(
    r"(?:https?://|doi\.org/)[^\s\"'<>\]]+|\b10\.\d{4,9}/[^\s\"'<>\]]+",
    re.IGNORECASE,
)


def _table_token_namespace(pw: Optional[str], letter: Optional[str], post: Optional[str]) -> Optional[str]:
    """Namespace for one token: an explicit S/A/E letter wins, else the prefix word decides."""
    if letter:
        return letter.upper()
    if pw:
        low = pw.lower()
        if "appendix" in low or "附录" in pw:
            return "A"
        if "extended" in low:
            return "E"
        if any(w in low for w in ("supplement", "supporting", "additional", "附表")):
            return "S"
    if post:
        return None
    return None


def _expand_table_range(start: str, end: str) -> Optional[List[str]]:
    """Expand '1'->'3' to ['1','2','3'] and '1.1'->'1.3' to ['1.1','1.2','1.3'] (same major)."""
    if "." not in start and "." not in end:
        try:
            s, e = int(start), int(end)
        except ValueError:
            return None
        if 1 <= s <= e <= 999 and e - s <= 99:
            return [str(i) for i in range(s, e + 1)]
        return None
    s_parts, e_parts = start.split("."), end.split(".")
    if len(s_parts) == len(e_parts) == 2 and s_parts[0] == e_parts[0]:
        try:
            s, e = int(s_parts[1]), int(e_parts[1])
            major = int(s_parts[0])
        except ValueError:
            return None
        if 1 <= major <= 999 and 0 <= s <= e <= 999 and e - s <= 99:
            return [f"{major}.{i}" for i in range(s, e + 1)]
    return None


def supplementary_table_ids(text):
    """Return namespaced table identities (S1, S1.1, A1, E1), never bare ordinal counts.

    Hierarchical numbers keep their suffix ('Table S1.1' -> 'S1.1', not 'S1').
    Ranges expand: 'Tables S1 to S3', bare 'S1-S3', 'Appendix A1-A3' and
    'Extended Data Tables 1-3' all enumerate every member. Lists ('S1, S2 and S3')
    enumerate their members. A bare 'S1' with no table context or range/list
    neighbour is ignored, as is a bare main-text 'Table 1'.
    """
    result = set()
    text = str(text or "")
    # Normalize unicode dashes so range separators are uniform.
    text = re.sub(r"[‐‑‒–—―]", "-", text)
    # Blank out URLs/DOIs (same length, offsets preserved) before scanning.
    text = _URL_DOI_MASK_RE.sub(lambda m: " " * len(m.group(0)), text)
    tokens = []  # (namespace-or-None, number, has_context, start, end)
    for m in _TABLE_TOKEN_RE.finditer(text):
        pw = m.group("pw")
        letter = m.group("letter")
        post = m.group("post")
        num = _parse_table_number(m.group("num"))
        if num is None:
            continue
        # Reject implausible table numbers: supplementary tables are numbered
        # from 1 and stay well under 1000; larger digit runs are article
        # numbers, years or accessions, not table ids.
        if num[0].isdigit():
            num_parts = num.split(".")
            try:
                if int(num_parts[0]) < 1 or any(int(p) > 999 for p in num_parts):
                    continue
            except ValueError:
                continue
        # A Latin prefix word glued to a bare number ('Supplementary3') is not
        # a table id unless an S/A/E letter is present; Chinese 附表/附录
        # legitimately take the number with no space.
        if pw and not letter and pw not in ("附表", "附录"):
            id_start = m.start("num")
            if id_start == m.end("pw"):
                continue
        # A bare number with neither a prefix word, a letter, nor a trailing
        # table word is not a table id on its own; kept as a weak token so it
        # can still inherit the namespace of a range/list neighbour
        # ('Supplementary Tables 2-5' -> S2..S5).
        if not pw and not letter and not post:
            tokens.append({"ns": None, "num": num, "context": False, "bare": True, "start": m.start(), "end": m.end()})
            continue
        ns = _table_token_namespace(pw, letter, post)
        if ns is None:
            continue
        tokens.append({"ns": ns, "num": num, "context": bool(pw or post), "bare": False, "start": m.start(), "end": m.end()})

    # Group consecutive tokens joined only by a range/list separator.
    groups = []  # each: [(token, sep_from_previous)] with sep None for the first
    for tok in tokens:
        if not groups:
            groups.append([(tok, None)])
            continue
        prev_tok = groups[-1][-1][0]
        between = text[prev_tok["end"]:tok["start"]]
        if _TABLE_RANGE_SEP_RE.match(between):
            groups[-1].append((tok, "range"))
        elif between.strip() and _TABLE_LIST_SEP_RE.match(between):
            groups[-1].append((tok, "list"))
        else:
            groups.append([(tok, None)])

    for group in groups:
        if len(group) == 1:
            tok = group[0][0]
            # A lone bare 'S1' (no table word anywhere) is not evidence.
            if tok["context"]:
                result.add(f"{tok['ns']}{tok['num']}")
            continue
        # Weak (bare-number) members inherit the namespace of the nearest
        # preceding namespaced member of the same group.
        resolved = []
        last_ns = None
        for tok, sep in group:
            ns = tok["ns"] or last_ns
            if ns is None:
                continue
            last_ns = ns
            resolved.append(({"ns": ns, "num": tok["num"], "context": tok["context"], "bare": tok.get("bare", False)}, sep))
        expanded: Set[str] = set()
        for idx, (tok, sep) in enumerate(resolved):
            if sep == "range" and idx > 0:
                prev_tok = resolved[idx - 1][0]
                if prev_tok["ns"] == tok["ns"]:
                    span = _expand_table_range(prev_tok["num"], tok["num"])
                    if span is not None:
                        expanded.update(f"{tok['ns']}{n}" for n in span)
                        continue
                    if tok.get("bare"):
                        # A failed range expansion must not mint a table id
                        # from a bare number (e.g. a year after 'S1, 2020-2021').
                        continue
            expanded.add(f"{tok['ns']}{tok['num']}")
        result.update(expanded)
    return result


def declared_file_count(text):
    """Only explicit file counts; table ranges are a different constraint."""
    patterns = [
        r'\b(\d+)\s+(?:supplementary|supplemental|supporting|additional)\s+files?\b',
        r'(?:supplementary|supplemental|supporting|additional)\s+files?\s*\(\s*(\d+)\s*\)',
        r'(?:supplementary|supporting)\s+(?:information|material)\s*\(\s*(\d+)\s+files?\s*\)',
        r'(\d+)\s*个?\s*(?:补充文件|补充附件|附加文件)',
    ]
    values = [int(m[1]) for pattern in patterns for m in re.finditer(pattern, text, re.I)]
    return max(values, default=0)


def candidate_evidence(candidates):
    """Expose inherited evidence only when every child in that group is present."""
    urls = {c.url for c in candidates}
    evidence = []
    for candidate in candidates:
        evidence.append(candidate.to_dict())
        for parent in candidate.extra_info.get('parent_evidence', []):
            if set(parent.get('required_urls', [])) <= urls:
                evidence.append(parent)
    return evidence


def covered_table_ids(candidates):
    """Table ids covered by candidates (H6).

    Prefers each candidate's explicit covered_table_ids field (filled during
    extraction); when a candidate has no explicit ids, falls back to inferring
    them from its link text, filename, and attributes, as before.
    """
    covered = set()
    for item in candidate_evidence(candidates):
        explicit = item.get('covered_table_ids')
        if explicit:
            covered.update(str(t) for t in explicit)
            continue
        for value in (item.get('link_text'), item.get('filename'), item.get('extra_info', {}).get('attributes', {})):
            covered.update(supplementary_table_ids(value))
    return covered


def is_result_obviously_incomplete(candidates, html_content, declared_count=None):
    if not html_content:
        return (True, 'Empty page content') if not candidates else (False, '')
    soup = html_content if isinstance(html_content, BeautifulSoup) else BeautifulSoup(html_content, DEFAULT_PARSER)
    raw_html = html_content if isinstance(html_content, str) else str(html_content)
    # H5: the two readings are judged separately, both on visible text only
    # (script/style content is not a declaration). A table range or a high
    # table number is never treated as a file count.
    expected_tables = detect_declared_table_range(raw_html)
    missing = expected_tables - covered_table_ids(candidates)
    file_count = detect_declared_file_count(raw_html)
    # Deduplicate identical URLs but never substitute table counts for files.
    if file_count and len({c.url for c in candidates}) < file_count:
        return True, f'File count ({len({c.url for c in candidates})}) is less than declared count ({file_count} files)'
    supp_keywords = ('supplement', 'suppl', 'appendix', 'supporting-info', 'supporting_info',
                     'esm', 'additional-file', 'extended-data', 'extended_data', '补充', '附表', '附录', '支撑材料')
    sections = soup.find_all(lambda el: el.name in ('section', 'div') and any(
        kw in (el.get('id', '') + ' ' + ' '.join(el.get('class', []))).lower() for kw in supp_keywords))
    evidence = candidate_evidence(candidates)
    meta_satisfies = bool(declared_count and len([c for c in candidates if c.section == 'meta_tags']) >= declared_count)
    for section in sections:
        targets = [extract_target_url_from_tag(tag) for tag in section.find_all(['a','button','input','form','div','span','iframe','object','embed'])]
        targets = [t for t in targets if t]
        selectors = [tag.get('data-journal-download-selector') for tag in section.find_all(attrs={'data-journal-download-selector': True})]
        matched = any(
            (item.get('extra_info', {}).get('browser_selector') in selectors and bool(selectors))
            or any(urllib.parse.urljoin(item.get('source_url') or item['url'], target).split('#')[0] == item['url'].split('#')[0] for target in targets)
            for item in evidence)
        if not matched and not meta_satisfies:
            # A named section can be fulfilled by corresponding files on a mirror.
            section_tables = supplementary_table_ids(section.get_text(' ', strip=True))
            if not section_tables or not section_tables <= covered_table_ids(candidates):
                return True, 'Supplementary container found in HTML but 0 candidates extracted from it (unresolved section)'
    if missing:
        return True, 'Missing supplementary tables: ' + ', '.join(sorted(missing))
    if not candidates:
        return True, 'Zero supplementary candidates found'
    # Retain legacy count hints only when there is no typed table declaration.
    if not expected_tables and declared_count and len(candidates) < declared_count:
        return True, f'Candidate count ({len(candidates)}) is less than declared count ({declared_count})'
    return False, ''


# ── File Validation (Issue #5: Removing Fixed 1 KB Threshold) ────────────────

NON_HTML_DATA_EXTS = {
    ".xlsx", ".xls", ".csv", ".tsv", ".zip", ".gz", ".tar", ".7z", ".rar",
    ".txt", ".json", ".xml", ".r", ".py", ".ipynb", ".m", ".nb",
    ".cif", ".pdb", ".mol", ".sdf", ".xyz", ".fasta", ".fa", ".gb",
    ".nii", ".dcm", ".h5", ".hdf5", ".mat", ".pkl", ".rda", ".sav",
    ".docx", ".doc", ".pptx", ".ppt", ".pdf",
}

_ZIP_MAGIC_EXTS = {".xlsx", ".docx", ".pptx", ".zip", ".npz"}
_GZIP_MAGIC_SUFFIXES = (".gz", ".tgz")
_BZIP2_MAGIC_SUFFIXES = (".bz2",)
_XZ_MAGIC_SUFFIXES = (".xz", ".txz")


# ── Blocked-page detection (H1) ──────────────────────────────────────────────
# A fetched article page is "blocked" when it is a Cloudflare / Turnstile
# challenge, an access-denied page, or a login wall rather than the article.
# Kept here (next to file validation) so both downloaders and the
# BrowserSkill adapter can use it without a circular import: supp_finder
# imports neither browserskill_adapter nor playwright_utils, while both of
# those are imported by the downloaders. The title markers below subsume
# the four keywords BrowserSkill used to check on its own
# ("just a moment" / "access denied" / "attention required" /
# "verify you are human").

_BLOCKED_TITLE_MARKERS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "access denied",
    "403 forbidden",
    "security check",
    "checking your browser",
    "please verify",
    "are you human",
    "human verification",
    "sign in to",
    "log in to",
    "institutional login",
    "login required",
    "sign in required",
    "please sign in",
    "please log in",
)

_BLOCKED_TITLE_EXACT = {"sign in", "log in", "login", "sign-in", "log-in"}

# Body phrases that only appear when the page itself is a challenge / login
# wall. Deliberately NOT included: a bare "Sign in" navigation link, which
# normal article pages carry in their header.
_BLOCKED_BODY_MARKERS = (
    "just a moment",
    "attention required",
    "verify you are human",
    "please verify you are human",
    "checking your browser",
    "challenge-platform",
    "cf-browser-verification",
    "turnstile",
    "human verification",
    "access denied",
    "403 forbidden",
    "sign in to access",
    "log in to access",
    "sign in to view",
    "log in to view",
    "sign in to continue",
    "log in to continue",
    "login required",
    "sign-in required",
    "authentication required",
    "you must be logged in",
    "institutional login",
)


def is_blocked_page(page_html: Optional[str]) -> bool:
    """True if *page_html* looks like a challenge / access-denied / login-wall page.

    Checks the <title> and the visible body text. A normal article page
    that merely links to a sign-in page is not blocked.
    """
    if not page_html or not isinstance(page_html, str):
        return False
    title_match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.I | re.S)
    title = (title_match.group(1) if title_match else "").strip().lower()
    if title:
        if title in _BLOCKED_TITLE_EXACT:
            return True
        if any(marker in title for marker in _BLOCKED_TITLE_MARKERS):
            return True
    text = re.sub(r"<[^>]+>", " ", page_html)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return any(marker in text for marker in _BLOCKED_BODY_MARKERS)


def page_fetch_is_failure(html: Optional[str], status: Optional[int] = None) -> bool:
    """Caller rule for H1: a page fetch failed when the HTTP status is an
    error (>= 400), or when the page is a blocked page. An unknown status
    (None, e.g. BrowserSkill exposes no navigation status) is only a
    failure when the page shows challenge / login features; on its own it
    is accepted, since no better signal exists. A ``status < 400`` alone
    must never be treated as success without this check.
    """
    if status is not None and status >= 400:
        return True
    return bool(html) and is_blocked_page(html)


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
                header_bytes = f.read(4096)
        except Exception as e:
            return False, f"Cannot read file header: {e}"
    else:
        header_bytes = content_bytes[:4096]

    # Normalize response headers
    hdr = {k.lower(): v for k, v in (headers or {}).items()}
    ct = (hdr.get("content-type") or "").lower()
    filepath_lower = filepath.lower()
    ext = os.path.splitext(filepath_lower)[1]

    # 1. Binary Magic Bytes Verification
    # ZIP / XLSX / DOCX / PPTX: PK\x03\x04
    if header_bytes.startswith(b"PK\x03\x04") or header_bytes.startswith(b"PK\x05\x06"):
        try:
            with zipfile.ZipFile(filepath) as archive:
                names = set(archive.namelist())
                if not names:
                    return False, "Empty ZIP archive"
                required = {".xlsx": "xl/workbook.xml", ".docx": "word/document.xml", ".pptx": "ppt/presentation.xml"}
                if ext in required and (required[ext] not in names or "[Content_Types].xml" not in names):
                    return False, "Missing Office document structure"
                # Validate CRC without allowing enormous archives to exhaust resources.
                expanded = sum(item.file_size for item in archive.infolist())
                if expanded > 512 * 1024 * 1024:
                    return True, "Valid ZIP directory structure; CRC deferred for archives above 512 MiB"
                if archive.testzip() is not None:
                    return False, "ZIP member checksum mismatch"
        except (OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
            return False, f"Invalid ZIP/Office container: {exc}"
        return True, f"Valid ZIP/Office document structure ({file_size} bytes)"

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

    # Parquet / Feather / BZip2 / XZ / SQLite / NumPy signatures
    if header_bytes.startswith(b"PAR1"):
        return True, f"Valid Parquet signature ({file_size} bytes)"
    if header_bytes.startswith(b"ARROW1"):
        return True, f"Valid Arrow/Feather signature ({file_size} bytes)"
    if header_bytes.startswith(b"BZh"):
        return True, f"Valid BZip2 signature ({file_size} bytes)"
    if header_bytes.startswith(b"\xfd7zXZ\x00"):
        return True, f"Valid XZ signature ({file_size} bytes)"
    if header_bytes.startswith(b"SQLite format 3\x00"):
        return True, f"Valid SQLite database signature ({file_size} bytes)"
    if header_bytes.startswith(b"\x93NUMPY"):
        return True, f"Valid NumPy array signature ({file_size} bytes)"

    # 2. Check for HTML content (either via HTML tags or text/html Content-Type)
    header_lower = header_bytes.lower()
    is_html_tag = any(tag in header_lower for tag in [b"<!doctype html", b"<html", b"<head", b"<body", b"<table"])
    is_html_ct = "text/html" in ct

    hard_error_indicators = [
        b"challenge-platform",
        b"cf-browser-verification",
        b"just a moment",
        b"access denied",
        b"403 forbidden",
        b"404 not found",
        b"page not found",
        b"login required",
        b"institutional login",
        b"login to institutional account",
        b"paywall",
    ]
    soft_auth_indicators = [
        b"sign in",
        b"purchase article",
        b"subscribe to",
    ]

    if is_html_tag or is_html_ct:
        html_sample = content_bytes[:65536] if content_bytes is not None else b""
        if not html_sample and os.path.exists(filepath):
            try:
                with open(filepath, "rb") as f:
                    html_sample = f.read(65536)
            except Exception:
                html_sample = header_bytes
        sample_lower = html_sample.lower()

        # Support legitimate HTML-table exported .xls files (common in older journals/publishers)
        if ext == ".xls":
            has_hard_err = any(ind in sample_lower for ind in hard_error_indicators + soft_auth_indicators)
            if not has_hard_err and b"<table" in sample_lower and (b"<tr" in sample_lower or b"<td" in sample_lower or b"<th" in sample_lower):
                return True, f"Valid HTML-table exported .xls spreadsheet ({file_size} bytes)"
            return False, f"Expected .xls data file but received HTML document ({file_size} bytes)"

        # If the file was expected to be a binary/data file (e.g. .xlsx, .pdf, .zip, .csv)
        # but content is HTML, it is an error/login page disguised as data file!
        if ext in NON_HTML_DATA_EXTS:
            return False, f"Expected {ext} data file but received HTML document ({file_size} bytes)"

        # Hard error/challenge indicators always reject
        for indicator in hard_error_indicators:
            if indicator in header_lower or indicator in sample_lower:
                return False, f"HTML error/challenge/login page detected: {indicator.decode('latin1')}"

        # Check if it contains a genuine data table structure (D1)
        has_table_structure = (
            b"<table" in sample_lower
            and b"<tr" in sample_lower
            and (b"<td" in sample_lower or b"<th" in sample_lower)
            and b'type="password"' not in sample_lower
            and b"type='password'" not in sample_lower
        )

        # Soft auth/navigation phrases ("sign in", "subscribe to", "purchase article"):
        # Reject if there is no genuine table structure; allow if it is a standalone HTML table view with site nav
        for indicator in soft_auth_indicators:
            if indicator in header_lower:
                if not has_table_structure:
                    return False, f"HTML error/challenge/login page detected: {indicator.decode('latin1')}"

        if ext in HTML_EXTENSIONS or ext == ".bin" or not ext:
            if has_table_structure:
                return True, f"Valid standalone HTML table attachment ({file_size} bytes)"
            if ext in HTML_EXTENSIONS:
                return False, f"Received HTML document without table structure ({file_size} bytes)"

        return False, f"Expected data file but received HTML document ({file_size} bytes)"

    # 2.5 Strict binary format enforcement: if extension requires binary magic bytes and did not match above, reject plain text / JSON error!
    if ext in _ZIP_MAGIC_EXTS:
        return False, f"Expected binary {ext} (ZIP/OOXML) signature but received non-ZIP content ({file_size} bytes)"
    if ext == ".pdf":
        return False, f"Expected binary .pdf (%PDF) signature but received non-PDF content ({file_size} bytes)"
    if filepath_lower.endswith(_GZIP_MAGIC_SUFFIXES):
        return False, f"Expected binary GZIP signature for {ext} but received non-GZIP content ({file_size} bytes)"
    if ext == ".7z":
        return False, f"Expected binary .7z signature but received non-7z content ({file_size} bytes)"
    if ext == ".rar":
        return False, f"Expected binary .rar signature but received non-RAR content ({file_size} bytes)"
    if ext in {".h5", ".hdf5"}:
        return False, f"Expected binary HDF5 signature for {ext} but received non-HDF5 content ({file_size} bytes)"
    if ext == ".parquet":
        return False, f"Expected binary Parquet signature but received non-Parquet content ({file_size} bytes)"
    if ext in {".feather", ".arrow"}:
        return False, f"Expected binary Arrow/Feather signature but received non-Arrow content ({file_size} bytes)"
    if filepath_lower.endswith(_BZIP2_MAGIC_SUFFIXES):
        return False, f"Expected binary BZip2 signature but received non-BZip2 content ({file_size} bytes)"
    if filepath_lower.endswith(_XZ_MAGIC_SUFFIXES):
        return False, f"Expected binary XZ signature but received non-XZ content ({file_size} bytes)"
    if ext in {".db", ".sqlite", ".sqlite3"}:
        return False, f"Expected binary SQLite signature but received non-SQLite content ({file_size} bytes)"
    if ext == ".npy":
        return False, f"Expected binary NumPy signature but received non-NumPy content ({file_size} bytes)"
    if ext in {".doc", ".ppt"}:
        if not (ext == ".doc" and header_bytes.startswith(b"{\\rtf")):
            return False, f"Expected binary Office OLE2 signature for {ext} but received non-Office content ({file_size} bytes)"
    if ext == ".xls":
        # Allow XML Spreadsheet 2003 (<?xml ... <Workbook)
        if b"<?xml" in header_lower or b"<workbook" in header_lower:
            return True, f"Valid XML Spreadsheet .xls ({file_size} bytes)"
        return False, f"Expected binary OLE2 or table .xls signature but received unrecognized content ({file_size} bytes)"

    # 3. Check for JSON or plain-text error responses across all text/scientific extensions
    stripped_lower = header_lower.lstrip()
    if ext == ".json" or stripped_lower.startswith(b"{"):
        if (
            b'"error"' in header_lower
            or b'"errors"' in header_lower
            or b'"status": 40' in header_lower
            or b'"statuscode": 40' in header_lower
            or b'"status":40' in header_lower
            or b'"statuscode":40' in header_lower
        ):
            return False, f"JSON error response detected ({file_size} bytes)"
    if file_size < 1024 and stripped_lower.startswith((
        b"403 forbidden", b"404 not found", b"access denied", b"error 403", b"error 404", b"unauthorized",
    )):
        return False, f"Plain-text HTTP error response detected ({file_size} bytes)"
    if file_size < 10 and not header_bytes.strip():
        return False, "File contains only whitespace"

    if ext in {
        ".csv", ".tsv", ".txt", ".json", ".xml", ".r", ".py", ".m", ".cif",
        ".fasta", ".fa", ".gb", ".pdb", ".mol", ".sdf", ".xyz", ".bed", ".vcf", ".gtf", ".gff",
    }:
        return True, f"Valid text data file ({file_size} bytes)"

    # 4. General non-empty file
    return True, f"Non-empty binary/text file ({file_size} bytes)"


def ensure_inferred_extension_on_disk(
    filepath: str,
    output_dir: str,
    url: str,
    manifest: Optional[Dict[str, Any]] = None,
    content_type: str = "",
) -> str:
    """
    If a downloaded file on disk has a generic `.bin` extension or no extension at all,
    inspect its magic bytes / content_type, rename it on disk with disambiguation,
    and update the `.download_manifest.json` mapping.
    """
    if not filepath or not os.path.exists(filepath):
        return filepath
    fname = os.path.basename(filepath)
    base, ext = os.path.splitext(fname)
    if ext.lower() != ".bin" and ext != "":
        return filepath

    try:
        with open(filepath, "rb") as f:
            head = f.read(8192)
    except Exception:
        return filepath

    inferred = infer_file_extension(head, content_type)
    if not inferred:
        return filepath

    new_fname = (base if ext.lower() == ".bin" else fname) + inferred
    m = manifest if manifest is not None else load_download_manifest(output_dir)
    final_fname = disambiguate_target_filename(output_dir, new_fname, url, m)
    new_filepath = os.path.join(output_dir, final_fname)

    if os.path.abspath(new_filepath) != os.path.abspath(filepath):
        try:
            os.replace(filepath, new_filepath)
        except Exception:
            return filepath

    if fname in m and final_fname != fname:
        m.pop(fname, None)
    record_manifest_entry(m, final_fname, url)
    save_download_manifest(output_dir, m)
    return new_filepath


# ── Title Extraction, Sanitization & Directory Migration ─────────────────────

_BLOCKED_OR_PLACEHOLDER_TITLES = {
    "", "unknown_article", "downloaded_article", "supplements",
    "just a moment", "just a moment...", "access denied",
    "attention required! _ cloudflare", "attention required! | cloudflare",
    "403 forbidden", "404 not found", "page not found",
    "security check", "verify you are human", "institutional login",
    "sign in", "log in", "login", "redirecting", "error", "elsevier",
    "sciencedirect", "springerlink", "wiley online library", "nature",
}


def is_placeholder_or_blocked_title(title: Optional[str]) -> bool:
    """Return True if a title is a placeholder, empty, or came from an anti-bot/error challenge page."""
    if not title:
        return True
    t_low = title.strip().lower().rstrip(". ")
    if not t_low or t_low in _BLOCKED_OR_PLACEHOLDER_TITLES:
        return True
    if any(t_low.startswith(prefix) for prefix in (
        "just a moment", "access denied", "403 ", "404 ", "attention required",
        "cloudflare", "error ", "security check", "verify you are human",
    )):
        return True
    return False


def migrate_or_merge_article_dir(old_dir: str, new_dir: str) -> str:
    """
    Safely rename or merge `old_dir` into `new_dir` when a real article title is resolved
    after initial downloads or directory creation.
    Merges `.download_manifest.json` and moves all files so nothing is stranded in `old_dir`.
    """
    if not old_dir or not new_dir:
        return new_dir or old_dir
    old_abs = os.path.abspath(old_dir)
    new_abs = os.path.abspath(new_dir)
    if old_abs == new_abs:
        return new_dir

    if not os.path.exists(old_abs):
        os.makedirs(new_abs, exist_ok=True)
        return new_dir

    if not os.path.exists(new_abs):
        try:
            os.makedirs(os.path.dirname(new_abs), exist_ok=True)
            os.rename(old_abs, new_abs)
            return new_dir
        except Exception:
            os.makedirs(new_abs, exist_ok=True)

    # Both old_abs and new_abs exist: merge manifest and files from old_abs into new_abs
    old_manifest = load_download_manifest(old_abs)
    new_manifest = load_download_manifest(new_abs)

    try:
        for entry in list(os.listdir(old_abs)):
            if entry == ".download_manifest.json":
                continue
            src_path = os.path.join(old_abs, entry)
            if os.path.isfile(src_path):
                old_entry = _normalize_manifest_entry(old_manifest.get(entry))
                url = manifest_entry_url(old_entry)
                dest_fname = disambiguate_target_filename(
                    new_abs, entry, url or f"__migrated__:{src_path}", new_manifest
                )
                dest_path = os.path.join(new_abs, dest_fname)
                try:
                    os.replace(src_path, dest_path)
                    if old_entry is not None:
                        new_manifest[dest_fname] = old_entry
                except Exception:
                    pass
    except Exception:
        pass

    for k, v in old_manifest.items():
        if k not in new_manifest and os.path.exists(os.path.join(new_abs, k)):
            new_manifest[k] = v
    if new_manifest:
        save_download_manifest(new_abs, new_manifest)

    # Remove old_manifest if old_abs has no other files left
    old_manifest_path = os.path.join(old_abs, ".download_manifest.json")
    try:
        remaining = [e for e in os.listdir(old_abs) if e != ".download_manifest.json"]
        if not remaining:
            if os.path.exists(old_manifest_path):
                os.remove(old_manifest_path)
            os.rmdir(old_abs)
    except Exception:
        pass

    return new_dir


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


# JS: count links that look like supplementary downloads (M13 settle signal).
_SUPP_LINK_COUNT_JS = """() => {
    return document.querySelectorAll(
        "a[href*='supp'], a[href*='mmc'], a[href*='esm'], " +
        "a[href$='.xlsx'], a[href$='.xls'], a[href$='.csv'], a[href$='.zip'], " +
        "[data-test*='supplementary'], [data-test*='source-data']"
    ).length;
}"""

# JS: open closed <details> and click collapsed supplementary triggers (M13).
# Scrolling is driven from Python (segmented scroll) instead of here.
_ACCORDION_CLICK_JS = """() => {
    let clicked = 0;
    // 1. Open only closed <details> elements (and do not click their <summary> afterward)
    document.querySelectorAll('details:not([open])').forEach(d => {
        d.open = true;
        clicked++;
    });

    // 2. Click collapsed buttons, tabs, or accordion triggers matching supplementary keywords
    const suppKeywords = [
        'supp', 'appendi', 'extended', 'additional', 'data',
        'table', 'material', 'source', 'esm', 'file',
        '附', '补充', '支撑'
    ];
    const selectors = [
        'button[aria-expanded="false"]',
        'a[aria-expanded="false"]',
        '[role="button"][aria-expanded="false"]',
        '[role="tab"][aria-expanded="false"]',
        '[data-toggle="collapse"]:not([aria-expanded="true"])',
        '.accordion-header:not([aria-expanded="true"])',
        '.accordion-button.collapsed',
        '.accordion-button[aria-expanded="false"]'
    ].join(', ');
    document.querySelectorAll(selectors).forEach(el => {
        if (el.getAttribute('aria-expanded') === 'true') return;
        if (el.classList && (el.classList.contains('active') || el.classList.contains('is-open'))) return;
        if (el.tagName && el.tagName.toLowerCase() === 'summary') {
            const parentDetails = el.closest('details');
            if (parentDetails && parentDetails.open) return;
        }
        const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').toLowerCase();
        if (/delete|remove|edit|upload|publish|purchase|删除|移除|编辑|上传|发布|购买/i.test(text)) return;
        const idClass = ((el.id || '') + ' ' + (typeof el.className === 'string' ? el.className : '')).toLowerCase();
        if (suppKeywords.some(kw => text.includes(kw) || idClass.includes(kw))) {
            try { el.click(); clicked++; } catch (e) {}
        }
    });
    return clicked;
}"""


def _js_int(page, expr: str, default: int = 0) -> int:
    """Evaluate a JS expression returning an int; default when unavailable (M13)."""
    try:
        return int(page.evaluate(expr))
    except Exception:
        return default


def expand_page_accordions_action(page):
    """Playwright page action for Scrapling: expand details, tabs, and accordion panels without re-closing open ones.

    M13: after expanding, scroll to the bottom in viewport-sized segments
    (pausing at each step so lazy loading can trigger), then wait until the
    DOM settles -- the supplementary-link count must stay unchanged across
    two consecutive polls -- with a bounded total wait (~5s) instead of a
    single fixed 600-1000ms sleep. The caller signature is unchanged.
    """
    try:
        clicked_count = 0
        has_evaluate = hasattr(page, "evaluate")
        has_wait = hasattr(page, "wait_for_timeout")
        if has_evaluate:
            clicked_count = _js_int(page, _ACCORDION_CLICK_JS, 0)
        if not has_wait:
            return
        if not has_evaluate:
            # No JS evaluation available: keep the old fixed wait.
            page.wait_for_timeout(1000 if clicked_count else 600)
            return
        # Segmented scroll to the bottom to trigger lazy loading.
        height = _js_int(page, "() => (document.body ? document.body.scrollHeight : 0)", 0)
        viewport = _js_int(page, "() => (window.innerHeight || 800)", 800)
        if height > 0 and viewport > 0:
            pos = 0
            while pos < height:
                pos += viewport
                try:
                    page.evaluate(f"() => window.scrollTo(0, {min(pos, height)})")
                except Exception:
                    break
                page.wait_for_timeout(250)
            try:
                page.evaluate(f"() => window.scrollTo(0, {height})")
            except Exception:
                pass
        # Debounced settle wait: stop once the supplementary-link count is
        # stable across two polls; give up after 10 polls (~5s).
        prev_count = None
        stable_polls = 0
        for _ in range(10):
            count = _js_int(page, _SUPP_LINK_COUNT_JS, -1)
            if count == prev_count:
                stable_polls += 1
                if stable_polls >= 1:
                    break
            else:
                stable_polls = 0
                prev_count = count
            page.wait_for_timeout(500)
    except Exception:
        pass

