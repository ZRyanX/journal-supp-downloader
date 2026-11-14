# Journal Supplementary Data Downloader (文献附表与附件自动下载工具)

本工具用于从学术期刊网站及关联数据仓储（ScienceDirect / Elsevier, SpringerLink / Nature, Wiley, Silverchair / GeoScienceWorld / Lyell Collection, Atypon, RSC, Cambridge, Frontiers, Copernicus, PeerJ, MDPI, OJS / J-STAGE / 中文期刊, Figshare, ScienceBase, DiVA 等）自动识别并批量下载文章的补充数据/附表文件（如 `xlsx`, `xls`, `csv`, `zip`, `docx`, `pdf` 等）。默认直连网络运行，支持静态解析、多出版商镜像回退、动态页面渲染与分级真实浏览器自动接管（`Tier 3-Auto`）。

本仓库已内置 [scansci-pdf](https://github.com/Rimagination/scansci-pdf) 核心组件，支持作为独立项目运行，也可与全局 `scansci-pdf` 联动。

---

## 目录
1. [核心能力](#核心能力)
2. [环境要求与安装](#环境要求与安装)
3. [使用方法](#使用方法)
4. [认证与配置指南（直连配置、API 与机构认证）](#认证与配置指南直连配置api-与机构认证)
   - [网络与环境变量配置文件](#1-网络与环境变量配置文件)
   - [Elsevier API 认证（申请 Developer Key）](#2-elsevier-api-认证申请-developer-key)
   - [机构认证与校园网配置](#3-机构认证与校园网配置)
   - [Cookie 认证注入（突破非 API 限制）](#4-cookie-认证注入突破非-api-限制)
5. [文件结构](#文件结构)
6. [常见问题 FAQ](#常见问题-faq)
7. [附表完整性与恢复边界](#附表完整性与恢复边界)

---

## 核心能力

* **多级下载策略 (Multi-Tier + Tier 3-Auto)**：
  - **Tier A**：Elsevier API 检索（极速，需 API 密钥与机构网络）。
  - **Tier 0**：Elsevier CDN 直链探测（无需 API 认证，快速探测 `mmc*.xlsx/pdf/zip`）。
  - **Tier 1**：静态页面与 `Requests` 会话提取（带 Cookie 与直连重试；自动修复 ISO-8859-1 中文文件名并按文件魔数校正 `.bin` 扩展名）。
  - **Tier 2**：Scrapling 隐匿浏览器渲染（`stealthy_fetch_bounded`，自动拦截阻塞页面渲染的第三方挂起域名，如 GSW/Silverchair 页面上的 `ajax.googleapis.com`）。
  - **Tier 3 / Tier 3-Auto (自动分级浏览器接管)**：当常规抓取遇拦截（401/403/CF）或未命中/缺漏附件时，在 `--browser-fallback-budget`（默认 `60s`）与 `--phase-timeout`（默认 `150s`）预算内按优先级自动尝试：**已就绪的 Tencent BrowserSkill (`bsk`) → 本地活动 Chrome `--cdp` 自动探测 → 独立有头真 Chrome (`--headful`)**（支持 `--no-bsk`、`--no-cdp`、`--no-auto-headful` 按项关闭）。
  - **Crossref 多出版商回退 (`[chooser-alt]`)**：主出版商无候选或受限时（例如 Lyell Collection `10.1144/` 无订阅权限），自动通过 Crossref 切换同 DOI 的替代出版商宿主（如 GeoScienceWorld）继续发现。
  - **Tier OA 与外部数据仓储穿透**：集成 Unpaywall & OpenAlex（PMC/Europe PMC）及 **Figshare（REST API + `__APOLLO_STATE__`）、ScienceBase（JSON + 裸 DOI 修正）、GSA（`10.1130/`）、DiVA（OAI-PMH 与镜像回退）** 深度解析，支持 `RepoBrowserSession` 跨候选页复用浏览器会话与 Playwright `context.request` 原生上下文下载。
* **出版商专用提取器与完整性质检**：针对 Silverchair、Atypon、RSC、Cambridge、Frontiers、Copernicus、PeerJ、MDPI、OJS/J-STAGE/中文期刊、Nuxt/JSON、SciEngine 等平台提供专用提取器与 SPA 空壳页面守卫；智能排除正文插图、参考文献与网页噪声，并依据文中附表数量与 `S1..Sn` 编号声明提示潜在缺漏。
* **可选 MarkItDown 结构化副本**：支持 `--convert-markdown`（轻量版亦支持 `--markitdown`），利用微软 MarkItDown 将下载的附表（`.docx`, `.xlsx`, `.xls`, `.pdf` 等）生成 Markdown 副本；支持 **Excel 多行合并表头自动展平** 与 **`.zip` 压缩包内嵌表格递归转换**，完整保留原始科研数据文件。
  启用前安装格式依赖：`pip install -r requirements-conversion.txt`。基础安装不包含转换组件；旧式 `.doc`/`.ppt` 不在内置转换支持范围内。

* **自动重命名与归档**：使用 Unpaywall / OpenAlex / CrossRef API 自动查询文献标题并规范化命名文件夹，将所有附件统一整理。

---

## 环境要求与安装

项目要求 **Python 3.11** 或更高版本。

1. **克隆项目并安装 Python 依赖**：
   ```bash
   pip install -r requirements.txt
   ```

2. **安装 Scrapling 浏览器内核（用于绕过 Cloudflare）**：
   ```bash
   scrapling install
   ```

---

## 使用方法（看不懂可以用Claude code、codex、antigravity、opencode等本地AI软件学习该项目并引导配置）

本工具提供两个主要运行入口，均默认直连网络（未显式传入 `--proxy` 时忽略系统/环境代理变量）：

### 1. 独立轻量版下载器 (`journal_downloader.py`)
面向非 Elsevier 常规期刊、独立网页链接及需要浏览器深度交互的站点：
```bash
# 基本使用 (会自动创建 journal_downloads 文件夹，失败时自动触发 Tier 3-Auto)
python scripts/journal_downloader.py "https://doi.org/10.1016/j.oregeorev.2022.104949"

# 显示浏览器窗口 / 强制使用系统真实 Chrome / 显式启用代理
python scripts/journal_downloader.py "<url_or_doi>" --headful --real-chrome
python scripts/journal_downloader.py "<url_or_doi>" --proxy "http://127.0.0.1:7890"

# 仅列出候选附件，不执行下载 / 转换 Markdown 副本
python scripts/journal_downloader.py "<url_or_doi>" --list-only
python scripts/journal_downloader.py "<url_or_doi>" --convert-markdown

# 预算与降级控制（默认总阶段 150s，浏览器导航 30s，回退预算 60s）
python scripts/journal_downloader.py "<url_or_doi>" --phase-timeout 180 --browser-goto-timeout 30 --browser-fallback-budget 60
python scripts/journal_downloader.py "<url_or_doi>" --no-bsk --no-cdp --no-auto-headful

# 显式使用 CDP / Tencent BrowserSkill（或借用已打开标签页）
python scripts/journal_downloader.py "<url_or_doi>" --cdp
python scripts/journal_downloader.py "<url_or_doi>" --bsk
python scripts/journal_downloader.py "<url_or_doi>" --bsk --bsk-tab <tab-id>
```

### 2. 高级集成版下载器 (`scansci_supp_downloader.py`)
集成 Elsevier API、CDN 直连、Cookie 注入、多出版商 Crossref 回退、外部仓储穿透与 `Tier 3-Auto` 的主入口：
```bash
# 使用 DOI 或期刊 URL
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349"

# 禁用 API，直接进入 CDN/页面抓取（可自定义单文件探测上限与连续失败阈值）
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349" --no-api --max-mmc 25 --max-misses 4

# 隧道/代理连续失败自愈阈值与预算控制
python scripts/scansci_supp_downloader.py "<url_or_doi>" --tunnel-fail-threshold 3 --phase-timeout 150 --browser-fallback-budget 60

# 显式直连日常 Chrome CDP / BrowserSkill 协同下载
python scripts/scansci_supp_downloader.py "<url_or_doi>" --cdp
python scripts/scansci_supp_downloader.py "<url_or_doi>" --bsk
python scripts/scansci_supp_downloader.py "<url_or_doi>" --bsk --bsk-tab <tab-id>
```

### 3. Tencent BrowserSkill 独立协同与管理工具 (`browserskill_adapter.py`)

BrowserSkill 保持为外部可选依赖，需要单独安装 `bsk` CLI 和浏览器扩展。[接入、兼容性与故障处理](references/browserskill.md) 提供完整说明。

**模式说明**：默认情况下，未传 `--bsk` 时常规页面优先直连抓取，仅当 `Tier 3-Auto` 触发且检测到 `bsk` 守护进程与浏览器已连接时按需接入（传入 `--no-bsk` 可彻底禁用）；传入 `--bsk` 则从页面阶段开始优先使用 BrowserSkill；`--bsk-auto` 显式声明失败/缺漏时按需恢复；指定 `--bsk-browser` / `--bsk-tab` 会自动启用接入（与 `--bsk-auto` 合用时保持按需模式）。

```bash
# 按需恢复模式：普通请求成功时无需启动 BrowserSkill
python scripts/journal_downloader.py "<doi_or_url>" --bsk-auto
python scripts/scansci_supp_downloader.py "<doi_or_url>" --bsk-auto

# 结构化健康检查，只有 CLI、daemon 和浏览器连接均可用才返回成功
python scripts/browserskill_adapter.py status --json
```

一次任务复用一个按需启动的会话，借用标签页保留至附件处理结束，并在成功、提前返回或异常时清理。HTTP 403、无效内容和连接失败均支持浏览器恢复；小文件页内传输限制为 8 MiB，大文件通过原生下载路径处理。文件先落到临时目录，内容校验通过后提交，避免把错误页登记为附件。
提供环境自检、活跃会话监控、标签页借用与网页抓取功能：
```bash
# 检查 BrowserSkill 与日常浏览器联通状态
python scripts/browserskill_adapter.py status

# 查看当前活跃的浏览器会话与可用标签页
python scripts/browserskill_adapter.py sessions
python scripts/browserskill_adapter.py tabs --scope user

# 独立测试通过 BrowserSkill 抓取期刊页面 DOM（保存到文件）
python scripts/browserskill_adapter.py fetch "<doi_or_url>" -o ./page.html

# 借用用户已打开的文献标签页抓取
python scripts/browserskill_adapter.py borrow-fetch <tab_id> -o ./page.html
```

---

## 认证与配置指南（直连配置、API 与机构认证）

在开源/独立运行模式下，本工具**默认直连网络**（未显式传入 `--proxy` 时自动忽略环境代理变量），并通过**本地配置文件**或**环境变量**完成 API 认证与机构权限验证。

### 1. 网络与环境变量配置文件

推荐通过以下任一方式配置个人参数（`.gitignore` 已自动忽略个人敏感文件，切勿提交个人密钥与 Cookie）：
1. **项目根目录 `.env`**（推荐）：复制 `.env.example` 为 `.env` 后填写：
   ```dotenv
   ELSEVIER_API_KEY=您的_API_KEY
   IS_CAMPUS_NETWORK=false
   ELSEVIER_INSTTOKEN=
   UNPAYWALL_EMAIL=
   CROSSREF_EMAIL=
   ```
2. **用户配置目录 `~/.journal_supp_downloader_profile/config.json`**：支持 JSON 字段 `elsevier_api_key`、`is_campus_network`、`elsevier_insttoken`、`unpaywall_email`、`crossref_email`。
3. **系统环境变量**：同名大写变量会优先生效。

> 提示：本技能**无需配置代理环境变量**；仅在确需走特定代理出口时通过 `--proxy http://host:port` 按次显式传入。

### 2. Elsevier API 认证（申请 Developer Key）

针对 Elsevier 期刊，API 下载速度最快且最稳定。用户需要申请免费的个人开发密钥：

#### 申请步骤：
1. 访问 [Elsevier Developer Portal](https://dev.elsevier.com/)。
2. 注册或登录您的 Elsevier 账号。
3. 点击导航栏中的 **"My API Key"**，然后点击 **"Create API Key"**。
4. 在申请表单中填入任意的 Label（如 `MyPaperDownloader`）和您的个人/机构网址（如学校官网），勾选同意协议并提交。
5. 系统将立即为您生成一串 32 位的 API Key（例如 `a1b2c3d4e5f67890abcdef1234567890...`）。

#### 环境变量配置：
也可将申请到的 Key 直接写入当前会话环境变量：
* **Windows (PowerShell)**:
  ```powershell
  $env:ELSEVIER_API_KEY="您的_API_KEY"
  ```
* **Linux / macOS**:
  ```bash
  export ELSEVIER_API_KEY="您的_API_KEY"
  ```

---

### 3. 机构认证与校园网配置

由于大部分学术文献受到数据库商的版权控制，API 检索和页面下载通常需要验证您的**机构订阅权限**（如高校网络）：

#### 方案 A：处于校园网 / 机构 IP 范围内（最推荐）
如果您在学校内或者已连接了学校的校园 VPN，Elsevier API 会自动通过您的公网 IP 识别学校机构身份。此时，您仅需配置 `ELSEVIER_API_KEY`，并声明处于校园网环境中：
* **Windows (PowerShell)**:
  ```powershell
  $env:IS_CAMPUS_NETWORK="true"
  ```
* **Linux / macOS**:
  ```bash
  export IS_CAMPUS_NETWORK="true"
  ```

#### 方案 B：使用机构 Token（InstToken）
若您身处校外，且学校图书馆/网信中心提供了 Elsevier 机构认证 Token（Institutional Token），您可以通过设置以下环境变量进行校外 API 授权：
* **Windows (PowerShell)**:
  ```powershell
  $env:ELSEVIER_INSTTOKEN="您的机构_TOKEN"
  ```
* **Linux / macOS**:
  ```bash
  export ELSEVIER_INSTTOKEN="您的机构_TOKEN"
  ```

---

### 4. Cookie 认证注入（突破非 API 限制）

部分期刊不支持 API 下载，或您的 API Key 超出了当日配额，亦或您在校外无法使用 VPN。此时，可以通过**导出已登录机构账号的浏览器 Cookie** 供脚本使用。

#### 提取与配置步骤：
1. 在电脑浏览器中，登录您的高校图书馆系统/WebVPN，确保能正常下载该文献的 PDF。
2. 安装浏览器 Cookie 导出插件（例如 Chrome 的 [EditThisCookie](https://chromewebstore.google.com/detail/editthiscookie/fngmhnnpjjcjijnebhflgfoecbefmebf) 或 [Cookie-Editor](https://cookie-editor.com/)）。
3. 访问目标文献的期刊主页（例如 `sciencedirect.com` 或 `link.springer.com`），打开插件，将当前网站的 Cookie 导出为 **JSON 格式**。
4. 在本项目根目录下新建一个名为 **`cookies.json`** 的文件，将导出的 JSON 数据粘贴进去。
5. 运行 `scansci_supp_downloader.py` 时，脚本会自动读取该文件中的 Cookie，将其注入到 HTTP 会话与无头浏览器中，以机构身份突破付费墙限制。

> [!TIP]
> 导出的 JSON 格式应类似于：
> ```json
> [
>   {
>     "domain": ".sciencedirect.com",
>     "name": "SD_SESSION_ID",
>     "value": "xxxxxx",
>     "path": "/"
>   }
> ]
> ```

---

## 文件结构

```
journal-supp-downloader/
├── README.md                       # 说明文档
├── SKILL.md                        # 技能规范与详细说明
├── .env.example                    # 个人 API / 邮箱配置模板（复制为 .env 使用）
├── pytest.ini                      # Pytest 配置
├── requirements.txt                # 核心依赖清单
├── requirements-conversion.txt     # 可选 MarkItDown 格式转换依赖清单
├── references/
│   └── browserskill.md             # BrowserSkill 配置、模式与诊断边界
├── scripts/
│   ├── browserskill_adapter.py     # 腾讯 BrowserSkill (bsk) 协同接口与 CLI 适配器
│   ├── discovery_state.py          # 多阶段发现状态、预算计时与自愈状态追踪
│   ├── supp_finder.py              # 统一补充附件与 9+ 出版商提取器、证据链与文件校验
│   ├── repository_resolver.py      # Figshare / ScienceBase / DiVA / GSA 外部仓储穿透与会话复用
│   ├── oa_resolver.py              # Unpaywall / OpenAlex / Crossref 开放获取与多出版商回退解析
│   ├── markitdown_converter.py     # 可选 MarkItDown 转换（含 Excel 多行表头展平与 ZIP 内嵌表格）
│   ├── journal_downloader.py       # 轻量版页面分析与下载器（Scrapling + Tier 3-Auto + Repo 穿透）
│   ├── scansci_supp_downloader.py  # 高级集成版下载器（API/CDN/Cookie/OA/Tier 3-Auto 多级加速）
│   ├── login_publishers.py         # 出版社登录与 Cookie 配置向导
│   ├── playwright_utils.py         # Playwright 直连上下文、CDP 自动探测与第三方挂起域名拦截
│   ├── test_api.py                 # API 认证连通性测试脚本
│   └── test_fetch.py               # 校园网直连与附件解析测试脚本
├── tests/                          # 单元测试与回归测试集（覆盖协议、出版商结构、仓储穿透与恢复）
│   ├── test_browserskill_adapter.py
│   ├── test_browserskill_reliability.py
│   ├── test_supp_finder.py
│   ├── test_oa_resolver.py
│   ├── test_playwright_utils.py
│   ├── test_markitdown_converter.py
│   ├── test_publisher_structures.py
│   ├── test_auto_browser_fallback.py
│   ├── test_personal_config_and_pipelines.py
│   └── ...                         # P0/P1 与回归测试套件（`python -m pytest -q` 一键运行）
└── scansci-pdf/                    # 内置的核心 PDF 下载与配置引擎
```

---

## 常见问题 FAQ

#### Q: 在 AI Agent 安装 Tencent/BrowserSkill 后是不是能更好地运用该技能的功能？
A: 对需要已有登录态的文章有帮助。安装 CLI 并连接扩展后，即可通过 `--bsk` 显式优先接入，或通过 `--bsk-auto` / 默认 `Tier 3-Auto` 自动检测并接管（不希望触发时可传 `--no-bsk`）：
1. **彻底免去配置 Chrome 远程调试端口**：传统 CDP 直连要求用户携带 `--remote-debugging-port` 启动浏览器；而安装 Tencent BrowserSkill 后，只需日常浏览器加载其轻量扩展并启动后台守护进程 (`bsk daemon start`)，AI Agent 即可直接与日常浏览器通信。
2. **无感复用校园网 SSO 与机构权限**：用户日常浏览器只要保持校园网 VPN / CARSI 或 Elsevier/Springer/GeoScienceWorld 登录，BrowserSkill 就能直接借用其身份发起请求，无需手动提取和更新 `cookies.json`。
3. **复用真实浏览器环境**：可使用已有登录态；Cloudflare、人机验证和机构认证仍可能需要用户操作，不能保证通过。
4. **独立 Agent Window**：请求后台启动；窗口焦点和借用确认仍受浏览器扩展设置影响。
5. **双向下载兜底**：启用 BrowserSkill 后，常规 HTTP 的 401/403 或无效内容会进入认证下载恢复路径；文件校验通过后才提交结果。

#### Q: 为什么不再默认读取系统代理，为什么部分 GSW/Silverchair 文献有时会卡住？
A: 本技能已改为**默认直连网络模式**（忽略系统环境代理，避免本地失效代理导致 TLS/隧道误判失败；仅在传入 `--proxy` 时使用代理）。此外针对 GeoScienceWorld / Silverchair 等站点易因海外第三方脚本（如 `ajax.googleapis.com`）挂起超时，工具会自动拦截这类非关键第三方域名；若主出版商（如 Lyell Collection `10.1144/`）无订阅权限，也会自动触发 Crossref `[chooser-alt]` 切换到对应 GeoScienceWorld 镜像并优先在真实浏览器或 Playwright 同源上下文下载。

#### Q: 提示 "No Cloudflare challenge found"？
A: 这是正常提示。说明目标期刊网站当前未开启 Cloudflare 验证或验证已被自动静默绕过，脚本已直接进入正常页面解析。

#### Q: 下载文件为 0 KB 或提示下载失败？
A: 
1. 确认该文献是否确实拥有附件（有些文献仅在正文中列出表格而无附件文件）。
2. 若提示权限拒绝（HTTP 401/403），请检查您的 IP 范围、确认 `Tier 3-Auto` / `--bsk` / `--cdp` 可用，或更新本地 `cookies.json` 文件。

#### Q: 无法解析 DOI 链接？
A: 某些网络环境下 `doi.org` 会被屏蔽或发生 SSL 握手失败。脚本已内置了 `curl.exe` 作为备用解析手段，如仍失败，请直接传入期刊文章的真实 URL 链接。

---

## 鸣谢与参考

本项目的核心文献检索组件来源于原作者的开源项目，并使用了以下优秀的开源项目与技术：

> 本仓库地址：[https://github.com/ZRyanX/journal-supp-downloader](https://github.com/ZRyanX/journal-supp-downloader)

| 库 / 工具 | 用途 | 链接 |
|-----------|------|------|
| **Tencent BrowserSkill** | 腾讯日常浏览器与 AI Agent 高可用桥接工具（免配置调试端口，无感复用登录态与机构订阅） | [https://github.com/Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill) |
| **Scrapling** | 隐匿浏览器抓取，绕过 Cloudflare Turnstile/Interstitial 检测 | [https://github.com/D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) |
| **scansci-pdf** | PDF 下载引擎、cookie 管理、机构认证配置 | [https://github.com/Rimagination/scansci-pdf](https://github.com/Rimagination/scansci-pdf) |
| **Playwright** | 跨平台浏览器自动化（CDP / persistent context / stealth 注入） | [https://github.com/microsoft/playwright-python](https://github.com/microsoft/playwright-python) |
| **BeautifulSoup** | HTML 页面解析与补充链接提取 | [https://code.launchpad.net/beautifulsoup](https://code.launchpad.net/beautifulsoup) |
| **Requests** | HTTP 会话管理与文件下载 | [https://github.com/psf/requests](https://github.com/psf/requests) |
| **urllib3** | HTTP 连接池与底层网络传输 | [https://github.com/urllib3/urllib3](https://github.com/urllib3/urllib3) |
| **Elsevier API** | 文章元数据检索、补充附件索引（Tier A 加速） | [https://dev.elsevier.com/](https://dev.elsevier.com/) |
| **CrossRef API** | DOI 解析与文献标题规范化查询 | [https://github.com/CrossRef](https://github.com/CrossRef) |
| **Unpaywall API** | 全球开放获取（OA）穿透与合法仓储元数据查询 | [https://unpaywall.org/products/api](https://unpaywall.org/products/api) |
| **OpenAlex API** | 全球学术开放目录与仓储全文镜像检索 | [https://docs.openalex.org/](https://docs.openalex.org/) |
| **Microsoft MarkItDown** | 微软多格式科研附表智能转 Markdown 工具 | [https://github.com/microsoft/markitdown](https://github.com/microsoft/markitdown) |
| **pycryptodome** | 加密工具库（scansci-pdf 依赖） | [https://github.com/Legrandin/pycryptodome](https://github.com/Legrandin/pycryptodome) |
| **Selenium** | 备用浏览器驱动（scansci-pdf 内部使用） | [https://github.com/SeleniumHQ/selenium](https://github.com/SeleniumHQ/selenium) |
| **Zotero Table Extractor** | 学术正文表格提取技能；正在持续优化中 | [https://github.com/ZRyanX/zotero-table-extractor](https://github.com/ZRyanX/zotero-table-extractor) |


## 附表完整性与恢复边界

完整性检查分别核对明确的文件数、S/A 表编号和每个补充区块；文件数不能由工作簿中的表数量抵扣。同一来源重试后采用最新区块状态，并保留已发现的编号声明。同表的多个格式不能补足其他表；明确标注 S1–S10 的单个工作簿可以覆盖该范围。无法证明完整时会保留缺漏提示。

API、CDN 和开放获取镜像的部分结果会保留并继续补查；当主出版商无候选或受限时，自动通过 Crossref `[chooser-alt]` 切换替代出版商宿主继续发现。候选记录各自的来源页，用于解析相对地址及下载 Referer。仓储介绍页会在两层、最多 16 页、单页 2 MiB 的限制内继续寻找附件（含 Figshare REST API/`__APOLLO_STATE__`、ScienceBase JSON 裸 DOI 修正、GSA、DiVA OAI-PMH 与 `RepoBrowserSession` 上下文复用），未解析成功的来源仍会报告。仓储父链接的附表证据会保留，只有对应子文件均成功取得时才计入覆盖；非可枚举的纯附件 `.zip` 归档不会误计入 S 表缺失误报。文件传输在真实浏览器恢复后优先使用同源 `Playwright context.request`，失败后亦会补查 OA 镜像，同一轮不重复请求已尝试的文件地址；`--no-oa` 禁用这项补救。

BrowserSkill 仅将明确标注下载的附表控件加入自动下载，并在点击前重新核验；删除、编辑等操作会被排除。挂起下载会持续等待同一个操作，检查失败、超时及实际落盘状态。alert 可以继续；confirm 仅在明确询问下载且不包含其他操作时接受，其余对话框报告需要明确决定。真实网站登录与扩展协议仍需要实机验证。

ZIP/Office 文件检查容器目录和必要文档成员；解压后总量不超过 512 MiB 时额外检查 CRC，超过时仅检查目录结构并明确说明校验范围。Markdown 转换保留原文件，支持 `.xlsx`/`.xls` 多行合并表头展平与 `.zip` 包内嵌表格转换，适合作为阅读副本。
