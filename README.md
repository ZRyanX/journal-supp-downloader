# Journal Supplementary Data Downloader (文献附表与附件自动下载工具)

本工具用于从主流学术期刊网站（Elsevier ScienceDirect, SpringerLink, Nature, Wiley 等）自动识别并批量下载文章的补充数据/附表文件（如 `xlsx`, `csv`, `zip`, `docx` 等）。支持处理动态页面与复用浏览器认证会话，并内置了多级下载策略（API -> CDN 直连 -> 页面解析 -> 浏览器渲染模拟）。

本仓库已内置 [scansci-pdf](https://github.com/Rimagination/scansci-pdf) 核心组件，支持作为独立项目运行，也可与全局 `scansci-pdf` 联动。

---

## 目录
1. [核心能力](#核心能力)
2. [环境要求与安装](#环境要求与安装)
3. [使用方法](#使用方法)
4. [认证与配置指南（API 认证与机构认证）](#认证与配置指南api-认证与机构认证)
   - [Elsevier API 认证（申请 Developer Key）](#1-elsevier-api-认证申请-developer-key)
   - [机构认证与校园网配置](#2-机构认证与校园网配置)
   - [Cookie 认证注入（突破非 API 限制）](#3-cookie-认证注入突破非-api-限制)
5. [文件结构](#文件结构)
6. [常见问题 FAQ](#常见问题-faq)

---

## 核心能力

* **多级下载策略 (Multi-Tier)**：
  - **Tier A**：Elsevier API 检索（极速，需 API 密钥与机构网络，约 3 秒）。
  - **Tier 0**：Elsevier CDN 暴力匹配（无需任何认证，约 1 秒/文件）。
  - **Tier 1**：期刊直接链接下载（基于 Requests 会话，带 Cookie）。
  - **Tier 2**：Scrapling 浏览器渲染（用于动态页面；验证是否通过取决于站点与浏览器环境）。
  - **Tier 3 (全新增强)**：CDP 实时浏览器直连（支持 `--cdp` 自动发现或 `--cdp-url`，直接附加到用户日常运行的 Chrome/Edge，复用机构 SSO 与有效凭据，可能仍需用户完成 Turnstile 验证）。
  - **Tier BSK (全新增强)**：Tencent BrowserSkill 深度协同（支持 `--bsk`，原生集成腾讯开源 [Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill)，AI Agent 无需设置任何调试端口即可直接借用日常 Chromium 浏览器会话，复用已有登录态；支持 DOM 展开、任务级会话复用与认证下载恢复，验证可能仍需用户操作）。
  - **Tier OA (全新增强)**：Unpaywall & OpenAlex 全球开放获取穿透直取（自动探测 PubMed Central / Europe PMC 等公开合法镜像，从可访问的公开来源查找附表，需核对其完整性）。
* **智能过滤**：自动识别真实的数据文件（支持 Parquet、Feather、Arrow、压缩表格等，排除插图 `gr1.jpg`、参考文献、页面广告等）。
* **可选 MarkItDown 结构化副本**：支持 `--convert-markdown`，利用微软 MarkItDown 将下载的附表（`.docx`, `.xlsx`, `.pdf`）一键解包转换为 Markdown 副本及摘要索引，100% 保护原始科研数据完整性。
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

本工具提供两个主要运行入口：

### 1. 独立轻量版下载器 (`journal_downloader.py`)
仅使用 Scrapling StealthyFetcher 抓取页面并提取附件，适合无需 API 验证的普通期刊：
```bash
# 基本使用 (会自动创建 journal_downloads 文件夹)
python scripts/journal_downloader.py "https://doi.org/10.1016/j.oregeorev.2022.104949"

# 显示浏览器窗口以供调试
python scripts/journal_downloader.py "<url>" --headful

# 仅列出链接，不执行下载
python scripts/journal_downloader.py "<url>" --list-only

# 直连已打开的日常 Chrome 浏览器（复用已有登录态与机构 SSO）
python scripts/journal_downloader.py "<url>" --cdp

# 使用腾讯 BrowserSkill 协同抓取（免配调试端口，独立 Agent Window）
python scripts/journal_downloader.py "<url_or_doi>" --bsk

# 借用日常浏览器已打开的文献标签页（免重新加载，零等待）
python scripts/journal_downloader.py "<url_or_doi>" --bsk --bsk-tab <tab-id>
```

### 2. 高级集成版下载器 (`scansci_supp_downloader.py`)
支持 API、CDN 直连、CDP 实时浏览器直连、BrowserSkill 协同及 Cookie 注入的多级加速下载器，适合 Elsevier/ScienceDirect 批量下载：
```bash
# 使用 DOI 链接
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349"

# 禁用 API，直接强制使用 CDN/页面抓取
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349" --no-api

# 强制在抓取网页时显示浏览器（用于观察 Cloudflare 绕过过程）
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349" --headful

# 直连日常浏览器 CDP 会话（突破最严苛的人机防护和机构订阅验证）
python scripts/scansci_supp_downloader.py "<url_or_doi>" --cdp

# 使用腾讯 BrowserSkill 协同下载（借用日常浏览器机构订阅与会话）
python scripts/scansci_supp_downloader.py "<url_or_doi>" --bsk

# 借用日常浏览器已打开的文献标签页协同下载
python scripts/scansci_supp_downloader.py "<url_or_doi>" --bsk --bsk-tab <tab-id>
```

### 3. Tencent BrowserSkill 独立协同与管理工具 (`browserskill_adapter.py`)

BrowserSkill 保持为外部可选依赖，需要单独安装 `bsk` CLI 和浏览器扩展。[接入、兼容性与故障处理](references/browserskill.md) 提供完整说明。

**行为变更**：仅安装 `bsk` 不再自动触发个人浏览器接入。使用 `--bsk` 优先读取页面，或 `--bsk-auto` 允许失败/附件缺漏时按需恢复；`--no-bsk` 显式禁用 BrowserSkill。指定 `--bsk-browser` / `--bsk-tab` 会自动启用接入，与 `--bsk-auto` 合用时保持按需模式。

```bash
# 自动恢复模式：普通请求成功时无需启动 BrowserSkill
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

## 认证与配置指南（API 认证与机构认证）

在开源/独立运行模式下，本工具通过**环境变量**及**本地配置文件**完成用户 API 认证与机构权限验证。

### 1. Elsevier API 认证（申请 Developer Key）

针对 Elsevier 期刊，API 下载速度最快且最稳定。用户需要申请免费的个人开发密钥：

#### 申请步骤：
1. 访问 [Elsevier Developer Portal](https://dev.elsevier.com/)。
2. 注册或登录您的 Elsevier 账号。
3. 点击导航栏中的 **"My API Key"**，然后点击 **"Create API Key"**。
4. 在申请表单中填入任意的 Label（如 `MyPaperDownloader`）和您的个人/机构网址（如学校官网），勾选同意协议并提交。
5. 系统将立即为您生成一串 32 位的 API Key（例如 `a1b2c3d4e5f67890abcdef1234567890...`）。

#### 环境变量配置：
将申请到的 Key 写入系统环境变量：
* **Windows (PowerShell)**:
  ```powershell
  $env:ELSEVIER_API_KEY="您的_API_KEY"
  ```
* **Linux / macOS**:
  ```bash
  export ELSEVIER_API_KEY="您的_API_KEY"
  ```

---

### 2. 机构认证与校园网配置

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

### 3. Cookie 认证注入（突破非 API 限制）

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
├── README.md                  # 说明文档
├── SKILL.md                   # 技能规范与详细说明
├── requirements.txt           # 依赖清单
├── references/
│   └── browserskill.md         # BrowserSkill 配置、模式与诊断边界
├── scripts/
│   ├── browserskill_adapter.py# 腾讯 BrowserSkill (bsk) 协同接口与 CLI 适配器
│   ├── supp_finder.py         # 统一补充附件与表格候选识别、证据链与文件验证模块
│   ├── oa_resolver.py         # Unpaywall & OpenAlex 开放获取（OA）穿透直取与 PMC 镜像解析
│   ├── markitdown_converter.py# 微软 MarkItDown 附表多格式智能转 Markdown 模块（可选）
│   ├── journal_downloader.py  # 基础页面分析与轻量下载器 (Scrapling + BrowserSkill)
│   ├── scansci_supp_downloader.py # 高级集成版下载器 (API/CDN/OA/Cookie/BrowserSkill 多级加速)
│   ├── login_publishers.py    # 出版社登录与 Cookie 配置向导
│   ├── playwright_utils.py    # Playwright Chromium 自动定位与 CDP / BrowserSkill 连接工具
│   ├── test_api.py            # API 认证连通性测试脚本
│   └── test_fetch.py          # 校园网直连与附件解析测试脚本
├── tests/
│   ├── test_browserskill_adapter.py # BrowserSkill 适配器与协同测试集 (会话、协议、下载与恢复测试)
│   ├── test_browserskill_reliability.py # 故障恢复、文件校验与完整 CLI 流程
│   ├── test_supp_finder.py    # 补充附件提取测试集 (85 tests)
│   ├── test_oa_resolver.py    # Unpaywall / OpenAlex 开放获取测试集 (7 tests)
│   ├── test_playwright_utils.py # CDP 与浏览器定位测试集 (59 tests)
│   └── test_markitdown_converter.py # MarkItDown 结构转换测试集 (4 tests)
└── scansci-pdf/               # 内置的核心 PDF 下载与配置引擎
```

---

## 常见问题 FAQ

#### Q: 在 AI Agent 安装 Tencent/BrowserSkill 后是不是能更好地运用该技能的功能？
A: 对需要已有登录态的文章有帮助。安装 CLI、连接扩展并启用 `--bsk` / `--bsk-auto` 后：
1. **彻底免去配置 Chrome 远程调试端口**：传统 CDP 直连要求用户彻底退出正在工作的浏览器并携带 `--remote-debugging-port=9222` 重启；而安装 Tencent BrowserSkill 后，只需日常浏览器加载其轻量扩展并启动后台守护进程 (`bsk daemon start`)，AI Agent 即可直接与日常浏览器通信。
2. **无感复用校园网 SSO 与机构权限**：用户日常浏览器只要保持校园网 VPN / CARSI 或 Elsevier/Springer 登录，BrowserSkill 就能直接借用其身份发起请求，无需手动提取和更新 `cookies.json`。
3. **复用真实浏览器环境**：可使用已有登录态；Cloudflare、人机验证和机构认证仍可能需要用户操作，不能保证通过。
4. **独立 Agent Window**：请求后台启动；窗口焦点和借用确认仍受浏览器扩展设置影响。
5. **双向下载兜底**：启用 BrowserSkill 后，常规 HTTP 的 401/403 或无效内容会进入认证下载恢复路径；文件校验通过后才提交结果。

#### Q: 提示 "No Cloudflare challenge found"？
A: 这是正常提示。说明目标期刊网站当前未开启 Cloudflare 验证或验证已被自动静默绕过，脚本已直接进入正常页面解析。

#### Q: 下载文件为 0 KB 或提示下载失败？
A: 
1. 确认该文献是否确实拥有附件（有些文献仅在正文中列出表格而无附件文件）。
2. 若提示权限拒绝（HTTP 401/403），请检查您的 IP 范围、使用 `--bsk` 复用日常已登录浏览器，或更新本地 `cookies.json` 文件。

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

