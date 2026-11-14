import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import oa_resolver as oa
import repository_resolver as rr
import scansci_supp_downloader as ssd
import supp_finder as sf


def test_load_local_skill_config_reads_dotenv_and_respects_env_precedence(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    (fake_home / ".scansci-pdf").mkdir(parents=True)
    (fake_home / ".journal_supp_downloader_profile").mkdir(parents=True)
    (fake_home / ".scansci-pdf" / "config.json").write_text(
        json.dumps({"elsevier_api_key": "scansci_k", "is_campus_network": False}),
        encoding="utf-8",
    )
    (fake_home / ".journal_supp_downloader_profile" / "config.json").write_text(
        json.dumps({"unpaywall_email": "profile@lab.org"}),
        encoding="utf-8",
    )
    skill_root = tmp_path / "skill"
    skill_root.mkdir()
    (skill_root / ".env").write_text(
        "# comment\n"
        "ELSEVIER_API_KEY='dotenv_k'\n"
        "IS_CAMPUS_NETWORK=true # inline note\n",
        encoding="utf-8",
    )

    for k in ("ELSEVIER_API_KEY", "IS_CAMPUS_NETWORK", "UNPAYWALL_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(fake_home)))

    cfg = sf.load_local_skill_config(skill_root=str(skill_root))
    assert cfg["elsevier_api_key"] == "dotenv_k"
    assert cfg["is_campus_network"] is True
    assert "fallback_proxy" not in cfg
    assert cfg["unpaywall_email"] == "profile@lab.org"

    monkeypatch.setenv("ELSEVIER_API_KEY", "env_override_k")
    cfg2 = sf.load_local_skill_config(skill_root=str(skill_root))
    assert cfg2["elsevier_api_key"] == "env_override_k"


def test_validate_title_similarity_and_oa_local_email(monkeypatch):
    assert sf.validate_title_similarity(
        "Geochemical constraints on antimony mineralization",
        "Geochemical Constraints on Antimony Mineralization in South China",
    ) is True
    assert sf.validate_title_similarity(
        "Deep mantle seismic anisotropy",
        "Machine learning crystal plasticity survey",
    ) is False

    seen_urls = []

    def _fake_get(url, **kwargs):
        seen_urls.append(url)
        return NS(status_code=200, json=lambda: {"doi": "10.1000/test", "title": "Paper", "is_oa": False})

    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    monkeypatch.setattr(sf, "load_local_skill_config", lambda: {"unpaywall_email": "custom@inst.edu"})
    res = oa.query_unpaywall("10.1000/test", session=NS(get=_fake_get))
    assert res is not None
    assert "email=custom@inst.edu" in seen_urls[0]


def test_figshare_api_expansion_and_translate_goog_fallback():
    mirror = rr.translate_goog_mirror_url("https://api.figshare.com/v2/articles/12349100")
    assert mirror.startswith("https://api-figshare-com.translate.goog/v2/articles/12349100")
    routes = list(rr.iter_download_fallback_routes(
        "https://ndownloader.figstatic.com/files/22759988",
        proxy=None,
    ))
    assert routes == [("https://ndownloader-figstatic-com.translate.goog/files/22759988?_x_tr_sl=en&_x_tr_tl=zh-CN&_x_tr_hl=en", None)]

    calls = []

    class _FakeSession:
        def get(self, url, **kwargs):
            calls.append((url, kwargs.get("proxies")))
            if "translate.goog" not in url:
                return NS(status_code=403, json=lambda: {})
            return NS(
                status_code=200,
                json=lambda: {
                    "title": "Supplemental Dataset",
                    "files": [
                        {
                            "id": 22759988,
                            "name": "G31272_Table_S1.xlsx",
                            "download_url": "https://ndownloader.figshare.com/files/22759988",
                            "computed_md5": "deadbeef",
                            "size": 10240,
                        }
                    ],
                },
            )

        def close(self):
            pass

    landing = sf.Candidate(
        url="https://figshare.com/articles/journal_contribution/Supplemental_material/12349100",
        filename="Supplemental_material.html",
        source_url="https://pubs.geoscienceworld.org/gsa/geology/article/38/10/927/130381",
    )
    expanded = rr.expand_repository_candidates([landing], session=_FakeSession())
    assert len(expanded) == 1
    assert expanded[0].filename == "G31272_Table_S1.xlsx"
    assert expanded[0].url == "https://ndownloader.figshare.com/files/22759988"
    assert expanded[0].extra_info.get("structural_supp") is True
    assert expanded[0].extra_info.get("computed_md5") == "deadbeef"
    assert any("api-figshare-com.translate.goog" in u for u, _ in calls)


def test_gsa_crossref_figshare_and_diva_oai_discovery():
    assert rr.is_gsa_or_gsw_target("10.1130/B37029.1") is True
    assert rr.is_diva_target("diva2:193288") is True
    assert rr.is_diva_target("urn:nbn:se:uu:diva-12345") is True

    class _FakeGsaSession:
        def get(self, url, **kwargs):
            if "api.crossref.org/works/10.1130/B37029.1" in url:
                return NS(
                    status_code=200,
                    json=lambda: {
                        "message": {
                            "title": ["Tectonic evolution of Basin X"],
                            "relation": {"has-supplement": [{"id": "10.1130/GSAB.S.25000001"}]},
                        }
                    },
                )
            if "doi.org/10.1130/GSAB.S.25000001" in url:
                return NS(
                    status_code=302,
                    headers={"Location": "https://gsapubs.figshare.com/articles/dataset/Supp/25000001"},
                )
            if "api.figshare.com/v2/articles/25000001" in url:
                return NS(
                    status_code=200,
                    json=lambda: {
                        "title": "Supp",
                        "files": [{"name": "Table_S1.csv", "download_url": "https://ndownloader.figshare.com/files/999"}],
                    },
                )
            return NS(status_code=404, json=lambda: {})

        def close(self):
            pass

    cands, title = rr.discover_gsa_figshare_candidates("10.1130/B37029.1", session=_FakeGsaSession())
    assert title == "Tectonic evolution of Basin X"
    assert [c.filename for c in cands] == ["Table_S1.csv"]

    class _FakeDivaSession:
        def get(self, url, **kwargs):
            if "urn.kb.se/resolve" in url:
                return NS(status_code=302, headers={"Location": "https://uu.diva-portal.org/smash/record.jsf?pid=diva2:193288"})
            if "/dice/oai?" in url:
                return NS(status_code=200, text="<record><dc:title>Nordic Ore Deposits</dc:title></record>")
            if "/smash/get/diva2:193288/FULLTEXT01.pdf" in url:
                return NS(status_code=200, iter_content=lambda n: iter([b"%PDF-1.7"]), close=lambda: None)
            return NS(status_code=404, text="", iter_content=lambda n: iter([]), close=lambda: None)

        def close(self):
            pass

    dcands, dtitle = rr.discover_diva_candidates("urn:nbn:se:uu:diva-12345", session=_FakeDivaSession())
    assert dtitle == "Nordic Ore Deposits"
    assert [c.filename for c in dcands] == ["FULLTEXT01.pdf"]


def test_scansci_load_config_merges_local_config(monkeypatch):
    monkeypatch.setattr(ssd, "_scansci_load_config", lambda: {"elsevier_api_key": "", "is_campus_network": False})
    monkeypatch.setattr(ssd, "load_local_skill_config", lambda: {"elsevier_api_key": "local_k", "is_campus_network": True})
    cfg = ssd.load_config()
    assert cfg["elsevier_api_key"] == "local_k"
    assert cfg["is_campus_network"] is True


def test_direct_sessions_ignore_env_proxy_by_default(monkeypatch):
    import requests
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    s_direct, owned_d = rr._resolve_http_session()
    try:
        assert owned_d is True
        assert s_direct.trust_env is False
        assert s_direct.proxies == {}
    finally:
        s_direct.close()

    s_explicit = sf.configure_session_direct(requests.Session(), proxy="http://127.0.0.1:8080")
    try:
        assert s_explicit.proxies == {"http": "http://127.0.0.1:8080", "https": "http://127.0.0.1:8080"}
    finally:
        s_explicit.close()

    routes = list(rr._iter_route_attempts("https://api.figshare.com/v2/articles/12349100"))
    assert all(px is None for _, px, _ in routes)
