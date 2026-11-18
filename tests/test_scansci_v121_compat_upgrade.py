"""Tests for compatible upgrade of scansci-pdf v1.21.0 capabilities and adapters."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import oa_resolver as oa  # noqa: E402
import scansci_supp_downloader as ssd  # noqa: E402
import supp_finder as sf  # noqa: E402


def test_scansci_v121_imports_and_catalogs():
    assert ssd.HAS_SCANSCI is True
    assert getattr(ssd.scansci_pdf, "__version__", "") == "1.21.0"
    assert callable(ssd.load_webvpn_cookies)
    assert callable(ssd._scansci_is_challenge_page)
    assert callable(ssd._scansci_is_elsevier_crasolve)
    assert callable(ssd._scansci_webvpn_session_status)
    assert callable(ssd._scansci_is_campus_connector_mode)
    assert callable(ssd._scansci_make_ezproxy_url)

    from scansci_pdf.schools import list_schools

    school_names = {s.name for s in list_schools()}
    assert len(school_names) >= 145
    for added in ("浙江大学", "南京大学", "中南大学", "中国海洋大学", "北京中医药大学"):
        assert added in school_names

    carsi_path = (
        Path(__file__).resolve().parents[1]
        / "scansci-pdf"
        / "src"
        / "scansci_pdf"
        / "data"
        / "publisher_carsi.json"
    )
    carsi_data = json.loads(carsi_path.read_text(encoding="utf-8"))
    for key in ("sciencedirect", "wiley", "sage", "sage-cn", "asce", "cnki", "wanfang", "cqvip"):
        assert key in carsi_data


def test_cookie_loading_supports_instsci_vpnsci_carsi_and_ezproxy(tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "instsci-cookies.json").write_text(
        json.dumps([{"name": "wengine_vpn_ticket", "value": "tok_instsci"}]),
        encoding="utf-8",
    )
    (cache_dir / "vpnsci_cookies.json").write_text(
        json.dumps([{"name": "legacy_vpn", "value": "tok_legacy"}]),
        encoding="utf-8",
    )
    carsi_dir = tmp_path / "custom_carsi"
    carsi_dir.mkdir()
    (carsi_dir / "cnki.json").write_text(
        json.dumps([{"name": "LID", "value": "cnki123", "domain": ".cnki.net", "path": "/"}]),
        encoding="utf-8",
    )
    (cache_dir / "ezproxy_cookies.json").write_text(
        json.dumps([{"name": "ezproxy", "value": "ez123", "domain": "ezproxy.lib.edu", "path": "/"}]),
        encoding="utf-8",
    )

    cfg = {
        "cache_dir": str(cache_dir),
        "carsi_cookie_dir": str(carsi_dir),
        "instsci_enabled": True,
        "instsci_base_url": "https://webvpn.example.edu.cn",
        "ezproxy_enabled": True,
        "ezproxy_login_url": "https://ezproxy.lib.edu/login?url={url}",
        "cookie_path": str(tmp_path / "missing_pub.json"),
    }

    jar = ssd.load_vpnsci_cookies_unified(cfg)
    assert jar["wengine_vpn_ticket"] == "tok_instsci"
    assert jar["legacy_vpn"] == "tok_legacy"

    args = NS(no_cookies=False, no_vpn=False, proxy=None)
    session, play_cookies = ssd._setup_authenticated_session_and_cookies(cfg, args)

    cookie_names = {c.name for c in session.cookies}
    assert {"wengine_vpn_ticket", "legacy_vpn", "LID", "ezproxy"}.issubset(cookie_names)
    play_names = {c["name"] for c in play_cookies}
    assert {"wengine_vpn_ticket", "legacy_vpn", "LID", "ezproxy"}.issubset(play_names)
    # WebVPN enabled (non-campus-connector) -> self-signed TLS tolerance preserved
    assert session.verify is False


def test_tls_verification_default_true_and_opt_in_self_signed(tmp_path):
    cfg_default = {"cache_dir": str(tmp_path), "cookie_path": str(tmp_path / "none.json")}
    args = NS(no_cookies=True, no_vpn=False, proxy=None)
    sess_default, _ = ssd._setup_authenticated_session_and_cookies(cfg_default, args)
    assert sess_default.verify is True

    cfg_self_signed = {**cfg_default, "institutional_allow_self_signed": True}
    sess_self_signed, _ = ssd._setup_authenticated_session_and_cookies(cfg_self_signed, args)
    assert sess_self_signed.verify is False


def test_resolve_scansci_proxy_precedence_and_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("SCANSCI_PDF_PROXY", raising=False)
    assert ssd.resolve_scansci_proxy({"network_proxy": "http://127.0.0.1:7890"}) == "http://127.0.0.1:7890"
    assert (
        ssd.resolve_scansci_proxy({"network_proxy": "http://127.0.0.1:7890"}, cli_proxy="http://127.0.0.1:9000")
        == "http://127.0.0.1:9000"
    )
    monkeypatch.setenv("SCANSCI_PDF_PROXY", "socks5://127.0.0.1:1080")
    assert ssd.resolve_scansci_proxy({"network_proxy": "http://127.0.0.1:7890"}) == "socks5://127.0.0.1:1080"
    assert (
        ssd.resolve_scansci_proxy({"network_proxy": "http://127.0.0.1:7890"}, cli_proxy="http://127.0.0.1:9000")
        == "http://127.0.0.1:9000"
    )

    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    (skill_root / ".env").write_text("SCANSCI_PDF_PROXY=http://127.0.0.1:8888\n", encoding="utf-8")
    monkeypatch.delenv("SCANSCI_PDF_PROXY", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    local_cfg = sf.load_local_skill_config(skill_root=str(skill_root))
    assert local_cfg["network_proxy"] == "http://127.0.0.1:8888"


def test_elsevier_api_uses_campus_proxy_and_diagnoses_403(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SCANSCI_PDF_PROXY", raising=False)
    seen = []

    def fake_session_get(self, url, **kwargs):
        seen.append((url, dict(self.proxies), self.trust_env, kwargs))
        return NS(
            status_code=403,
            text="<service-error><statusText>Requestor configuration settings insufficient</statusText></service-error>",
        )

    with patch("requests.Session.get", fake_session_get):
        files, mmcs = ssd.try_elsevier_api_campus(
            "10.1016/j.test.2026.01",
            "S12345",
            str(tmp_path),
            {"elsevier_api_key": "k_test", "network_proxy": "http://127.0.0.1:8118"},
        )
    assert files == []
    assert mmcs == set()
    assert len(seen) == 1
    assert seen[0][1] == {"http": "http://127.0.0.1:8118", "https": "http://127.0.0.1:8118"}
    assert seen[0][2] is False
    out = capsys.readouterr().out
    assert "Requestor configuration settings insufficient" in out
    assert "ELSEVIER_INSTTOKEN" in out

    # Off-campus direct (no proxy) gives off-campus egress advice
    with patch("requests.Session.get", fake_session_get):
        ssd.try_elsevier_api_campus(
            "10.1016/j.test.2026.01",
            "S12345",
            str(tmp_path),
            {"elsevier_api_key": "k_test", "is_campus_network": True},
        )
    out2 = capsys.readouterr().out
    assert "SCANSCI_PDF_PROXY" in out2


def test_run_download_campus_connector_and_ezproxy_routing(tmp_path, monkeypatch, capsys):
    # 1. EasyConnect / aTrust mode skips convert_url and keeps direct publisher URL
    cfg_ec = {
        "vpnsci_enabled": True,
        "vpnsci_school": "测试高校",
        "network_proxy": "socks5://127.0.0.1:1080",
    }
    monkeypatch.delenv("SCANSCI_PDF_PROXY", raising=False)
    monkeypatch.setattr(ssd, "load_config", lambda: dict(cfg_ec))
    monkeypatch.setattr(ssd, "_scansci_is_campus_connector_mode", lambda c: True)
    monkeypatch.setattr(ssd, "_scansci_get_socks5_proxy", lambda c: "socks5://127.0.0.1:1080")
    monkeypatch.setattr(ssd, "convert_url", MagicMock(side_effect=AssertionError("convert_url must not run")))
    monkeypatch.setattr(ssd, "resolve_doi_url", lambda d: "https://pubs.acs.org/doi/10.1021/test")
    monkeypatch.setattr(ssd, "_setup_authenticated_session_and_cookies", lambda *a, **k: (NS(verify=True), []))
    seen_ec_urls = []
    monkeypatch.setattr(
        ssd,
        "fetch_page_html",
        lambda u, *a, **k: seen_ec_urls.append(u) or ("<html><title>Paper</title></html>", 200, u),
    )
    monkeypatch.setattr(ssd, "resolve_oa_info", lambda *a, **k: None)

    args = NS(
        url_or_doi="10.1021/test",
        output_dir=str(tmp_path),
        no_vpn=False,
        no_cookies=False,
        no_api=True,
        headful=False,
        skip_cdn=True,
        max_mmc=5,
        max_misses=2,
        proxy=None,
        tunnel_fail_threshold=3,
        phase_timeout=30.0,
        browser_goto_timeout=10.0,
        browser_fallback_budget=15.0,
        list_only=True,
        oa=False,
        no_oa=True,
        cdp=False,
        cdp_url=None,
        browserskill=False,
        bsk=False,
        bsk_auto=False,
        no_bsk=True,
        no_cdp=True,
        no_auto_headful=True,
        real_chrome=False,
        convert_markdown=False,
    )
    bsk_rt = NS(enabled=False, can_use_for_page=False, can_use_for_fallback=False)
    ssd.run_download(args, bsk_rt)
    out = capsys.readouterr().out
    assert "EasyConnect/aTrust SOCKS5 direct mode active" in out
    assert seen_ec_urls and all(u == "https://pubs.acs.org/doi/10.1021/test" for u in seen_ec_urls)

    # 2. EZProxy mode rewrites URL when WebVPN is disabled
    cfg_ez = {
        "vpnsci_enabled": False,
        "ezproxy_enabled": True,
        "ezproxy_login_url": "https://{host_dashed}.lib.ezproxy.example.edu",
    }
    seen_urls = []
    monkeypatch.setattr(ssd, "load_config", lambda: dict(cfg_ez))
    monkeypatch.setattr(ssd, "_scansci_is_campus_connector_mode", lambda c: False)
    monkeypatch.setattr(
        ssd,
        "fetch_page_html",
        lambda u, *a, **k: seen_urls.append(u) or ("<html><title>Paper</title></html>", 200, u),
    )
    ssd.run_download(args, bsk_rt)
    out_ez = capsys.readouterr().out
    assert "EZProxy URL:" in out_ez
    assert seen_urls and all(u == "https://pubs-acs-org.lib.ezproxy.example.edu/doi/10.1021/test" for u in seen_urls)


def test_openaire_query_and_third_tier_oa_fallback():
    xml_body = (
        "<response><results><result><metadata><oaf:entity><oaf:result>"
        "<children><result><instance><webresource><url>https://zenodo.org/records/999999/files/supp.pdf</url></webresource></instance></result>"
        "<result><instance><webresource><url>https://zenodo.org/records/999999</url></webresource></instance></result>"
        "<result><instance><webresource><url>https://hal.science/hal-01234567</url></webresource></instance></result>"
        "</children></oaf:result></oaf:entity></metadata></result></results></response>"
    )
    fake_sess = NS(get=lambda url, **kw: NS(status_code=200, text=xml_body))
    res_direct = oa.query_openaire("10.1000/openaire-test", session=fake_sess)
    assert res_direct is not None
    assert res_direct.source == "openaire"
    assert res_direct.oa_landing_urls == [
        "https://zenodo.org/records/999999",
        "https://hal.science/hal-01234567",
    ]

    with patch("oa_resolver.query_unpaywall", return_value=None), patch(
        "oa_resolver.query_openalex", return_value=None
    ), patch("oa_resolver.query_openaire", return_value=res_direct) as m_openaire:
        merged = oa.resolve_oa_info("10.1000/openaire-test")
    assert m_openaire.called
    assert merged.source == "openaire"
    assert "https://zenodo.org/records/999999" in merged.oa_landing_urls
