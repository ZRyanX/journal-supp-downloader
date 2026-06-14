"""Standalone CARSI download source — decoupled from WebVPN.

CARSI can now be used independently: set carsi_enabled=True and carsi_idp_name,
and it will be tried in the download tier system without requiring vpnsci_enabled.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..log import get_logger

log = get_logger()


def try_carsi(doi: str, output_path: Path, config: dict[str, Any]) -> dict[str, Any] | None:
    """Try downloading via CARSI federated auth without WebVPN dependency.

    Returns a result dict on success, None on failure.
    """
    if not config.get("carsi_enabled", False):
        return None

    idp_name = config.get("carsi_idp_name", "").strip()
    if not idp_name:
        return None

    try:
        from .carsi import CARSIClient, detect_publisher
        from .vpnsci import _resolve_doi_url

        resolved_url = _resolve_doi_url(doi)
        if not resolved_url:
            resolved_url = f"https://doi.org/{doi}"

        publisher = detect_publisher(resolved_url)
        if not publisher:
            return None

        log.info(f"   [CARSI] Trying {publisher} via {idp_name} for {doi}")
        client = CARSIClient(config)

        # Try stealth browser first (stealth browser, handles Cloudflare)
        result = client.download_via_camofox(doi, resolved_url, output_path)
        if result:
            return result

        # Fallback to Selenium browser
        log.info(f"   [CARSI] Trying selenium download for {doi}")
        result = client.download_via_browser(doi, resolved_url, output_path)
        if result:
            return result
    except ImportError:
        return None
    except Exception as e:
        log.info(f"   [CARSI] {e}")
    return None
