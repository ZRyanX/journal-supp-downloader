# Journal Supplementary Data Downloader (文献附表与附件自动下载工具)

本工具用于从主流学术期刊网站（Elsevier ScienceDirect, SpringerLink, Nature, Wiley 等）自动识别并批量下载文章的补充数据/附表文件（如 `xlsx`, `csv`, `zip`, `docx` 等）。支持绕过 Cloudflare 验证，并内置了多级下载策略（API -> CDN 直连 -> 页面解析 -> 浏览器渲染模拟）。

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
  - **Tier 2**：Scrapling 浏览器渲染（自动绕过 Cloudflare Turnstile/验证码，约 15 秒）。
* **智能过滤**：自动识别真实的数据文件（排除插图 `gr1.jpg`、参考文献、页面广告等）。
* **自动重命名与归档**：使用 CrossRef API 自动查询文献标题并规范化命名文件夹，将所有附件统一整理。

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

## 使用方法

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
```

### 2. 高级集成版下载器 (`scansci_supp_downloader.py`)
支持 API、CDN 直连及 Cookie 注入的多级加速下载器，适合 Elsevier/ScienceDirect 批量下载：
```bash
# 使用 DOI 链接
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349"

# 禁用 API，直接强制使用 CDN/页面抓取
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349" --no-api

# 强制在抓取网页时显示浏览器（用于观察 Cloudflare 绕过过程）
python scripts/scansci_supp_downloader.py "10.1016/j.oregeorev.2026.107349" --headful
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
5. 系统将立即为您生成一串 32 位的 API Key（例如 `66e68474293c31b16c0...`）。

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
├── requirements.txt           # 依赖清单
├── scripts/
│   ├── journal_downloader.py  # 基础页面分析下载器
│   ├── scansci_supp_downloader.py # 高级集成版下载器 (API/CDN/Cookie)
│   ├── test_api.py            # API 认证连通性测试脚本
│   └── test_fetch.py          # 校园网直连与附件解析测试脚本
└── scansci-pdf/               # 内置的核心 PDF 下载与配置引擎
```

---

## 常见问题 FAQ

#### Q: 提示 "No Cloudflare challenge found"？
A: 这是正常提示。说明目标期刊网站当前未开启 Cloudflare 验证或验证已被自动静默绕过，脚本已直接进入正常页面解析。

#### Q: 下载文件为 0 KB 或提示下载失败？
A: 
1. 确认该文献是否确实拥有附件（有些文献仅在正文中列出表格而无附件文件）。
2. 若提示权限拒绝（HTTP 401/403），请检查您的 IP 范围或更新本地 `cookies.json` 文件。

#### Q: 无法解析 DOI 链接？
A: 某些网络环境下 `doi.org` 会被屏蔽或发生 SSL 握手失败。脚本已内置了 `curl.exe` 作为备用解析手段，如仍失败，请直接传入期刊文章的真实 URL 链接。

---

## 鸣谢与参考

本项目的核心文献检索组件来源于原作者的开源项目：
- [scansci-pdf](https://github.com/Rimagination/scansci-pdf)

