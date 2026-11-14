# Journal Supplementary Data Downloader

> 🌐 Language: **English** | [简体中文 (Chinese Documentation)](README.zh-CN.md)

**Journal Supplementary Data Downloader** is an automated CLI tool and AI Agent skill for discovering, downloading, and verifying supplementary data files and tables (`xlsx`, `xls`, `csv`, `zip`, `docx`, `pdf`, etc.) from academic publishers and external research data repositories.

It runs **direct-network by default** (ignoring stale proxy environment variables unless `--proxy` is passed) and combines static HTML extraction, publisher-specific extractors, multi-publisher Crossref host fallback, deep repository resolution, and a tiered live-browser recovery cascade (`Tier 3-Auto`: BrowserSkill → Chrome CDP → Headful Chrome).

Supported platforms include **ScienceDirect / Elsevier**, **SpringerLink / Nature**, **Wiley**, **Silverchair / GeoScienceWorld / Lyell Collection**, **Atypon**, **RSC**, **Cambridge**, **Frontiers**, **Copernicus**, **PeerJ**, **MDPI**, **SciEngine**, **OJS / J-STAGE / Chinese journals**, **PMC / Europe PMC**, **Figshare**, **ScienceBase**, **GSA**, and **DiVA**.

Built with core components from [scansci-pdf](https://github.com/Rimagination/scansci-pdf) so it works both standalone and alongside a global `scansci-pdf` setup.

---

## Table of Contents
1. [Key Features](#key-features)
2. [Requirements & Installation](#requirements--installation)
3. [Usage](#usage)
4. [Authentication & Configuration](#authentication--configuration)
   - [Direct Network & Local Config (`.env` / `config.json`)](#1-direct-network--local-config-env--configjson)
   - [Elsevier API Key](#2-elsevier-api-key)
   - [Institutional Access & Campus Network](#3-institutional-access--campus-network)
   - [Browser Cookie Injection](#4-browser-cookie-injection)
5. [Project Structure](#project-structure)
6. [FAQ](#faq)
7. [Acknowledgements & References](#acknowledgements--references)
8. [Completeness & Recovery Boundaries](#completeness--recovery-boundaries)

---

## Key Features

* **Multi-Tier Discovery & `Tier 3-Auto` Recovery**:
  - **Tier A**: Elsevier API retrieval (fast; requires Elsevier API key + campus IP or InstToken).
  - **Tier 0**: Elsevier CDN direct probing (zero-auth probing for `mmc*.xlsx/pdf/zip`).
  - **Tier 1**: Static page & `Requests` session extraction (with Cookies, direct-network retry, ISO-8859-1 CJK filename repair, and magic-byte `.bin` extension recovery).
  - **Tier 2**: Bounded Scrapling stealth rendering (`stealthy_fetch_bounded`) with automatic blocking of hanging third-party domains (such as `ajax.googleapis.com` on GeoScienceWorld / Silverchair pages).
  - **Tier 3 / `Tier 3-Auto` (Cascading Live Browser Fallback)**: When fast HTTP/headless tiers are blocked (`401`/`403`/Cloudflare) or return incomplete candidates, automatically escalates within `--browser-fallback-budget` (default `60s`) and `--phase-timeout` (default `150s`): **connected Tencent BrowserSkill (`bsk`) → active local Chrome `--cdp` → standalone headful Chrome (`--headful`)** (each opt-outable via `--no-bsk`, `--no-cdp`, `--no-auto-headful`).
  - **Crossref Multi-Publisher Fallback (`[chooser-alt]`)**: When the primary publisher yields no candidates or lacks subscription access (e.g., Lyell Collection `10.1144/`), automatically queries Crossref for alternate publisher hosts for the same DOI (e.g., GeoScienceWorld).
  - **Tier OA & External Repository Resolution**: Integrates **Unpaywall** & **OpenAlex** (PMC / Europe PMC) alongside deep extractors for **Figshare** (REST API + `__APOLLO_STATE__`), **ScienceBase** (JSON + malformed bare DOI repair like `http://10.5066/...`), **GSA** (`10.1130/`), and **DiVA** (`diva-portal.org` / `diva2:` / `urn:nbn:se:` OAI-PMH & mirrors), reusing a shared browser context via `RepoBrowserSession` and downloading through Playwright `context.request`.
* **Publisher-Specific Extractors & Completeness Guardrails**: Dedicated extractors for Silverchair, Atypon, RSC, Cambridge, Frontiers, Copernicus, PeerJ, MDPI, OJS/J-STAGE/Chinese journals, Nuxt/JSON, and SciEngine; SPA shell detection; noise filtering for figures/references; and evidence checks comparing discovered files against declared file counts and `S1..Sn` table numbers.
* **Optional MarkItDown Conversion**: Pass `--convert-markdown` (or `--markitdown` in `journal_downloader.py`) to generate readable Markdown copies of downloaded `.docx`, `.xlsx`, `.xls`, and `.pdf` supplements while keeping original files untouched — including **multi-row merged Excel header flattening** and **recursive conversion of tables inside `.zip` archives**. Requires `pip install -r requirements-conversion.txt` (legacy `.doc`/`.ppt` excluded).
* **Automatic Folder Naming & Archiving**: Queries Unpaywall / OpenAlex / Crossref metadata to normalize output folder names by paper title.

---

## Requirements & Installation

Requires **Python 3.11** or higher.

1. **Install core Python dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Install Scrapling browser runtime (for Cloudflare / dynamic page bypass)**:
   ```bash
   scrapling install
   ```

3. *(Optional)* **Install MarkItDown format conversion dependencies**:
   ```bash
   pip install -r requirements-conversion.txt
   ```

---

## Usage

> New to CLI tools? Point local AI coding agents (Claude Code, Codex, Antigravity, OpenCode, etc.) at this repository and [SKILL.md](SKILL.md) to drive setup and batch runs automatically.

Both entry points run **direct-network by default** (system/environment proxy variables are ignored unless `--proxy` is explicitly specified).

### 1. Lightweight Downloader (`journal_downloader.py`)
Best for general publishers, standalone article URLs, and interactive browser recovery:
```bash
# Basic usage (outputs to ./journal_downloads; auto-triggers Tier 3-Auto on failure)
python scripts/journal_downloader.py "https://doi.org/10.1016/j.oregeorev.2022.104949"

# Show browser window / force system Chrome / use explicit proxy
python scripts/journal_downloader.py "<url_or_doi>" --headful --real-chrome
python scripts/journal_downloader.py "<url_or_doi>" --proxy "http://127.0.0.1:7890"

# List candidate attachments only / convert downloaded supplements to Markdown
python scripts/journal_downloader.py "<url_or_doi>" --list-only
python scripts/journal_downloader.py "<url_or_doi>" --convert-markdown

# Phase & browser fallback budgets (defaults: 150s phase, 30s goto, 60s browser fallback)
python scripts/journal_downloader.py "<url_or_doi>" --phase-timeout 180 --browser-goto-timeout 30 --browser-fallback-budget 60
python scripts/journal_downloader.py "<url_or_doi>" --no-bsk --no-cdp --no-auto-headful

# Explicitly attach to Chrome CDP or Tencent BrowserSkill (optionally borrowing an open tab)
python scripts/journal_downloader.py "<url_or_doi>" --cdp
python scripts/journal_downloader.py "<url_or_doi>" --bsk
python scripts/journal_downloader.py "<url_or_doi>" --bsk --bsk-tab <tab-id>
```

### 2. Advanced Integrated Downloader (`scansci_supp_downloader.py`)
Full-featured pipeline combining Elsevier API, CDN probing, cookie injection, Crossref `[chooser-alt]` fallback, repository expansion, and `Tier 3-Auto`:
```bash
# Download by DOI or publisher URL
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349"

# Skip Elsevier API; probe CDN / article page with custom limits
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349" --no-api --max-mmc 25 --max-misses 4

# Tune tunnel circuit-breaker threshold and timeouts
python scripts/scansci_supp_downloader.py "<url_or_doi>" --tunnel-fail-threshold 3 --phase-timeout 150 --browser-fallback-budget 60

# Explicitly attach to Chrome CDP or Tencent BrowserSkill
python scripts/scansci_supp_downloader.py "<url_or_doi>" --cdp
python scripts/scansci_supp_downloader.py "<url_or_doi>" --bsk
python scripts/scansci_supp_downloader.py "<url_or_doi>" --bsk --bsk-tab <tab-id>
```

### 3. Tencent BrowserSkill Diagnostics & Adapter (`browserskill_adapter.py`)

[Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill) is an optional external runtime (`bsk` CLI + browser extension). See [references/browserskill.md](references/browserskill.md) for mode details, compatibility, and troubleshooting.

**Behavior**: When neither `--bsk` nor `--no-bsk` is passed, standard HTTP/headless tiers run first; only if `Tier 3-Auto` triggers and an active `bsk` daemon + browser connection is detected will it automatically attach (`--no-bsk` disables this completely). Passing `--bsk` prioritizes BrowserSkill on initial fetch; `--bsk-auto` opts into on-demand recovery explicitly; specifying `--bsk-browser` or `--bsk-tab` enables BrowserSkill automatically.

```bash
# On-demand recovery mode
python scripts/journal_downloader.py "<doi_or_url>" --bsk-auto
python scripts/scansci_supp_downloader.py "<doi_or_url>" --bsk-auto

# Structured health check (exits 0 only when CLI, daemon, and browser are all ready)
python scripts/browserskill_adapter.py status --json
python scripts/browserskill_adapter.py sessions
python scripts/browserskill_adapter.py tabs --scope user

# Standalone DOM fetch / borrow an open user tab
python scripts/browserskill_adapter.py fetch "<doi_or_url>" -o ./page.html
python scripts/browserskill_adapter.py borrow-fetch <tab_id> -o ./page.html
```

---

## Authentication & Configuration

### 1. Direct Network & Local Config (`.env` / `config.json`)

The tool operates **direct-network by default** (no global proxy setup needed; environment proxy variables are ignored unless `--proxy` is explicitly passed). Configure credentials via any of the following (`.gitignore` already excludes `.env`, `config.json`, and `*cookies.json`):

1. **Repository `.env` file** *(recommended)*: copy `.env.example` to `.env`:
   ```dotenv
   ELSEVIER_API_KEY=your_elsevier_api_key
   IS_CAMPUS_NETWORK=false
   ELSEVIER_INSTTOKEN=
   UNPAYWALL_EMAIL=
   CROSSREF_EMAIL=
   ```
2. **User profile config `~/.journal_supp_downloader_profile/config.json`**: JSON keys `elsevier_api_key`, `is_campus_network`, `elsevier_insttoken`, `unpaywall_email`, `crossref_email`.
3. **Environment variables**: uppercase environment variables take precedence.

---

### 2. Elsevier API Key

For Elsevier / ScienceDirect journals, the API tier provides the fastest and most reliable access:
1. Visit the [Elsevier Developer Portal](https://dev.elsevier.com/) and sign in.
2. Open **My API Key** → **Create API Key**.
3. Fill in a label and website URL, accept terms, and copy your 32-character API key.
4. Add it to `.env`, `config.json`, or your shell environment:
   * **PowerShell**: `$env:ELSEVIER_API_KEY="YOUR_API_KEY"`
   * **Linux / macOS**: `export ELSEVIER_API_KEY="YOUR_API_KEY"`

---

### 3. Institutional Access & Campus Network

* **Option A — Campus IP / University VPN** *(recommended)*: When connected to campus IP ranges or university VPN, set `IS_CAMPUS_NETWORK=true`.
* **Option B — Institutional Token (InstToken)**: If your library provides an Elsevier InstToken for off-campus API calls, set `ELSEVIER_INSTTOKEN="YOUR_INSTTOKEN"`.

---

### 4. Browser Cookie Injection

For subscribed journals outside campus APIs or without API quotas, you can export browser cookies as JSON (`cookies.json` at the repo root) or run `python scripts/login_publishers.py` to save sessions to `~/.journal_supp_downloader_profile/cookies.json`:

```json
[
  {
    "domain": ".sciencedirect.com",
    "name": "SD_SESSION_ID",
    "value": "xxxxxx",
    "path": "/"
  }
]
```

---

## Project Structure

```
journal-supp-downloader/
├── README.md                       # English documentation (main)
├── README.zh-CN.md                 # Chinese documentation (中文说明)
├── SKILL.md                        # AI Agent skill specification & workflow rules
├── .env.example                    # Template for API keys & Unpaywall/Crossref emails
├── pytest.ini                      # Pytest configuration
├── requirements.txt                # Core Python dependencies
├── requirements-conversion.txt     # Optional MarkItDown conversion dependencies
├── references/
│   └── browserskill.md             # BrowserSkill modes, setup & diagnostics guide
├── scripts/
│   ├── browserskill_adapter.py     # Tencent BrowserSkill (bsk) session & CLI adapter
│   ├── discovery_state.py          # Phase budgets, discovery state & recovery tracking
│   ├── supp_finder.py              # Unified attachment & 9+ publisher extractors + validation
│   ├── repository_resolver.py      # Figshare / ScienceBase / DiVA / GSA repo expansion & RepoBrowserSession
│   ├── oa_resolver.py              # Unpaywall / OpenAlex / Crossref OA & [chooser-alt] host fallback
│   ├── markitdown_converter.py     # Optional Markdown converter (multi-row Excel headers + ZIP tables)
│   ├── journal_downloader.py       # Lightweight downloader (Scrapling + Tier 3-Auto + Repo resolution)
│   ├── scansci_supp_downloader.py  # Full multi-tier downloader (API/CDN/Cookies/OA/Tier 3-Auto)
│   ├── login_publishers.py         # Interactive publisher login & cookie saver
│   ├── playwright_utils.py         # Direct Playwright context, CDP auto-discovery & hanging domain blocker
│   ├── test_api.py                 # Elsevier API connectivity check
│   └── test_fetch.py               # Campus-IP & attachment parsing check
├── tests/                          # Automated unit & regression test suite (`python -m pytest -q`)
│   ├── test_browserskill_adapter.py
│   ├── test_browserskill_reliability.py
│   ├── test_supp_finder.py
│   ├── test_oa_resolver.py
│   ├── test_playwright_utils.py
│   ├── test_markitdown_converter.py
│   ├── test_publisher_structures.py
│   ├── test_auto_browser_fallback.py
│   ├── test_personal_config_and_pipelines.py
│   └── ...                         # Additional P0/P1 & regression test modules
└── scansci-pdf/                    # Embedded PDF engine & institutional config modules
```

---

## FAQ

#### Q: Why does installing Tencent/BrowserSkill help on subscription-gated journals?
A: Once the `bsk` CLI and browser extension are connected:
1. **No debug-port restart needed**: Unlike Chrome CDP (`--remote-debugging-port=9222`), BrowserSkill reuses your running browser session cleanly.
2. **Seamless SSO & Campus VPN**: Reuses existing university SSO, CARSI, ScienceDirect, Springer, or GeoScienceWorld logins without exporting `cookies.json`.
3. **Automatic fallback in `Tier 3-Auto`**: Automatically assists when HTTP/headless tiers hit 401/403 or incomplete extraction (disable anytime with `--no-bsk`).

#### Q: Why does the tool ignore my system proxy env vars, and how does it handle GeoScienceWorld / Silverchair hangs?
A: The tool defaults to **direct network mode** so stale local proxy variables no longer trigger TLS/tunnel errors (pass `--proxy http://host:port` when a proxy is actually desired). On GeoScienceWorld / Silverchair pages, hanging third-party scripts (e.g. `ajax.googleapis.com`) are automatically aborted, and if a primary host (such as Lyell Collection `10.1144/`) denies access, Crossref `[chooser-alt]` automatically retries alternate hosts like GeoScienceWorld with live browser / Playwright `context.request` downloads.

#### Q: What does "No Cloudflare challenge found" mean?
A: It is informational — no interactive challenge appeared, so the script proceeded directly to HTML/supplement extraction.

#### Q: Downloads fail or produce empty / 0 KB files?
A:
1. Verify that the paper actually provides downloadable supplementary files (inline article HTML tables belong to [zotero-table-extractor](https://github.com/ZRyanX/zotero-table-extractor)).
2. For `HTTP 401/403`, verify campus IP/VPN, ensure `Tier 3-Auto` / `--bsk` / `--cdp` has an authenticated browser, or refresh `cookies.json`.

#### Q: Cannot resolve a `doi.org` link?
A: Both downloaders include `curl` fallback and Crossref lookups. If a network blocks `doi.org`, pass the direct publisher article URL instead.

---

## Acknowledgements & References

Built upon upstream open-source tools and academic APIs:

> Repository: [https://github.com/ZRyanX/journal-supp-downloader](https://github.com/ZRyanX/journal-supp-downloader)

| Library / Service | Purpose | Link |
|---|---|---|
| **Tencent BrowserSkill** | Browser-to-Agent bridge reusing live authenticated Chromium sessions | [https://github.com/Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill) |
| **Scrapling** | Stealth browser fetching for dynamic pages & Cloudflare bypass | [https://github.com/D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) |
| **scansci-pdf** | PDF downloader engine, cookie management & campus authentication | [https://github.com/Rimagination/scansci-pdf](https://github.com/Rimagination/scansci-pdf) |
| **Playwright** | Cross-platform CDP, persistent contexts & same-context requests | [https://github.com/microsoft/playwright-python](https://github.com/microsoft/playwright-python) |
| **BeautifulSoup / Requests / urllib3** | HTML parsing, session pooling & HTTP transfers | [https://github.com/psf/requests](https://github.com/psf/requests) |
| **Elsevier / Crossref / Unpaywall / OpenAlex APIs** | Metadata, OA mirror lookup & multi-publisher resolution | [https://dev.elsevier.com/](https://dev.elsevier.com/) |
| **Microsoft MarkItDown** | Optional conversion of spreadsheets, DOCX & PDFs to Markdown | [https://github.com/microsoft/markitdown](https://github.com/microsoft/markitdown) |
| **Zotero Table Extractor** | Companion skill for extracting inline HTML tables in paper body | [https://github.com/ZRyanX/zotero-table-extractor](https://github.com/ZRyanX/zotero-table-extractor) |

---

## Completeness & Recovery Boundaries

Completeness validation checks declared file counts, `S/A` table numbering, and per-section coverage independently; sheet counts inside a workbook do not substitute for declared file counts, and multiple formats of the same table do not satisfy distinct table IDs (whereas a single workbook explicitly spanning `S1–S10` does). When completeness cannot be proven, warnings are preserved explicitly.

Partial hits from API, CDN, and OA sources are retained for follow-up recovery. When the primary host fails or returns zero candidates, Crossref `[chooser-alt]` alternates are explored within phase budgets. Repository landing pages are traversed within bounded limits (2 hops, max 16 pages, 2 MiB/page across Figshare REST API/`__APOLLO_STATE__`, ScienceBase JSON + bare DOI repair, GSA, and DiVA OAI-PMH) via shared `RepoBrowserSession` contexts; opaque `.zip` archives without enumerable internal table names avoid false-positive `S-table` missing alerts. Same-origin `Playwright context.request` is preferred after live browser recovery, followed by deduplicated OA fallbacks unless `--no-oa` is set.

BrowserSkill triggers only explicitly verified supplement download controls (excluding edit/delete actions) and commits validated downloads atomically from temporary staging paths. ZIP/Office integrity checks verify archive directory structure and CRC (for unpacked sizes up to 512 MiB). MarkItDown conversion preserves originals while flattening merged multi-row Excel headers and extracting tables inside `.zip` archives.
