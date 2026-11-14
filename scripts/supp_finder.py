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
import shutil
import sys
import tempfile
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from bs4 import BeautifulSoup, NavigableString


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
    r"(?<![a-z])(?:supplementary|supplemental|supplements?|suppl?(?![a-z])|supp(?:data|files?|tables?|info|materials?|s)?(?![a-z])|supp(?=(?-i:[A-Z])))"
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
    r"|(?<![a-z0-9])(?:mo)?esm(?:[-_]?\d+)?(?![a-z0-9])"
    r"|si[-_ ]?file(?:s)?"
    r"|extended[-_ ]?data"
    r"|online[-_ ]?(?:supplementary|supplemental|supplements?|suppl?(?![a-z]))"
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
    # Require separator or CamelCase 'ST' for S-table so normal English words like "stable" / "Stable 13C" never match.
    r"(?:\b(?:e[-_]?|s[-_])(?:tables?|tabs?\.?|tbls?\.?)(?:\s*" + _HIER_NUM_RE + r"(?:\s*(?:[-–至]|to)\s*" + _HIER_NUM_RE + r")?)?\b|\b(?-i:ST(?:ables?|abs?\.?|bls?\.?))(?:\s*" + _HIER_NUM_RE + r"(?:\s*(?:[-–至]|to)\s*" + _HIER_NUM_RE + r")?)?\b)"
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
    r"(?:\b(?:tables?|tabs?\.?|tbls?\.?)\s+(?:" + _HIER_NUM_RE + r"|[sS]?[-_.]?[ivxldcmIVXLDCM]+)(?:\s*(?:[-–至]|to)\s*(?:" + _HIER_NUM_RE + r"|[sS]?[-_.]?[ivxldcmIVXLDCM]+))?)"
    r"|"
    # Branch 4: Data S1, Dataset S1, File S1, Data SI, etc.
    r"(?:\b(?:datasets?|data|files?)\s+(?:s\d+(?:[._-]\d+)*|s[-_.]?[ivxldcm]+)[a-zA-Z]?(?:\s*(?:[-–至]|to)\s*(?:s?\d+(?:[._-]\d+)*|s?[-_.]?[ivxldcm]+)[a-zA-Z]?)?\b)"
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
    r"(?:(?:e[-_]?|s[-_])(?:tables?|tbls?|tabs?)[-_.]*[a-z]?[-_.]?\d+(?:[._-]\d+)*(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+(?:[._-]\d+)*)?|(?-i:ST(?:ables?|bls?|abs?))[-_.]*[a-z]?[-_.]?\d+(?:[._-]\d+)*(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?\d+(?:[._-]\d+)*)?)"
    r"|"
    r"(?:appendix[-_.]*(?:tables?|tbls?|tabs?)?[-_.]*[a-z0-9]+(?:[._-]\d+)*(?:[-_.]*(?:to|[-–至])[-_.]*[a-z0-9]+(?:[._-]\d+)*)?)"
    r"|"
    r"(?:\bs\d+(?:[._-]\d+)*[a-z]?[-_.]*(?:tables?|tbls?|tabs?|datasets?|data|files?))"
    r"|"
    r"(?:source[-_.]?data(?:[-_.](?:tables?|tabs?|tbls?|figs?|figures?|files?)?[-_.]*[a-z0-9]+(?:[._-]\d+)*)?)"
    r"|"
    r"(?:(?:附表|补充表|附录表|附加表|附录)(?:[-_.]*[a-z]?[-_.]?[0-9一二三四五六七八九十]+(?:[._-]\d+)*(?:[-_.]*(?:to|[-–至])[-_.]*[a-z]?[-_.]?[0-9一二三四五六七八九十]+(?:[._-]\d+)*)?)?)"
    r"|"
    r"(?:(?:tables?|tbls?)[-_.]*(?:[a-z]?[-_.]?\d+(?:[._-]\d+)*|[sS]?[-_.]*[ivxldcm]+)(?:[-_.]*(?:to|[-–至])[-_.]*(?:[a-z]?[-_.]?\d+(?:[._-]\d+)*|[sS]?[-_.]*[ivxldcm]+))?)"
    r"|"
    r"(?:tabs?[-_.]*(?:s[-_.]?\d+(?:[._-]\d+)*|[a-z]?[-_.]?\d+(?:[._-]\d+)*\.(?:xlsx?|csv|tsv|pdf|docx?|zip|txt|json|xml|html?)))"
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
    # Check eTable / e-Table / STable / S-Table (avoid matching "stable" / "Stable 13C")
    if re.search(r'\b(?:e[-_]?|s[-_])(?:tables?|tabs?\.?|tbls?\.?)(?:[-_.\s]*\d+|\b)|\b(?-i:ST(?:ables?|abs?\.?|bls?\.?))(?:[-_.\s]*\d+|\b)', label, re.IGNORECASE):
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
    r"/article/pii/\w+/pdfft(?:\?|$)",
    r"/science/article/pii/\w+/pdf\?",
    r"/action/downloadcitation\b",
    r"/citation/download\b",
    r"/servlet/linkout\b",
    r"/plat/search\b",
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
    r"|10\.1594/pangaea"
    r"|10\.57760/sciencedb"
    r"|10\.25384/"
    r"|10\.17882/"
    r"|10\.5066/"
    r"|10\.15468/"
    r"|10\.4121/"
    r"|10\.11588/"
    r"|10\.7937/"
    r"|doi\.org/10\.\d+/[^\s\"'<>?#]*(?:[._/-]s\d{1,4}|[._/-]s\.\d+(?:\.v\d+)?|[._/-]suppl(?:ement)?\d*|[._/-]esm\d*|[._/-]mmc\d+|[._/-]table[-_.]?s?\d+|(?:\.c\.|\.figshare\.)\d+)(?:[/?#]|$)"
    r")",
    re.IGNORECASE,
)

# Fragment anchors pointing to an in-page supplementary section rather than a downloadable file
_ARTICLE_SECTION_FRAGMENT_RE = re.compile(
    r"^(?:supplement(?:ary|al)?[-_]?(?:materials?|data|info(?:rmation)?|files?|tables?)?|"
    r"suppl(?:ement(?:al|ary)?)?(?:[-_]?(?:materials?|data|info(?:rmation)?|files?|tables?|\d+))?|"
    r"supporting[-_]?(?:info(?:rmation)?|materials?|files?|data)|appendix\d*|additional[-_]?files?|"
    r"data[-_]?availability|sec[-_]?supp\w*|extended[-_]?data|source[-_]?data|esiright|sm\d*|sd\d*|moesm\d*)$",
    re.IGNORECASE,
)

# Article figures to exclude (gr1, ga1, fx1, fig1, figure_1, g53947.1_f03.png, etc.) (C2)
ARTICLE_FIGURE_PATTERN = re.compile(
    r"(?:^|[\W_])(?:gr|ga|fx|fig|figure)[-_]?\d+[a-z]?(?:[-_](?:lrg|sml|large|small|preview|highres|html|m|l|s|med|thumb|thumbnail)|\.(?:jpe?g|png|gif|tif|tiff|webp|svg)|$|\?)"
    r"|(?:^|[\W_])f\d+[a-z]?(?:[-_](?:lrg|sml|large|small|preview|highres|m|l|s|med|thumb|thumbnail))?\.(?:jpe?g|png|gif|tif|tiff|webp|svg)(?:$|\?)"
    r"|/downloadfile/downloadimage\.aspx(?:\?|$)",
    re.IGNORECASE,
)

# Known HTML file extensions
HTML_EXTENSIONS = {".html", ".htm", ".php", ".asp", ".jsp"}

# Selector for genuine supplementary assets during browser rendering (excludes bare containers or generic 'table' links)
SUPP_WAIT_SELECTOR = (
    "a[href*='mmc'], a[href*='supp'], a[href*='esm'], a[href*='content_public'], "
    "a[href*='.xlsx'], a[href*='.xls'], a[href*='.csv'], a[href*='.zip'], "
    "[data-component*='attachment'], [data-test*='supplementary'], [data-test*='source-data'], "
    "[data-test*='supp-'], [data-testid*='supplementary'], "
    ".widget-article-supplementary-data a, .widget-instance-ArticleSupplementaryData a, "
    ".widget-ArticleDataSupplements a, .dataSuppLink a, a[href*='/article-supplement/'], "
    ".widget-SupplementaryData a, .data-supplement a, [data-supplement] a, "
    ".article-page-section.supplemental a, section[data-magellan-destination='supplemental'] a, "
    "#supplementaryMaterial a, #supplementaryMaterial-content a, "
    "#supp-materials a, #Supplementary a, a[href*='/fileup/'], .supplementary_files a, a.obj_galley_link_supplementary, "
    ".related-file-list a, .suppl-sec a, "
    "a[href*='downloadSupplement'], a[href*='/article/file?id='], "
    "a[href*='/doi/suppl/'], a[href*='/suppl_file/'], a[href*='/suppdata/'], "
    "a[href*='/data/suppl/'], a[href*='DownloadFile'], a[href*='getDataSupplement'], "
    "a[href*='File/Download'], a[href*='dfzljdn9uc3pi.cloudfront.net'], "
    "a[href*='/cms/attachment/'], a[href*='downloadArticleFile.do']"
)


# ── Data Structures ──────────────────────────────────────────────────────────

_BARE_DOI_HOST_URL_RE = re.compile(r"^https?://(10\.\d{4,9})/([^\s]+)$", re.IGNORECASE)


def normalize_bare_doi_url(url: str) -> str:
    """Rewrite typoed bare-DOI URLs like ``http://10.5066/P94MI7E5`` to ``https://doi.org/...``."""
    u = (url or "").strip()
    m = _BARE_DOI_HOST_URL_RE.match(u)
    if m:
        return f"https://doi.org/{m.group(1)}/{m.group(2)}"
    return u


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

    def __post_init__(self):
        self.url = normalize_bare_doi_url(self.url)

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

    @classmethod
    def from_dict(cls, data):
        """Rebuild a Candidate from a to_dict() payload (P1-E2).

        Used to seed Phase B downloads from the Phase A candidate list
        file. Returns None for entries without a URL so one bad entry
        never poisons the whole seed list.
        """
        if not isinstance(data, dict) or not data.get("url"):
            return None
        return cls(
            url=data["url"],
            filename=data.get("filename", "") or "",
            link_text=data.get("link_text", "") or "",
            section=data.get("section", "") or "",
            match_rule=data.get("match_rule", "") or "",
            source_tag=data.get("source_tag", "") or "a",
            extra_info=data.get("extra_info", {}) or {},
            source_url=data.get("source_url", "") or "",
            covered_table_ids=set(data.get("covered_table_ids", []) or []),
        )


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
    # P1-B: (url, reason) for candidates dropped by the junk-candidate
    # filter (junk_candidate_reason); unlike excluded_links these are
    # logged with their reason instead of being silently discarded.
    junk_filtered: List[Tuple[str, str]] = field(default_factory=list)
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


def _host_of(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0]


def _same_pmc_instance(url: str, base_url: Union[str, List[str], Tuple[str, ...], Set[str]]) -> bool:
    """True when a PMC /bin/ candidate comes from the same article instance
    as the page it was found on (so it is that article's own file)."""
    m = re.search(r"/instance/(\d+)/", url or "")
    if not m:
        return False
    return any(f"/instance/{m.group(1)}/" in b for b in _iter_base_urls(base_url))


def junk_candidate_reason(
    url: str,
    text: str = "",
    filename: str = "",
    attrs: Optional[Dict[str, str]] = None,
    base_url: Union[str, List[str], Tuple[str, ...], Set[str]] = "",
    is_data_file: bool = False,
    is_explicit_supp: bool = False,
) -> Optional[str]:
    """Reason a candidate is confirmed junk (P1-B), or None.

    Only patterns confirmed in the 50-paper live test are listed. The
    GEO / CC rules never fire on a real data file or an explicitly
    supplementary link, so genuine supplements
    (xlsx/xls/csv/tsv/docx/pdf/zip/...) are unaffected; the CRAN and
    Table/Appendix rules use page-shape guards instead (a CRAN tarball
    lives under /src/contrib/, not on a package page; an explicit
    'Supplementary Table S1' served as HTML is kept). The first two
    groups are checked regardless of extension because a challenge page
    and a preprint-version attachment are never this article's supplement
    even when they happen to be a PDF.
    """
    if not url:
        return None
    url_lower = url.lower()
    labels = [(text or ""), (filename or "")]
    if attrs:
        labels.extend(str(v) for v in attrs.values() if isinstance(v, str))

    # Client Challenge page (eLife anti-bot wall): the link *is* the wall.
    for label in labels:
        if re.sub(r"\s+", " ", label.strip().lower()) == "client challenge":
            return "client_challenge_page"
    if "client-challenge" in url_lower:
        return "client_challenge_page"

    # bioRxiv / medRxiv preprint attachments: the preprint version's
    # supplement (e.g. the NIHPP...-supplement-1.pdf filed under a
    # different PMC instance, #46), not the published article's file.
    # Not junk when the page being scanned IS the preprint itself.
    base_hosts = {_host_of(b) for b in _iter_base_urls(base_url)}
    base_is_preprint = bool(base_hosts & {"biorxiv.org", "medrxiv.org"}) or any(
        "10.1101/" in b.lower() for b in _iter_base_urls(base_url)
    )
    if not base_is_preprint:
        host = _host_of(url)
        if host in {"biorxiv.org", "medrxiv.org"} or "10.1101/" in url_lower:
            return "biorxiv_preprint_attachment"
        base_name = os.path.basename(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).lower()
        fname_lower = (filename or "").lower()
        if (
            re.match(r"nihpp\d{4}\.\d{2}\.\d{2}\.", base_name)
            or re.match(r"nihpp\d{4}\.\d{2}\.\d{2}\.", fname_lower)
        ) and not _same_pmc_instance(url, base_url):
            return "biorxiv_preprint_attachment"

    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    host = _host_of(url)
    path = parts.path.lower()

    # Non-supplement third-party AI/assistant sidebar widgets (e.g. GeoScienceWorld "Try GeoSapien AI")
    if host == "geosapien.ai" or host.endswith(".geosapien.ai"):
        return "third_party_widget"

    # CRAN package page (e.g. /web/packages/MASS/index.html, or the home
    # page). Checked before the data-file immunity because
    # check_data_extension mistakes the '.r-' in 'cran.r-project.org' for
    # an R-script extension; the page-like path condition is what protects
    # real CRAN files (tarballs live under /src/contrib/, not here).
    if host in {"cran.r-project.org", "www.cran.r-project.org"} and (
        path == "/" or path.startswith("/web/packages/") or path.endswith((".html", ".htm"))
    ):
        return "cran_package_page"

    # Publisher interactive ePDF / PDF reader pages (/doi/epdf/<doi>, /doi/pdf/<doi>,
    # /article/pii/.../pdfft) and Silverchair /article-pdf/ are the main article, not supplements.
    if "/doi/epdf/" in path or path.endswith("/doi/epdf"):
        return "article_epdf_viewer"
    if (
        (
            "/article-pdf/" in path
            or "/doi/pdf/" in path
            or "/doi/pdfdirect/" in path
            or "/doi/reader/" in path
            or re.search(r"/article/pii/\w+/pdfft/?$", path)
            or re.search(r"/(?:pr[a-z]+|rmp|physrev[a-z]*)/pdf/10\.1103/", path)
            or re.search(r"/(?:prl|prb|pra|prc|prd|pre|prx|rmp|prapplied|prfluids|prmaterials|prresearch|prper|physrev[a-z]*)/pdf/", path)
        )
        and not is_explicit_supp
        and not any(m in url_lower for m in ("suppl", "appendix", "esm", "mmc"))
    ):
        return "article_pdf_viewer"
    _citation_label = " ".join([
        re.sub(r"\s+", " ", (text or "").strip()),
        str((attrs or {}).get("title") or ""),
        str((attrs or {}).get("aria-label") or ""),
    ]).strip()
    if (
        re.search(r"/(?:citation|cite)/(?:bibtex|endnote|refman|ris|txt|simple|download)\b", path)
        or re.search(r"/action/downloadcitation\b|/citation/download\b|/(?:export[-_]?citation|citation[-_]?export)\b", path)
        or re.search(r"(?:^|&)(?:citationformat|format)=(?:ris|bibtex|endnote|medline|refworks|enw|ciw)(?:&|$)", parts.query.lower())
        or (
            re.search(r"\b(?:refworks|bibtex|endnote|refman|zotero|mendeley)\b|^(?:ris|enw)$", _citation_label, re.IGNORECASE)
            and not is_explicit_supp
            and not SUPP_KEYWORD_PATTERN.search(f"{url_lower} {text or ''} {filename or ''}")
        )
    ):
        return "citation_export"
    if "/servlet/linkout" in path:
        return "citation_linkout"
    if "/plat/search" in path:
        return "site_search_url"
    # RSS / Atom / Advance-Access feeds (RSC advanceAccess_*.xml, Schweizerbart rss.xml)
    if (
        (path.endswith((".xml", ".rss")) and re.search(r"(?:^|/)(?:rss|feed|atom|advance[-_]?access[^/]*)\.(?:xml|rss)$", path))
        or "type=rss" in parts.query.lower()
    ):
        return "rss_feed_xml"
    # Sitemap / robots / PDF-reader utility downloads (China Geology / YGXB / CAGS Bulletin)
    if re.search(r"/(?:sitemap|robots)\.(?:txt|xml)$", path) or re.sub(r"\s+", " ", (text or "").strip().lower()) in {"站点地图", "sitemap"}:
        return "site_utility_file"
    if (
        re.search(r"/(?:[a-z0-9_-]*pdf[-_]?reader|acrobat[-_]?reader|foxit[-_]?reader|reader)\.(?:rar|zip|exe)$", path)
        or re.search(r"(?:pdf\s*阅读器|download\s*pdf\s*reader)", (text or ""), re.IGNORECASE)
    ):
        return "reader_utility_archive"
    # Editorial / author-service / bookseller information pages (Cambridge Core / De Gruyter)
    if (
        re.search(r"/(?:services/authors|for-booksellers|author[-_]?services|publishing/services)/", path)
        and not is_data_file
        and not path.endswith(".pdf")
    ):
        return "editorial_service_page"
    # Atypon /doi/suppl/<doi>?scroll=top tab anchor without /suppl_file/, /article-supplement/, or file payload
    if (
        "/doi/suppl/" in path
        and "/suppl_file/" not in path
        and "/article-supplement/" not in path
        and not is_data_file
        and not path.endswith(".pdf")
        and not re.search(r"(?:^|&)(?:file|attachment|download)=", parts.query.lower())
    ):
        return "atypon_suppl_tab_page"
    # Same-article page with section-navigation query (e.g. ACS ?goto=supporting-info)
    if (
        not is_data_file
        and not path.endswith(".pdf")
        and re.search(r"(?:^|&)(?:goto|scroll|tab)=", parts.query.lower())
        and not re.search(r"(?:^|&)(?:file|filename|attachment|download|id)=", parts.query.lower())
        and (
            (base_url and any(
                urllib.parse.urlsplit(b).netloc.lower() == host
                and urllib.parse.urlsplit(b).path.rstrip("/").lower() == path.rstrip("/")
                for b in _iter_base_urls(base_url)
            ))
            or "goto=supporting" in parts.query.lower()
        )
    ):
        return "article_section_nav_url"
    # Nature / Springer / PMC article figure or inline HTML table viewer subpages
    # (/articles/.../figures/7, /tables/1, /articles/pmc7530294/table/tbl1/).
    # Preserve appendix subtables like /table/app7table1/.
    if (
        not is_data_file
        and not path.endswith(".pdf")
        and re.search(r"/articles/[^/]+/(?:figures/\d+|figure/(?:fig|f)[-_]?\d+[a-z0-9_-]*)/?$", path)
    ):
        return "article_figure_page"
    if (
        not is_data_file
        and not path.endswith(".pdf")
        and re.search(r"/articles/[^/]+/(?:tables/\d+|table/(?:tbl|tab|table|t)[-_]?\d+[a-z0-9_-]*)/?$", path)
    ):
        return "article_inline_table_page"

    # Chinese Magtech/Founder downloadArticleFile.do main-article attachments (attachType=PDF/HTML/XML)
    if "/downloadarticlefile.do" in path:
        q_low = parts.query.lower()
        if "attachtype=" in q_low and not re.search(r"attachtype=(?:supp\w*|attach\w*|appendix|data)", q_low) and not is_explicit_supp:
            return "article_main_attachment"

    # Full-text article XML links (e.g. Copernicus "Full-text XML", "Article XML", "JATS XML")
    if path.endswith(".xml") and not is_explicit_supp:
        combined_xml_meta = f"{url_lower} {text or ''} {filename or ''}"
        if not (
            SUPP_KEYWORD_PATTERN.search(combined_xml_meta)
            or TABLE_FILENAME_PATTERN.search(combined_xml_meta)
            or MMC_PATTERN.search(url_lower)
        ):
            text_norm_xml = re.sub(r"\s+", " ", (text or "").strip()).lower()
            if (
                re.match(r"^(?:download\s+)?(?:full[- ]?text|article|jats|manuscript|tei|pmc)?\s*xml(?:\s*\([^)]*\))?$", text_norm_xml)
                or "type=manuscript" in parts.query.lower()
            ):
                return "article_fulltext_xml"

    # ScienceDirect's "Download all" bundle endpoint (/sdfe/pdf/download/<pii>/attachments)
    # is a POST-only form action that rejects GET with HTTP 405; individual mmc
    # links sit alongside it and are extracted directly.
    if re.search(r"/sdfe/pdf/download/[^/]+/attachments/?$", path):
        return "sciencedirect_bulk_post_endpoint"

    # PLOS editorial guide pages (/s/supporting-information, /s/data-availability)
    if host.endswith("plos.org") and re.search(r"/s/(?:supporting-information|data-availability)/?$", path):
        return "plos_editorial_info_page"

    # Cross-path in-page section anchors on HTML/article routes (e.g. Frontiers
    # https://.../articles/<doi>/full#supplementary-material) are page fragments, not files.
    if (
        parts.fragment
        and not parts.query
        and _ARTICLE_SECTION_FRAGMENT_RE.match(parts.fragment.strip())
        and not is_data_file
        and not path.endswith(".pdf")
        and not any(seg in path for seg in ("/fetch", "/download", "/stream", "/file", "/suppl_file", "/attachment", "/bin/"))
    ):
        return "article_section_fragment_url"

    # Table / Appendix .html navigation page: an in-article table rendered
    # as a web page, not a downloadable supplement. Only when the link is
    # a bare 'Table N' / 'Appendix N' label (or filename) pointing at an
    # .html page with no supplementary marker (/bin/, mmc, esm, supp...).
    # Exempted only by an explicit supplementary word, not by the bare
    # table label itself: 'Appendix 7' is a table label, 'Supplementary
    # Table S1' is a supplement.
    if (
        path.endswith((".html", ".htm"))
        and not is_data_file
        and not any(marker in url_lower for marker in ("/bin/", "mmc", "esm", "supp"))
        and "suppl" not in ((text or "") + " " + (filename or "")).lower()
    ):
        text_norm = (text or "").strip()
        fname_norm = (filename or "").strip()
        base_name = os.path.basename(urllib.parse.unquote(parts.path))
        combined_label = f"{text_norm} {fname_norm} {base_name}"
        has_explicit_s_table = bool(
            re.search(
                r"(?:\b(?:tables?|tabs?\.?|tbls?\.?|appendix)[-_.\s]*s[-_.]*(?:\d+|[ivx]+)[a-z]?(?:[._-]\d+)*\b"
                r"|\bs[-_.]?\d+[a-z]?[-_.]*(?:tables?|tabs?\.?|tbls?\.?)\b"
                r"|\bs[-_](?:tables?|tabs?\.?|tbls?\.?)\b"
                r"|\b(?-i:ST(?:ables?|abs?\.?|bls?\.?))[-_.\s]*\d*"
                r"|\b(?:extended[-_\s]*data|source[-_\s]*data|e[-_]?table|supporting|additional[-_\s]*file)\b"
                r"|附表|补充表|附录表|附加表)",
                combined_label,
                re.IGNORECASE,
            )
        )
        if not has_explicit_s_table and (
            re.match(r"^(?:table|appendix)\s+\S+$", text_norm, re.IGNORECASE)
            or re.match(r"^(?:table|appendix)\b.*\.html?$", fname_norm, re.IGNORECASE)
            or re.match(r"^(?:table|appendix)[ _-]?\w*\.html?$", base_name, re.IGNORECASE)
        ):
            return "table_appendix_html_nav"

    if is_data_file or is_explicit_supp:
        return None

    # GEO query page: an accession lookup (acc.cgi?acc=GSE...), not a file.
    if host in {"ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov"} and path.startswith("/geo/query/acc.cgi"):
        return "geo_acc_cgi"
    # Creative Commons licence page (e.g. /licenses/by/4.0/).
    if host == "creativecommons.org" and path.startswith("/licenses/"):
        return "cc_license"
    return None


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


_NON_DOWNLOAD_PAGE_QUERY_KEYS = {"pagetype", "lang", "locale", "hl"}
_LEADING_LANG_PATH_RE = re.compile(r"^/(?:en|zh|cn|zh-cn|en-us)(?=/)", re.IGNORECASE)


def _strip_leading_lang_path(path: str) -> str:
    stripped = _LEADING_LANG_PATH_RE.sub("", (path or "").rstrip("/"))
    return stripped or "/"


def is_same_article_page_url(url: str, base_url: Union[str, List[str], Tuple[str, ...], Set[str]]) -> bool:
    """Check if a URL points to the article page itself rather than a downloadable attachment."""
    if not url or not base_url:
        return False
    u = urllib.parse.urlsplit(url.split("#")[0].strip())
    if not u.netloc or not u.path:
        return False
    u_path_norm = _strip_leading_lang_path(u.path)
    u_params = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    for b_str in _iter_base_urls(base_url):
        b = urllib.parse.urlsplit(b_str.split("#")[0].strip())
        b_path_norm = _strip_leading_lang_path(b.path)
        if u.netloc.lower() == b.netloc.lower() and (
            u.path.rstrip("/") == b.path.rstrip("/") or u_path_norm == b_path_norm
        ):
            if u.query == b.query:
                return True
            if u_params and all(k.lower() in _NON_DOWNLOAD_PAGE_QUERY_KEYS for k, _ in u_params):
                return True
    return False


def is_excluded_url(
    url: str,
    base_url: Union[str, List[str], Tuple[str, ...], Set[str]] = "",
    text: str = "",
    attrs: Optional[Dict[str, str]] = None,
    filename: str = "",
    diag: Optional["FindingDiagnostics"] = None,
) -> bool:
    """Check if a URL should be excluded (references, external sites, anchors, etc.).

    P1-B: a URL dropped by the junk-candidate filter is recorded in
    ``diag.junk_filtered`` with its reason and logged as a ``[Filtered]``
    line instead of being silently discarded (only when ``diag`` is given,
    i.e. inside a find_all_candidates run).
    """
    if not url or url.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "blob:")):
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

    # P1-B: confirmed junk candidates (GEO acc.cgi, CC licence, CRAN,
    # Table/Appendix .html nav, bioRxiv preprint, Client Challenge).
    junk_reason = junk_candidate_reason(
        url, text=text, filename=filename, attrs=attrs, base_url=base_url,
        is_data_file=is_data_or_supp_file, is_explicit_supp=is_explicit_supp_context,
    )
    if junk_reason:
        if diag is not None and (url, junk_reason) not in diag.junk_filtered:
            diag.junk_filtered.append((url, junk_reason))
            print(f"    [Filtered] {url} ({junk_reason})")
        return True

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


_AUTH_QUERY_PARAM_NAMES = {
    "signature", "policy", "key-pair-id", "expires", "hmac", "token",
    "x-amz-signature", "x-amz-credential", "x-amz-security-token",
    "x-amz-date", "x-amz-algorithm", "x-amz-signedheaders",
}


def _url_without_auth_query_tokens(url: str) -> str:
    """Strip signed CDN auth query parameters so random tokens (e.g. Signature=...128ESM...) do not match keywords."""
    if not url or "?" not in url:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        kept = [
            (k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _AUTH_QUERY_PARAM_NAMES
        ]
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(kept), parts.fragment))
    except Exception:
        return url


def _is_inline_figure_action(url: str, text: str = "", attrs: Optional[Dict[str, str]] = None) -> bool:
    """True only for explicit inline article figure viewers/slide buttons or bare main/extended figure images/slides."""
    if not url:
        return False
    path_low = urllib.parse.urlsplit(url).path.lower()
    if "/downloadfile/downloadimage.aspx" in path_low:
        return True
    label_low = re.sub(r"\s+", " ", (text or "").strip()).lower()
    if label_low in {"view large", "download slide", "open in viewer", "view figure", "download figure"}:
        return True
    # Explicit bare main/extended figure images or slide PPTs (e.g. 'Download Figure 1 PPT' -> *.f1.ppt, 'Extended Data Fig. 1' -> fig1.png)
    if (
        re.search(r"\.(?:png|jpe?g|gif|svg|webp|tiff?|pptx?)$", path_low)
        and re.match(
            r"^(?:(?:download|view|open)\s+)?(?:extended\s+data\s+)?(?:figs?\.?|figures?|graphical\s+abstract)\s*(?:\d+[a-z]?|[ivx]+)?(?:\s+(?:ppt|pptx|slide|slides|image))?$",
            label_low,
        )
        and not re.search(r"supplementary|supplemental|supporting|source[-_ ]?data|table|dataset|additional[-_ ]?file|appendix", label_low)
        and is_article_figure_url(url, text=text, attrs=attrs)
    ):
        return True
    return False


def is_article_figure_url(url: str, text: str = "", attrs: Optional[Dict[str, str]] = None) -> bool:
    """Check if a URL is an article figure (gr1, ga1, fx1, fig1, /figures/7, *.f1.ppt, etc.). Never flags data files or supps (C2)."""
    if not url:
        return False
    url_kw = _url_without_auth_query_tokens(url)
    # Figure PowerPoint slides (e.g. Annual Reviews / Ingenta *.f1.ppt..*.f5.ppt) without explicit supp/source-data labels
    if (
        re.search(r"(?:\.(?:f|fig)\d+[a-z]?|[._/-]fig(?:ure)?\d+[a-z]?)\.pptx?(?:$|\?)", url, re.IGNORECASE)
        and not SUPP_KEYWORD_PATTERN.search(url_kw)
        and not SUPP_KEYWORD_PATTERN.search(text or "")
        and not is_explicit_supp_table(text or "")
        and not (attrs and any(SUPP_KEYWORD_PATTERN.search(str(v)) or is_explicit_supp_table(str(v)) for v in attrs.values() if v))
    ):
        return True
    # Supplementary data files, MMCs, table files, or explicit supp keywords are never article figures
    if (
        check_data_extension(url)
        or MMC_PATTERN.search(url_kw)
        or TABLE_FILENAME_PATTERN.search(url_kw)
        or SUPP_KEYWORD_PATTERN.search(url_kw)
    ):
        return False
    # If link text or attributes explicitly declare Source Data or a Supplementary Table, do not treat as a figure
    is_image_url = bool(re.search(r"\.(?:png|jpe?g|gif|svg|webp|tiff?)(?:$|\?)", url, re.IGNORECASE))
    is_figure_route = bool(re.search(r"/figures?/\d+(?:[/?#]|$)", url, re.IGNORECASE))
    is_fig_or_image = is_image_url or is_figure_route
    if text and (
        is_explicit_supp_table(text)
        or TABLE_FILENAME_PATTERN.search(text)
        or (SUPP_KEYWORD_PATTERN.search(text) and not (is_fig_or_image and re.search(r"\b(?:figs?\.?|figures?)\b", text, re.IGNORECASE)))
    ):
        return False
    if attrs:
        for v in attrs.values():
            if v and isinstance(v, str) and (
                is_explicit_supp_table(v)
                or TABLE_FILENAME_PATTERN.search(v)
                or (SUPP_KEYWORD_PATTERN.search(v) and not (is_fig_or_image and re.search(r"\b(?:figs?\.?|figures?)\b", v, re.IGNORECASE)))
            ):
                return False
    if is_figure_route and not urllib.parse.urlsplit(url).path.lower().endswith(".pdf"):
        return True
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

    # Handle MDPI extensionless path /s1, /s2 (never match ScienceDirect /pii/S... article IDs)
    host_lower = (parsed.netloc or "").lower()
    path_lower = path.lower()
    if "/pii/" not in path_lower and (
        "mdpi.com" in host_lower
        or "mdpi-res.com" in host_lower
        or re.search(r'/\d{4}-\d{3}[\dx]/', path_lower)
    ):
        m = re.search(r'/(s[1-9]\d?)/?$', path, re.IGNORECASE)
        if m:
            return f"{m.group(1)}.zip"

    # 2.1 Handle Silverchair ACS/RSC /article-supplement/{id}/{fmt}/{stem}/ extensionless (or dotted-stem) routes
    segments = [seg for seg in path.split("/") if seg]
    if "/article-supplement/" in path_lower and len(segments) >= 2:
        fmt_seg = segments[-2].lower()
        if fmt_seg in {"pdf", "xlsx", "xls", "csv", "tsv", "zip", "docx", "doc", "pptx", "ppt"} and fname and not fname_lower.endswith(f".{fmt_seg}"):
            return sanitize_filename(f"{fname}.{fmt_seg}")

    # If path basename has a non-generic extension like .xlsx, .pdf, .zip, etc.
    if fname and "." in fname and fname_lower not in {"file.php", "download.php", "index.html", "index.php"}:
        return sanitize_filename(fname)

    # 2.2 Handle CDN/versioned asset paths where filename sits in an earlier segment
    # e.g. Frontiers: /articles/1988876/file/Table_1.xlsx/1988876_table_1/1
    # Restrict to unambiguous file extensions (not short DOI/repo suffixes like .r/.m/.py/.xml).
    _unambiguous_seg_exts = (
        ".xlsx", ".xls", ".csv", ".tsv", ".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".txz",
        ".7z", ".rar", ".docx", ".doc", ".pptx", ".ppt", ".pdf", ".h5", ".hdf5",
        ".parquet", ".feather", ".mat", ".sav", ".dta", ".sas7bdat", ".cif", ".pdb", ".nii", ".nii.gz",
    )
    for seg in reversed(segments[:-1] if fname else segments):
        seg_lower = seg.lower()
        if not re.match(r"^10\.\d+$", seg_lower) and seg_lower.endswith(_unambiguous_seg_exts):
            return sanitize_filename(seg)

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

    # 4. Use suggested default name if provided (skip raw URLs/DOIs when a non-generic path basename exists)
    if default_name:
        def_strip = default_name.strip()
        is_raw_url_label = def_strip.lower().startswith(("http://", "https://", "doi:"))
        if not (is_raw_url_label and fname and fname_lower not in GENERIC_PATH_BASENAMES):
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

    # Strict boundary check on path/query only (skip netloc like cran.r-project.org and
    # skip short ambiguous extensions like .r/.m/.py/.mat in mid-path such as .mat-nano-quantum-sensing)
    path_and_query = path + ("?" + parsed.query.lower() if parsed.query else "")
    for ext in SORTED_DATA_EXTENSIONS:
        if ext in _SHORT_AMBIGUOUS_EXTS:
            continue
        pattern = re.escape(ext) + r'(?:[/&#?\s,;)]|$)'
        if re.search(pattern, path_and_query):
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
    "content-inner-wrap", "articlemainview", "article-main", "article_main",
    "widget-articlefulltext",
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
                if p_text and p_text != tag_text and len(p_text) <= 600:
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
                    if n_text and n_text != tag_text and len(n_text) <= 600:
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
        "supplement", "appendix", "appendices", "supporting information",
        "supporting info", "additional file", "additional material",
        "additional data", "extended data", "extended table",
        "extended data table", "extended data tables", "extended tables",
        "supplementary table", "supplementary tables",
        "supplemental table", "source data", "补充材料", "附表", "附录", "附加材料",
        "支撑材料", "补充表格", "补充信息", "补充数据",
    ]
    supp_attr_keywords = [
        "supplement", "suppl-", "suppl_", "-suppl", "_suppl", "appendix", "appendices",
        "supporting-info", "supporting_info", "supporting-information", "supporting_information",
        "extended-data", "extended_data", "extendeddata",
        "additional-file", "additional-material", "additional-data",
        "data-availability", "data-supplement", "supplementary-data",
        "supplementarymaterial", "supplemental-material", "supplemental-files",
        "supp-table", "supplementary-table", "supp-item", "supp-info", "source-data",
        "core-supplementary", "core-collateral", "esiright", "suppdata", "datasupplink",
        "articledatasupplements", "article-data-supplements",
        "supinfolist", "suppinfolist", "supinfobox", "supportinginfo",
        "support-info", "support-information",
        "access-supplementary", "tab__supplementary",
        "related-file-list", "obj_galley_link_supplementary",
        "补充", "附表", "附录", "附加", "支撑材料",
    ]
    _esm_attr_re = re.compile(r"(?<![a-z0-9])(?:mo)?esm(?:[-_]?\d+)?(?![a-z0-9])", re.IGNORECASE)
    heading_tag_names = ["h1", "h2", "h3", "h4", "h5", "h6", "caption", "figcaption", "summary"]
    _url_attr_names = {"href", "src", "action", "formaction", "cite", "data-url"}

    # Never mark navigation / footer links as living inside a supplementary section
    anc = getattr(tag, "parent", None)
    while anc and hasattr(anc, "name"):
        if anc.name in ("nav", "footer"):
            return False
        anc = anc.parent

    curr = tag
    prev_child = None
    steps = 0
    while curr and steps < 6:
        if _is_whole_article_wrapper(curr):
            break

        # 1. Check curr's own attributes (id, class, data-*, role, aria-label, etc.)
        #    When steps == 0 (the control tag itself), skip URL attributes (href/src/...)
        #    so links like <a href="/services/.../suppliers"> never self-classify as supp containers.
        if hasattr(curr, "attrs") and isinstance(curr.attrs, dict):
            for k, v in curr.attrs.items():
                if steps == 0 and str(k).lower() in _url_attr_names:
                    continue
                val_str = " ".join(v) if isinstance(v, list) else str(v)
                combined = f"{k} {val_str}".lower()
                if any(kw in combined for kw in supp_attr_keywords) or _esm_attr_re.search(combined) or re.search(r"(?:^|[^a-z])suppl(?:$|[^a-z])", combined):
                    return True

        # 2. Check direct child headings of curr (or direct header wrapper of curr) that precede prev_child
        def _heading_is_supp(h_text: str) -> bool:
            hl = h_text.lower()
            return any(kw in hl for kw in heading_keywords) or bool(re.search(r"(?:^|[^a-z])suppl\.?(?:$|[^a-z])", hl))

        if hasattr(curr, "name") and curr.name in ["section", "div", "aside", "details", "figure", "dl", "ul", "ol", "table"]:
            for h in curr.find_all(heading_tag_names, recursive=False, limit=3):
                if _heading_is_supp(h.get_text(" ", strip=True)):
                    return True
            # Also check a direct child header wrapper (e.g. <div class="section-title"><h2>Supplementary</h2></div>)
            for child_hdr in curr.find_all(["header", "div"], recursive=False, limit=2):
                if child_hdr == prev_child:
                    break
                hdr_cls = " ".join(child_hdr.get("class", [])).lower() if hasattr(child_hdr, "get") else ""
                if child_hdr.name == "header" or any(k in hdr_cls for k in ["header", "heading", "title", "caption"]):
                    for h in child_hdr.find_all(heading_tag_names, limit=2):
                        if _heading_is_supp(h.get_text(" ", strip=True)):
                            return True

        # 3. Check preceding sibling headings of curr (only when curr is the section content block immediately following a heading)
        if hasattr(curr, "find_previous_sibling"):
            prev_h = curr.find_previous_sibling(heading_tag_names)
            if prev_h:
                # Only treat prev_h as governing curr if there is no other non-heading section between prev_h and curr
                prev_any = curr.find_previous_sibling()
                if prev_any == prev_h and _heading_is_supp(prev_h.get_text(" ", strip=True)):
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
    tag_name = (getattr(tag, "name", "") or "").lower()
    # 1. Standard attributes
    for attr in [
        "data-download-url",
        "data-file-url",
        "data-asset-url",
        "data-supp-file",
        "data-supplement-url",
        "data-attachment-url",
        "data-resource-url",
        "data-download-link",
        "data-file-path",
        "data-filepath",
        "data-document-url",
        "href",
        "formaction",
        "data",     # <object data="...">
        "data-url",
        "data-href",
        "data-file",
        "data-src",
        "data-target-url",
        "data-download",
        "data-link",
        "data-uri",
        "content",  # <meta>
        "src",      # <iframe> / <embed>
    ]:
        if attr == "data" and tag_name != "object":
            continue
        if attr == "content" and tag_name != "meta":
            continue
        val = tag.get(attr)
        if val and isinstance(val, str):
            val_clean = val.strip()
            val_low = val_clean.lower()
            # In-page anchors, non-HTTP pseudo-schemes, and bare numeric IDs (e.g. data="161")
            # are not downloadable attachment URLs.
            if (
                val_clean
                and not val_clean.startswith("#")
                and not val_low.startswith(("javascript:", "mailto:", "tel:", "data:", "blob:"))
                and not val_clean.isdigit()
            ):
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
            if val and not val.startswith("#") and not val.lower().startswith("javascript:"):
                return val

        # Fallback: scan any quoted URL/path inside onclick with download/query/data/table characteristics
        for qm in re.finditer(r'''['"]([^'"]+)['"]''', onclick):
            val = qm.group(1).strip()
            if val and not val.startswith("#") and not val.lower().startswith("javascript:"):
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
                if "exportsupplementary" in act_clean.lower():
                    id_inp = form.find("input", attrs={"name": "id"})
                    if id_inp is not None and not str(id_inp.get("value") or "").strip():
                        return None
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

_NAV_LINK_TEXT_RE = re.compile(
    r"^(?:[.\u2026\s\u00ab\u00bb\u2039\u203a<>←→]*)"
    r"(?:prev(?:ious)?(?:\s+article)?|next(?:\s+article)?|back|home|top)"
    r"(?:[.\u2026\s\u00ab\u00bb\u2039\u203a<>←→]*)$",
    re.IGNORECASE,
)


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
    if not data_ext and fname and not re.search(r'/s[1-9]\d?/?$', path_lower):
        # Guard against short ambiguous script extensions (.r/.m/.py/.xml) on DOI/non-file names
        if not re.match(r'^10\.\d{4,}/', fname):
            ext_candidate = check_data_extension(fname)
            if ext_candidate and (
                ext_candidate not in {".r", ".m", ".py", ".xml"}
                or attrs.get("download")
                or any(seg in path_lower for seg in ("/suppl", "/suppdata/", "/download", "/file/", "/attachment/"))
            ):
                data_ext = ext_candidate
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
            "/article-supplement/", "/suppdata/", "/content_public/", "/downloadfile", "/public-pages-files",
            "/cms/attachment/", "/cms/asset/", "/download/supplement/", "/article/download/",
            "/data/suppl/", "/getdatasupplement",
        ]) or
        tag_name in ["button", "input"] or
        (tag is not None and (
            tag.has_attr("data-url") or tag.has_attr("onclick") or tag.has_attr("download")
            or tag.has_attr("data-supp-file") or tag.has_attr("data-supplement-url")
        ))
    )
    is_html = any(path_lower.endswith(h_ext) for h_ext in HTML_EXTENSIONS)

    # Case C: Table label detected in text, attributes, or nearby heading (Issue #2, #3, A3, A4)
    if is_table:
        own_explicit_supp = is_explicit_supp_table(text) or any(
            is_explicit_supp_table(str(v)) for v in attrs.values() if v
        )
        is_explicit_supp = own_explicit_supp
        if not is_explicit_supp and ":" in table_rule:
            is_explicit_supp = is_explicit_supp_table(table_rule.split(":", 1)[1])
        is_nearby_only = table_rule.startswith("nearby_context:")
        is_nav_text = bool(text and _NAV_LINK_TEXT_RE.match(text.strip()))

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
            elif has_appendix_context and (has_query_or_endpoint or "/appendix" in path_lower or "/supp" in path_lower):
                return True, f"table_endpoint ({table_rule})"
        else:
            if is_html:
                if (is_supp_section or is_explicit_supp or is_table_filename or "download" in attrs) and not (is_nearby_only and is_nav_text):
                    return True, f"html_table_attachment ({table_rule})"
            elif data_ext or path_lower.endswith(".pdf") or is_supp_section or "download" in attrs or is_table_filename:
                if not (is_nearby_only and is_nav_text):
                    return True, table_rule
            elif not is_html:
                _has_dl_path_or_key = (
                    path_lower not in ("", "/")
                    or any(k in (parsed.query or "").lower() for k in ("file=", "filename=", "attachment=", "download=", "supp", "id="))
                )
                if is_supp_section:
                    if not (is_nearby_only and (is_nav_text or not _has_dl_path_or_key)):
                        return True, f"table_endpoint ({table_rule})"
                elif is_explicit_supp:
                    if own_explicit_supp or (has_query_or_endpoint and not (is_nearby_only and (is_nav_text or not _has_dl_path_or_key))):
                        return True, f"table_endpoint ({table_rule})"

    # Case D: Table filename pattern (e.g. table-s1.pdf, tbl_s2.xlsx)
    if is_table_filename:
        return True, "table_filename_pattern"

    # Ignore subscription/auth/full-size-table UI links before endpoint matching
    text_clean_low = re.sub(r"\s+", " ", (text or "").strip()).lower()
    if text_clean_low in {"subscription required", "full size table", "sign in", "log in", "login"} and not data_ext:
        return False, ""

    # Case E: PDF or extensionless supplementary endpoint with supp keywords (Issue #3)
    is_pdf = path_lower.endswith(".pdf")
    is_wiley_dl = (
        "downloadsupplement" in path_lower
        or "downloadsupplement" in url_lower
        or ("wiley.com" in url_lower and ("/suppl" in path_lower or "/action/downloadsupplement" in path_lower) and not is_html)
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

    # Case G: MDPI style /s1, /s2 (require MDPI host or ISSN path, exclude ScienceDirect /pii/S...)
    host_lower = (parsed.netloc or "").lower()
    if (
        "/pii/" not in path_lower
        and ("mdpi.com" in host_lower or "mdpi-res.com" in host_lower or re.search(r'/\d{4}-\d{3}[\dx]/', path_lower))
        and re.search(r'/s[1-9]\d?/?$', path_lower)
    ):
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
    # P1-B: a default port is not a different file (PMC serves the same
    # /bin/ file with and without :443, e.g. #46's MOESM1 x3).
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
        prefix = userinfo + "@"
    else:
        hostport, prefix = netloc, ""
    if scheme == "https" and hostport.endswith(":443"):
        hostport = hostport[:-4]
    elif scheme == "http" and hostport.endswith(":80"):
        hostport = hostport[:-3]
    netloc = prefix + hostport
    return urllib.parse.urlunsplit((scheme, netloc, parts.path, query, ""))


# P1-B: a trailing _N on a filename is (almost always) a collision suffix
# produced by the D2 disambiguators (find_all_candidates / download_file),
# not part of the file's identity: 'MOESM1_ESM_2.pdf', 'Appendix 7_18.bin'
# and 's001_2.docx' are the same file as their unsuffixed form. A large
# number is a real identifier instead (Wiley attachment_1001), so only
# _2.._999 are stripped. Generic placeholder names carry no identity at
# all and are exempt from filename dedup (URL dedup still applies).
_DUP_SUFFIX_RE = re.compile(r"_(\d+)$")
_GENERIC_DEDUP_EXEMPT_FILENAMES = frozenset({
    "download.bin", "download", "attachment.bin", "attachment",
    "file.bin", "file", "asset.bin", "content.bin",
    "index.html", "index.htm",
})


def filename_dedup_key(filename: str) -> Optional[str]:
    """Normalized filename dedup key, or None when the name carries no
    identity (empty / generic placeholder) and must not dedup by name."""
    name = urllib.parse.unquote((filename or "").strip()).lower()
    if not name or name in _GENERIC_DEDUP_EXEMPT_FILENAMES:
        return None
    base, ext = os.path.splitext(name)
    while True:
        m = _DUP_SUFFIX_RE.search(base)
        if not m:
            break
        if not 2 <= int(m.group(1)) <= 999:
            break
        base = base[:m.start()]
    key = base + ext
    if not key or key in _GENERIC_DEDUP_EXEMPT_FILENAMES:
        return None
    return key


def dedupe_candidates(candidates: List["Candidate"]) -> List["Candidate"]:
    """Merge candidates that point at the same file by normalized URL or by
    normalized filename (P1-B). Order is preserved; the first occurrence is
    kept and absorbs the table ids of the duplicates (H6 merge)."""
    kept: List["Candidate"] = []
    by_url: Dict[str, "Candidate"] = {}
    by_fname: Dict[str, "Candidate"] = {}
    for cand in candidates:
        raw_url = (cand.url or "").strip()
        norm_url = _normalize_url_for_dedup(raw_url.split("#")[0].strip()) if raw_url else ""
        fname_key = filename_dedup_key(cand.filename)
        existing = by_url.get(norm_url) if norm_url else None
        if existing is None and fname_key is not None:
            existing = by_fname.get(fname_key)
        if existing is not None:
            _merge_duplicate_candidate(existing, cand)
            continue
        if norm_url:
            by_url[norm_url] = cand
        if fname_key is not None:
            by_fname[fname_key] = cand
        kept.append(cand)
    return kept


def _merge_duplicate_candidate(existing: "Candidate", cand: "Candidate") -> None:
    """Fold a duplicate candidate into the kept one (H6): union the table
    ids, prefer an explicit supplementary label, prefer a specific filename
    over a generic one, and prefer the unsuffixed filename (P1-B)."""
    existing.covered_table_ids |= cand.covered_table_ids
    if cand.extra_info.get("structural_supp"):
        existing.extra_info["structural_supp"] = True
    if existing.section in ("", "page_body") and cand.section in _DOM_SUPP_SECTIONS:
        existing.section = cand.section
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
    elif _DUP_SUFFIX_RE.search(os.path.splitext(existing.filename)[0] or "") and not _DUP_SUFFIX_RE.search(
        os.path.splitext(cand.filename)[0] or ""
    ):
        existing.filename = cand.filename


# ── P1-CE: phase budget + candidate priority ────────────────────────────────
# 50-paper retest (~/workspace/jsd-50-paper-test-p1-report.md, sec. 4 item 3):
# Phase A timed out on 49/50 papers and Phase B on 41/50 against a 150s
# budget that only existed in the test runner, so nothing in the tools
# knew about it. The budget below is that same 150s, now a real,
# configurable parameter shared by both entry points.

DEFAULT_PHASE_TIMEOUT_SECONDS = 150


def resolve_phase_timeout(value, default=DEFAULT_PHASE_TIMEOUT_SECONDS):
    """Normalize a --phase-timeout value to seconds (float).

    Accepts numbers and numeric strings; anything missing, non-numeric,
    or <= 0 falls back to *default* (150s) instead of failing the run or
    silently disabling the budget.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return float(default)
    if seconds <= 0 or seconds != seconds:  # <= 0 or NaN
        return float(default)
    return seconds


# ── P1 H-1: independent budget for the browser fallback ───────────────────
# Round-3 report (~/workspace/jsd-50-paper-test-p1fix-report.md, sec. 2(h)
# H-1, sec. 4 item 1): after --proxy fixed the tunnel failures, the
# browser fallback stopped failing fast and started hanging -- one page
# load over the proxy takes 60-90s, so the 150s phase budget dies inside
# a single page load and the OA mirror / download rounds never run
# (#1: R2 13 files via mirrors after a fast browser failure; R3 0 files
# after a 120s+ hang on nature.com). The phase budget alone cannot fix
# this: a single StealthyFetcher.fetch / page.goto call has no notion
# of the phase deadline (P1-CE legacy note). The browser fallback
# therefore gets its own two quotas, shared by both entry points:
# one page goto at most 30s, 60s of browser time in total per phase.
# Either quota running out degrades to the OA mirror round (requests)
# instead of killing the phase.
#
# The quotas bound *attempts*, not a blocking call already in flight:
# a goto is launched with timeout=min(goto, remaining), and no new
# browser attempt starts once the total is spent. A live CDP /
# BrowserSkill browser cannot take a per-request proxy or a goto
# timeout from us (P1-D legacy note); those paths only receive the
# capped timeout value where their API accepts one, and are skipped
# outright once the budget is gone.
#
# P1 C: the two quotas above are not the whole story -- the *phase*
# deadline (P1-CE, --phase-timeout, default 150s) is a third cap. A
# 60s fallback quota can outlive a short phase, and a 30s goto
# launched with 10s of phase left overshoots the deadline on its own;
# that is exactly how round 3 died inside page loads (the deadline
# logic fired 0 times: every load took its full slice and the runner
# killed the phase before the download loop's deadline check ever
# ran). The budget therefore takes the phase deadline as well: one
# attempt runs at most min(goto, total remaining, phase remaining),
# and the budget counts as exhausted once the phase deadline passes,
# so the fallback quota can never exceed the remaining phase budget.
# Enforcement happens *before launch* (clamping the timeout passed
# to the underlying call) because no API can interrupt a call already
# in flight beyond its own timeout: BSK navigate() honours its
# subprocess timeout, CDP page.goto() honours its timeout, and the
# requests/StealthyFetcher timeouts are real interrupts too -- but
# only if they are given a value that respects the deadline.

def playwright_proxy_config(proxy):
    """Convert a --proxy URL/dict to Playwright's proxy dict."""
    if not proxy:
        return None
    if isinstance(proxy, dict):
        return dict(proxy)
    parsed = urllib.parse.urlparse(str(proxy))
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid proxy URL (expected http://[user:pass@]host:port)") from None
    if not parsed.hostname:
        raise ValueError("Invalid proxy URL (expected http://[user:pass@]host:port)")
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if port:
        server += f":{port}"
    config = {"server": server}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS = 30
DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS = 60


def resolve_browser_goto_timeout(value, default=DEFAULT_BROWSER_GOTO_TIMEOUT_SECONDS):
    """Normalize a --browser-goto-timeout value to seconds (float).

    Same contract as resolve_phase_timeout: numbers and numeric
    strings accepted; anything missing, non-numeric, or <= 0 falls
    back to *default* (30s).
    """
    return resolve_phase_timeout(value, default=default)


def resolve_browser_fallback_budget(value, default=DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS):
    """Normalize a --browser-fallback-budget value to seconds (float).

    Same contract as resolve_phase_timeout, default 60s.
    """
    return resolve_phase_timeout(value, default=default)


class BrowserFallbackBudget:
    """Wall-clock quota for browser fallback attempts within one phase.

    Created once per phase (run_download) and shared by every browser
    attempt in it, so the total browser time -- not just one goto --
    is capped. Uses time.monotonic, so tests can drive it with a fake
    clock the same way the phase-deadline tests do.

    P1 C: ``phase_deadline`` (a ``time.monotonic()`` value, normally
    the run's ``--phase-timeout`` deadline) is a third cap on top of
    the goto and total quotas: one attempt runs at most min(goto,
    total remaining, phase remaining), and the budget counts as
    exhausted once the phase deadline passes. With no phase deadline
    the budget behaves exactly as H-1 left it.
    """

    def __init__(self, goto_timeout=None, total_budget=None,
                 phase_deadline=None):
        self.goto_timeout = resolve_browser_goto_timeout(goto_timeout)
        self.total_budget = resolve_browser_fallback_budget(total_budget)
        self.phase_deadline = phase_deadline
        self._started_at = time.monotonic()
        self._announced = False

    def elapsed(self):
        """Seconds of wall time since this budget was created."""
        return time.monotonic() - self._started_at

    def remaining(self):
        """Seconds of the total budget not yet spent (<= 0 when gone)."""
        return self.total_budget - self.elapsed()

    def phase_remaining(self):
        """Seconds until the phase deadline, or None when this budget
        has no phase deadline attached."""
        if self.phase_deadline is None:
            return None
        return self.phase_deadline - time.monotonic()

    def set_phase_deadline(self, deadline):
        """Attach (or replace) the phase deadline. Used by
        fetch_page_html when the caller passes a deadline without a
        run-shared budget; the explicit deadline always wins."""
        self.phase_deadline = deadline

    @property
    def exhausted(self):
        """True once the total browser budget is spent, or once the
        phase deadline has passed (P1 C: the fallback quota must not
        outlive the phase)."""
        if self.remaining() <= 0:
            return True
        remaining_phase = self.phase_remaining()
        return remaining_phase is not None and remaining_phase <= 0

    def attempt_timeout(self):
        """Seconds the next browser attempt may run: the per-page goto
        timeout, capped at whatever of the total budget is left and
        at the remaining phase budget (P1 C)."""
        cap = min(self.goto_timeout, self.remaining())
        remaining_phase = self.phase_remaining()
        if remaining_phase is not None:
            cap = min(cap, remaining_phase)
        return max(0.0, cap)

    def attempt_timeout_ms(self):
        """attempt_timeout() in milliseconds (StealthyFetcher units)."""
        return int(self.attempt_timeout() * 1000)

    def announce_exhausted(self):
        """Print the downgrade line once and return True; later calls
        are silent and return False, so the log is not spammed once
        per skipped attempt."""
        if self._announced:
            return False
        self._announced = True
        # P1 C: say when the phase deadline (not the fallback quota)
        # is what ran out, so the log does not blame the 60s quota
        # for a stop the 150s phase caused.
        phase_note = ""
        if (self.phase_deadline is not None
                and self.phase_remaining() <= 0
                and self.remaining() > 0):
            phase_note = "，阶段预算已到"
        print(f"  [Browser] 回退预算耗尽，降级到 OA 镜像轮次"
              f"（已用 {self.elapsed():.1f}s / 配额 {self.total_budget:g}s，"
              f"单页 {self.goto_timeout:g}s{phase_note}）")
        return True


def browser_budget_from_args(args, phase_deadline=None):
    """Build a BrowserFallbackBudget from CLI-parsed args (getattr, so
    direct callers / old tests that lack the new attributes still get
    the 30s/60s defaults). ``phase_deadline`` (P1 C) is the run's
    ``time.monotonic()`` phase deadline, or None for a standalone
    budget with no phase to respect."""
    return BrowserFallbackBudget(
        goto_timeout=getattr(args, "browser_goto_timeout", None),
        total_budget=getattr(args, "browser_fallback_budget", None),
        phase_deadline=phase_deadline,
    )


def ensure_phase_browser_budget(budget, phase_deadline=None, explicit_budget=None):
    """Allocate a fresh phase-bounded recovery sub-budget when an alternate
    publisher or repository expansion runs after the primary host spent the
    initial budget, unless the caller explicitly pinned a custom budget."""
    if budget is None:
        return BrowserFallbackBudget(phase_deadline=phase_deadline)
    has_custom_cap = (
        explicit_budget is not None
        and float(explicit_budget) != float(DEFAULT_BROWSER_FALLBACK_BUDGET_SECONDS)
    )
    if not budget.exhausted or has_custom_cap or phase_deadline is None:
        return budget
    rem_phase = phase_deadline - time.monotonic()
    if rem_phase < 15.0:
        return budget
    alloc = min(max(float(budget.total_budget or 60.0), 60.0), max(20.0, rem_phase - 10.0))
    return BrowserFallbackBudget(
        goto_timeout=budget.goto_timeout,
        total_budget=alloc,
        phase_deadline=phase_deadline,
    )


_DEFAULT_SLOW_THIRD_PARTY_DOMAINS = frozenset({
    "ajax.googleapis.com",
    "fonts.googleapis.com",
    "scholar.google.com",
    "www.googletagmanager.com",
    "googletagmanager.com",
    "www.google-analytics.com",
    "google-analytics.com",
    "cookie-cdn.cookiepro.com",
    "cdn.cookielaw.org",
    "geolocation.onetrust.com",
    "js.trendmd.com",
})


def _merge_blocked_domains(url: str, existing_domains, proxy: str = "") -> set[str]:
    """Merge default hanging third-party tracker/CDN domains unless target URL hosts them."""
    target_host = (urllib.parse.urlparse(url).hostname or "").lower()
    extra = set(_DEFAULT_SLOW_THIRD_PARTY_DOMAINS)
    if proxy:
        extra -= {"ajax.googleapis.com", "fonts.googleapis.com"}
    merged = set(existing_domains or ())
    for dom in extra:
        if target_host == dom or target_host.endswith("." + dom):
            continue
        merged.add(dom)
    return merged


def stealthy_fetch_bounded(fetcher_cls, url: str, **kwargs):
    """Call ``fetcher_cls.fetch(url, **kwargs)`` while honouring ``timeout < 60000``
    even when ``solve_cloudflare=True``.

    Scrapling's ``StealthConfig.__post_init__`` silently forces
    ``timeout = 60000`` whenever ``solve_cloudflare=True`` and
    ``timeout < 60000``.  Calling ``StealthySession(**session_kwargs)``
    followed by ``engine.fetch(url, timeout=req_ms, ...)`` keeps
    ``solve_cloudflare=True`` without resetting ``params.timeout`` back
    to 60000 ms, aborts third-party CDN/consent scripts that stall `<head>`
    parsing (e.g. ``ajax.googleapis.com`` / ``cookiepro`` on Silverchair/GSW),
    and tightens ``domcontentloaded``/``load``/``networkidle`` and ``page_action``
    timeouts to the remaining attempt budget.  When ``fetcher_cls`` is mocked
    in tests, delegates directly to ``fetcher_cls.fetch(url, **kwargs)``.
    """
    is_real_stealth = (
        getattr(fetcher_cls, "__module__", "") == "scrapling.fetchers.stealth_chrome"
        and getattr(getattr(fetcher_cls, "fetch", None), "__module__", "") == "scrapling.fetchers.stealth_chrome"
    )
    req_timeout = kwargs.get("timeout")
    if not is_real_stealth:
        return fetcher_cls.fetch(url, **kwargs)

    from scrapling.fetchers import StealthySession

    req_ms = max(1000, int(req_timeout)) if req_timeout is not None else 45000
    deadline_t = time.monotonic() + (req_ms / 1000.0)
    session_kwargs = dict(kwargs)
    session_kwargs.setdefault("retries", 1)
    session_kwargs.setdefault("block_ads", True)
    session_kwargs["blocked_domains"] = _merge_blocked_domains(
        url,
        session_kwargs.get("blocked_domains"),
        proxy=str(session_kwargs.get("proxy") or ""),
    )
    orig_setup = session_kwargs.pop("page_setup", None)
    orig_action = session_kwargs.pop("page_action", None)
    wait_ms = session_kwargs.pop("wait", None)
    session_kwargs.pop("timeout", None)

    def _wrapped_setup(page):
        def _clamp_post_load(_p=None):
            rem_ms = max(1000, int((deadline_t - time.monotonic()) * 1000))
            capped_ms = min(req_ms, rem_ms)
            try:
                page.set_default_timeout(capped_ms)
            except Exception:
                pass
            try:
                page.set_default_navigation_timeout(capped_ms)
            except Exception:
                pass

        _clamp_post_load()

        orig_wait_for_load_state = getattr(page, "wait_for_load_state", None)
        if callable(orig_wait_for_load_state):
            def _bounded_wait_for_load_state(state="load", *w_args, **w_kwargs):
                if state in ("networkidle", "load", "domcontentloaded"):
                    rem_ms = int((deadline_t - time.monotonic()) * 1000)
                    if rem_ms <= 0:
                        return None
                    max_wait = 4000 if state == "networkidle" else 8000
                    cap_ms = min(max_wait, max(500, rem_ms))
                    if "timeout" in w_kwargs and w_kwargs["timeout"] is not None:
                        w_kwargs["timeout"] = min(int(w_kwargs["timeout"]), cap_ms)
                    elif w_args:
                        w_args = (min(int(w_args[0]), cap_ms),) + w_args[1:]
                    else:
                        w_kwargs["timeout"] = cap_ms
                    try:
                        return orig_wait_for_load_state(state, *w_args, **w_kwargs)
                    except Exception:
                        if state in ("load", "domcontentloaded"):
                            return None
                        raise
                return orig_wait_for_load_state(state, *w_args, **w_kwargs)

            try:
                page.wait_for_load_state = _bounded_wait_for_load_state
            except Exception:
                pass

        try:
            page.on("domcontentloaded", _clamp_post_load)
            page.on("load", _clamp_post_load)
        except Exception:
            pass
        if orig_setup is not None:
            orig_setup(page)

    def _wrapped_action(page):
        if time.monotonic() >= deadline_t:
            return
        rem_ms = max(1000, int((deadline_t - time.monotonic()) * 1000))
        capped_ms = min(req_ms, rem_ms)
        try:
            page.set_default_timeout(capped_ms)
        except Exception:
            pass
        try:
            page.set_default_navigation_timeout(capped_ms)
        except Exception:
            pass
        if orig_action is not None:
            return orig_action(page)

    fetch_kwargs = {
        "timeout": req_ms,
        "page_setup": _wrapped_setup,
    }
    if orig_action is not None:
        fetch_kwargs["page_action"] = _wrapped_action
    if wait_ms is not None:
        fetch_kwargs["wait"] = min(int(wait_ms), max(0, req_ms // 3))

    with StealthySession(**session_kwargs) as engine:
        try:
            return engine.fetch(url, **fetch_kwargs)
        except Exception as exc:
            err_l = str(exc).lower()
            is_nav_race = (
                "navigating and changing the content" in err_l
                or "execution context was destroyed" in err_l
            )
            rem_s = deadline_t - time.monotonic()
            if not is_nav_race or rem_s <= 4.0:
                raise
            retry_kwargs = dict(fetch_kwargs)
            retry_kwargs["timeout"] = max(1000, min(req_ms, int(rem_s * 1000)))
            return engine.fetch(url, **retry_kwargs)


# A .pdf candidate is a real supplement file here (candidates are already
# supplementary-filtered); .pdf is not in DATA_EXTENSIONS because that set
# is also used to reject bare article PDFs during extraction.
_PRIORITY_DATA_EXTENSIONS = frozenset(DATA_EXTENSIONS) | {".pdf"}


# P1 round-3 minor (a): the MITM egress proxy re-signs TLS with its own
# CA, which Chromium does not trust -- round 3, Wiley #21-#23: one page's
# first goto timed out and the retry failed with
# net::ERR_CERT_AUTHORITY_INVALID, while a later attempt in the same
# fetch went through. No call site passed any explicit trust config:
# trust was entirely implicit in scrapling's own default
# (StealthySessionMixin sets ignore_https_errors=True inside its
# _context_options), so nothing guaranteed a fresh context or a retry
# path carried the same trust config as the first context. One shared
# helper builds that config so every StealthyFetcher.fetch /
# StealthySession call -- first call and every retry, which copy the
# same kwargs dict -- carries identical proxy + trust settings:
#   * context level: additional_args={"ignore_https_errors": True}
#     (scrapling merges additional_args into the context options with
#     priority over its own defaults, so this also pins the behaviour
#     if a scrapling upgrade ever drops the implicit default);
#   * browser-launch level: extra_flags=["--ignore-certificate-errors"]
#     (applied from process start, before any context exists -- this is
#     what covers a first-navigation window in a fresh browser).
# It returns {} with no proxy, so direct runs keep full certificate
# validation; only proxy mode relaxes it, because the proxy CA is the
# reason certs fail there. A fresh dict is built on every call so a
# caller mutating one call's kwargs cannot leak into the next.
# CDP / BrowserSkill paths attach to a live browser and cannot take a
# per-request proxy or trust config; they are intentionally untouched.
def proxy_browser_trust_kwargs(proxy):
    """StealthyFetcher / StealthySession kwargs carrying the MITM-proxy
    certificate trust config for ``proxy`` (``http://[user:pass@]host:port``).

    Returns {} when no proxy is set (no trust relaxation at all).
    """
    if not proxy:
        return {}
    return {
        "additional_args": {"ignore_https_errors": True},
        "extra_flags": ["--ignore-certificate-errors"],
    }


def has_system_chrome() -> bool:
    """Return True if a standalone desktop Chrome binary is installed locally."""
    for env_var in ("CHROME_PATH", "GOOGLE_CHROME_BIN"):
        candidate = os.environ.get(env_var, "").strip()
        if candidate and os.path.exists(candidate):
            return True
    if sys.platform == "darwin":
        mac_paths = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
        return any(os.path.exists(p) for p in mac_paths)
    if sys.platform.startswith("win"):
        win_paths = (
            os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        )
        return any(p and os.path.exists(p) for p in win_paths)
    return any(shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chrome"))


def has_gui_display() -> bool:
    """Return True when a desktop GUI session is available for headful browser fallback."""
    if os.environ.get("HEADLESS_ONLY", "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.environ.get("CI", "").strip().lower() in ("1", "true") and not os.environ.get("ALLOW_CI_HEADFUL"):
        return False
    if sys.platform == "darwin" or sys.platform.startswith("win"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def add_auto_browser_arguments(parser) -> None:
    """Register opt-out flags for automatic live-browser / headful recovery."""
    parser.add_argument(
        "--no-cdp",
        action="store_true",
        help="Disable automatic attach to a running local Chrome CDP session on blocked/empty discovery",
    )
    parser.add_argument(
        "--no-auto-headful",
        "--no-auto-browser",
        dest="no_auto_headful",
        action="store_true",
        help="Disable automatic standalone headful Chrome fallback on blocked/empty discovery",
    )


def run_auto_browser_recovery(
    *,
    need_recovery: bool,
    retry_allowed: bool,
    browser_allowed_fn,
    try_bsk_fn=None,
    try_cdp_fn=None,
    try_headful_fn=None,
    evaluate_fn=None,
    bsk_enabled: bool = True,
    cdp_enabled: bool = True,
    headful_enabled: bool = True,
) -> Dict[str, Any]:
    """Run priority-ordered automatic browser recovery after fast/headless discovery.

    Priority 1 (when an interactive session is already active; <5 ms probe if absent):
      1a. Connected BrowserSkill (``bsk``) live browser session.
      1b. Active local Chrome CDP session via ``discover_active_cdp_port()``.
    Priority 2 (zero-setup rescue & backup when Priority 1 is absent or incomplete):
      Standalone headful Chrome/Chromium (``headless=False``, ``real_chrome``,
      ``network_idle=False``, ``solve_cloudflare=True``).
    """
    summary: Dict[str, Any] = {"mode": None, "recovered": False, "attempts": []}
    if not need_recovery or not retry_allowed or evaluate_fn is None:
        return summary

    def _run_step(name: str, fn):
        if fn is None or not browser_allowed_fn():
            return False, False
        summary["attempts"].append(name)
        res = fn()
        if not res:
            return False, False
        html_str, status, final_url, extra_cookies = res
        if not html_str:
            return False, False
        enough, accepted = evaluate_fn(html_str, status, final_url, extra_cookies, name)
        if accepted:
            summary["mode"] = name
            summary["recovered"] = True
        return bool(enough), bool(accepted)

    if bsk_enabled:
        enough, _ = _run_step("auto_bsk", try_bsk_fn)
        if enough:
            return summary

    if cdp_enabled:
        enough, _ = _run_step("auto_cdp", try_cdp_fn)
        if enough:
            return summary

    if headful_enabled:
        _run_step("auto_headful", try_headful_fn)

    return summary


def candidate_priority(cand: "Candidate") -> int:
    """Priority tier for download order (P1-CE; lower runs first).

    0 -- a real supplement file: the URL/filename ends in a data
         extension (.xlsx/.xls/.csv/.tsv/.docx/.pdf/.zip/...).
    1 -- explicitly supplementary-marked but no data extension (a
         supplementary landing page, a .html supplement table, a
         repository record labelled as supplementary, or a candidate
         known to cover table ids).
    2 -- everything else (external / repository / other pages).

    Nothing is ever dropped by priority; tier 2 only runs last, so a
    150s budget is spent on real files before junk/external candidates.
    """
    for source in (getattr(cand, "url", "") or "", getattr(cand, "filename", "") or ""):
        ext = check_data_extension(source) if source else None
        if ext and ext in _PRIORITY_DATA_EXTENSIONS:
            return 0
        path = urllib.parse.urlparse(source).path.lower()
        if path.endswith(".pdf"):
            return 0
    label = " ".join(
        str(v) for v in (
            getattr(cand, "link_text", ""),
            getattr(cand, "section", ""),
            getattr(cand, "match_rule", ""),
        ) if v
    )
    if (
        getattr(cand, "covered_table_ids", None)
        or getattr(cand, "section", "") == "supplementary_section"
        or is_explicit_supp_table(getattr(cand, "link_text", "") or "")
        or SUPP_KEYWORD_PATTERN.search(label)
    ):
        return 1
    return 2


_PMC_BIN_S_FILE_RE = re.compile(r"\.s\d+\.", re.IGNORECASE)


def _is_pmc_bin_s_file(cand: "Candidate") -> bool:
    """True for a PMC /bin/ s file (P1 round-3 minor (c), #31:
    ``pbio.3003193.s001.tiff`` -- the article's own supplementary
    files, as opposed to a repository data package)."""
    url = getattr(cand, "url", "") or ""
    path = urllib.parse.urlparse(url).path.lower()
    if "/bin/" not in path:
        return False
    name = getattr(cand, "filename", "") or ""
    return bool(_PMC_BIN_S_FILE_RE.search(name) or _PMC_BIN_S_FILE_RE.search(url))


def _is_repository_data_package(cand: "Candidate") -> bool:
    """True for a repository data package as opposed to an article
    supplement file (P1 round-3 minor (c), #31): a zenodo.org record
    link, or a Data Availability section link. These are real research
    data, but they are not the article's s files and must not consume
    the download budget before them."""
    url = getattr(cand, "url", "") or ""
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "zenodo.org" or host.endswith(".zenodo.org"):
        return True
    label = " ".join(
        str(v) for v in (
            getattr(cand, "link_text", ""),
            getattr(cand, "section", ""),
        ) if v
    ).lower()
    return "data availability" in label


def _priority_sub_rank(cand: "Candidate") -> int:
    """Within-tier download order (P1 round-3 minor (c)).

    0 -- a PMC /bin/ s file; 1 -- anything else; 2 -- a repository
    data package (Zenodo / Data Availability). Only the order *inside*
    a tier changes: the tier itself still comes from
    ``candidate_priority`` (0 real file / 1 explicitly supplementary /
    2 other), so a tier-2 s file never jumps ahead of a tier-0 file.
    """
    if _is_pmc_bin_s_file(cand):
        return 0
    if _is_repository_data_package(cand):
        return 2
    return 1


def sort_candidates_by_priority(candidates):
    """Return candidates in download order: tier 0, then 1, then 2.

    Stable within a tier (discovery order is kept) and lossless -- the
    result is a permutation of the input, never a filter. Within a
    tier, PMC /bin/ s files run before repository data packages
    (Zenodo / Data Availability, P1 round-3 minor (c)): on #31 the
    Zenodo .csv/.zip packages and the true s files were interleaved
    and a Phase B timeout left all 17 s files undownloaded.
    """
    return sorted(
        candidates,
        key=lambda c: (candidate_priority(c), _priority_sub_rank(c)),
    )


def _infer_candidate_table_ids(link_text: str, filename: str, attrs: Optional[Dict[str, Any]] = None) -> Set[str]:
    """Table ids named by a candidate's own label, filename, or attributes (H6).

    The label/filename itself is supplementary context (P1-A), so bare
    lists there still resolve; the word-boundary rule in
    supplementary_table_ids still applies.
    """
    ids: Set[str] = set()
    for value in (link_text, filename):
        ids |= supplementary_table_ids(value, in_supplementary_context=True)
    for value in (attrs or {}).values():
        if isinstance(value, str):
            ids |= supplementary_table_ids(value, in_supplementary_context=True)
        elif isinstance(value, list):
            for item in value:
                ids |= supplementary_table_ids(str(item), in_supplementary_context=True)
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
    # The parent row holds a single download control, so its own text is
    # supplementary context (P1-A).
    return supplementary_table_ids(direct_text, in_supplementary_context=True)


# Inline JSON payloads whose URLs static parsing may miss (H3).
_INLINE_JSON_MARKERS = (
    "__NEXT_DATA__", "__INITIAL_STATE__", "__NUXT__", "__NUXT_DATA__",
    "__PRELOADED_STATE__", "xplGlobal.document.metadata",
)

_SUPP_SIGNAL_KEYWORDS = (
    "supplement", "appendix", "appendices", "supporting-info", "supporting_info",
    "additional-file", "extended-data", "extended_data",
    "core-collateral-supp", "esiright", "suppdata", "supinfolist", "suppinfolist",
    "补充", "附表", "附录", "支撑材料",
)
_SUPP_SIGNAL_TOKEN_RE = re.compile(
    r"(?<![a-z0-9])(?:(?:mo)?esm(?:[-_]?\d+)?|suppl\.?)(?![a-z0-9])",
    re.IGNORECASE,
)
_NAV_MENU_CHROME_RE = re.compile(
    r"(?:publication__(?:sup__)?menu|dropdown__menu|main[-_]?nav|header[-_]?nav|site[-_]?menu|journal[-_]?menu|services?menu|top[-_]?nav|navbar)",
    re.IGNORECASE,
)


def _ident_has_supp_signal(ident: str) -> bool:
    if not ident:
        return False
    low = ident.lower()
    return any(kw in low for kw in _SUPP_SIGNAL_KEYWORDS) or bool(_SUPP_SIGNAL_TOKEN_RE.search(low))


def _has_actionable_supp_content(section) -> bool:
    """True when a supplementary section contains non-empty text or at least one actionable control.

    Empty placeholder containers such as ``<div class="Supplements-left-list"><a href="javascript:;"></a></div>``
    or ``<a href=""></a>`` (ChinaGeology RHHZ2 when a paper has no supplements) return False.
    """
    if section is None:
        return False
    if section.get_text(" ", strip=True):
        return True
    for ctrl in section.find_all(["a", "button", "input", "form"]):
        if ctrl.name == "a":
            href = (ctrl.get("href") or "").strip()
            href_low = href.lower()
            has_href = bool(href) and not href_low.startswith(("javascript:", "#", "mailto:", "tel:"))
            has_js = bool(ctrl.get("onclick") or ctrl.get("data-url") or ctrl.get("data-journal-download-selector"))
            if has_href or has_js or ctrl.get_text(" ", strip=True):
                return True
        elif ctrl.name == "input":
            if (ctrl.get("type") or "").lower() != "hidden" and (ctrl.get("value") or ctrl.get("onclick") or ctrl.get("data-journal-download-selector")):
                return True
        elif ctrl.name == "button":
            if ctrl.get_text(" ", strip=True) or ctrl.get("onclick") or ctrl.get("data-url") or ctrl.get("data-journal-download-selector"):
                return True
        elif ctrl.name == "form":
            if ctrl.get("action") and ctrl.find(["button", "input"]):
                return True
    return False


def _is_journal_nav_chrome(el) -> bool:
    """True when an element sits inside site/journal navigation or dropdown menus (even outside <nav>)."""
    if el is None:
        return False
    if el.name in ("nav", "footer") or el.find_parent(["nav", "footer"]) is not None:
        return True
    anc = el
    while anc and hasattr(anc, "name"):
        role = str(anc.get("role") or "").lower()
        if role in {"navigation", "menubar", "menu"}:
            return True
        cls_str = " ".join(anc.get("class") or []) if isinstance(anc.get("class"), list) else str(anc.get("class") or "")
        ident = f"{anc.get('id') or ''} {cls_str}".strip()
        if ident and _NAV_MENU_CHROME_RE.search(ident) and not re.search(
            r"supplementary[-_]?material|supplemental[-_]?material|supporting[-_]?info|article[-_]?section",
            ident,
            re.IGNORECASE,
        ):
            return True
        anc = anc.parent
    return False


def _is_empty_spa_shell(soup: BeautifulSoup, html_content: str) -> bool:
    """True when static HTML is an unhydrated Vue/React/Next/Nuxt SPA mount shell."""
    if not html_content:
        return False
    spa_mount = soup.find(id=re.compile(r"^(?:app|root|__next|__nuxt)$"))
    if spa_mount is None:
        return False
    if "<script" not in html_content.lower():
        return False
    mount_text = spa_mount.get_text(" ", strip=True)
    if mount_text:
        return False
    body = soup.find("body")
    if body is None:
        return True
    visible_chunks = [
        t.strip()
        for t in body.find_all(string=True)
        if t.strip() and getattr(t.parent, "name", "").lower() not in {"script", "style", "noscript"}
    ]
    return len(" ".join(visible_chunks)) < 200


def _page_has_supplementary_signal(soup) -> bool:
    """True when the page shows a supplementary heading, section, or collapsed control (H3)."""
    for el in soup.find_all(["section", "div", "aside", "details", "figure"]):
        if _is_journal_nav_chrome(el):
            continue
        ident = (el.get("id", "") + " " + " ".join(el.get("class", []))).lower()
        if _ident_has_supp_signal(ident):
            # Ignore completely empty placeholder wrappers when no interactive controls or text exist inside
            if not _has_actionable_supp_content(el):
                continue
            return True
    inline_table_ids = None
    for heading in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        if _is_journal_nav_chrome(heading):
            continue
        htext = heading.get_text(" ", strip=True) or ""
        # Springer Nature / BMC puts an "Additional information" (Rights/Publisher's note) heading on all articles
        if re.sub(r"\s+", " ", htext.strip()).lower() in {"additional information", "additional info"}:
            continue
        if SUPP_KEYWORD_PATTERN.search(htext):
            # In-article lettered Appendix headings (e.g. IOP "Appendix A: ...") whose tables are already
            # rendered inline as HTML <table> elements without supplementary keywords are not unloaded downloads.
            if re.match(r"^appendix\s+[a-z]\b", htext.strip(), re.IGNORECASE) and not re.search(
                r"supplement|supporting|additional|file|download", htext, re.IGNORECASE
            ):
                if inline_table_ids is None:
                    inline_table_ids = _inline_html_table_ids(soup)
                if inline_table_ids:
                    continue
            return True
    _zero_supp_ctrl_re = re.compile(
        r"^(?:supplements?|supplementary(?:\s+(?:materials?|files?|info(?:rmation)?|data))?|supporting\s+info(?:rmation)?|additional\s+files?|附件|补充材料|补充文件)\s*\(\s*0\s*\)$",
        re.IGNORECASE,
    )
    ctrl_candidates = []
    explicit_zero_supp_count = False
    for el in soup.find_all(["button", "a", "summary"]):
        # Ignore navigation/footer/publication-menu chrome (e.g. Cambridge/De Gruyter/Lyell journal menus)
        if _is_journal_nav_chrome(el):
            continue
        href_low = str(el.get("href") or "").lower()
        if any(seg in href_low for seg in ("/services/authors/", "/for-booksellers", "/author-services/")):
            continue
        text = " ".join([
            el.get_text(" ", strip=True) or "",
            str(el.get("aria-label") or ""),
            str(el.get("title") or ""),
        ])
        if not text.strip():
            continue
        clean_inner = re.sub(r"\s+", " ", (el.get_text(" ", strip=True) or "")).strip()
        if clean_inner.lower() in {"additional information", "additional info"}:
            continue
        if _zero_supp_ctrl_re.match(clean_inner):
            explicit_zero_supp_count = True
            continue
        if SUPP_KEYWORD_PATTERN.search(text):
            ctrl_candidates.append(el)

    for el in ctrl_candidates:
        href_raw = str(el.get("href") or "").strip()
        is_tab_toggle = (
            str(el.get("data-toggle") or "").lower() == "tab"
            or str(el.get("role") or "").lower() == "tab"
        )
        if explicit_zero_supp_count and is_tab_toggle and href_raw.startswith("#") and len(href_raw) > 1:
            target_pane = soup.find(id=href_raw[1:])
            if target_pane is None or not _has_actionable_supp_content(target_pane):
                continue
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
    seen_fnames: Set[str] = set()
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
        if is_excluded_url(raw_cand_url, base_url=page_urls, text=cand.link_text, attrs=evidence_attrs, filename=cand.filename, diag=diag):
            diag.excluded_links.append((raw_cand_url, "excluded_url_pattern"))
            return
        norm_url = raw_cand_url.split("#")[0].strip()
        if not norm_url:
            return
        if is_same_article_page_url(norm_url, page_urls):
            diag.excluded_links.append((norm_url, "same_article_page_url"))
            return
        if is_excluded_url(norm_url, base_url=page_urls, text=cand.link_text, attrs=evidence_attrs, filename=cand.filename, diag=diag):
            diag.excluded_links.append((norm_url, "excluded_url_pattern"))
            return
        # Design principle: recall > precision. Candidates already obtained via an
        # explicit HTML supplementary structural position must NOT be dropped by
        # downstream heuristic figure-exclusion rules (only explicit inline figure
        # viewer/slide actions like View large / DownloadImage.aspx are excluded).
        is_structural_cand = bool(cand.extra_info.get("structural_supp"))
        if is_structural_cand:
            if _is_inline_figure_action(norm_url, text=cand.link_text, attrs=evidence_attrs):
                diag.excluded_links.append((norm_url, "article_figure_pattern"))
                return
        elif is_article_figure_url(norm_url, text=cand.link_text, attrs=evidence_attrs):
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
                    _merge_duplicate_candidate(existing, cand)
                    break
            return
        # P1-B: same file reached via a different (mirror) URL -- e.g. the
        # Springer static-content URL and the PMC /bin/ URL for one MOESM
        # (#46), or the 18 /table/app7tableN/ links all labelled
        # 'Appendix 7' (#36) -- keeps ONE candidate, keyed on the
        # normalized filename (_2-style collision suffixes ignored).
        fname_key = filename_dedup_key(cand.filename)
        if fname_key is not None and fname_key in seen_fnames:
            for existing in candidates:
                if filename_dedup_key(existing.filename) == fname_key:
                    _merge_duplicate_candidate(existing, cand)
                    break
            return
        seen_urls.add(dedup_key)
        if fname_key is not None:
            seen_fnames.add(fname_key)
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
                    covered_table_ids=supplementary_table_ids(
                        control_label, in_supplementary_context=True)))
            continue

        link_text = tag.get("value", "") if tag.name == "input" else tag.get_text(separator=" ", strip=True)
        attrs = {k: tag[k] for k in ["aria-label", "title", "download", "data-filename", "data-title", "data-caption", "value"] if tag.has_attr(k)}

        full_url = urllib.parse.urljoin(effective_base, target_raw)
        is_supp_section = is_supplementary_container(tag)
        if is_excluded_url(full_url, base_url=page_urls, text=link_text, attrs=attrs, diag=diag):
            diag.excluded_links.append((full_url, "excluded_url_pattern"))
            continue
        if is_same_article_page_url(full_url, page_urls):
            diag.excluded_links.append((full_url, "same_article_page_url"))
            continue
        if is_supp_section:
            if _is_inline_figure_action(full_url, text=link_text, attrs=attrs):
                diag.excluded_links.append((full_url, "article_figure_pattern"))
                continue
        elif is_article_figure_url(full_url, text=link_text, attrs=attrs):
            diag.excluded_links.append((full_url, "article_figure_pattern"))
            continue

        default_candidate_name = attrs.get("data-filename") or attrs.get("title") or link_text
        fname = attrs.get("download") or extract_filename_from_url(full_url, default_name=default_candidate_name)
        parsed = urllib.parse.urlparse(full_url)
        path_lower = parsed.path.lower()

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
            cand_extra = {"attributes": attrs}
            if is_supp_section:
                cand_extra["structural_supp"] = True
            add_candidate(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=link_text,
                section=section,
                match_rule=rule,
                source_tag=tag.name,
                extra_info=cand_extra,
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
        or soup.find("a", attrs={"data-track-action": re.compile(r"download|supplementary", re.IGNORECASE)}) is not None
        or soup.find(attrs={"data-test": re.compile(r"supp[-_]|supplementary|source[-_]?data", re.IGNORECASE)}) is not None
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
        _extract_pmc_candidates(soup, effective_base, add_candidate, diag=diag)

    # GeoScienceWorld / Silverchair / OUP / AIP / JAMA / Allen Press / ACS / RSC supplementary widgets
    if (
        any(d in combined_url for d in [
            "geoscienceworld", "silverchair", "academic.oup.com", "10.5382", "10.2113",
            "10.1130", "10.2138", "10.1093", "pubs.aip.org", "jamanetwork.com",
            "meridian.allenpress.com", "pubs.asha.org", "ashpublications.org",
            "diabetesjournals.org", "aappublications.org", "ersjournals.com", "karger.com",
            "pubs.acs.org", "pubs.rsc.org", "rsc.org", "10.1021", "10.1039",
        ])
        or soup.select_one(
            ".widget-article-supplementary-data, .widget-SupplementaryData, "
            ".widget-instance-ArticleSupplementaryData, .widget-ArticleDataSupplements, .dataSuppLink, "
            ".supplementary-data-wrapper, .data-supplement, .data-supplement-item, "
            ".supplementary-data, #supplementary-data, [data-supplement], "
            "a[href*='content_public'], a[href*='silverchair-cdn'], "
            "a[href*='getDataSupplement'], a[href*='/article-supplement/'], [data-supplement-url]"
        ) is not None
    ):
        _extract_silverchair_candidates(soup, effective_base, add_candidate, diag=diag)

    # Atypon Literatum (Taylor & Francis, SAGE, ACS, AGU/Wiley, Science/AAAS, PNAS, Lyell/GSL, ASCE, De Gruyter, Emerald, APS)
    if (
        any(d in combined_url for d in [
            "tandfonline", "sagepub", "pubs.acs.org", "science.org", "pnas.org",
            "wiley.com", "lyellcollection.org", "ascelibrary.org", "annualreviews.org",
            "journals.asm.org", "pubs.rsna.org", "degruyter", "emerald.com", "cdnsciencepub",
            "journals.aps.org",
            "10.1080", "10.1177", "10.1021", "10.1126", "10.1073", "10.1029", "10.1144",
            "10.1515", "10.1108", "10.1139", "10.1061", "10.1146", "10.1128", "10.1103",
        ])
        or soup.select_one(
            "a[href*='/doi/suppl/'], a[href*='/suppl_file/'], "
            ".supplemental-material-container, .core-supplementary-material, "
            ".core-collateral, .article-section__supporting-information, "
            ".supInfoList, .supInfoBox, #supportingInfo, .article_supporting-info, "
            ".journal-article-supplementary-files, [data-widget-name*='supplement'], "
            "section[sec-type='supplementary-material'], .support-info, #support-information-section, "
            ".article-page-section.supplemental, section[data-magellan-destination='supplemental'], "
            "a.supplementary-download, [data-testid='supplementary-material-link']"
        ) is not None
    ):
        _extract_atypon_candidates(soup, effective_base, add_candidate, diag=diag)

    # Royal Society of Chemistry (RSC)
    if (
        "pubs.rsc.org" in combined_url or "rsc.org" in combined_url
        or soup.select_one(".ESIright_highlight_txt_red, .ESIright_highlight_txt_red_hidden, a[href*='/suppdata/']") is not None
    ):
        _extract_rsc_candidates(soup, effective_base, add_candidate, diag=diag)

    # Cambridge Core
    if (
        "cambridge.org" in combined_url
        or soup.select_one("a.access-supplementary, .doi-supplementary-material, .tab__supplementary") is not None
    ):
        _extract_cambridge_candidates(soup, effective_base, add_candidate, diag=diag)

    # Frontiers
    if (
        "frontiersin.org" in combined_url or "10.3389" in combined_url
        or soup.select_one("#supplementaryMaterial, #supplementaryMaterial-content, .Statement__Supplementarymaterial") is not None
    ):
        _extract_frontiers_candidates(soup, effective_base, add_candidate, diag=diag)

    # Copernicus / EGU
    if "copernicus.org" in combined_url or "10.5194" in combined_url:
        _extract_copernicus_candidates(soup, effective_base, add_candidate, diag=diag)

    # PeerJ
    if (
        "peerj.com" in combined_url or "10.7717" in combined_url
        or soup.select_one("#supp-materials, .article-supplementary, table.supplementary-files, a[href*='/download/supplement/']") is not None
    ):
        _extract_peerj_candidates(soup, effective_base, add_candidate, diag=diag)

    # MDPI
    if (
        "mdpi.com" in combined_url or "10.3390" in combined_url
        or soup.select_one("a[href*='/data/suppl/'], a.UD_Article_Supplementary, #supplementary-material-sec, .art-suppl, .html-supplementary") is not None
    ):
        _extract_mdpi_candidates(soup, effective_base, add_candidate, diag=diag)

    # OJS / J-STAGE / Chinese Scholarly Platforms (SciEngine / SciOpen / Magtech / RHHZ1 / RHHZ2)
    if (
        any(d in combined_url for d in [
            "jstage.jst.go.jp", "sciengine.com", "sciopen.com", "scichina.com",
            "geochina.cn", "dzxb.org", "ysxb.ac.cn", "cgsjournals.com", "earth-science.net",
            "chinageology.cgs.cn", "ygxb.ac.cn",
        ])
        or soup.select_one(
            ".supplementary_files, a.obj_galley_link_supplementary, "
            ".related-file-list, a[href*='downloadArticleFile.do'], "
            "a[href*='File/Download'], .suppl-sec, "
            "#Supplementary, a[href*='/fileup/'], "
            "[onclick*='exportSupplementary'], form[action*='exportSupplementary'], "
            ".detail-wrapper .icon-text-line"
        ) is not None
    ):
        _extract_ojs_jstage_cn_candidates(soup, effective_base, add_candidate, diag=diag)

    # Embedded JSON data structures (Next.js / Nuxt / Publisher JSON payloads)
    embedded_matched_any = False

    def _add_embedded_candidate(cand: Candidate):
        nonlocal embedded_matched_any
        before = len(candidates)
        add_candidate(cand)
        # Mark True if candidate was added OR deduplicated against an already-extracted candidate
        # (prevents false needs_browser=True when DOM extractors already found the JSON's files).
        if len(candidates) > before or any(
            _normalize_url_for_dedup(existing.url.split("#")[0].strip()) == _normalize_url_for_dedup((cand.url or "").split("#")[0].strip())
            for existing in candidates
        ):
            embedded_matched_any = True

    _extract_embedded_json_candidates(soup, effective_base, _add_embedded_candidate, diag=diag)

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
    article_text = extract_article_declaration_text(soup)
    diag.declared_count = detect_declared_supplement_count(article_text)
    is_inc, inc_reason = is_result_obviously_incomplete(
        candidates, soup, diag.declared_count, article_text=article_text
    )
    diag.is_incomplete = is_inc
    diag.incomplete_reason = inc_reason
    diag._soup = soup
    diag._article_text = article_text

    # ── H3: dynamic 'not loaded' signal ───────────────────────────────────
    # Supplementary heading/section present but nothing extracted, or inline
    # JSON (__NEXT_DATA__ / __INITIAL_STATE__ / ...) present but no URL was
    # parsed out of it: static parsing cannot settle the page, so a browser
    # render is needed. Callers should treat this as a must-render signal.
    embedded_found = embedded_matched_any or any(c.match_rule.startswith("embedded_") for c in candidates)
    matched_json_markers = [marker for marker in _INLINE_JSON_MARKERS if marker in html_content]
    has_inline_json = bool(matched_json_markers)
    # Publisher-specific SSR DOM supplementary sections (Elsevier, Frontiers, Cambridge)
    # already render the full attachment list in DOM while also embedding hydration state;
    # similarly, bare Nuxt SSR state (__NUXT__) on a full rendered page with no supplementary
    # signals must not trigger a false browser retry.
    dom_publisher_supp_complete = bool(
        candidates
        and not is_inc
        and any(c.section in _DOM_SUPP_SECTIONS for c in candidates)
        and "__NEXT_DATA__" not in matched_json_markers
        and "__INITIAL_STATE__" not in matched_json_markers
    )
    nuxt_only_rendered_page = (
        bool(matched_json_markers)
        and set(matched_json_markers).issubset({"__NUXT__", "__NUXT_DATA__"})
        and not _is_empty_spa_shell(soup, html_content)
        and not _page_has_supplementary_signal(soup)
    )
    diag.needs_browser = (
        (not candidates and (_page_has_supplementary_signal(soup) or _is_empty_spa_shell(soup, html_content)))
        or (has_inline_json and not embedded_found and not dom_publisher_supp_complete and not nuxt_only_rendered_page)
    )

    return diag


# ── Publisher-Specific Extractors ────────────────────────────────────────────

def _extract_elsevier_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Elsevier-specific asset, Cell Press CMS attachment, and MMC extraction."""
    seen_tags: Set[int] = set()
    for el in soup.find_all(["a", "button"], attrs={"data-component": True}):
        comp = el.get("data-component", "").lower()
        if "attachment" in comp or "mmc" in comp:
            url = extract_target_url_from_tag(el)
            if url:
                seen_tags.add(id(el))
                full_url = urllib.parse.urljoin(base_url, url)
                link_text = el.get_text(" ", strip=True)
                add_func(Candidate(
                    url=full_url,
                    filename=extract_filename_from_url(full_url, default_name=link_text),
                    link_text=link_text,
                    section="elsevier_component",
                    match_rule="elsevier_attachment_component",
                    source_tag=el.name,
                    extra_info={"attributes": _attrs_for_tag(el), "structural_supp": True},
                    covered_table_ids=_parent_direct_table_ids(el),
                ))

    for el in soup.select(
        "a[data-aa='supplementary-material'], a[href*='/cms/attachment/'], "
        ".article-sec-supplements a, .Appendices a[href*='mmc'], "
        ".Appendices a.download-link, .Appendices a.anchor"
    ):
        if id(el) in seen_tags:
            continue
        target = extract_target_url_from_tag(el)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        path_lower = urllib.parse.urlsplit(full_url).path.lower()
        if "/pdfft" in path_lower or is_excluded_url(full_url, base_url=base_url):
            continue
        link_text = el.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(el)
        if not (
            check_data_extension(full_url)
            or MMC_PATTERN.search(full_url)
            or "/cms/attachment/" in path_lower
            or path_lower.endswith(".pdf")
            or SUPP_KEYWORD_PATTERN.search(label)
        ):
            continue
        if _is_inline_figure_action(full_url, text=label):
            continue
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(el.get("download") or extract_filename_from_url(full_url, default_name=label)),
            link_text=label,
            section="elsevier_component",
            match_rule="elsevier_cell_supp_attachment",
            source_tag=el.name,
            extra_info={"attributes": _attrs_for_tag(el), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(el),
        ))


def _extract_springer_nature_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Springer, Nature, and EMBO supplementary & source data file extraction."""
    for a in soup.find_all("a", attrs={"data-track-action": re.compile(r"download", re.IGNORECASE)}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            url_lower = full_url.lower()
            link_text = a.get_text(" ", strip=True)
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
            is_supp_springer = bool(
                is_supplementary_container(a)
                or "/esm/" in url_lower
                or "/mediaobjects/" in url_lower
                or "moesm" in url_lower
                or "supp" in track_action
                or "source" in track_action
            )
            extra_a = {"attributes": _attrs_for_tag(a)}
            if is_supp_springer:
                extra_a["structural_supp"] = True
            add_func(Candidate(
                url=full_url,
                filename=extract_filename_from_url(full_url, default_name=link_text),
                link_text=link_text,
                section="springer_nature_download",
                match_rule="springer_data_track_download",
                extra_info=extra_a,
            ))

    # Nature supplementary file links (old & current c-article-supplementary DOM)
    for a in soup.find_all("a", attrs={"data-test": re.compile(r"^(?:supplementary-file-link|supp[-_]?(?:info[-_]?link|link))$", re.IGNORECASE)}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            link_text = a.get_text(" ", strip=True)
            attrs_a = _attrs_for_tag(a)
            if is_excluded_url(full_url, base_url=base_url, text=link_text, attrs=attrs_a):
                continue
            add_func(Candidate(
                url=full_url,
                filename=extract_filename_from_url(full_url, default_name=link_text),
                link_text=link_text,
                section="nature_supplementary",
                match_rule="nature_data_test_supp_link",
                source_tag=a.name,
                extra_info={"attributes": attrs_a, "structural_supp": True},
                covered_table_ids=_parent_direct_table_ids(a),
            ))

    # Springer / BMC supplementary items ([data-test="supp-item"] / view supplementary info)
    for a in soup.find_all("a", attrs={"data-track-action": re.compile(r"view\s+supplementary\s+info", re.IGNORECASE)}):
        href = a.get("href")
        if href:
            full_url = urllib.parse.urljoin(base_url, href)
            url_lower = full_url.lower()
            if "/content/pdf/" in url_lower and "/esm/" not in url_lower:
                continue
            link_text = a.get_text(" ", strip=True)
            add_func(Candidate(
                url=full_url,
                filename=extract_filename_from_url(full_url, default_name=link_text),
                link_text=link_text,
                section="springer_nature_download",
                match_rule="springer_view_supp_info",
                extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
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
                source_tag=a.name,
                extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
                covered_table_ids=_parent_direct_table_ids(a),
            ))

    # Nature Extended Data tables/files (skip Extended Data Figures / static images)
    for a in soup.select("a[data-test='extended-data-link'], #ExtendedData a, section[data-title='Extended Data'] a"):
        target = extract_target_url_from_tag(a)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        path_lower = urllib.parse.urlsplit(full_url).path.lower()
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if any(path_lower.endswith(img_ext) for img_ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
            continue
        if _is_inline_figure_action(full_url, text=label) or is_excluded_url(full_url, base_url=base_url, text=label):
            continue
        if not (
            check_data_extension(full_url)
            or path_lower.endswith(".pdf")
            or is_explicit_supp_table(label)
            or SUPP_KEYWORD_PATTERN.search(label)
        ):
            continue
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(a.get("download") or extract_filename_from_url(full_url, default_name=label)),
            link_text=label,
            section="nature_supplementary",
            match_rule="nature_extended_data_table",
            source_tag=a.name,
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(a),
        ))


def _extract_wiley_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """Wiley Online Library supplementary extraction."""
    for a in soup.find_all("a", href=re.compile(r"/action/downloadSupplement", re.IGNORECASE)):
        href = a.get("href")
        full_url = urllib.parse.urljoin(base_url, href)
        link_text = a.get_text(" ", strip=True)
        fname = extract_filename_from_url(full_url, default_name=link_text)
        add_func(Candidate(
            url=full_url,
            filename=fname,
            link_text=link_text,
            section="wiley_supplement",
            match_rule="wiley_download_supplement_action",
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
        ))


def _extract_plos_candidates(soup: BeautifulSoup, base_url: str, add_func):
    """PLOS (Public Library of Science) supplementary file extraction."""
    for a in soup.find_all("a", href=re.compile(r"/article/file\?id=", re.IGNORECASE)):
        href = a.get("href")
        full_url = urllib.parse.urljoin(base_url, href)
        full_lower = full_url.lower()
        # Exclude printable PDF and main article XML manuscript
        if "type=printable" in full_lower or "type=manuscript" in full_lower:
            continue
        link_text = a.get_text(" ", strip=True)
        fname = extract_filename_from_url(full_url, default_name=link_text)
        add_func(Candidate(
            url=full_url,
            filename=fname,
            link_text=link_text,
            section="plos_supplement",
            match_rule="plos_article_file_action",
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
        ))


def _attrs_for_tag(tag) -> Dict[str, str]:
    """Extract normalized descriptive attributes for coverage matching and diagnostics."""
    if tag is None or not hasattr(tag, "has_attr"):
        return {}
    return {
        k: tag[k]
        for k in ["aria-label", "title", "download", "data-filename", "data-title", "data-caption", "value"]
        if tag.has_attr(k) and isinstance(tag.get(k), str)
    }


def _sibling_or_container_label(tag, fallback: str = "") -> str:
    """Derive descriptive filename/caption text when a download icon link has empty text."""
    for attr in ("aria-label", "title", "data-filename", "data-title", "download"):
        val = tag.get(attr)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # Check immediate non-control siblings first so multi-link rows do not cross-contaminate
    for sib in (getattr(tag, "previous_sibling", None), getattr(tag, "next_sibling", None)):
        if sib is None:
            continue
        if isinstance(sib, str):
            st = sib.strip()
            if st and len(st) <= 180:
                return st
        elif hasattr(sib, "get_text") and getattr(sib, "name", None) not in ("a", "button", "input", "script", "style"):
            st = sib.get_text(" ", strip=True)
            if st and len(st) <= 180:
                return st
    parent = getattr(tag, "parent", None)
    for _ in range(2):
        if parent is None:
            break
        if hasattr(parent, "find_all"):
            controls = [
                c for c in parent.find_all(["a", "button", "input"], limit=6)
                if extract_target_url_from_tag(c)
            ]
            if len(controls) > 1:
                break
        txt = parent.get_text(" ", strip=True) if hasattr(parent, "get_text") else ""
        if txt and len(txt) <= 180:
            return txt
        parent = getattr(parent, "parent", None)
    return fallback


def _extract_silverchair_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """GeoScienceWorld / Silverchair / OUP / AIP / JAMA / Allen Press supplementary extraction."""
    container_selectors = (
        ".widget-article-supplementary-data, .widget-SupplementaryData, "
        ".widget-instance-ArticleSupplementaryData, .widget-ArticleDataSupplements, .dataSuppLink, "
        ".supplementary-data-wrapper, .data-supplement, .data-supplement-item, "
        ".supplementary-data, .supplementary-data-list, "
        ".supplement-item, #supplementary-data, [id*='supplementary-data'], "
        "[data-widgetname*='Supplement'], [data-supplement], section.supplementary-material, "
        ".figshare-wrapper, #articlefulltext_figshare, figshare-widget"
    )
    seen_tags = set()
    for sec in soup.select(container_selectors):
        is_figshare_sec = bool(
            sec.name == "figshare-widget"
            or "figshare" in str(sec.get("id") or "").lower()
            or any("figshare" in str(c).lower() for c in (sec.get("class") or []))
            or sec.find("figshare-widget") is not None
        )
        widget_title = ""
        if is_figshare_sec:
            t_el = sec.select_one("h1, h2, h3, h4, [class*='title'], [class*='header'] a, [class*='Heading']")
            if t_el:
                widget_title = re.sub(r"\s+", " ", t_el.get_text(" ", strip=True) or "").strip()
        for el in sec.find_all(["a", "button", "form"]):
            if id(el) in seen_tags:
                continue
            target = extract_target_url_from_tag(el)
            if not target:
                continue
            seen_tags.add(id(el))
            full_url = urllib.parse.urljoin(base_url, target)
            url_lower = full_url.lower()
            path_lower = urllib.parse.urlsplit(full_url).path.lower()
            if "/article-pdf/" in path_lower or "/citation/download" in path_lower:
                continue
            link_text = el.get_text(" ", strip=True)
            label = link_text or _sibling_or_container_label(el)
            if is_figshare_sec and widget_title and re.sub(r"\s+", " ", (label or "").strip().lower()) in {"", "download", "fig share", "open in figshare"}:
                label = f"{widget_title} ({link_text})" if link_text else widget_title
            if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
                continue
            # Ignore links to another article/citation DOI unless repo/component DOI or explicit supp context
            if "doi.org/10." in url_lower and not REPO_OR_COMPONENT_DOI_PATTERN.search(full_url) and not SUPP_KEYWORD_PATTERN.search(label):
                continue
            fname_default = None if (
                is_figshare_sec and re.sub(r"\s+", " ", (link_text or "").strip().lower()) in {"", "download", "fig share", "open in figshare"}
            ) else (link_text or label)
            fname = el.get("download") or extract_filename_from_url(full_url, default_name=fname_default)
            add_func(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=label,
                section="silverchair_supplement",
                match_rule="silverchair_supp_widget",
                source_tag=el.name,
                extra_info={"attributes": _attrs_for_tag(el), "structural_supp": True},
                covered_table_ids=_parent_direct_table_ids(el),
            ))

    for el in soup.find_all(["a", "button"]):
        if id(el) in seen_tags:
            continue
        target = extract_target_url_from_tag(el)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        url_lower = full_url.lower()
        url_kw_lower = _url_without_auth_query_tokens(full_url).lower()
        path_lower = urllib.parse.urlsplit(full_url).path.lower()
        if "/article-pdf/" in path_lower or "/citation/download" in path_lower:
            continue
        link_text = el.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(el)
        is_sc_cdn = "silverchair-cdn.com" in url_lower or "content_public" in path_lower
        is_sc_dl = (
            "/suppl/" in path_lower
            or "/article-supplement/" in path_lower
            or "/downloadfile" in path_lower
            or "/getdatasupplement" in path_lower
            or "content-type=data-supplement" in url_lower
            or el.has_attr("data-supplement-url")
        )
        is_gsw_repo = bool(REPO_OR_COMPONENT_DOI_PATTERN.search(full_url))
        if not (is_sc_cdn or is_sc_dl or is_gsw_repo):
            continue
        is_in_supp_container = is_supplementary_container(el)
        if is_sc_cdn and not (
            check_data_extension(full_url)
            or path_lower.endswith(".pdf")
            or SUPP_KEYWORD_PATTERN.search(url_kw_lower + " " + label)
            or is_in_supp_container
        ):
            continue
        if is_sc_dl and not (
            check_data_extension(full_url)
            or path_lower.endswith(".pdf")
            or SUPP_KEYWORD_PATTERN.search(url_kw_lower + " " + label)
            or is_explicit_supp_table(label)
            or is_in_supp_container
        ):
            continue
        is_structural_sc = bool(
            is_in_supp_container
            or "/article-supplement/" in path_lower
            or "/getdatasupplement" in path_lower
            or "content-type=data-supplement" in url_lower
            or el.has_attr("data-supplement-url")
        )
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag):
            continue
        if is_structural_sc:
            if _is_inline_figure_action(full_url, text=label):
                continue
        elif is_article_figure_url(full_url, text=label):
            continue
        fname = el.get("download") or extract_filename_from_url(full_url, default_name=label)
        sc_extra = {"attributes": _attrs_for_tag(el)}
        if is_structural_sc:
            sc_extra["structural_supp"] = True
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="silverchair_supplement",
            match_rule="silverchair_asset_link",
            source_tag=el.name,
            extra_info=sc_extra,
            covered_table_ids=_parent_direct_table_ids(el),
        ))


def _extract_atypon_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """Atypon Literatum (T&F, SAGE, ACS, AGU/Wiley, Science/AAAS, PNAS, Lyell/GSL, De Gruyter, Emerald) extraction."""
    # 1. Direct Atypon /doi/suppl/ and /suppl_file/ links
    for el in soup.find_all(["a", "button", "form"]):
        target = extract_target_url_from_tag(el)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        path_lower = urllib.parse.urlsplit(full_url).path.lower()
        if any(p in path_lower for p in ("/doi/epdf/", "/doi/pdf/", "/doi/pdfdirect/", "/doi/reader/")):
            continue
        if "/doi/suppl/" in path_lower or "/suppl_file/" in path_lower:
            if (
                "/doi/suppl/" in path_lower
                and "/suppl_file/" not in path_lower
                and not check_data_extension(full_url)
                and not path_lower.endswith(".pdf")
                and not re.search(r"(?:^|&)(?:file|attachment|download)=", urllib.parse.urlsplit(full_url).query.lower())
            ):
                continue
            link_text = el.get_text(" ", strip=True)
            label = link_text or _sibling_or_container_label(el)
            if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
                continue
            fname = el.get("download") or extract_filename_from_url(full_url, default_name=label)
            add_func(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=label,
                section="atypon_supplement",
                match_rule="atypon_suppl_endpoint",
                source_tag=el.name,
                extra_info={"attributes": _attrs_for_tag(el), "structural_supp": True},
                covered_table_ids=_parent_direct_table_ids(el),
            ))

    # 2. Atypon / Literatum supplementary containers and explicit testid/class links
    atypon_containers = (
        ".supplemental-material-container, .core-supplementary-material, "
        ".core-collateral, .supporting-information, .article-section__supporting-information, "
        ".supInfoList, .supInfoBox, #supportingInfo, .article_supporting-info, "
        "#supporting-information, #supplemental-material, #supplemental-information, "
        "[id*='supplemental-material'], .journal-article-supplementary-files, "
        "[data-widget-name*='supplement'], section[sec-type='supplementary-material'], "
        ".article-page-section.supplemental, section[data-magellan-destination='supplemental'], "
        ".support-info, #support-information-section, .supp-material, .suppl-item, "
        ".supplementary-file, [data-testid='supplementary-material']"
    )
    for sec in soup.select(atypon_containers):
        for el in sec.find_all(["a", "button"]):
            target = extract_target_url_from_tag(el)
            if not target:
                continue
            full_url = urllib.parse.urljoin(base_url, target)
            path_lower = urllib.parse.urlsplit(full_url).path.lower()
            if any(p in path_lower for p in ("/doi/epdf/", "/doi/pdf/", "/doi/pdfdirect/", "/doi/reader/")):
                continue
            link_text = el.get_text(" ", strip=True)
            label = link_text or _sibling_or_container_label(el)
            if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
                continue
            if not (
                check_data_extension(full_url)
                or path_lower.endswith(".pdf")
                or "/suppl" in path_lower
                or "/download" in path_lower
                or "/cms/asset/" in path_lower
                or REPO_OR_COMPONENT_DOI_PATTERN.search(full_url)
                or SUPP_KEYWORD_PATTERN.search(label)
                or el.has_attr("download")
            ):
                continue
            fname = el.get("download") or extract_filename_from_url(full_url, default_name=label)
            add_func(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=label,
                section="atypon_supplement",
                match_rule="atypon_container_link",
                source_tag=el.name,
                extra_info={"attributes": _attrs_for_tag(el), "structural_supp": True},
                covered_table_ids=_parent_direct_table_ids(el),
            ))

    for a in soup.select("[data-testid='supplementary-material-link'], a.supplementary-material-link, a.supplementary-download, a.suppl-link, a[data-supp-file]"):
        target = a.get("data-supp-file") or extract_target_url_from_tag(a)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        path_lower = urllib.parse.urlsplit(full_url).path.lower()
        if any(p in path_lower for p in ("/doi/epdf/", "/doi/pdf/", "/doi/pdfdirect/", "/doi/reader/")):
            continue
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="atypon_supplement",
            match_rule="atypon_explicit_supp_link",
            source_tag=a.name,
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(a),
        ))


def _extract_rsc_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """Royal Society of Chemistry (RSC) ESI supplementary file extraction."""
    for a in soup.select(".ESIright_highlight_txt_red a, .ESIright_highlight_txt_red_hidden a, #supplementary-information a, a[href*='/suppdata/']"):
        href = a.get("href")
        if not href:
            continue
        full_url = urllib.parse.urljoin(base_url, href)
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="rsc_supplement",
            match_rule="rsc_esi_link",
            source_tag=a.name,
            extra_info={"structural_supp": True},
        ))


def _extract_cambridge_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """Cambridge Core supplementary material extraction."""
    for a in soup.select(".supplementary-material a, .doi-supplementary-material a, .tab__supplementary a, a.access-supplementary"):
        href = a.get("href")
        if not href:
            continue
        full_url = urllib.parse.urljoin(base_url, href)
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        if not (
            check_data_extension(full_url)
            or urllib.parse.urlsplit(full_url).path.lower().endswith(".pdf")
            or "static.cambridge.org" in full_url.lower()
            or REPO_OR_COMPONENT_DOI_PATTERN.search(full_url)
            or SUPP_KEYWORD_PATTERN.search(label + " " + full_url)
            or a.has_attr("download")
        ):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="cambridge_supplement",
            match_rule="cambridge_supp_link",
            source_tag=a.name,
            extra_info={"structural_supp": True},
        ))


def _extract_frontiers_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """Frontiers rendered supplementary sidepanel & CDN file extraction."""
    selectors = (
        "#supplementaryMaterial a, #supplementaryMaterial-content a, "
        ".Statement__Supplementarymaterial a, "
        "a[href*='public-pages-files'][href*='/file/']"
    )
    for a in soup.select(selectors):
        href = a.get("href")
        if not href:
            continue
        full_url = urllib.parse.urljoin(base_url, href)
        url_lower = full_url.lower()
        if "/citation/" in url_lower:
            continue
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if label.strip().lower() in {"bibtex", "endnote", "reference manager", "simple text file"}:
            continue
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="frontiers_supplement",
            match_rule="frontiers_supp_panel_link",
            source_tag=a.name,
            extra_info={"structural_supp": True},
        ))


def _extract_copernicus_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """Copernicus / EGU (-supplement.pdf, data files, and component DOIs) extraction."""
    for a in soup.find_all("a", href=re.compile(r"-supplement(?:\.[a-z0-9]+|$)", re.IGNORECASE)):
        href = a.get("href") or ""
        full_url = urllib.parse.urljoin(base_url, href)
        link_text = a.get_text(" ", strip=True)
        if is_excluded_url(full_url, base_url=base_url, text=link_text, diag=diag) or _is_inline_figure_action(full_url, text=link_text):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=link_text)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=link_text,
            section="copernicus_supplement",
            match_rule="copernicus_supplement_link",
            source_tag=a.name,
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(a),
        ))


def _extract_peerj_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """PeerJ supplementary material containers and CloudFront /download/supplement/ links."""
    selectors = (
        "#supp-materials a, .supp-material a, .article-supplementary a, "
        "table.supplementary-files a, a[href*='/download/supplement/'], "
        "a[href*='dfzljdn9uc3pi.cloudfront.net']"
    )
    for a in soup.select(selectors):
        target = extract_target_url_from_tag(a)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        path_lower = urllib.parse.urlsplit(full_url).path.lower()
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if "dfzljdn9uc3pi.cloudfront.net" in full_url.lower() and not (
            check_data_extension(full_url) or path_lower.endswith(".pdf")
            or "/supplement/" in path_lower or SUPP_KEYWORD_PATTERN.search(label)
        ):
            continue
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="peerj_supplement",
            match_rule="peerj_supp_link",
            source_tag=a.name,
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(a),
        ))


def _extract_mdpi_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """MDPI supplementary containers (/s1, /data/suppl/, mdpi-res.com) extraction."""
    selectors = (
        "#supplementary-material-sec a, .supplementary-file a, .art-suppl a, "
        ".html-supplementary a, a[href*='/data/suppl/'], a.UD_Article_Supplementary, "
        "a[href*='mdpi-res.com'][href*='supp']"
    )
    for a in soup.select(selectors):
        target = extract_target_url_from_tag(a)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        path_lower = urllib.parse.urlsplit(full_url).path.lower()
        if path_lower.endswith(("/pdf", "/pdf/", "/xml", "/xml/")):
            continue
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="mdpi_supplement",
            match_rule="mdpi_supp_link",
            source_tag=a.name,
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(a),
        ))


def _extract_ojs_jstage_cn_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
    """OJS, J-STAGE, and Chinese scholarly platform (SciEngine/SciOpen/Magtech) extraction."""
    # OJS supplementary galleys
    for a in soup.select(".supplementary_files a, a.obj_galley_link_supplementary"):
        target = extract_target_url_from_tag(a)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="ojs_supplement",
            match_rule="ojs_supp_galley",
            source_tag=a.name,
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(a),
        ))

    # J-STAGE supplementary lists
    for a in soup.select(".related-file-list a, .supplementary-data a, #supplementary a"):
        target = extract_target_url_from_tag(a)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="jstage_supplement",
            match_rule="jstage_supp_link",
            source_tag=a.name,
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(a),
        ))

    # Chinese platforms (SciEngine / SciOpen / Magtech / RHHZ1 / RHHZ2)
    for a in soup.select(
        ".suppl-sec a, .supplementary-materials a, #Supplementary a, "
        "a[href*='File/Download'], a[href*='downloadFile'], a[href*='downloadArticleFile.do'], "
        "a[href*='/fileup/']"
    ):
        target = extract_target_url_from_tag(a)
        if not target:
            continue
        full_url = urllib.parse.urljoin(base_url, target)
        url_lower = full_url.lower()
        link_text = a.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(a)
        if "downloadarticlefile.do" in url_lower:
            m_type = re.search(r"[?&]attachtype=([^&]+)", full_url, re.IGNORECASE)
            if m_type and m_type.group(1).upper() not in ("SUPPLEMENT", "SUPPLEMENTARY", "ATTACH", "APPENDIX", "DATA"):
                continue
            if not m_type and not (
                SUPP_KEYWORD_PATTERN.search(label) or is_explicit_supp_table(label) or is_supplementary_container(a)
            ):
                continue
        if is_excluded_url(full_url, base_url=base_url, text=label, diag=diag) or _is_inline_figure_action(full_url, text=label):
            continue
        if not (
            check_data_extension(full_url)
            or check_data_extension(label)
            or SUPP_KEYWORD_PATTERN.search(label + " " + url_lower)
            or is_explicit_supp_table(label)
            or is_supplementary_container(a)
            or a.find_parent(id="Supplementary") is not None
        ):
            continue
        fname = a.get("download") or extract_filename_from_url(full_url, default_name=label)
        add_func(Candidate(
            url=full_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="chinese_platform_supplement",
            match_rule="chinese_platform_supp_link",
            source_tag=a.name,
            extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
            covered_table_ids=_parent_direct_table_ids(a),
        ))

    # RHHZ2 platforms (chinageology.cgs.cn, ygxb.ac.cn): onclick="exportSupplementary(id)" + hidden form POST
    export_form = soup.select_one("form[action*='exportSupplementary']")
    if export_form and export_form.get("action"):
        action_url = urllib.parse.urljoin(base_url, export_form.get("action"))
    else:
        bp_inputs = (
            soup.find_all("input", attrs={"id": re.compile(r"basePath", re.I)})
            + soup.find_all("input", attrs={"name": re.compile(r"basePath", re.I)})
        )
        bp_vals = []
        for bp_inp in bp_inputs:
            v = str(bp_inp.get("value") or "").strip()
            if v and v not in bp_vals:
                bp_vals.append(v)
        parsed_b = urllib.parse.urlsplit(base_url)
        article_prefix = (
            parsed_b.path.split("/article/", 1)[0].rstrip("/") + "/"
            if "/article/" in parsed_b.path
            else None
        )
        chosen_bp = ""
        if article_prefix and any(v.rstrip("/") + "/" == article_prefix for v in bp_vals):
            chosen_bp = article_prefix
        elif bp_vals:
            non_archive_bps = [
                v for v in bp_vals
                if not re.match(r"^/(?:file|upload|static|pdf|html)[a-z0-9_-]*/journal/article/", v, re.IGNORECASE)
            ]
            chosen_bp = non_archive_bps[-1] if non_archive_bps else bp_vals[-1]
        if chosen_bp:
            action_url = urllib.parse.urljoin(urllib.parse.urljoin(base_url, chosen_bp.rstrip("/") + "/"), "article/exportSupplementary")
        elif article_prefix:
            prefix_path = article_prefix.rstrip("/") + "/article/exportSupplementary"
            action_url = urllib.parse.urlunsplit((parsed_b.scheme, parsed_b.netloc, prefix_path, "", ""))
        else:
            action_url = urllib.parse.urljoin(base_url, "/article/exportSupplementary")
    page_type_val = ""
    if export_form is not None:
        pt_inp = export_form.find("input", attrs={"name": "pageType"})
        if pt_inp and pt_inp.get("value") is not None:
            page_type_val = str(pt_inp.get("value"))
    check_base = re.sub(r"exportSupplementary/?$", "checkSupplement", action_url, flags=re.IGNORECASE)
    for el in soup.select("[onclick*='exportSupplementary']"):
        onclick = str(el.get("onclick") or "")
        m_id = re.search(r"exportSupplementary\s*\(\s*['\"]?([^'\")\s,]+)['\"]?", onclick)
        if not m_id:
            continue
        supp_id = m_id.group(1).strip()
        if not supp_id:
            continue
        link_text = el.get_text(" ", strip=True)
        label = link_text or _sibling_or_container_label(el) or f"supplement_{supp_id}"
        post_url = f"{action_url}?id={urllib.parse.quote(supp_id)}"
        check_url = f"{check_base}?id={urllib.parse.quote(supp_id)}"
        fname = extract_filename_from_url(post_url, default_name=label)
        post_data = {"id": supp_id}
        if page_type_val:
            post_data["pageType"] = page_type_val
        add_func(Candidate(
            url=post_url,
            filename=sanitize_filename(fname),
            link_text=label,
            section="chinese_platform_supplement",
            match_rule="rhhz2_export_supplementary",
            source_tag=el.name,
            extra_info={
                "attributes": _attrs_for_tag(el),
                "http_method": "POST",
                "post_url": action_url,
                "post_data": post_data,
                "check_url": check_url,
                "structural_supp": True,
            },
            covered_table_ids=_parent_direct_table_ids(el),
        ))

    # SciEngine rendered SPA attachments (<div class="detail-wrapper">...<div class="icon-text-line"><div class="text">...</div></div>)
    for wrapper in soup.select("div.detail-wrapper"):
        title_el = wrapper.select_one(".title")
        title_txt = title_el.get_text(" ", strip=True) if title_el else ""
        if title_txt and not re.search(r"(?:附件|补充|附录|supplement|appendix|supporting|data)", title_txt, re.IGNORECASE):
            continue
        for idx, line in enumerate(wrapper.select("div.icon-text-line"), start=1):
            if line.find("a", href=True):
                continue
            txt_el = line.select_one(".text")
            label = (txt_el.get_text(" ", strip=True) if txt_el else line.get_text(" ", strip=True)) or ""
            if not label:
                continue
            selector = str(line.get("data-journal-download-selector") or f"div.detail-wrapper div.icon-text-line:nth-of-type({idx})")
            api_hint_url = f"{base_url.rstrip('/')}?sciengine_attachment={urllib.parse.quote(label)}"
            add_func(Candidate(
                url=api_hint_url,
                filename=sanitize_filename(label),
                link_text=label,
                section="chinese_platform_supplement",
                match_rule="sciengine_rendered_attachment",
                source_tag=line.name,
                extra_info={
                    "attributes": _attrs_for_tag(line),
                    "browser_selector": selector,
                    "sciengine_attachment": True,
                    "structural_supp": True,
                },
                covered_table_ids=_parent_direct_table_ids(line),
            ))


def _is_plausible_feat_suppl_link(url: str, link_text: str, filename: str) -> bool:
    """True when a PMC ``click_feat_suppl`` link actually points at
    supplementary content (P1 round-3 minor (b)).

    PMC puts ``data-ga-action="click_feat_suppl"`` on *every* external
    link (``usa-link--external``) -- verified live on PMC12434933
    (round-3 #50, a Heliyon corrigendum with no supplements): the
    article's own DOI link *and* the Twingate blog glossary reference
    link both carry it, so the bare attribute is not a supplement
    signal at all. A link only counts when its URL, link text or
    filename carries supplementary signal: a PMC /bin/ attachment, a
    data extension, a supplementary keyword / table filename / MMC id,
    or a repository/component DOI. Anything else -- a blog page, a DOI
    link, a glossary entry -- is dropped by the caller.
    """
    parsed = urllib.parse.urlparse(url or "")
    path = (parsed.path or "").lower()
    if "/bin/" in path:
        return True
    if check_data_extension(url or "") or check_data_extension(filename or ""):
        return True
    for value in (url or "", link_text or "", filename or ""):
        if not value:
            continue
        if (
            SUPP_KEYWORD_PATTERN.search(value)
            or TABLE_FILENAME_PATTERN.search(value)
            or MMC_PATTERN.search(value)
            or REPO_OR_COMPONENT_DOI_PATTERN.search(value)
        ):
            return True
    return False


def _extract_pmc_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
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
            if is_excluded_url(full_url, base_url=base_url, text=link_text, diag=diag) or _is_inline_figure_action(full_url, text=link_text):
                continue
            fname = a.get("download") or extract_filename_from_url(full_url, default_name=link_text)
            add_func(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=link_text,
                section="pmc_supplementary_section",
                match_rule="pmc_supp_section_link",
                source_tag=a.name,
                extra_info={"attributes": _attrs_for_tag(a), "structural_supp": True},
                covered_table_ids=_parent_direct_table_ids(a),
            ))

    # 2. Direct binary links (/bin/ or click_feat_suppl)
    for a in soup.find_all("a", href=re.compile(r"/bin/", re.IGNORECASE)):
        href = a.get("href")
        link_text = a.get_text(separator=" ", strip=True)
        full_url = urllib.parse.urljoin(base_url, href)
        if is_excluded_url(full_url, base_url=base_url, text=link_text, diag=diag) or is_article_figure_url(full_url, text=link_text):
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
            if is_excluded_url(full_url, base_url=base_url, text=link_text, diag=diag) or is_article_figure_url(full_url, text=link_text):
                continue
            fname = a.get("download") or extract_filename_from_url(full_url, default_name=link_text)
            # P1 round-3 minor (b): PMC stamps click_feat_suppl on every
            # external link (DOI links, reference links, blog pages), so
            # the attribute alone mints false candidates (#50 Twingate).
            # Only links that actually point at supplementary content
            # become candidates; the rest are skipped, not excluded.
            if not _is_plausible_feat_suppl_link(full_url, link_text, fname):
                continue
            add_func(Candidate(
                url=full_url,
                filename=sanitize_filename(fname),
                link_text=link_text,
                section="pmc_featured_suppl",
                match_rule="pmc_feat_suppl_link",
                source_tag=a.name,
            ))


_SHORT_AMBIGUOUS_EXTS = {".r", ".m", ".mat", ".fa", ".gb", ".nb", ".py", ".db", ".gz", ".xz"}


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


_XML_SUPP_MARKER_RE = re.compile(
    r"(?:mmc[-_]?\d+|table[-_ ]?s?\d+|\bsuppl(?:ement(?:ary|al)?)?\b|source[-_]?data|附表|附录|补充)",
    re.IGNORECASE,
)


def _decode_nuxt_data_payload(payload, max_depth: int = 12, max_nodes: int = 40000):
    """Decode Nuxt 3 devalue flat-array hydration payloads into nested Python objects."""
    if not isinstance(payload, list) or not payload:
        return payload
    budget = [max_nodes]
    memo: Dict[int, Any] = {}

    def _resolve(idx, depth: int, active: Set[int]):
        if depth > max_depth or budget[0] <= 0 or not isinstance(idx, int) or idx < 0 or idx >= len(payload):
            return None
        if idx in memo:
            return memo[idx]
        if idx in active:
            return None
        budget[0] -= 1
        val = payload[idx]
        if isinstance(val, dict):
            active.add(idx)
            out = {k: (_resolve(v, depth + 1, active) if isinstance(v, int) else v) for k, v in val.items()}
            active.remove(idx)
            memo[idx] = out
            return out
        if isinstance(val, list):
            if (
                len(val) == 2
                and isinstance(val[0], str)
                and val[0] in ("ShallowReactive", "Reactive", "Ref", "ShallowRef", "Set")
                and isinstance(val[1], int)
            ):
                active.add(idx)
                res = _resolve(val[1], depth + 1, active)
                active.remove(idx)
                memo[idx] = res
                return res
            active.add(idx)
            out_list = [(_resolve(el, depth + 1, active) if isinstance(el, int) else el) for el in val[:200]]
            active.remove(idx)
            memo[idx] = out_list
            return out_list
        memo[idx] = val
        return val

    return _resolve(0, 0, set())


def _walk_json_for_supp_objects(node, base_url: str, add_func, consumed_names: Optional[Set[str]] = None, diag=None, _parent_key: str = ""):
    """Recursively inspect parsed JSON objects for extensionless supplementary/table download entries."""
    if isinstance(node, list):
        for item in node:
            _walk_json_for_supp_objects(item, base_url, add_func, consumed_names, diag=diag, _parent_key=_parent_key)
        return
    if not isinstance(node, dict):
        return

    url_keys = (
        "downloadUrl", "download_url", "download-url",
        "fileUrl", "file_url", "assetUrl", "asset_url",
        "contentUrl", "suppUrl", "supplementaryUrl", "supplementary_url",
        "suppFileUrl", "attachmentUrl", "attachment_url",
        "mediaPath", "media_path", "filePath", "file_path",
        "dataUrl", "data_url", "suppFile", "mediaUrl", "media_url", "supplementUrl",
        "url", "href", "uri",
    )
    name_keys = (
        "fileName", "filename", "file_name", "file-name",
        "attachment-eid", "name", "title", "captionTitle", "label", "caption", "description",
    )
    type_keys = ("mimeType", "mime_type", "contentType", "content_type", "type", "documentType", "assetType", "mediaType", "category", "section")

    raw_url = None
    matched_url_key = ""
    for uk in url_keys:
        v = node.get(uk)
        if isinstance(v, str) and v.strip() and not v.strip().startswith(("#", "javascript:", "mailto:")):
            raw_url = v.strip()
            matched_url_key = uk
            break

    # ScienceDirect __PRELOADED_STATE__ attachments may have attachment-eid ("1-s2.0-...-mmc1.xlsx")
    # alongside a parent /pdfft main-PDF URL or no url key at all.
    eid_val = node.get("attachment-eid")
    synthesized_eid_url = None
    if isinstance(eid_val, str) and re.match(r"^(?:1-s2\.0-\w+-)?mmc\d+\.\w+$", eid_val.strip(), re.IGNORECASE):
        synthesized_eid_url = f"https://ars.els-cdn.com/content/image/{eid_val.strip()}"
        if not raw_url:
            raw_url = synthesized_eid_url
            matched_url_key = "attachment-eid"

    if raw_url:
        name_vals = [str(node[nk]).strip() for nk in name_keys if isinstance(node.get(nk), str) and str(node[nk]).strip()]
        type_vals = [str(node[tk]).strip() for tk in type_keys if isinstance(node.get(tk), str) and str(node[tk]).strip()]
        combined_meta = " ".join(name_vals + type_vals + ([_parent_key] if _parent_key else []))
        best_name = name_vals[0] if name_vals else ""

        if consumed_names is not None:
            for nv in name_vals:
                if nv != raw_url and "/" not in nv:
                    consumed_names.add(nv)

        has_data_name = any(check_data_extension(nv) for nv in name_vals)
        is_media_key = matched_url_key in ("mediaPath", "media_path", "suppUrl", "supplementaryUrl", "supplementary_url", "suppFileUrl", "attachmentUrl", "attachment_url", "attachment-eid", "supplementUrl")
        has_supp_meta = bool(
            has_data_name
            or is_explicit_supp_table(combined_meta)
            or TABLE_FILENAME_PATTERN.search(combined_meta)
            or SUPP_KEYWORD_PATTERN.search(combined_meta)
            or any(m in combined_meta.lower() for m in ["spreadsheet", "excel", "csv", "zip", "supplementary", "supplement", "multimedia", "attachment", "source data", "source_data"])
            or (is_media_key and ("multimedia" in _parent_key.lower() or "supp" in _parent_key.lower() or check_data_extension(raw_url) or raw_url.lower().endswith(".pdf")))
        )
        if has_supp_meta and _is_plausible_json_url_or_path(raw_url):
            cand_url = urllib.parse.urljoin(base_url, raw_url)
            cand_split = urllib.parse.urlsplit(cand_url)
            cand_path = cand_split.path.lower()
            is_section_frag = bool(cand_split.fragment and _ARTICLE_SECTION_FRAGMENT_RE.match(cand_split.fragment.strip())) and not check_data_extension(cand_path) and not cand_path.endswith(".pdf")
            is_main_pdf = (
                ("/content/pdf/" in cand_path and "/esm/" not in cand_path)
                or ("/stamp/" in cand_path and "multimedia" not in combined_meta.lower() and "supp" not in combined_meta.lower())
                or "/pdfft" in cand_path
                or "/doi/pdf/" in cand_path
                or "/doi/pdfdirect/" in cand_path
                or "/doi/epdf/" in cand_path
                or "/doi/reader/" in cand_path
            )
            if is_main_pdf and synthesized_eid_url:
                cand_url = synthesized_eid_url
                cand_split = urllib.parse.urlsplit(cand_url)
                cand_path = cand_split.path.lower()
                is_main_pdf = False
            if not is_section_frag and not is_main_pdf and not any(cand_path.endswith(ext) for ext in [".js", ".css", ".map", ".svg", ".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf"]):
                fname_only = " ".join(
                    str(node.get(k) or "").strip()
                    for k in ("fileName", "filename", "file_name", "file-name")
                    if isinstance(node.get(k), str)
                )
                is_xml_target = (
                    cand_path.endswith(".xml")
                    or cand_path.endswith("/xml")
                    or fname_only.lower().endswith(".xml")
                    or any("xml" in tv.lower() for tv in type_vals)
                )
                if not (is_xml_target and not _XML_SUPP_MARKER_RE.search(f"{cand_url} {fname_only}")):
                    if not any(kw in cand_path for kw in ["table-of-contents", "table_of_contents", "/support", "customer-support", "/toc"]):
                        if not is_excluded_url(cand_url, base_url=base_url, text=best_name, diag=diag) and not is_article_figure_url(cand_url, text=best_name):
                            fname = extract_filename_from_url(cand_url, default_name=best_name)
                            add_func(Candidate(
                                url=cand_url,
                                filename=sanitize_filename(fname),
                                link_text=(best_name or "embedded_json_link")[:80],
                                section="embedded_json",
                                match_rule="embedded_script_json_object",
                                source_tag="script",
                            ))

    for k, val in node.items():
        if isinstance(val, (dict, list)):
            _walk_json_for_supp_objects(val, base_url, add_func, consumed_names, diag=diag, _parent_key=str(k))


def _extract_embedded_json_candidates(soup: BeautifulSoup, base_url: str, add_func, diag=None):
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
        is_nuxt_data_script = (script.get("id") or "") == "__NUXT_DATA__"

        if any(kw in content_unescaped.lower() for kw in [
            "mmc", "supplement", "supp", "table", "dataset", "attached", "attachment",
            "asset", "multimedia", "mediapath", "media_path", "additional",
            "附表", "附录", "source data", "source_data", "sourcedata", "downloadurl", "fileurl",
        ]):
            consumed_names: Set[str] = set()
            # 1. Structured JSON object walk (catches extensionless downloadUrl paired with fileName/title)
            str_stripped = content.strip()
            if str_stripped.startswith(("{", "[")):
                try:
                    parsed_json = json.loads(str_stripped)
                    if is_nuxt_data_script and isinstance(parsed_json, list):
                        parsed_json = _decode_nuxt_data_payload(parsed_json)
                    _walk_json_for_supp_objects(parsed_json, base_url, add_func, consumed_names, diag=diag)
                except Exception:
                    pass
            else:
                decoder = json.JSONDecoder()
                for assign_m in re.finditer(r'(?:=|\bJSON\.parse\s*\()\s*([\[{])', content_unescaped):
                    start_idx = assign_m.start(1)
                    try:
                        parsed_sub, _ = decoder.raw_decode(content_unescaped, start_idx)
                        _walk_json_for_supp_objects(parsed_sub, base_url, add_func, consumed_names, diag=diag)
                    except Exception:
                        pass
            # Also scan inline JS object literals containing downloadUrl/fileUrl + fileName/title
            for obj_m in re.finditer(r'\{[^{}]{10,1000}\}', content_unescaped):
                try:
                    obj_data = json.loads(obj_m.group(0))
                    if isinstance(obj_data, dict):
                        _walk_json_for_supp_objects(obj_data, base_url, add_func, consumed_names, diag=diag)
                except Exception:
                    pass

            # 2. Regex match for direct file URLs/paths
            base_doi_match = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>?#]+)", base_url or "", re.IGNORECASE)
            base_doi_norm = base_doi_match.group(1).rstrip("/").lower() if base_doi_match else ""
            for m in pattern.finditer(content_unescaped):
                cand_raw = m.group(1).strip()
                if cand_raw in consumed_names:
                    continue
                if is_nuxt_data_script and "/" not in cand_raw and "?" not in cand_raw:
                    # Inside __NUXT_DATA__ JATS ASTs, bare filenames like "Table1.xlsx" are
                    # XML metadata labels (@_xlink:href), not resolvable relative URLs.
                    continue
                if not _is_plausible_json_url_or_path(cand_raw):
                    continue
                cand_url = urllib.parse.urljoin(base_url, cand_raw)
                cand_split = urllib.parse.urlsplit(cand_url)
                cand_path = cand_split.path.lower()
                if cand_split.fragment and _ARTICLE_SECTION_FRAGMENT_RE.match(cand_split.fragment.strip()) and not check_data_extension(cand_path) and not cand_path.endswith(".pdf"):
                    continue
                # Exclude frontend static web assets
                if any(cand_path.endswith(ext) for ext in [
                    ".js", ".css", ".map", ".svg", ".json", ".html", ".htm",
                    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf"
                ]):
                    continue
                # Exclude navigation/UI routes
                if any(kw in cand_path for kw in ["table-of-contents", "table_of_contents", "/support", "customer-support", "/toc"]):
                    continue
                # Cross-article DOI guard: if base_url identifies an article DOI (e.g. AJS /doi/10.1086/...)
                # and the embedded script URL points to a different non-repository DOI, skip unrelated article promos.
                if base_doi_norm and not REPO_OR_COMPONENT_DOI_PATTERN.search(cand_url):
                    cand_doi_m = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>?#]+)", cand_url, re.IGNORECASE)
                    if cand_doi_m:
                        cand_doi_norm = cand_doi_m.group(1).rstrip("/").lower()
                        if cand_doi_norm != base_doi_norm and not cand_doi_norm.startswith(base_doi_norm + "."):
                            continue
                # Require local supplementary/table/data context near Pattern 1 regex matches when the URL/filename
                # itself carries no supplementary/mmc/table indicator (guards against unrelated .zip promo assets like plutons.zip).
                cand_meta = f"{cand_raw} {cand_url}".lower()
                if not (
                    MMC_PATTERN.search(cand_meta)
                    or TABLE_FILENAME_PATTERN.search(cand_meta)
                    or SUPP_KEYWORD_PATTERN.search(cand_meta)
                    or REPO_OR_COMPONENT_DOI_PATTERN.search(cand_meta)
                ):
                    local_ctx = content_unescaped[max(0, m.start() - 220) : min(len(content_unescaped), m.end() + 220)].lower()
                    if not any(kw in local_ctx for kw in (
                        "supp", "mmc", "esm", "table", "appendix", "source_data", "sourcedata",
                        "source-data", "additional", "attached", "attachment", "data_sheet", "datasheet",
                        "附表", "附录", "补充", "downloadurl", "fileurl", "mediapath", "media_path",
                    )):
                        continue
                # Article full-text JATS XML URLs (e.g. minerals-15-00814.xml) live in LD-JSON/script blocks;
                # only extract .xml from <script> when URL/path carries explicit supplementary markers.
                if (cand_path.endswith(".xml") or cand_path.endswith("/xml")) and not _XML_SUPP_MARKER_RE.search(cand_raw):
                    continue
                if not is_excluded_url(cand_url, base_url=base_url, diag=diag) and not is_article_figure_url(cand_url):
                    add_func(Candidate(
                        url=cand_url,
                        filename=extract_filename_from_url(cand_url),
                        link_text="embedded_json_link",
                        section="embedded_json",
                        match_rule="embedded_script_json_url",
                        source_tag="script",
                    ))


# ── Completeness & Incomplete Result Detection (Issue #4) ────────────────────

def _strip_nonvisible_html(html_content: str) -> str:
    """Strip HTML comments and <script>/<style>/<noscript>/<svg> blocks (including truncated trailing tags)."""
    if not html_content:
        return ""
    cleaned = re.sub(r"<!--.*?-->", " ", html_content, flags=re.DOTALL)
    cleaned = re.sub(
        r"<(script|style|noscript|svg)\b[^>]*>.*?</\1>",
        " ",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Also strip unclosed trailing non-visible blocks in truncated 64KB chunks
    cleaned = re.sub(r"<(script|style|noscript|svg)\b[^>]*>.*$", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
    return cleaned


strip_nonvisible_html = _strip_nonvisible_html


_EXCLUDED_DECL_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "aside"}
_DECL_SITE_CHROME_RE = re.compile(
    r"(?:^|[\s_-])(?:site[-_]?header|navbar|nav[-_]?bar|top[-_]?nav|menu|breadcrumb|cookie[-_]?banner)(?:$|[\s_-])",
    re.IGNORECASE,
)
_DECL_REF_SIDEBAR_RE = re.compile(
    r"(?:^|[\s_-])(?:references?|bibliography|ref[-_]?list|citation[-_]?list|cited[-_]?by|sidebar|related[-_ ]?articles?|recommended[-_ ]?articles?|similar[-_ ]?articles?)(?:$|[\s_-])",
    re.IGNORECASE,
)
_DECL_SUPP_KEEP_RE = re.compile(
    r"supplement|suppl|supporting|additional|appendix|extended[-_ ]?data|source[-_ ]?data|esm|附表|附录|补充",
    re.IGNORECASE,
)


def extract_article_declaration_text(soup) -> str:
    """Extract visible text from a parsed BeautifulSoup tree while skipping
    HTML comments, scripts/styles/nav/footer/sidebars, and reference lists so
    cited tables from other papers do not pollute expected supplementary IDs.
    """
    chunks: List[str] = []

    def _walk(node):
        name = getattr(node, "name", None)
        if name is None:
            # Skip Comment / Doctype / ProcessingInstruction subclasses of NavigableString
            if type(node) is NavigableString:
                text = str(node).strip()
                if text:
                    chunks.append(text)
            return
        if name in _EXCLUDED_DECL_TAGS:
            return
        attrs_str = ""
        if getattr(node, "attrs", None):
            el_id = node.get("id") or ""
            el_cls = node.get("class") or []
            if isinstance(el_cls, (list, tuple)):
                el_cls = " ".join(str(x) for x in el_cls)
            attrs_str = f"{el_id} {el_cls}".strip()
        if name == "header" and attrs_str and _DECL_SITE_CHROME_RE.search(attrs_str) and not _DECL_SUPP_KEEP_RE.search(attrs_str):
            return
        if attrs_str and _DECL_REF_SIDEBAR_RE.search(attrs_str) and not _DECL_SUPP_KEEP_RE.search(attrs_str):
            return
        for child in node.children:
            _walk(child)

    _walk(soup)
    raw = " ".join(chunks)
    if not raw.strip():
        return soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", raw).strip()


def _clean_text_for_declarations(html_content: str) -> str:
    """Strip <script>/<style> blocks and tags so declarations are matched on visible text."""
    cleaned = _strip_nonvisible_html(html_content)
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

    P1-A: when a page carries several file-count statements that disagree,
    the FIRST statement in document order wins -- it is the article's own
    declaration. Taking the largest (the old max()) let a restatement or a
    sidebar count inflate the declaration, and mixed the file-count reading
    with the table-range reading; the two readings are counted independently.
    """
    if not html_content:
        return None
    cleaned = _clean_text_for_declarations(html_content)
    found_counts: List[tuple] = []  # (position, value), earliest wins

    # Pattern 1: 'N Supplementary Files' / 'N Supplementary Materials' (never 'N Supplementary Tables')
    for m in re.finditer(r'\b(\d{1,2})\s+(?:supplementary|supplemental|supporting|additional)\s+(?:files?|datasets?|materials?|items?)\b', cleaned, re.IGNORECASE):
        try:
            val = int(m.group(1))
            if 1 <= val <= 99:
                found_counts.append((m.start(), val))
        except ValueError:
            pass

    # Pattern 1b: 'N attachments' / 'Total N files'
    for m in re.finditer(r'\b(\d{1,2})\s+attachments?\b|\btotal\s+(\d{1,2})\s+files?\b', cleaned, re.IGNORECASE):
        try:
            val = int(m.group(1) or m.group(2))
            if 1 <= val <= 99:
                found_counts.append((m.start(), val))
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
                found_counts.append((m.start(), val))
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
            found_counts.append((m.start(), c_num))

    return min(found_counts)[1] if found_counts else None


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
    "silverchair_supplement",
    "atypon_supplement",
    "rsc_supplement",
    "cambridge_supplement",
    "frontiers_supplement",
    "copernicus_supplement",
    "peerj_supplement",
    "mdpi_supplement",
    "ojs_supplement",
    "jstage_supplement",
    "chinese_platform_supplement",
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
    r"(?:(?P<pw>(?:electronic\s+supplementary\s+material|supplementary|supplemental|supporting|additional|extended\s+data|appendix|esm|suppl?\.?|si)"
    r"(?!\w)(?:[-_.\s]+(?:information|info|materials?|data|files?))?(?:[-_.\s]+(?:table(?:s(?![-_.]?\d))?|tab(?:s(?![-_.]?\d))?\.?|tbl(?:s(?![-_.]?\d))?\.?))?"
    r"|附表|附录|table(?:s(?![-_.]?\d))?|tab(?:s(?![-_.]?\d))?\.?|tbl(?:s(?![-_.]?\d))?\.?)[-_.\s]*)?"
    r"(?:(?P<letter>[SAE])[-_.\s]*)?(?P<num>\d+(?:\.\d+)*|[一二两三四五六七八九十]+)"
    r"(?P<post>(?:\s+(?:tables?|tabs?\.?|tbls?\.?|datasets?|data|files?|表格|数据|文件))(?!\w)(?!\s*(?:[SAE][-_.\s]*)?(?:\d|[一二两三四五六七八九十])))?",
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
    """Namespace for one token: an explicit S/A/E letter wins, else the prefix word decides.

    P1FIX round-3 Cause A(c): a supplementary/supporting/additional prefix
    only names a table when it carries the word "table" -- "Supplementary
    Data 1", "Supplementary Files 2" and the like are data files, not
    tables, and must not mint S ids. "Appendix ..." and "Extended Data ..."
    keep their A/E namespaces: an appendix or extended-data number is a
    supplementary item number, and the bare forms ("Appendix 1-4",
    "Extended Data 1-3") are established legitimate inputs.
    """
    if letter:
        return letter.upper()
    if pw:
        low = pw.lower()
        if "appendix" in low or "附录" in pw:
            return "A"
        if "extended" in low:
            return "E"
        # A "supplement(ary|al)/supporting/additional" prefix names a table
        # only when it also carries the word "table" -- "Supplementary
        # Data 1" is a data file, not a table. A bare "Tables N" prefix,
        # on the other hand, is a main-text table, not a supplementary
        # declaration (H5/P1: "Tables 1 to 3" declares nothing), so it
        # assigns no namespace. "附表" is inherently a supplementary
        # table and always maps to S.
        if "附表" in pw:
            return "S"
        if re.search(r"\b(?:tables?|tabs?\.?|tbls?\.?)\b", low) and (
            any(w in low for w in ("supplement", "supporting", "additional", "supp", "esm"))
            or re.search(r"\bsi\b", low)
        ):
            return "S"
        return None
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


# A supplementary signal word near a bare table-id mention (P1-A). The
# context gate in supplementary_table_ids() accepts a multi-token group
# without any explicit table word only when such a signal sits within a
# short window around it. Deliberately narrow: bare "table" / "data" /
# "file" / "material" alone are NOT signals -- "Table 5 lists samples
# S1, S2 ..." and "the data were 10-100" would otherwise bless sample
# ids and measurement ranges. An explicit prefix/trailing table word on
# the token itself ("Table S1", "S1 Table") still counts, as before.
_SUPP_CONTEXT_RE = re.compile(
    r"electronic\s+supplementary\s+material"
    r"|supplementar(?:y|ies)|supplemental"
    r"|supporting\s+(?:information|material|materials?|data|files?|info)"
    r"|additional\s+(?:files?|materials?|data|information)"
    r"|appendix|appendices|extended\s+data|\besm\b|suppl?\.?(?!\w)"
    r"|supporting-info|supporting_info|additional-file|additional_file"
    r"|extended-data|extended_data"
    r"|附表|附录|补充|附加|支撑材料|扩展数据",
    re.IGNORECASE,
)
_SUPP_CONTEXT_WINDOW = 120


def _group_has_supp_signal(text: str, start: int, end: int) -> bool:
    """True when a supplementary signal word sits within a short window
    around text[start:end] (P1-A context gate for bare id mentions)."""
    lo = max(0, start - _SUPP_CONTEXT_WINDOW)
    hi = min(len(text), end + _SUPP_CONTEXT_WINDOW)
    return _SUPP_CONTEXT_RE.search(text[lo:hi]) is not None


# A figure/movie word immediately before a lettered range ("Figures S1-S3",
# "Fig. S1-S3", "Supplementary Fig. S1-S3") names figures, not tables
# (P1FIX round-3, Cause A(c)). A mixed "Tables and Figures" phrase names
# tables too and is not blocked.
_FIGURE_PREFIX_RE = re.compile(
    r"(?:figs?\.?|figures?|movies?|videos?|films?)\s*$", re.IGNORECASE)
_MIXED_TABLE_FIGURE_RE = re.compile(
    r"tables?\b\s+and\s+(?:figs?\.?|figures?)\b"
    r"|(?:figs?\.?|figures?)\b\s+and\s+tables?\b",
    re.IGNORECASE,
)
_FIGURE_PREFIX_WINDOW = 60


def _group_is_figure_prefixed(text: str, start: int) -> bool:
    """True when the words right before a token group name figures/movies
    rather than tables (P1FIX round-3, Cause A(c))."""
    window = text[max(0, start - _FIGURE_PREFIX_WINDOW):start]
    if not _FIGURE_PREFIX_RE.search(window.rstrip()):
        return False
    if _MIXED_TABLE_FIGURE_RE.search(window):
        return False
    return True


_PAGE_RANGE_PREFIX_RE = re.compile(
    r"(?:\b(?:pages?|pp?\.?)\s*|(?:suppl(?:ement)?\.?\s*\d*\)\s*[,:]\s*))$",
    re.IGNORECASE,
)


def _group_is_page_prefixed(text: str, start: int) -> bool:
    """True when the tokens immediately follow a journal pagination indicator (e.g. 'pages S221-S231', '(Suppl 2), S221-S231')."""
    window = text[max(0, start - 35):start]
    return bool(_PAGE_RANGE_PREFIX_RE.search(window))


def _group_is_large_numbered_nonsupp(resolved) -> bool:
    """True when every member is an E/A/S id numbered above 99: e-page /
    supplement-issue pagination numbers (e115-e120, S221-S231) are three-digit
    by nature, while genuine Extended Data / Appendix / Supplementary tables are
    numbered small unless preceded by an explicit Table keyword."""
    for tok, _sep in resolved:
        if tok["ns"] not in ("E", "A", "S"):
            return False
        try:
            first = int(str(tok["num"]).split(".")[0])
        except ValueError:
            return False
        if first <= 99:
            return False
    return len(resolved) > 0


def _range_is_multisegment_code(text: str, start_tok, end_tok, is_group_start: bool) -> bool:
    """True when a token range sits inside a longer dashed code such as an
    ethics approval number ("R-FP-S-2-0023-0000" -> S2-S23, P1FIX round-3
    Cause A(b)). Two tells, either suffices:

    * the range end is followed at once by "-<digits>" where the range end
      itself or the following segment is zero-padded ("-0023-0000"): a
      trailing zero-padded segment is a code segment, not a new range
      member. A plain year suffix ("Tables S1-S3-2024") is not: neither
      segment is zero-padded.
    * the range start is glued to a preceding dashed code segment
      ("...FP-S-2..."): a dash with an alphanumeric before it. This tell
      only applies to the group's first pair: later pairs are separated
      from their predecessor by a range dash by construction.
    """
    end = end_tok["end"]
    m = re.match(r"-(\d+)", text[end:])
    if m:
        end_raw = str(end_tok.get("raw_num", ""))
        if end_raw.startswith("0") or m.group(1).startswith("0"):
            return True
    if is_group_start:
        start = start_tok["start"]
        if start >= 2 and text[start - 1] == "-" and text[start - 2].isalnum():
            return True
    return False


def _drop_multisegment_code_ranges(resolved, text: str):
    """Drop range pairs that sit inside a longer dashed code (P1FIX round-3,
    Cause A(b)) before anchoring/expansion, so an approval number mints
    neither endpoint. Other members of the group survive."""
    drop = set()
    for idx in range(1, len(resolved)):
        tok, sep = resolved[idx]
        prev_tok = resolved[idx - 1][0]
        if sep != "range" or prev_tok["ns"] != tok["ns"]:
            continue
        if _range_is_multisegment_code(text, prev_tok, tok, is_group_start=(idx - 1 == 0)):
            drop.add(idx - 1)
            drop.add(idx)
    if not drop:
        return resolved
    return [item for i, item in enumerate(resolved) if i not in drop]


def _group_is_anchored(resolved, text: str) -> bool:
    """True when a resolved token group is a table declaration (P1-A, P1FIX round-3).

    Anchored by an explicit table/supplementary word on some member
    ("Table S1", "Supplementary Tables 2-5"), by an attached-letter range
    in the S namespace only ("S1-S3" -- a bare S range is a long-standing
    contract forced by existing tests), or by a supplementary signal word
    near the group. Two P1FIX round-3 tightenings:

    * An attached-letter range in the E/A namespace ("e115-e120") is NOT
      self-anchoring: e-page numbers look exactly like one, so they need a
      supplementary signal or an explicit table word. A nearby signal
      still does not anchor a large-numbered E/A group (every member above
      99): those are e-page numbers.
    * A group immediately preceded by a figure/movie word ("Figures S1-S3",
      "Fig. S1-S3") is a figure reference, not a table declaration, and is
      never anchored -- not even by a nearby supplementary signal. A mixed
      "Tables and Figures" phrase still counts as a table mention.
    A bare list of lettered ids with none of these (sample ids, reference
    numbers) is not anchored and must not mint table ids.
    """
    if any(tok["context"] for tok, _sep in resolved):
        return True
    if _group_is_figure_prefixed(text, resolved[0][0]["start"]):
        return False
    if _group_is_page_prefixed(text, resolved[0][0]["start"]):
        return False
    if _group_is_large_numbered_nonsupp(resolved):
        return False
    for idx in range(1, len(resolved)):
        tok, sep = resolved[idx]
        prev_tok = resolved[idx - 1][0]
        if (sep == "range" and prev_tok["ns"] == tok["ns"] == "S"
                and prev_tok.get("attached") and not prev_tok.get("bare")):
            return True
    if not _group_has_supp_signal(
            text, resolved[0][0]["start"], resolved[-1][0]["end"]):
        return False
    return True


def supplementary_table_ids(text, in_supplementary_context=False):
    """Return namespaced table identities (S1, S1.1, A1, E1), never bare ordinal counts.

    Hierarchical numbers keep their suffix ('Table S1.1' -> 'S1.1', not 'S1').
    Ranges expand: 'Tables S1 to S3', bare 'S1-S3', 'Appendix A1-A3' and
    'Extended Data Tables 1-3' all enumerate every member. Lists ('S1, S2 and S3')
    enumerate their members. A bare 'S1' with no table context or range/list
    neighbour is ignored, as is a bare main-text 'Table 1'.

    P1-A context gate: a table mention must carry supplementary context.
    A namespace letter must start a word of its own -- the final letter of
    an ordinary word glued to a following number ("were 10-100" -> E10..E100,
    "data 1-60" -> A1..A60, "was 113" -> S113 in the 50-paper retest) is
    never a namespace. A multi-token group with no explicit table word is
    accepted only as an attached-letter range, when a supplementary signal
    sits within a short window around it, or when the caller passes
    in_supplementary_context=True (a candidate's link text / filename /
    attributes, a single download row, or a supplementary section -- the
    link or section itself is the supplementary context). When in doubt
    the extractor under-extracts rather than minting ids: a lone 'S1' and
    a bare list such as 'Samples S1, S2 and S3' stay unextracted.
    """
    result = set()
    text = str(text or "")
    # Normalize unicode dashes so range separators are uniform.
    text = re.sub(r"[‐‑‒–—―]", "-", text)
    # Blank out URLs/DOIs (same length, offsets preserved) before scanning.
    text = _URL_DOI_MASK_RE.sub(lambda m: " " * len(m.group(0)), text)
    # Blank out Elsevier ScienceDirect PII prefixes (e.g. 1-s2.0-S0169136824005171)
    # so '-s2.0-' is not misread as supplementary table 'S2.0'.
    text = re.sub(r"\b1-s2\.0-[A-Za-z0-9]+\b", lambda m: " " * len(m.group(0)), text)
    tokens = []  # (namespace-or-None, number, has_context, start, end)
    for m in _TABLE_TOKEN_RE.finditer(text):
        pw = m.group("pw")
        letter = m.group("letter")
        post = m.group("post")
        # P1-A: the letter must start a word of its own. The letter group
        # also matches the final letter of an ordinary word followed by a
        # space, which minted whole fake ranges out of measurement ranges
        # ("were 10" -> E10, "data 1" -> A1, "was 113" -> S113). The only
        # exception is a letter glued to its own prefix word ("附表S1",
        # where the prefix ends exactly where the letter starts).
        if letter:
            letter_start = m.start("letter")
            if not (pw and m.end("pw") == letter_start):
                if letter_start > 0 and text[letter_start - 1].isalnum():
                    continue
        num = _parse_table_number(m.group("num"))
        if num is None:
            continue
        # Reject implausible table numbers: supplementary tables are numbered
        # from 1 and stay well under 1000; larger digit runs are article
        # numbers, years or accessions, not table ids.
        if num[0].isdigit():
            num_parts = num.split(".")
            try:
                if any(int(p) < 1 or int(p) > 999 for p in num_parts):
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
        # Whether the namespace letter is attached to its number ('S1',
        # 'S-1', 'S.1' -- but not 'S 1'): only an attached letter can
        # anchor a bare range, so a spaced-out letter never does.
        attached = letter is None or not any(
            ch.isspace() for ch in text[m.end("letter"):m.start("num")])
        # A bare number with neither a prefix word, a letter, nor a trailing
        # table word is not a table id on its own; kept as a weak token so it
        # can still inherit the namespace of a range/list neighbour
        # ('Supplementary Tables 2-5' -> S2..S5).
        if not pw and not letter and not post:
            # Bare digits glued to an alphabetic identifier (e.g. MOESM2, v2, fig1)
            # are not standalone table numbers.
            if m.start("num") > 0 and text[m.start("num") - 1].isalpha():
                continue
            tokens.append({"ns": None, "num": num, "context": False, "bare": True, "attached": attached, "start": m.start(), "end": m.end(), "raw_num": m.group("num")})
            continue
        ns = _table_token_namespace(pw, letter, post)
        if ns is None:
            continue
        tokens.append({"ns": ns, "num": num, "context": bool(pw or post), "bare": False, "attached": attached, "start": m.start(), "end": m.end(), "raw_num": m.group("num")})

    # Group consecutive tokens joined only by a range/list separator.
    groups = []  # each: [(token, sep_from_previous)] with sep None for the first
    for tok in tokens:
        if not groups:
            groups.append([(tok, None)])
            continue
        prev_tok = groups[-1][-1][0]
        between = text[prev_tok["end"]:tok["start"]]
        if _TABLE_RANGE_SEP_RE.match(between) or (in_supplementary_context and between.strip() == "_"):
            groups[-1].append((tok, "range"))
        elif between.strip() and _TABLE_LIST_SEP_RE.match(between):
            groups[-1].append((tok, "list"))
        else:
            groups.append([(tok, None)])

    for group in groups:
        if len(group) == 1:
            tok = group[0][0]
            # A lone bare 'S1' (no table word anywhere) is not evidence
            # unless caller explicitly supplied supplementary context (e.g. 'S1.xlsx').
            if tok["ns"] and (in_supplementary_context or tok["context"]):
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
            resolved.append(({"ns": ns, "num": tok["num"], "context": tok["context"], "bare": tok.get("bare", False), "attached": tok.get("attached", False), "start": tok["start"], "end": tok["end"], "raw_num": tok.get("raw_num", tok["num"])}, sep))
        if not resolved:
            continue
        # P1FIX round-3 Cause A(b): a range inside a longer dashed code
        # (ethics approval numbers) is not a table range; drop the pair
        # before anchoring/expansion so it mints neither endpoint.
        resolved = _drop_multisegment_code_ranges(resolved, text)
        if not resolved:
            continue
        if len(resolved) == 1:
            # A group reduced to one token follows the lone-token rule:
            # an explicit table word on it, or caller-supplied
            # supplementary context, is required.
            tok = resolved[0][0]
            if in_supplementary_context or tok["context"]:
                result.add(f"{tok['ns']}{tok['num']}")
            continue
        # P1-A: a multi-token group with no anchor is not a declaration
        # (bare list of lettered ids, e.g. sample ids or reference numbers).
        if not in_supplementary_context and not _group_is_anchored(resolved, text):
            continue
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
    """Only explicit file counts; table ranges are a different constraint.

    P1-A: when several file-count statements disagree, the FIRST statement
    in document order wins; the old max() let a larger restatement inflate
    the declaration. The file-count and table-range readings are counted
    independently -- a table range never feeds this number.
    """
    patterns = [
        r'\b(\d+)\s+(?:supplementary|supplemental|supporting|additional)\s+files?\b',
        r'(?:supplementary|supplemental|supporting|additional)\s+files?\s*\(\s*(\d+)\s*\)',
        r'(?:supplementary|supporting)\s+(?:information|material)\s*\(\s*(\d+)\s+files?\s*\)',
        r'(\d+)\s*个?\s*(?:补充文件|补充附件|附加文件)',
    ]
    values = [(m.start(), int(m.group(1))) for pattern in patterns
              for m in re.finditer(pattern, text, re.I)]
    return min(values)[1] if values else 0


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
            # A candidate's own label/filename is supplementary context (P1-A).
            covered.update(supplementary_table_ids(value, in_supplementary_context=True))
    return covered


_ARCHIVE_BUNDLE_EXTS = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".7z", ".rar")
_OPAQUE_BUNDLE_NAME_RE = re.compile(
    r"(?:moesm\d+|mmc\d+|esm[-_]?\d+|additional[-_ ]?file[-_]?\d+|"
    r"data[-_ ]?sheets?[-_ ]?\d+|"
    r"(?:^|[^a-z])suppl(?:ement(?:ary|al|s)?)?(?:[-_ ]?(?:material|information|data|files?|docs?)s?)?[-_ ]?\d*(?:\.[a-z0-9]+|\b)|"
    r"(?:^|[^a-z0-9])sup[-_]?\d+(?:[-_ ]?suppmat)?|(?:^|[^a-z])supp[-_]?mat(?:erials?)?|"
    r"附录[-_ ]?\d*|附件[-_ ]?\d*|补充(?:材料|文件|数据)[-_ ]?\d*|"
    r"\bsi[-_]s?0*\d+\b|\b[a-z0-9._-]+[-_](?:sm|si)(?:\.[a-z0-9]+)?\b)",
    re.IGNORECASE,
)
_CN_OPAQUE_BUNDLE_NAME_RE = re.compile(r"(?:附录[-_ ]?\d*|附件[-_ ]?\d*|补充(?:材料|文件|数据)[-_ ]?\d*)")


_SPECIFIC_TABLE_WORD_RE = re.compile(
    r"(?:^|[\W_])(?:tables?|tabs?\.?|tbls?\.?|附表|补充表|附录表|附加表)(?:[\W_\d]|$)",
    re.IGNORECASE,
)


def _inline_html_table_ids(soup) -> Set[str]:
    """Table IDs already rendered inline as HTML <table> elements in the article body."""
    inline_ids: Set[str] = set()
    if soup is None:
        return inline_ids
    for tbl in soup.find_all("table"):
        cap_texts: List[str] = []
        cap = tbl.find("caption")
        if cap:
            cap_texts.append(cap.get_text(" ", strip=True))
        for attr_val in (tbl.get("id"), tbl.get("aria-label"), tbl.get("summary")):
            if attr_val:
                cap_texts.append(str(attr_val))
        parent = tbl.parent
        if parent and not _is_whole_article_wrapper(parent):
            p_id = parent.get("id")
            if p_id:
                cap_texts.append(str(p_id))
            prev = parent.find_previous_sibling(["p", "div", "h2", "h3", "h4", "h5", "h6", "header"])
            if prev:
                ptxt = prev.get_text(" ", strip=True)
                if ptxt and len(ptxt) <= 400:
                    cap_texts.append(ptxt)
        prev_tbl = tbl.find_previous_sibling(["p", "div", "h2", "h3", "h4", "h5", "h6", "caption"])
        if prev_tbl:
            ptxt = prev_tbl.get_text(" ", strip=True)
            if ptxt and len(ptxt) <= 400:
                cap_texts.append(ptxt)
        for ctxt in cap_texts:
            if re.search(r"\b(?:tables?|tabs?\.?|tbls?\.?|附表|补充表|附录表)\s+[a-z]?\d+", ctxt, re.IGNORECASE):
                inline_ids.update(supplementary_table_ids(ctxt, in_supplementary_context=False))
    return inline_ids


def has_opaque_bundle_candidate(candidates) -> bool:
    """True when at least one candidate is an archive or opaque publisher bundle
    (e.g. MOESM2_ESM.xlsx, mmc1.xlsx, s1.zip, SI-S01.pdf, kuzmina-sm.pdf, Data_Sheet_1.docx,
    adma201908311-sup-0001-SuppMat.pdf, 附录_1.xls) that can contain multiple tables without
    listing individual sub-table IDs in its filename."""
    for c in candidates or ():
        fname = (getattr(c, "filename", None) or "").lower()
        url = (getattr(c, "url", None) or "").lower()
        path = urllib.parse.urlparse(url).path.lower() if url else ""
        if fname.endswith(_ARCHIVE_BUNDLE_EXTS) or path.endswith(_ARCHIVE_BUNDLE_EXTS):
            return True
        label = f"{getattr(c, 'link_text', '') or ''} {fname} {os.path.basename(path)}"
        has_specific_table_word = bool(_SPECIFIC_TABLE_WORD_RE.search(label))
        is_si_bundle_file = bool(
            re.search(r"(?:^|[^a-z0-9])(?:si[-_]s?0*\d+|[^/\s]+[-_](?:sm|si))\.(?:pdf|docx?)\b", f"{fname} {os.path.basename(path)}", re.IGNORECASE)
            and not has_specific_table_word
        )
        if is_si_bundle_file:
            return True
        if _OPAQUE_BUNDLE_NAME_RE.search(fname) and not has_specific_table_word:
            if not supplementary_table_ids(fname, in_supplementary_context=True) or _CN_OPAQUE_BUNDLE_NAME_RE.search(fname):
                return True
        if getattr(c, "covered_table_ids", None):
            continue
        if _OPAQUE_BUNDLE_NAME_RE.search(label) and not supplementary_table_ids(
            label, in_supplementary_context=True
        ):
            return True
    return False


def is_result_obviously_incomplete(candidates, html_content, declared_count=None, article_text=None):
    if not html_content:
        return (True, 'Empty page content') if not candidates else (False, '')
    soup = html_content if isinstance(html_content, BeautifulSoup) else BeautifulSoup(html_content, DEFAULT_PARSER)
    decl_source = article_text if article_text is not None else extract_article_declaration_text(soup)
    # H5: the two readings are judged separately, both on visible article text only
    # (script/style/nav/reference content is not a declaration). A table range or
    # a high table number is never treated as a file count.
    expected_tables = detect_declared_table_range(decl_source)
    missing = expected_tables - covered_table_ids(candidates) - _inline_html_table_ids(soup)
    if missing and candidates and has_opaque_bundle_candidate(candidates):
        missing = set()
    file_count = detect_declared_file_count(decl_source)
    # Deduplicate identical URLs but never substitute table counts for files.
    if file_count and len({c.url for c in candidates}) < file_count:
        return True, f'File count ({len({c.url for c in candidates})}) is less than declared count ({file_count} files)'
    supp_keywords = ('supplement', 'supporting-info', 'supporting_info',
                     'additional-file', 'extended-data', 'extended_data', '补充', '附表', '附录', '支撑材料')
    _inc_token_re = re.compile(r"(?<![a-z0-9])(?:(?:mo)?esm(?:[-_]?\d+)?|suppl\.?)(?![a-z0-9])", re.IGNORECASE)
    sections = soup.find_all(
        lambda el: el.name in ('section', 'div')
        and not _is_journal_nav_chrome(el)
        and (
            any(kw in (el.get('id', '') + ' ' + ' '.join(el.get('class', []))).lower() for kw in supp_keywords)
            or bool(_inc_token_re.search(el.get('id', '') + ' ' + ' '.join(el.get('class', []))))
        )
    )
    evidence = candidate_evidence(candidates)
    meta_satisfies = bool(declared_count and len([c for c in candidates if c.section == 'meta_tags']) >= declared_count)

    def _tier_item_matches_target(item, target_url: str) -> bool:
        if item.get('match_rule') != 'tier_api_or_cdn':
            return False
        item_fn = item.get('filename') or ''
        target_fn = extract_filename_from_url(target_url)
        if item_fn and target_fn:
            k1 = filename_dedup_key(item_fn)
            k2 = filename_dedup_key(target_fn)
            if k1 is not None and k1 == k2:
                return True
        m_item = MMC_PATTERN.search(item_fn)
        if m_item:
            m_target = MMC_PATTERN.search(target_url)
            if m_target and m_item.group(0).lower() == m_target.group(0).lower():
                return True
        return False

    section_records = []
    base_guess = candidates[0].source_url if candidates and candidates[0].source_url else ""
    for section in sections:
        raw_targets = [extract_target_url_from_tag(tag) for tag in section.find_all(['a','button','input','form','div','span','iframe','object','embed'])]
        raw_targets = [
            t for t in raw_targets
            if t
            and not t.strip().startswith(('#', 'javascript:', 'mailto:', 'tel:'))
            and 'creativecommons.org/licenses/' not in t.lower()
        ]
        targets = [
            t for t in raw_targets
            if not is_excluded_url(urllib.parse.urljoin(base_guess, t) if base_guess else t, base_url=base_guess)
            and not is_article_figure_url(urllib.parse.urljoin(base_guess, t) if base_guess else t)
        ]
        had_filtered_only = bool(raw_targets) and not targets
        selectors = [tag.get('data-journal-download-selector') for tag in section.find_all(attrs={'data-journal-download-selector': True})]
        matched = any(
            (item.get('extra_info', {}).get('browser_selector') in selectors and bool(selectors))
            or any(
                urllib.parse.urljoin(item.get('source_url') or item['url'], target).split('#')[0] == item['url'].split('#')[0]
                or _tier_item_matches_target(item, target)
                for target in targets
            )
            for item in evidence)
        section_records.append((section, targets, selectors, matched, had_filtered_only))

    any_section_matched = any(matched for _, _, _, matched, _ in section_records)
    any_dom_supp_candidate = any(
        c.section in _DOM_SUPP_SECTIONS or c.section in ('accordion', 'meta_tags') for c in candidates
    )

    filtered_only_parents = [
        sec for sec, targs, sels, is_m, had_filt in section_records
        if not is_m and any_section_matched and had_filt and not sels
    ]
    for section, targets, selectors, matched, had_filtered_only in section_records:
        if matched or meta_satisfies:
            continue
        # Nested sub-containers (e.g. a title div or badge wrapper) inside an already-matched
        # supplementary section, or an outer wrapper enclosing one, are fulfilled by that match.
        if any(
            m_sec is not section and (section in m_sec.descendants or m_sec in section.descendants)
            for m_sec, _, _, is_m, _ in section_records if is_m
        ):
            continue
        # If another DOM supplementary container on the page ALREADY yielded candidates, an
        # editorial pointer/statement block referencing it via an in-page #anchor, OR a sibling
        # item whose only links were intentionally filtered figure/inline-table subpages
        # (and its nested caption/description divs), is not unresolved.
        if any(section is f_sec or section in f_sec.descendants for f_sec in filtered_only_parents):
            continue
        if (
            not targets
            and not selectors
            and not _has_actionable_supp_content(section)
        ):
            continue
        sec_ident = (section.get('id', '') + ' ' + ' '.join(section.get('class', []))).lower()
        has_fragment_pointer = any(
            (a_tag.get('href') or '').strip().startswith('#')
            or ('#' in (a_tag.get('href') or '') and _ARTICLE_SECTION_FRAGMENT_RE.search((a_tag.get('href') or '').split('#', 1)[1]))
            for a_tag in section.find_all('a', href=True)
        )
        if any_section_matched and not targets and not selectors and not section.find('button') and (
            'statement' in sec_ident or 'description' in sec_ident or has_fragment_pointer
        ):
            continue
        # A named section can be fulfilled by corresponding files on a mirror.
        # The section itself was selected for its supplementary markers,
        # so its own text is supplementary context (P1-A).
        section_tables = supplementary_table_ids(
            section.get_text(' ', strip=True), in_supplementary_context=True)
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
    "client challenge",
    "attention required",
    "verify you are human",
    "validate user",
    "radware",
    "bot manager",
    "pardon our interruption",
    "请稍候",
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

_BLOCKED_TITLE_EXACT = {
    "sign in", "log in", "login", "sign-in", "log-in",
    "captcha", "validate user", "client challenge", "请稍候", "请稍候...",
}

# Body phrases that only appear when the page itself is a challenge / login
# wall. Deliberately NOT included: a bare "Sign in" navigation link, which
# normal article pages carry in their header.
_BLOCKED_CHALLENGE_BODY_MARKERS = (
    "just a moment",
    "client challenge",
    "attention required",
    "verify you are human",
    "please verify you are human",
    "checking your browser",
    "checking if the site connection is secure",
    "challenge-platform",
    "cf-browser-verification",
    "turnstile",
    "radware bot manager",
    "validate.perfdrive.com",
    "pardon our interruption",
    "human verification",
    "access denied",
    "403 forbidden",
)
_BLOCKED_AUTH_BODY_MARKERS = (
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
_MODAL_TOLERATED_AUTH_MARKERS = {
    "institutional login",
    "log in to view",
    "sign in to view",
    "log in to access",
    "sign in to access",
    "sign in to continue",
    "log in to continue",
}
_BLOCKED_BODY_MARKERS = _BLOCKED_CHALLENGE_BODY_MARKERS + _BLOCKED_AUTH_BODY_MARKERS


def is_blocked_page(page_html: Optional[str]) -> bool:
    """True if *page_html* looks like a challenge / access-denied / login-wall page.

    Checks the <title> and visible body text (stripping <script>/<style>/<noscript>/<svg>
    and comments first so JS bundles like Nuxt/Turnstile or challenge-platform snippets
    do not trigger false positives). Auth/login markers also ignore site <nav>/<header>/<footer>
    chrome or hidden login modals when substantial non-chrome article text exists.
    """
    if not page_html or not isinstance(page_html, str):
        return False
    cleaned_html = _strip_nonvisible_html(page_html)
    title_match = re.search(r"<title[^>]*>(.*?)</title>", cleaned_html, re.I | re.S)
    title = html.unescape(title_match.group(1) if title_match else "").strip().lower()
    if title:
        if title in _BLOCKED_TITLE_EXACT:
            return True
        if any(marker in title for marker in _BLOCKED_TITLE_MARKERS):
            return True
    visible_text = re.sub(r"<[^>]+>", " ", cleaned_html)
    visible_text = html.unescape(visible_text)
    visible_text = re.sub(r"\s+", " ", visible_text).strip().lower()
    if any(marker in visible_text for marker in _BLOCKED_CHALLENGE_BODY_MARKERS):
        return True
    non_chrome_html = re.sub(
        r"<(nav|header|footer)\b[^>]*>.*?</\1>",
        " ",
        cleaned_html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    non_chrome_text = re.sub(r"<[^>]+>", " ", non_chrome_html)
    non_chrome_text = html.unescape(non_chrome_text)
    non_chrome_text = re.sub(r"\s+", " ", non_chrome_text).strip().lower()
    # Captcha / Silverchair crawlprevention / Cloudflare / Radware / Client Challenge pages
    raw_html_low = page_html.lower()
    non_head_html = re.sub(r"<head\b[^>]*>.*?</head>", " ", cleaned_html, flags=re.IGNORECASE | re.DOTALL)
    non_head_text = re.sub(r"<[^>]+>", " ", non_head_html)
    non_head_text = html.unescape(non_head_text)
    non_head_text = re.sub(r"\s+", " ", non_head_text).strip().lower()
    # Pre-challenge <NO BODY> shells (403 Cloudflare/Radware/Imperva/Atypon challenge scripts
    # in <head> with no <body> tag and virtually no non-head visible text)
    if (
        "<head" in raw_html_low
        and "<script" in raw_html_low
        and not re.search(r"<body\b", raw_html_low)
        and len(non_head_text) < 200
    ):
        return True
    # F5 BIG-IP /TSPD/ bot defense interstitial (e.g. IEEE Xplore /TSPD/ + apm_do_not_touch with empty body)
    if ("/tspd/" in raw_html_low or "apm_do_not_touch" in raw_html_low) and len(non_head_text) < 500:
        return True
    # AWS WAF challenge / JS token / captcha (e.g. De Gruyter)
    if len(non_chrome_text) < 2500 and any(
        m in raw_html_low for m in ("aws-waf-captcha", "awswaf", "gokuprops", "awswafintegration")
    ):
        return True
    # CNKI verification / clickWord human check
    if len(non_chrome_text) < 2500 and (
        ("安全验证" in non_chrome_text or "安全验证" in title or "verify/home" in raw_html_low)
        and any(w in raw_html_low for w in ("clickword", "captchaid", "cnki", "/verify/"))
    ):
        return True
    if title and "captcha" in title and (
        len(non_chrome_text) < 2500
        or any(w in title for w in ("bot", "radware", "security", "verify", "human", "challenge", "validate"))
    ):
        return True
    if len(non_chrome_text) < 5000 and any(
        m in raw_html_low for m in ("/crawlprevention/governor", "validate.perfdrive.com", "/_fs-ch-")
    ):
        return True
    if "请稍候" in non_chrome_text and (
        len(non_chrome_text) < 2500
        or any(w in non_chrome_text for w in ("连接安全性", "安全检查", "验证", "cloudflare", "turnstile"))
    ):
        return True
    if "captcha" in non_chrome_text and len(non_chrome_text) < 1200 and (
        any(
            w in non_chrome_text
            for w in (
                "bot", "verify", "human", "security", "radware", "perfdrive", "robot",
                "complete the captcha", "solve the captcha", "please complete",
            )
        )
        or any(w in raw_html_low for w in ("perfdrive", "recaptcha", "hcaptcha"))
    ):
        return True
    auth_target = non_chrome_text if len(non_chrome_text) > 200 else visible_text
    has_password = 'type="password"' in cleaned_html.lower() or "type='password'" in cleaned_html.lower()
    matched_auth = [m for m in _BLOCKED_AUTH_BODY_MARKERS if m in auth_target]
    if not matched_auth:
        return False
    # A full article page whose non-<nav> header or hidden login modal happens to carry
    # auth prompts ("Institutional Login", "Log in to view", "Log in to access",
    # "Sign in to continue") alongside >= 5000 visible non-chrome article chars is not
    # a blocked login wall.
    if matched_auth == ["institutional login"] and not has_password and len(non_chrome_text) >= 1200:
        return False
    if len(non_chrome_text) >= 5000 and set(matched_auth).issubset(_MODAL_TOLERATED_AUTH_MARKERS):
        return False
    return True


def page_fetch_is_failure(html: Optional[str], status: Optional[int] = None) -> bool:
    """Caller rule for H1: a page fetch failed when the HTTP status is an
    error (>= 400), or when the page is a blocked page. An unknown status
    (None, e.g. BrowserSkill exposes no navigation status) is only a
    failure when the page shows challenge / login features; on its own it
    is accepted, since no better signal exists. A ``status < 400`` alone
    must never be treated as success without this check.
    """
    if isinstance(status, int) and not isinstance(status, bool) and status >= 400:
        return True
    return isinstance(html, str) and bool(html) and is_blocked_page(html)


class RepoBrowserSession:
    """Reusable single-browser session for repository expansion (Figshare/ScienceBase).

    Reuses one StealthySession across collection/item subpages so each item does
    not spawn a new Chromium process or block on SUPP_WAIT_SELECTOR.
    """

    def __init__(
        self,
        *,
        play_cookies=None,
        headful: bool = True,
        real_chrome: bool = False,
        cdp_url: Optional[str] = None,
        proxy: Optional[str] = None,
        budget=None,
        phase_deadline: Optional[float] = None,
        explicit_budget=None,
        bsk_fetch=None,
        allow_auto_headful: bool = True,
    ):
        self.play_cookies = play_cookies
        self.headful = bool(headful)
        self.real_chrome = bool(real_chrome)
        self.cdp_url = cdp_url
        self.proxy = proxy
        self.budget = budget
        self.phase_deadline = phase_deadline
        self.explicit_budget = explicit_budget
        self.bsk_fetch = bsk_fetch
        self.allow_auto_headful = bool(allow_auto_headful)
        self._engine = None

    def _ensure_engine(self, first_url: str):
        if self._engine is not None:
            return self._engine
        from scrapling.fetchers import StealthySession
        kwargs = {
            "headless": not self.headful,
            "network_idle": False,
            "solve_cloudflare": True,
            "timeout": 25000,
            "retries": 1,
            "max_pages": 2,
            "blocked_domains": _merge_blocked_domains(
                first_url, None, proxy=self.proxy or ""
            ),
        }
        if self.cdp_url:
            kwargs["cdp_url"] = self.cdp_url
        elif self.real_chrome:
            kwargs["real_chrome"] = True
        if self.proxy:
            kwargs["proxy"] = playwright_proxy_config(self.proxy)
            kwargs.update(proxy_browser_trust_kwargs(self.proxy))
        if self.play_cookies:
            try:
                from playwright_utils import normalize_playwright_cookies
                norm = normalize_playwright_cookies(self.play_cookies, default_url=first_url)
                if norm:
                    kwargs["cookies"] = norm
            except Exception:
                pass
        eng = StealthySession(**kwargs)
        eng.__enter__()
        self._engine = eng
        return eng

    def _merge_page_cookies(self, page):
        pc = getattr(page, "cookies", None)
        if isinstance(self.play_cookies, list) and isinstance(pc, (list, tuple, dict)) and pc:
            try:
                from playwright_utils import merge_cookies
                self.play_cookies[:] = merge_cookies(self.play_cookies, pc)
            except Exception:
                pass

    def _fetch_once(self, engine, url: str, *, timeout_ms: int, wait_ms: int, solve_cloudflare: bool):
        bd = _merge_blocked_domains(url, None, proxy=self.proxy or "")
        return engine.fetch(
            url,
            timeout=timeout_ms,
            wait=wait_ms,
            network_idle=False,
            solve_cloudflare=solve_cloudflare,
            blocked_domains=bd,
        )

    def fetch(self, repo_url: str):
        """Fetch a repository landing/item page via bsk or a shared StealthySession."""
        if self.phase_deadline is not None and time.monotonic() >= self.phase_deadline:
            return "", None, repo_url
        cur_budget = ensure_phase_browser_budget(
            self.budget,
            phase_deadline=self.phase_deadline,
            explicit_budget=self.explicit_budget,
        )
        if cur_budget.exhausted:
            return "", None, repo_url
        if self.bsk_fetch is not None:
            try:
                bh, bs, bf = self.bsk_fetch(repo_url)
                if bh and bs != 202 and not page_fetch_is_failure(bh, bs):
                    return bh, bs, bf or repo_url
            except Exception:
                pass
        if not (self.headful or self.allow_auto_headful):
            return "", None, repo_url
        try:
            engine = self._ensure_engine(repo_url)
        except Exception:
            return "", None, repo_url

        rem_s = 30.0 if self.phase_deadline is None else max(4.0, self.phase_deadline - time.monotonic())
        fast_ms = max(4000, min(18000, int(rem_s * 1000)))
        page = None
        for attempt in range(2):
            try:
                page = self._fetch_once(
                    engine,
                    repo_url,
                    timeout_ms=fast_ms,
                    wait_ms=1800,
                    solve_cloudflare=False,
                )
                break
            except Exception as exc:
                err_l = str(exc).lower()
                is_nav = "navigating and changing the content" in err_l or "execution context was destroyed" in err_l
                if attempt == 0 and is_nav:
                    time.sleep(1.5)
                    continue
                page = None
                break

        html = str(getattr(page, "html_content", None) or getattr(page, "text", "") or "") if page else ""
        status = getattr(page, "status", None) if page else None
        final_url = (getattr(page, "url", None) or repo_url) if page else repo_url
        if page is not None:
            self._merge_page_cookies(page)

        if not html or status == 202 or page_fetch_is_failure(html, status):
            rem_s2 = 30.0 if self.phase_deadline is None else max(4.0, self.phase_deadline - time.monotonic())
            if rem_s2 >= 6.0:
                try:
                    page2 = self._fetch_once(
                        engine,
                        repo_url,
                        timeout_ms=min(28000, int(rem_s2 * 1000)),
                        wait_ms=1800,
                        solve_cloudflare=True,
                    )
                    if page2 is not None:
                        self._merge_page_cookies(page2)
                        html2 = str(getattr(page2, "html_content", None) or getattr(page2, "text", "") or "")
                        st2 = getattr(page2, "status", None)
                        fu2 = getattr(page2, "url", None) or final_url
                        if html2:
                            return html2, st2, fu2
                except Exception:
                    pass
        return html, status, final_url

    def close(self):
        if self._engine is not None:
            try:
                self._engine.__exit__(None, None, None)
            except Exception:
                pass
            self._engine = None


def single_line(text, max_len: int = 1000) -> str:
    """Collapse multi-line exception / reason text to a single line.

    P1-F: Playwright exceptions span several lines (``... at <url>\\nCall
    log:\\n  - navigating to ...\\n)``), so a ``FAIL (...)`` line printed
    verbatim has its closing paren 2-3 lines later and a single-line
    regex misreads it as a bare FAIL. Only the formatting changes; the
    reason content is preserved (truncated at *max_len* with an ellipsis
    when absurdly long).
    """
    flat = re.sub(r"\s+", " ", str(text)).strip()
    if len(flat) > max_len:
        flat = flat[:max_len].rstrip() + " \u2026"
    return flat


def remove_invalid_existing_download(filepath: str, manifest=None, reason: str = "") -> bool:
    """Delete a previously landed file that fails validation (P1-F).

    The H9 download paths validate a temp file before committing, so
    garbage normally never lands; this is the cleanup for files an
    earlier run already left on disk. The file is removed from disk and
    its manifest entry is dropped, so it is not [SKIP]-counted as a
    success and does not enter completeness statistics. Returns True
    when a file was removed.
    """
    name = os.path.basename(filepath)
    try:
        os.remove(filepath)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if manifest is not None and name in manifest:
        manifest.pop(name, None)
        try:
            save_download_manifest(os.path.dirname(filepath) or ".", manifest)
        except Exception:
            pass
    print(f"  [Removed] {name} (invalid existing download: {reason})")
    return True


# ── P1-F: downloaded-file authenticity ──────────────────────────────────────
# A bare "Table N.html" / "Appendix N.html" filename is an in-article
# table rendered as a web page, never a supplement -- even when the page
# contains a genuine data table (BMC #43/#44, eLife #34/#36/#37; #36
# landed 18 copies of Appendix 7.html). "Table S1.html" and
# "Supplementary Table S1.html" do NOT match: the S / Supplementary
# marker is what makes a table a supplement.
_NAV_PAGE_FILENAME_RE = re.compile(
    r"^(?:table|appendix)[\s_\-]+\d+(?:_\d+)?\.html?$", re.IGNORECASE
)
# NOTE on the separator above: the evidenced navigation pages are
# "Table 1.html" / "Appendix 7.html" (word, separator, number). A bare
# "table1.html" with no separator is NOT flagged: that is the D1
# rename path, where a .bin download that turns out to be an HTML
# table attachment is legitimately renamed to .html
# (test_d1_download_html_table_rename).

# Challenge / anti-bot markers that never occur in genuine supplement
# data, so they reject even without any HTML context (a challenge wall
# served as .csv / .bin is still a wall).
_CHALLENGE_CONTENT_MARKERS = (
    b"client challenge",
    b"challenge-platform",
    b"cf-browser-verification",
    b"checking your browser",
    b"verify you are human",
    b"please verify you are human",
    b"human verification",
    b"radware bot manager",
    b"validate.perfdrive.com",
    b"/crawlprevention/governor",
)


def _download_basename(filepath: str) -> str:
    """The intended download name for *filepath*, undoing the H9 temp
    naming (``.<stem>.<random><ext>``) so a staged ``Table 1.html``
    is still recognised as ``Table 1.html``."""
    base = os.path.basename(filepath)
    stem, ext = os.path.splitext(base)
    if stem.startswith("."):
        inner = stem[1:]
        stem = inner.rsplit(".", 1)[0] if "." in inner else inner
        base = stem + ext
    return base


def _has_genuine_data_table(sample_lower: bytes) -> bool:
    """True when *sample_lower* holds a real multi-row data table
    (at least two rows with cells), as opposed to a one-row layout
    table on a licence / navigation page."""
    return sample_lower.count(b"<tr") >= 2 and (
        sample_lower.count(b"<td") + sample_lower.count(b"<th") >= 2
    )


def _looks_like_license_page(sample_lower: bytes) -> bool:
    """True when the page presents itself as a Creative Commons licence
    deed (title or licences link), rather than merely mentioning a
    licence in passing."""
    title_match = re.search(rb"<title[^>]*>(.*?)</title>", sample_lower, re.S)
    if title_match and b"creative commons" in title_match.group(1):
        return True
    return b"creativecommons.org/licenses" in sample_lower


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

    # P1-F: a bare "Table N.html" / "Appendix N.html" name is a
    # navigation page, not a supplement -- whatever its content is.
    _nav_name = _download_basename(filepath)
    if _NAV_PAGE_FILENAME_RE.match(_nav_name):
        return False, (
            f"Navigation-page filename {_nav_name!r}: an in-article "
            "Table/Appendix page, not a supplement file"
        )

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

    # P1-F: a challenge / anti-bot wall is garbage even when it carries
    # a table or arrives under a data extension. These markers never
    # occur in genuine supplement data, so no HTML context is required.
    # 2. Check for HTML content (either via HTML tags or text/html Content-Type)
    _sample = content_bytes[:65536] if content_bytes is not None else None
    if _sample is None:
        try:
            with open(filepath, "rb") as _f:
                _sample = _f.read(65536)
        except Exception:
            _sample = header_bytes
    _sample_lower = _sample.lower()

    header_lower = header_bytes.lower()
    html_probe_lower = _sample_lower[:16384]
    is_html_tag = any(tag in html_probe_lower for tag in [b"<!doctype html", b"<html", b"<head", b"<body", b"<table"])
    is_html_ct = "text/html" in ct

    # Non-HTML text/binary payloads (e.g. a challenge page served as .csv/.bin)
    # are still checked directly against _CHALLENGE_CONTENT_MARKERS.
    if not (is_html_tag or is_html_ct):
        for _marker in _CHALLENGE_CONTENT_MARKERS:
            if _marker in _sample_lower:
                return False, (
                    "Challenge/anti-bot page detected: "
                    f"{_marker.decode('latin1')}"
                )

    challenge_error_indicators = [
        b"challenge-platform",
        b"cf-browser-verification",
        b"client challenge",
        b"checking your browser",
        b"verify you are human",
        b"please verify you are human",
        b"attention required",
        b"turnstile",
        b"human verification",
        b"just a moment",
        b"access denied",
        b"403 forbidden",
        b"404 not found",
        b"page not found",
    ]
    auth_error_indicators = [
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
        html_sample = _sample
        sample_lower = _sample_lower
        # Strip <script>/<style>/<noscript>/<svg>/comments (and truncated trailing <script>/<style>)
        # before matching text error/challenge markers so legitimate standalone HTML tables
        # wrapped in publisher JS/analytics do not false-positive.
        visible_html_lower = re.sub(rb"<!--.*?-->", b" ", sample_lower, flags=re.S)
        visible_html_lower = re.sub(
            rb"<(script|style|noscript|svg)\b[^>]*>.*?</\1>",
            b" ",
            visible_html_lower,
            flags=re.S,
        )
        visible_html_lower = re.sub(rb"<(script|style|noscript|svg)\b[^>]*>.*$", b" ", visible_html_lower, flags=re.S)
        title_match = re.search(rb"<title[^>]*>(.*?)</title>", visible_html_lower, re.S)
        title_lower = re.sub(rb"\s+", b" ", title_match.group(1)).strip() if title_match else b""
        visible_text_lower = re.sub(rb"<[^>]+>", b" ", visible_html_lower)
        visible_text_lower = re.sub(rb"\s+", b" ", visible_text_lower).strip()
        non_chrome_html_lower = re.sub(
            rb"<(nav|header|footer)\b[^>]*>.*?</\1>",
            b" ",
            visible_html_lower,
            flags=re.S,
        )
        non_chrome_text_lower = re.sub(rb"<[^>]+>", b" ", non_chrome_html_lower)
        non_chrome_text_lower = re.sub(rb"\s+", b" ", non_chrome_text_lower).strip()
        auth_text_target = non_chrome_text_lower if non_chrome_text_lower else visible_text_lower

        has_password_input = (
            b'type="password"' in visible_html_lower
            or b"type='password'" in visible_html_lower
        )
        has_genuine_table = _has_genuine_data_table(visible_html_lower) and not has_password_input

        # Support legitimate HTML-table exported .xls files (common in older journals/publishers)
        if ext == ".xls":
            has_hard_err = (
                any(ind in title_lower or ind in visible_text_lower for ind in challenge_error_indicators)
                or any(
                    ind in title_lower or (ind in auth_text_target and not has_genuine_table)
                    for ind in auth_error_indicators + soft_auth_indicators
                )
            )
            if not has_hard_err and b"<table" in visible_html_lower and (b"<tr" in visible_html_lower or b"<td" in visible_html_lower or b"<th" in visible_html_lower):
                return True, f"Valid HTML-table exported .xls spreadsheet ({file_size} bytes)"
            return False, f"Expected .xls data file but received HTML document ({file_size} bytes)"

        # If the file was expected to be a binary/data file (e.g. .xlsx, .pdf, .zip, .csv)
        # but content is HTML, it is an error/login page disguised as data file!
        if ext in NON_HTML_DATA_EXTS:
            return False, f"Expected {ext} data file but received HTML document ({file_size} bytes)"

        # Check if it contains a genuine data table structure (D1)
        has_table_structure = (
            b"<table" in visible_html_lower
            and b"<tr" in visible_html_lower
            and (b"<td" in visible_html_lower or b"<th" in visible_html_lower)
            and not has_password_input
        )

        # Challenge / HTTP error indicators in <title> or visible text always reject
        for indicator in challenge_error_indicators:
            if indicator in title_lower or indicator in visible_text_lower:
                return False, f"HTML error/challenge/login page detected: {indicator.decode('latin1')}"

        # P1-F: a Creative Commons licence deed is not a supplement. A
        # one-row layout table must not rescue it; a genuine multi-row
        # data table (e.g. a real HTML supplement that links its
        # licence) still passes.
        if _looks_like_license_page(visible_html_lower) and not _has_genuine_data_table(visible_html_lower):
            return False, "Creative Commons licence page detected"

        # Auth/paywall indicators in <title> always reject; in body reject unless a genuine multi-row data table is present
        for indicator in auth_error_indicators:
            if indicator in title_lower or (indicator in auth_text_target and not has_genuine_table):
                return False, f"HTML error/challenge/login page detected: {indicator.decode('latin1')}"

        # Soft auth/navigation phrases ("sign in", "subscribe to", "purchase article"):
        # Reject if there is no genuine table structure; allow if it is a standalone HTML table view with site nav
        for indicator in soft_auth_indicators:
            if indicator in visible_text_lower:
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
    "just a moment", "just a moment...", "client challenge", "access denied",
    "attention required! _ cloudflare", "attention required! | cloudflare",
    "403 forbidden", "404 not found", "page not found",
    "security check", "verify you are human", "validate user",
    "radware bot manager captcha", "请稍候", "请稍候...",
    "institutional login",
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
        "just a moment", "client challenge", "access denied", "403 ", "404 ", "attention required",
        "cloudflare", "error ", "security check", "verify you are human",
        "validate user", "radware", "请稍候",
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

    raw_html_str = (
        str(getattr(page_or_html, "html_content", None) or getattr(page_or_html, "text", None) or page_or_html)
    )
    title = None
    if hasattr(page_or_html, "css"):
        try:
            title = page_or_html.css('title::text').get()
            if title is not None:
                title = re.sub(r"<!--.*?-->", "", str(title), flags=re.DOTALL).strip()
        except Exception:
            title = None

    if not title:
        m = re.search(r'<title[^>]*>(.*?)</title>', raw_html_str, re.IGNORECASE | re.DOTALL)
        if m:
            candidate_title = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.DOTALL)
            candidate_title = re.sub(r"<[^>]+>", "", candidate_title).strip()
            if candidate_title:
                title = candidate_title

    # Fallback 1: IEEE Xplore Angular SPA embeds metadata JSON when <title><!----><!----></title> is empty
    if not title:
        m_xpl = re.search(r"xplGlobal\.document\.metadata\s*=\s*\{", raw_html_str)
        if m_xpl:
            json_start = m_xpl.end() - 1
            try:
                meta_obj, _ = json.JSONDecoder().raw_decode(raw_html_str, json_start)
                if isinstance(meta_obj, dict) and meta_obj.get("title"):
                    title = str(meta_obj["title"]).strip()
            except Exception:
                snippet = raw_html_str[json_start:json_start + 8192]
                m_t = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)+)"', snippet)
                if m_t:
                    try:
                        title = json.loads(f'"{m_t.group(1)}"')
                    except Exception:
                        title = m_t.group(1)

    # Fallback 2: <meta name="citation_title"> / <meta property="og:title"> / <meta name="dc.title">
    if not title:
        for meta_pat in (
            r'<meta\b[^>]*\bname\s*=\s*["\']citation_title["\'][^>]*\bcontent\s*=\s*["\']([^"\']+)["\']',
            r'<meta\b[^>]*\bcontent\s*=\s*["\']([^"\']+)["\'][^>]*\bname\s*=\s*["\']citation_title["\']',
            r'<meta\b[^>]*\bproperty\s*=\s*["\']og:title["\'][^>]*\bcontent\s*=\s*["\']([^"\']+)["\']',
            r'<meta\b[^>]*\bcontent\s*=\s*["\']([^"\']+)["\'][^>]*\bproperty\s*=\s*["\']og:title["\']',
            r'<meta\b[^>]*\bname\s*=\s*["\']dc\.title["\'][^>]*\bcontent\s*=\s*["\']([^"\']+)["\']',
        ):
            m_meta = re.search(meta_pat, raw_html_str, re.IGNORECASE)
            if m_meta and m_meta.group(1).strip():
                title = m_meta.group(1).strip()
                break

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


def fetch_sciengine_api_candidates(
    url_or_doi: str,
    source_url: Optional[str] = None,
    timeout: float = 15.0,
    proxies: Optional[dict] = None,
    session=None,
) -> Tuple[List[Candidate], str]:
    """Query SciEngine's public sci-open REST API for supplementary/appendix files.

    SciEngine article pages return an unhydrated Vue SPA shell to static HTTP
    clients and attach JS click handlers in rendered DOM. When a URL or DOI
    targets SciEngine (``sciengine.com`` / ``10.1360/...``), this helper
    resolves ``article.baseId`` and queries ``getMoopList`` to produce direct
    HTTP file download candidates at ``/sci-open/api/v1/open/file/<filePath>``.
    Returns ``(candidates, article_title)``.
    """
    combined = f"{url_or_doi or ''} {source_url or ''}".lower()
    doi_m = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>?#]+)", url_or_doi or "", re.IGNORECASE)
    if not doi_m and source_url:
        doi_m = re.search(r"\b(10\.\d{4,9}/[^\s\"'<>?#]+)", source_url, re.IGNORECASE)
    doi = doi_m.group(1).rstrip(".,;:/") if doi_m else None
    is_sciengine = "sciengine.com" in combined or bool(doi and doi.lower().startswith("10.1360/"))
    if not is_sciengine or not doi:
        return [], ""

    origin = "https://www.sciengine.com"
    for cand_src in (source_url, url_or_doi):
        if cand_src and cand_src.lower().startswith(("http://", "https://")) and "sciengine.com" in cand_src.lower():
            parsed = urllib.parse.urlsplit(cand_src)
            if parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
                break

    try:
        import requests
    except ImportError:
        return [], ""

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    http_get = session.get if session is not None else requests.get
    req_kwargs: dict = {"headers": headers, "timeout": timeout}
    if proxies:
        req_kwargs["proxies"] = proxies

    try:
        basic_resp = http_get(
            f"{origin}/sci-open/api/v1/open/article/basic",
            params={"doi": doi},
            **req_kwargs,
        )
        if basic_resp.status_code >= 400:
            return [], ""
        basic_json = basic_resp.json()
        article_obj = (basic_json.get("data") or {}).get("article") or {}
        base_id = str(article_obj.get("baseId") or "").strip()
        title = str(article_obj.get("titleEn") or article_obj.get("titleZh") or article_obj.get("title") or "").strip()
        if not base_id:
            return [], title

        moop_resp = http_get(
            f"{origin}/sci-open/api/v1/open/article/getMoopList",
            params={"articleBaseId": base_id, "articleType": "dataMedia"},
            **req_kwargs,
        )
        if moop_resp.status_code >= 400:
            return [], title
        moop_json = moop_resp.json()
        items = moop_json.get("data") or []
        if isinstance(items, dict):
            items = items.get("list") or items.get("records") or items.get("items") or []
        if not isinstance(items, list):
            return [], title

        out: List[Candidate] = []
        seen: set = set()
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            file_path = str(item.get("filePath") or "").strip()
            link_url = str(item.get("linkUrl") or "").strip()
            if file_path.startswith(("http://", "https://")):
                dl_url = file_path
            elif file_path.startswith("/"):
                dl_url = f"{origin}{file_path}"
            elif file_path:
                dl_url = f"{origin}/sci-open/api/v1/open/file/{urllib.parse.quote(file_path)}"
            elif link_url:
                dl_url = urllib.parse.urljoin(f"{origin}/", link_url)
            else:
                continue
            raw_fname = str(item.get("fileName") or item.get("titleZh") or item.get("titleEn") or f"supplement_{idx}.bin").strip()
            label = str(item.get("titleZh") or item.get("titleEn") or raw_fname).strip()
            fname = sanitize_filename(raw_fname)
            if not os.path.splitext(fname)[1]:
                url_ext = os.path.splitext(urllib.parse.urlsplit(dl_url).path)[1]
                label_ext = os.path.splitext(label)[1]
                ft = str(item.get("fileType") or "").lower().strip()
                ft_map = {
                    "application/pdf": ".pdf",
                    "application/zip": ".zip",
                    "application/x-zip-compressed": ".zip",
                    "application/vnd.ms-excel": ".xls",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
                    "application/msword": ".doc",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
                    "text/csv": ".csv",
                    "text/plain": ".txt",
                }
                ext = url_ext or label_ext or ft_map.get(ft, "")
                if ext and len(ext) <= 6:
                    fname = f"{fname}{ext}"
            if dl_url in seen:
                continue
            seen.add(dl_url)
            out.append(Candidate(
                url=dl_url,
                filename=fname,
                link_text=label,
                section="chinese_platform_supplement",
                match_rule="sciengine_open_api",
                source_tag="api",
                source_url=source_url or url_or_doi,
                covered_table_ids=supplementary_table_ids(f"{label} {raw_fname}", in_supplementary_context=True),
            ))
        return out, title
    except Exception:
        return [], ""


_LOCAL_ENV_TO_CONFIG_KEY = {
    "ELSEVIER_API_KEY": "elsevier_api_key",
    "IS_CAMPUS_NETWORK": "is_campus_network",
    "ELSEVIER_INSTTOKEN": "elsevier_insttoken",
    "UNPAYWALL_EMAIL": "unpaywall_email",
    "CROSSREF_EMAIL": "crossref_email",
    "OPENALEX_API_KEY": "openalex_api_key",
    "CORE_API_KEY": "core_api_key",
}


def configure_session_direct(session, proxy: Optional[str] = None):
    """Ensure direct connection by default (ignoring env proxies) unless an explicit proxy is supplied."""
    if session is None:
        return session
    if not proxy:
        try:
            session.trust_env = False
        except Exception:
            pass
    else:
        try:
            session.proxies = {"http": proxy, "https": proxy}
        except Exception:
            pass
    return session


def _parse_bool_flag(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in ("1", "true", "yes", "y")


def load_local_env_file(env_path: str) -> Dict[str, str]:
    """Parse a dotenv file into KEY -> value without mutating os.environ."""
    parsed: Dict[str, str] = {}
    if not env_path or not os.path.isfile(env_path):
        return parsed
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                elif " #" in val:
                    val = val.split(" #", 1)[0].rstrip()
                if key:
                    parsed[key] = val
    except Exception:
        pass
    return parsed


def load_local_skill_config(skill_root: Optional[str] = None) -> Dict[str, Any]:
    """Load local skill API/network configuration from JSON profiles, .env files, and os.environ.

    Precedence (lowest -> highest):
      ~/.scansci-pdf/config.json
      < ~/.journal_supp_downloader_profile/config.json
      < <skill_root>/config.json
      < <skill_root>/config.local.json
      < <skill_root>/.env
      < <skill_root>/.env.local
      < os.environ
    """
    root = os.path.abspath(skill_root) if skill_root else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = os.path.expanduser("~")
    cfg: Dict[str, Any] = {}

    json_paths = [
        os.path.join(home, ".scansci-pdf", "config.json"),
        os.path.join(home, ".journal_supp_downloader_profile", "config.json"),
        os.path.join(root, "config.json"),
        os.path.join(root, "config.local.json"),
    ]
    for jpath in json_paths:
        if not os.path.isfile(jpath):
            continue
        try:
            with open(jpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                for k, v in data.items():
                    if v not in (None, ""):
                        cfg[k] = v
        except Exception:
            pass

    for env_name in (".env", ".env.local"):
        env_map = load_local_env_file(os.path.join(root, env_name))
        for env_key, cfg_key in _LOCAL_ENV_TO_CONFIG_KEY.items():
            if env_key in env_map and env_map[env_key] != "":
                val = env_map[env_key]
                cfg[cfg_key] = _parse_bool_flag(val) if cfg_key == "is_campus_network" else val

    for env_key, cfg_key in _LOCAL_ENV_TO_CONFIG_KEY.items():
        if env_key in os.environ:
            raw_val = os.environ[env_key].strip()
            if cfg_key == "is_campus_network":
                cfg[cfg_key] = _parse_bool_flag(raw_val)
            else:
                cfg[cfg_key] = raw_val

    return cfg


def validate_title_similarity(target_title: str, fetched_title: str, min_similarity: float = 0.4) -> bool:
    """Check Jaccard similarity of non-stopword tokens between two article titles."""
    if not target_title or not fetched_title:
        return True
    stopwords = {"the", "a", "an", "of", "in", "and", "or", "on", "for", "with", "by", "at", "from", "study", "analysis"}
    words_target = set(re.findall(r"\w+", target_title.lower())) - stopwords
    words_fetched = set(re.findall(r"\w+", fetched_title.lower())) - stopwords
    if not words_target or not words_fetched:
        return True
    union = words_target.union(words_fetched)
    similarity = len(words_target.intersection(words_fetched)) / len(union) if union else 1.0
    return similarity >= min_similarity


