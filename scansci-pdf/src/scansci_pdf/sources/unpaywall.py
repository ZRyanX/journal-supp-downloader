"""Unpaywall source for open-access papers."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from ..config import DEFAULT_CONFIG
from ..network import fetch_json, polite_delay
from ..pdf_utils import download_pdf, is_plausible_pdf_url, dedupe


def extract_unpaywall_pdf_candidates(payload: dict[str, Any]) -> list[str]:
    """Extract PDF URLs prioritized: best_oa > repository > publisher.

    Repository URLs (EuropePMC, arXiv, PMC) are more likely to be freely
    accessible than publisher URLs which often require subscription.
    """
    repo_urls: list[str] = []
    publisher_urls: list[str] = []

    # best_oa_location first
    best = payload.get("best_oa_location")
    if isinstance(best, dict):
        url = best.get("url_for_pdf") or ""
        if is_plausible_pdf_url(url):
            if best.get("host_type") == "publisher":
                publisher_urls.append(url)
            else:
                repo_urls.append(url)

    # Separate by host_type
    all_locations = payload.get("oa_locations", [])
    if isinstance(all_locations, list):
        for loc in all_locations:
            if not isinstance(loc, dict):
                continue
            url = loc.get("url_for_pdf") or ""
            if not is_plausible_pdf_url(url):
                continue
            is_publisher = loc.get("host_type") == "publisher"
            if is_publisher:
                if url not in publisher_urls:
                    publisher_urls.append(url)
            else:
                if url not in repo_urls:
                    repo_urls.append(url)

    # Repository URLs first (more likely free), then publisher URLs
    return dedupe(repo_urls + publisher_urls)


def try_unpaywall(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    email = urllib.parse.quote(str(config.get("email") or DEFAULT_CONFIG["email"]))
    q = urllib.parse.quote(doi, safe="")
    url = f"https://api.unpaywall.org/v2/{q}?email={email}"
    payload = fetch_json(url, config)
    if not payload:
        return None

    candidates = extract_unpaywall_pdf_candidates(payload)
    for pdf_url in candidates[: int(config.get("max_unpaywall_candidates", 2))]:
        polite_delay(config)
        result = download_pdf(pdf_url, output_path, config, "Unpaywall")
        if result:
            result["doi"] = doi
            result["identifier"] = doi
            return result
    return None
