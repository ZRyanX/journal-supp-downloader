---
name: journal-supp-downloader
description: 使用 Scrapling 从学术期刊网站（Elsevier、Springer、Nature 等）自动下载补充数据/附件。适用于具有 Cloudflare 防护的期刊网站。支持 DOI 链接和直接文章链接。
---

# Journal Supplementary Data Downloader

本技能提供了一套完整的流程，用于从学术期刊网站（如 Elsevier ScienceDirect、SpringerLink、Nature 等）自动下载文章的补充数据文件（Supplementary Data、Appendix、MMC 文件、独立表格等）。

## 核心能力与处理边界

- **Cloudflare 绕过**：使用 Scrapling 的 `StealthyFetcher` 自动绕过 Cloudflare Turnstile/Interstitial 检测
- **Tencent BrowserSkill 深度协同**：原生集成腾讯开源 [Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill) (`bsk`) 接口。当 AI Agent 安装配置 BrowserSkill 后，无需手动配置远程调试端口即可直接借用日常浏览器（Chrome/Edge）的登录态与高校 SSO 订阅，在后台 Agent Window 中自然穿透 Turnstile 并高保真抓取附表，支持失败时通过已认证浏览器会话直落下载。
- **DOI 与重定向基址解析**：直接接受 DOI 链接，自动跟随重定向并以最终着陆页或 `<base>` 标签作为相对链接的基址，杜绝错误拼接到 `doi.org`
- **全方位表格与附件识别**：支持 `Supplementary Table S1`、`Extended Data Table 1`、`补充表 S1`、`附表 1` 等中英文命名，覆盖连字符命名（如 `table-s1.pdf`）、`aria-label`、`title`、`download` 属性及按钮控件
- **开放获取穿透直取（Tier OA）**：融合 Unpaywall REST API 与 OpenAlex API，一键查询全球合法开放获取镜像（如 PubMed Central / PMC、Europe PMC），遇到出版商付费墙或 Cloudflare 阻断时自动穿透至免认证的公开附表直取通道
- **PubMed Central / PMC 专属提取**：专门适配 NCBI PMC 与 Europe PMC 结构，高保真解析 `/bin/` 附件路径、`click_feat_suppl` 与补充材料容器
- **智能过滤与安全验证**：精准识别补充数据文件（xlsx、csv、zip、docx、pdf 附表等），自动排除正文插图（gr/ga/fx 等）、参考文献链接与外部导航；取消生硬的 1 KB 限制，以文件签名和内容校验为准，安全保留微型 CSV/表格
- **可选 MarkItDown 结构化副本**：支持 `--convert-markdown`，利用微软开源 MarkItDown 将下载的 `.docx`、`.xlsx`、`.pdf`、`.zip` 自动解包生成 Markdown 副本（`.md`）及索引，完全保留原始数据文件
- **自动组织与诊断证据**：按文章标题创建子文件夹，提供详细的候选发现规则与诊断原因
- **边界说明**：本技能专注于下载与管理**外部独立的补充数据文件与附件**（包括独立下载的 HTML 附表与原始数据包），**不提取或解析正文排版内的内联 HTML 表格**（正文内联表格请使用网页正文提取工具）。对于下载的数据文件，MarkItDown 作为可选副本生成器，绝不覆盖或损坏原始科研数据。

## 前置安装

首次使用前，需要安装 Scrapling 及其浏览器依赖：

```bash
pip install "scrapling[all]"
scrapling install
```

## 使用方法

### 1. 轻量版下载器 (`scripts/journal_downloader.py`)

适用于单篇快速抓取与 Cloudflare 绕过：

```bash
python scripts/journal_downloader.py <url_or_doi> [-o output_dir] [options]
```

#### 基本示例

```bash
# 使用 DOI
python scripts/journal_downloader.py "https://doi.org/10.1016/j.oregeorev.2022.104949"

# 使用直接链接
python scripts/journal_downloader.py "https://www.sciencedirect.com/science/article/pii/S0169136822002578"

# 指定输出目录
python scripts/journal_downloader.py "<doi>" -o ./my_data

# 仅列出补充文件链接与诊断证据，不下载
python scripts/journal_downloader.py "<doi>" --list-only
```

#### 常用参数

| 参数 | 说明 |
|------|------|
| `-o`, `--output-dir` | 保存补充附件的输出目录（默认 `./downloads`） |
| `--headful` | 显示浏览器窗口（非无头模式），用于调试 |
| `--no-cloudflare` | 禁用 Cloudflare 绕过（对于无防护的网站） |
| `--real-chrome` | 使用系统安装的 Chrome，而非 Chromium |
| `--cdp` | 自动探测并直连正在运行的日常浏览器（复用已登录凭据、机构SSO并自然穿透 Turnstile） |
| `--cdp-url` | 指定 CDP 调试端点 URL 或端口（如 `http://127.0.0.1:9222`、`9222`） |
| `--bsk`, `--browserskill` | 直连腾讯 BrowserSkill (`bsk`)，借用日常浏览器会话与机构 SSO 穿透 Turnstile |
| `--bsk-browser` | 指定 BrowserSkill 目标浏览器实例名称或 ID |
| `--bsk-tab` | 指定借用日常浏览器已打开标签页 ID（免重新打开、直接继承阅读会话） |
| `--proxy` | 代理地址 `http://user:pass@host:port` |
| `--timeout` | 页面加载超时毫秒数（默认 60000） |
| `--wait-selector` | 等待特定 CSS 选择器出现后再抓取 |
| `--list-only` | 仅列出候选文件、URL、区块及匹配规则证据，不下载 |
| `--oa` | 优先/强制使用开放获取镜像穿透（PMC / Europe PMC） |
| `--no-oa` | 禁用开放获取镜像穿透查询 |
| `--convert-markdown` | 下载完成后，使用 MarkItDown 将附表与文档转为 Markdown 副本（`.md`） |

#### 调试示例

```bash
# 显示浏览器窗口排查问题
python scripts/journal_downloader.py "<url>" --headful --no-cloudflare

# 如果 Cloudflare 验证需要等待特定元素
python scripts/journal_downloader.py "<url>" --wait-selector "#article-body"
```

### 2. 集成版下载器 (`scripts/scansci_supp_downloader.py`)

适用于高校/科研机构环境，多级加速（Elsevier API 直连 → CDN 极速探测 → WebVPN/Cookies 注入 → 浏览器动态渲染兜底）：

```bash
python scripts/scansci_supp_downloader.py <url_or_doi> [-o output_dir] [options]
```

#### 基本示例

```bash
# 校园网/VPN 下秒级下载（自动走 API/CDN）
python scripts/scansci_supp_downloader.py "https://doi.org/10.1016/j.oregeorev.2022.104949"

# 自定义输出目录
python scripts/scansci_supp_downloader.py "<doi>" -o ./journal_downloads

# 仅探测并输出诊断证据，不实际写入文件
python scripts/scansci_supp_downloader.py "<doi>" --list-only
```

#### 常用参数

| 参数 | 说明 |
|------|------|
| `url_or_doi` | 论文 DOI（如 `10.1016/...`）或着陆页完整 URL |
| `-o`, `--output-dir` | 保存补充附件的输出目录（默认 `./journal_downloads`） |
| `--no-api` | 跳过 Elsevier API 校园网直连层（Tier A） |
| `--skip-cdn` | 跳过 Elsevier CDN 极速探测层（Tier 0） |
| `--max-mmc` | CDN 探测扫描的最大 MMC 编号（默认 25） |
| `--max-misses` | 连续未命中停止 CDN 探测的阈值（默认 4） |
| `--no-vpn` | 禁用 WebVPN 代理转换与转链 |
| `--no-cookies` | 禁用已保存凭据与 Cookies 注入 |
| `--headful` | 抓取网页时显示真实浏览器窗口 |
| `--list-only` | 仅输出发现的候选补充文件与诊断证据，不触发下载 |
| `--cdp` | 自动探测并直连正在运行的日常浏览器（复用已登录凭据、机构SSO并自然穿透 Turnstile） |
| `--cdp-url` | 指定 CDP 调试端点 URL 或端口（如 `http://127.0.0.1:9222`、`9222`） |
| `--bsk`, `--browserskill` | 直连腾讯 BrowserSkill (`bsk`)，借用日常浏览器会话与机构 SSO 穿透 Turnstile |
| `--bsk-browser` | 指定 BrowserSkill 目标浏览器实例名称或 ID |
| `--bsk-tab` | 指定借用日常浏览器已打开标签页 ID（免重新打开、直接继承阅读会话） |
| `--oa` | 优先/强制使用开放获取镜像穿透（PMC / Europe PMC） |
| `--no-oa` | 禁用开放获取镜像穿透查询 |
| `--convert-markdown` | 下载完成后，使用 MarkItDown 将附表与文档转为 Markdown 副本（`.md`） |

## 认证与会话支持区分

本技能包含两个互补的下载器，所支持的认证机制有所不同：

### 1. 集成版下载器 (`scripts/scansci_supp_downloader.py`)
- **定位**：深度集成 `scansci-pdf` 的高校与科研机构多级下载器。
- **支持认证**：
  - **机构 IP 识别（Tier A）**：校园网下直连 Elsevier Article API，秒级提取全部 MMC 并走 CDN 下载。
  - **CDP 日常浏览器直连（Tier 3）**：支持 `--cdp` 自动发现或 `--cdp-url` 直连用户日常运行的 Chrome/Edge 浏览器，复用真实登录态与校园 SSO，100% 自然通过 Cloudflare Turnstile。
  - **scansci-pdf 已存 Cookie**：自动读取 scansci-pdf 已保存的出版商 Cookies、CARSI 凭据与 WebVPN 会话，亦可读取登录向导保存的会话。
  - **校园 WebVPN / CARSI**：支持 WebVPN 自动转链与 CARSI SSO 会话凭证注入。
  - **登录向导 Cookies**：自动读取 `scripts/login_publishers.py` 保存的会话。
  - **动态渲染兜底**：当检测到页面声明的补充文件缺漏或补充容器存在但未提取到候选时，自动触发浏览器多级回退渲染与重试。

### 2. 轻量版下载器 (`scripts/journal_downloader.py`)
- **定位**：独立的 Scrapling 极简下载器，无需安装配置复杂的 scansci-pdf 机构网络组件。
- **支持认证与动态特性**：
  - **Cloudflare 隐身绕过**：内置 Turnstile/WAF 人机验证绕过。
  - **CDP 日常浏览器直连**：支持 `--cdp` / `--cdp-url`，可直接附加到已打开的 Chrome/Edge 会话，无需启动孤立浏览器，轻松下载机构订阅附表。
  - **登录向导 Cookies**：运行 `python scripts/login_publishers.py` 完成出版社登录后，轻量版在页面抓取和文件下载中均会自动注入已保存的 Cookies。
  - **动态 SPA 占位重试**：当静态抓取发现补充容器未注水（incomplete）时，自动触发补充容器选择器等待并重试；亦可通过 `--wait-selector` 手动指定。
  - *(注：轻量版不读取 scansci-pdf 目录中的 WebVPN/CARSI 凭据，仅支持手动登录向导保存的 Cookies 或 live CDP 会话)*。

### 3. Tencent BrowserSkill 深度协同 (`scripts/browserskill_adapter.py`)
- **定位**：通过腾讯开源 [Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill) (`bsk`) 与用户日常运行的 Chrome/Edge 浏览器建立高可用通信管道。
- **与传统 CDP 直连的区别与核心优势**：
  - **免配置调试端口**：传统 CDP 直连要求用户完全关闭所有浏览器窗口并带 `--remote-debugging-port=9222` 重启，流程割裂；BrowserSkill 通过浏览器扩展 + 本地 daemon (`bsk daemon start`)，AI Agent 随时可接入正在使用的浏览器。
  - **无感复用校园网 SSO 与机构权限**：直接借用用户已登录的大学 CARSI、校外 WebVPN、Elsevier ScienceDirect、Nature、Springer 等付费账号与 Cookie，无需手动导出注入 `cookies.json`。
  - **100% 自然绕过 Cloudflare Turnstile**：依托用户真实的浏览器指纹与环境，人机验证自然通过。
  - **无打扰后台 Agent Window (`--no-focus`)**：任务在独立的 Agent 窗口运行，不会抢占桌面焦点或打扰用户正常网页浏览。
  - **标签页借用（Tab Borrowing）零摩擦协同**：支持 `--bsk-tab <tab-id>` 或 `borrow-fetch <tab-id>`。若用户在日常浏览器中已打开文献页面，Agent 可直接临时借用该标签页提取补充信息与下载文件，任务完成后自动归还，无需重复导航和人机验证。
  - **原生高保真 DOM 提取 (`bsk get-html`)**：通过 BrowserSkill 原生 `get-html` 命令快速提取完整页面 DOM，自动展开 `<details>` 与折叠手风琴（accordion）附表区块，兼顾内存效率与速度。
  - **会话持久化与已认证下载兜底**：页面解析与附件下载共享同一浏览器会话生命周期；当常规 HTTP 请求因权限问题遭遇 401/403 付费墙拦截时，下载器自动利用当前已认证会话的 Cookie/身份发起在页 Base64 流式直取，并以 `bsk download` 兜底完成下载。
- **命令行使用**：
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

  # 观察文献页面中的交互元素与引用（@e1, @e2 等）
  python scripts/browserskill_adapter.py observe "<doi_or_url>"

  # 轻量版下载器使用 BrowserSkill（支持指定浏览器或借用标签页）
  python scripts/journal_downloader.py "<doi_or_url>" --bsk
  python scripts/journal_downloader.py "<doi_or_url>" --bsk --bsk-tab <tab_id>

  # 高级版下载器使用 BrowserSkill
  python scripts/scansci_supp_downloader.py "<doi_or_url>" --bsk
  python scripts/scansci_supp_downloader.py "<doi_or_url>" --bsk --bsk-tab <tab_id>
  ```

### 手动登录向导

若需要保存出版社登录授权会话（如校外访问、未配置机构 WebVPN/CARSI 或无头服务器环境）：

```bash
python scripts/login_publishers.py
```

该向导会启动可见浏览器窗口，在完成 Elsevier、Springer、Nature 等登录后，自动将 Cookies 保存至 `~/.journal_supp_downloader_profile/cookies.json`。两个下载器均会自动复用该会话。

## 工作流程

1. **获取页面与基址计算**：
   - 使用 Scrapling 或 Requests 抓取文章页面，解决 Cloudflare 挑战并跟随重定向。
   - 解析最终着陆页 URL 及 HTML 中的 `<base href>` 标签，作为解析相对附件地址的基准 URL。
2. **多维度候选扫描（统一策略）**：
   - **策略 1（元数据与标准规范）**：提取 Highwire `citation_supplementary_material`、Elsevier MMC 等元数据。
   - **策略 2（补充材料区域）**：在 Supplementary Material / Appendix / Supporting Information 等区块内提取文件与按钮。
   - **策略 3（表格标签与中英文规则）**：匹配 `Table S1`、`Supplementary Table 1`、`Extended Data Table 1`、`补充表 S1`、`附表 1` 等，涵盖文本、`aria-label`、`title`、`download` 属性及表格说明。
   - **策略 4（控件与出版商提取器）**：扫描 `<button data-url="...">`、`onclick` 事件、嵌入式 JSON/API 以及针对 Elsevier、Springer、Nature、Wiley 的专属规则。
3. **完整性诊断与浏览器回退**：
   - 自动检测页面声明的附件数（如 “4 Supplementary Tables”）。
   - 若发现候选集不完整（如候选数少于声明数，或存在补充材料区块但未提取出附件），高级版自动回退至浏览器执行 JS 补查。
4. **验证与下载**：
   - 排除正文插图与引文链接。
   - 以 HTTP 状态、文件签名魔数（ZIP/PDF/GZIP 等）和错误页特征进行内容级校验，安全保留小于 1 KB 的真实小型 CSV 或简易数据表。

## 支持的期刊平台

- **Elsevier / ScienceDirect**：支持 API 直连、CDN 极速探测、MMC 识别与组件按钮解析
- **Springer / SpringerLink**：识别 supplementary 区域文件、`data-track-action` 按钮与表格
- **Nature / Nature.com**：识别 Extended Data Table、Supplementary Information 链接及 meta 标签
- **Wiley / Wiley Online Library**：识别 `downloadSupplement` 动作、补充区域及表格文件
- **Taylor & Francis / MDPI / GeoSciWorld**：支持 `/s1` 等路径规则及各类补充文件链接
- **其他期刊网站**：通用表格命名识别、补充区块定位与 Cloudflare 绕过

## 工具脚本

| 脚本 | 说明 |
|------|------|
| `scripts/browserskill_adapter.py` | 腾讯 BrowserSkill (`bsk`) 深度协同适配器与 CLI 接口模块（状态诊断、会话管理、DOM 提取与下载兜底） |
| `scripts/supp_finder.py` | 统一的补充附件与表格候选识别、证据记录及文件验证模块 |
| `scripts/oa_resolver.py` | Unpaywall & OpenAlex 开放获取（OA）穿透直取与 PMC 镜像解析模块（支持 CLI 独立查询：`python scripts/oa_resolver.py <doi>`） |
| `scripts/markitdown_converter.py` | 微软 MarkItDown 附表多格式智能转 Markdown 模块（支持 CLI 独立转换：`python scripts/markitdown_converter.py <dir_or_file>`） |
| `scripts/journal_downloader.py` | 基础轻量版页面分析与下载器（Scrapling StealthyFetcher + Cookie 复用 + BrowserSkill 支持） |
| `scripts/scansci_supp_downloader.py` | 高级集成版下载器（API/CDN/Cookie 多级策略 + 完整性动态渲染 + BrowserSkill 支持） |
| `scripts/login_publishers.py` | 出版社登录与 Cookie 配置向导 |
| `scripts/playwright_utils.py` | Playwright Chromium 自动定位与 CDP / BrowserSkill 连接工具 |
| `scripts/test_api.py` | Elsevier API 连通性测试 |
| `scripts/test_fetch.py` | 校园网直连与附件解析测试 |

## 常见问题

**Q: 在 AI Agent 安装 Tencent/BrowserSkill 后是不是能更好地运用该技能的功能？**  
A: **是的，会带来显著的质的飞跃**：
1. **彻底免去配置 Chrome 远程调试端口**：传统 CDP 直连要求用户彻底退出正在工作的浏览器并携带 `--remote-debugging-port=9222` 重启；而安装 Tencent BrowserSkill 后，只需日常浏览器加载其轻量扩展并启动后台守护进程 (`bsk daemon start`)，AI Agent 即可直接与日常浏览器通信。
2. **无感复用校园网 SSO 与机构权限**：用户日常浏览器只要保持校园网 VPN / CARSI 或 Elsevier/Springer 登录，BrowserSkill 就能直接借用其身份发起请求，无需手动提取和更新 `cookies.json`。
3. **100% 自然绕过 Cloudflare Turnstile**：依托用户真实的浏览器指纹与环境，人机验证自然通过，杜绝无头浏览器被 WAF 阻断的困境。
4. **后台静默运行（`--no-focus`）**：任务在独立的 Agent 窗口运行，不会夺取用户屏幕焦点或干扰日常工作。
5. **双向下载兜底**：若常规 HTTP 请求因权限问题遭遇 401/403 付费墙，下载器会自动回退至 `bsk download` 通过真实浏览器会话完成附件下载。

**Q: 显示 "No Cloudflare challenge found" ？**  
A: 这是正常信息，说明该网站当前没有触发 Cloudflare 验证。脚本仍会正常抓取。

**Q: 下载了多余的图片/PDF ？**  
A: 脚本已自动排除文章正文插图（`gr1.jpg` 等）和参考文献链接。如有新变种，可在 `supp_finder.py` 的排除规则中添加。

**Q: 小于 1 KB 的文件会不会被丢弃？**  
A: 不会。现已采用基于文件头签名与错误页检测的验证逻辑，只要是合法的 CSV、TSV 或文本数据，即使只有几十字节也会完整保留。

