#!/usr/bin/env python3
"""
Open Access & Publisher Penetration Resolver (Unpaywall & OpenAlex)
===================================================================
Provides zero-friction DOI resolution, title extraction, and Open Access
repository mirror discovery (e.g. PubMed Central / PMC, Europe PMC, Zenodo)
to bypass publisher paywalls and anti-bot verification when retrieving
supplementary materials.
"""

import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import requests


DOI_REGEX = re.compile(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)

# Default contact email for Unpaywall API polite pool
DEFAULT_UNPAYWALL_EMAIL = os.environ.get(
    "UNPAYWALL_EMAIL", "supp_downloader@academic-research.org"
)


@dataclass
class OAResult:
    """Standardized metadata and Open Access discovery result."""
    doi: str = ""
    title: str = ""
    is_oa: bool = False
    oa_status: str = "closed"
    publisher_url: str = ""
    oa_landing_urls: List[str] = field(default_factory=list)
    source: str = "none"
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def has_oa_mirror(self) -> bool:
        return bool(self.oa_landing_urls)


def extract_doi(doi_or_url: str) -> Optional[str]:
    """Extract a clean DOI string from raw DOI or URL input."""
    if not doi_or_url:
        return None
    cleaned = urllib.parse.unquote(doi_or_url.strip())
    m = DOI_REGEX.search(cleaned)
    if m:
        # Strip trailing punctuation, brackets, quotes, and trailing slashes
        doi = m.group(0).rstrip(".,;)>]/\"'")
        return doi
    return None


def extract_doi_from_html(html: str) -> Optional[str]:
    """Extract article DOI from HTML metadata tags, JSON-LD, or top text."""
    if not html:
        return None
    meta_patterns = [
        r'<meta[^>]+(?:name|property)=["\'](?:citation_doi|dc\.identifier|doi|prism\.doi)["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\'](?:citation_doi|dc\.identifier|doi|prism\.doi)["\']',
        r'["\'](?:doi|DOI)["\']\s*:\s*["\'](10\.\d{4,9}/[^"\']+)["\']',
    ]
    for pat in meta_patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            doi = extract_doi(m.group(1))
            if doi:
                return doi
    # Fallback to search in initial 60KB (head/metadata chunk)
    return extract_doi(html[:60000])


def is_doi(input_str: str) -> bool:
    """Check if the given string represents or contains a DOI."""
    return bool(extract_doi(input_str))


def _score_oa_url(url: str) -> int:
    """
    Score and prioritize OA repository landing URLs.
    PMC and Europe PMC have the highest fidelity open supplementary data hosting.
    """
    u = (url or "").lower()
    score = 0
    if "ncbi.nlm.nih.gov/pmc" in u or "pmc.ncbi.nlm.nih.gov" in u:
        score += 100
    elif "europepmc.org" in u:
        score += 80
    elif "zenodo.org" in u or "figshare.com" in u or "osf.io" in u:
        score += 60
    elif "arxiv.org" in u:
        score += 40
    elif any(ir in u for ir in ["repo", "dspace", "eprints", "hal.science"]):
        score += 30
    return score


def query_unpaywall(
    doi: str,
    email: Optional[str] = None,
    timeout: int = 10,
    session: Optional[requests.Session] = None,
) -> Optional[OAResult]:
    """
    Query Unpaywall REST API for DOI metadata and OA locations.
    Endpoint: https://api.unpaywall.org/v2/{doi}?email={email}
    """
    clean_doi = extract_doi(doi)
    if not clean_doi:
        return None

    email = email or DEFAULT_UNPAYWALL_EMAIL
    encoded_doi = urllib.parse.quote(clean_doi, safe="")
    api_url = f"https://api.unpaywall.org/v2/{encoded_doi}?email={email}"

    http = session or requests
    try:
        resp = http.get(
            api_url,
            timeout=timeout,
            headers={"User-Agent": "journal-supp-downloader/1.0 (academic tool)"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, dict) or data.get("error"):
        return None

    doi_val = data.get("doi") or clean_doi
    title = data.get("title") or ""
    is_oa = bool(data.get("is_oa"))
    oa_status = str(data.get("oa_status") or "closed")

    # Canonical publisher landing URL
    publisher_url = ""
    best_loc = data.get("best_oa_location") or {}
    if isinstance(best_loc, dict):
        publisher_url = best_loc.get("url_for_landing_page") or ""
    if not publisher_url:
        publisher_url = data.get("doi_url") or f"https://doi.org/{doi_val}"

    # Extract OA repository landing URLs
    oa_urls: List[str] = []
    seen = set()

    for loc in data.get("oa_locations", []):
        if not isinstance(loc, dict):
            continue
        landing = loc.get("url_for_landing_page") or loc.get("url") or ""
        if landing and landing not in seen:
            # Exclude doi.org itself from OA mirrors unless it's the only one
            if "doi.org" in landing:
                continue
            seen.add(landing)
            oa_urls.append(landing)

    # Sort OA URLs by repository quality (PMC / Europe PMC first)
    oa_urls.sort(key=_score_oa_url, reverse=True)

    return OAResult(
        doi=doi_val,
        title=title,
        is_oa=is_oa,
        oa_status=oa_status,
        publisher_url=publisher_url,
        oa_landing_urls=oa_urls,
        source="unpaywall",
        extra={"publisher": data.get("publisher", "")},
    )


def query_openalex(
    doi: str,
    timeout: int = 10,
    session: Optional[requests.Session] = None,
) -> Optional[OAResult]:
    """
    Query OpenAlex API for DOI metadata and OA locations.
    Endpoint: https://api.openalex.org/works/doi:{doi}
    """
    clean_doi = extract_doi(doi)
    if not clean_doi:
        return None

    encoded_doi = urllib.parse.quote(clean_doi, safe="")
    api_url = f"https://api.openalex.org/works/doi:{encoded_doi}"

    http = session or requests
    try:
        resp = http.get(
            api_url,
            timeout=timeout,
            headers={"User-Agent": "journal-supp-downloader/1.0 (mailto:supp_downloader@academic-research.org)"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    if not isinstance(data, dict) or not data.get("id"):
        return None

    doi_val = data.get("doi") or clean_doi
    if doi_val.startswith("https://doi.org/"):
        doi_val = doi_val[len("https://doi.org/"):]

    title = data.get("title") or ""
    oa_info = data.get("open_access", {})
    is_oa = bool(oa_info.get("is_oa"))
    oa_status = str(oa_info.get("oa_status") or "closed")

    # Primary publisher landing page
    primary_loc = data.get("primary_location") or {}
    if not isinstance(primary_loc, dict):
        primary_loc = {}
    publisher_url = primary_loc.get("landing_page_url") or f"https://doi.org/{clean_doi}"

    # Extract OA repository landing URLs
    oa_urls: List[str] = []
    seen = set()

    for loc in data.get("locations", []):
        if not isinstance(loc, dict):
            continue
        landing = loc.get("landing_page_url") or loc.get("pdf_url") or ""
        if landing and landing not in seen:
            if "doi.org" in landing:
                continue
            seen.add(landing)
            oa_urls.append(landing)

    oa_urls.sort(key=_score_oa_url, reverse=True)

    return OAResult(
        doi=doi_val,
        title=title,
        is_oa=is_oa,
        oa_status=oa_status,
        publisher_url=publisher_url,
        oa_landing_urls=oa_urls,
        source="openalex",
    )


def resolve_oa_info(
    doi_or_url: str,
    prefer_openalex: bool = False,
    timeout: int = 10,
    session: Optional[requests.Session] = None,
) -> OAResult:
    """
    Unified entry point: resolve a DOI with Unpaywall and OpenAlex with automatic fallback.
    Returns OAResult with title, canonical publisher URL, and all discovered OA mirrors.
    """
    doi = extract_doi(doi_or_url)
    if not doi:
        return OAResult(
            doi="",
            publisher_url=doi_or_url,
            source="passthrough",
        )

    primary_fn, fallback_fn = (
        (query_openalex, query_unpaywall)
        if prefer_openalex
        else (query_unpaywall, query_openalex)
    )

    res1 = primary_fn(doi, timeout=timeout, session=session)
    if not res1 or not res1.has_oa_mirror:
        res2 = fallback_fn(doi, timeout=timeout, session=session)
        if res2:
            if not res1:
                return res2
            # Merge: take title/publisher if missing, and combine OA landing URLs
            if not res1.title and res2.title:
                res1.title = res2.title
            if not res1.publisher_url and res2.publisher_url:
                res1.publisher_url = res2.publisher_url
            for u in res2.oa_landing_urls:
                if u not in res1.oa_landing_urls:
                    res1.oa_landing_urls.append(u)
            res1.oa_landing_urls.sort(key=_score_oa_url, reverse=True)
            if res2.is_oa:
                res1.is_oa = True
            return res1

    if not res1:
        return OAResult(
            doi=doi,
            publisher_url=f"https://doi.org/{doi}",
            source="none",
        )

    return res1


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Query Unpaywall and OpenAlex for OA mirrors of academic DOIs")
    parser.add_argument("doi_or_url", help="DOI or URL to resolve")
    parser.add_argument("--openalex", action="store_true", help="Prefer OpenAlex API over Unpaywall")
    parser.add_argument("--json", action="store_true", help="Output full result as JSON")
    args = parser.parse_args()

    result = resolve_oa_info(args.doi_or_url, prefer_openalex=args.openalex)
    if args.json:
        print(json.dumps({
            "doi": result.doi,
            "title": result.title,
            "is_oa": result.is_oa,
            "oa_status": result.oa_status,
            "publisher_url": result.publisher_url,
            "oa_landing_urls": result.oa_landing_urls,
            "source": result.source,
        }, indent=2, ensure_ascii=False))
    else:
        print(f"DOI:           {result.doi or 'N/A'}")
        print(f"Title:         {result.title or 'N/A'}")
        print(f"Open Access:   {result.is_oa} ({result.oa_status})")
        print(f"Publisher URL: {result.publisher_url or 'N/A'}")
        print(f"Source:        {result.source}")
        print(f"OA Mirrors ({len(result.oa_landing_urls)}):")
        for u in result.oa_landing_urls:
            print(f"  - {u}")


if __name__ == "__main__":
    main()
