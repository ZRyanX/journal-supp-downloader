"""Semantic Scholar Open Access API source."""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any

from ..network import fetch_json, polite_delay
from ..pdf_utils import download_pdf, is_plausible_pdf_url


def try_semanticscholar(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    q = urllib.parse.quote(doi, safe="")
    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{q}?fields=openAccessPdf,externalIds"
    payload = fetch_json(url, config)
    if not payload:
        return None

    oa_pdf = payload.get("openAccessPdf")
    if isinstance(oa_pdf, dict):
        pdf_url = oa_pdf.get("url", "")
        if pdf_url and is_plausible_pdf_url(pdf_url):
            polite_delay(config)
            result = download_pdf(pdf_url, output_path, config, "SemanticScholar")
            if result:
                result["doi"] = doi
                result["identifier"] = doi
                return result

    ext_ids = payload.get("externalIds", {})
    if isinstance(ext_ids, dict):
        arxiv_id = ext_ids.get("ArXiv")
        if arxiv_id:
            arxiv_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
            polite_delay(config)
            result = download_pdf(arxiv_url, output_path, config, "SemanticScholar(arXiv)", require_pdf_like_url=False)
            if result:
                result["doi"] = doi
                result["identifier"] = doi
                return result

    return None
