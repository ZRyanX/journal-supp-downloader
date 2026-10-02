---
name: journal-supp-downloader
description: 使用 Scrapling 从学术期刊网站（Elsevier、Springer、Nature 等）自动下载补充数据/附件。适用于具有 Cloudflare 防护的期刊网站。支持 DOI 链接和直接文章链接。
---

# Journal Supplementary Data Downloader

本技能提供了一套完整的流程，用于从学术期刊网站（如 Elsevier ScienceDirect、SpringerLink、Nature 等）自动下载文章的补充数据文件（Supplementary Data、Appendix、MMC 文件、独立表格等）。

## 核心能力与处理边界

- **Cloudflare 绕过**：使用 Scrapling 的 `StealthyFetcher` 自动绕过 Cloudflare Turnstile/Interstitial 检测
- **DOI 与重定向基址解析**：直接接受 DOI 链接，自动跟随重定向并以最终着陆页或 `<base>` 标签作为相对链接的基址，杜绝错误拼接到 `doi.org`
- **全方位表格与附件识别**：支持 `Supplementary Table S1`、`Extended Data Table 1`、`补充表 S1`、`附表 1` 等中英文命名，覆盖连字符命名（如 `table-s1.pdf`）、`aria-label`、`title`、`download` 属性及按钮控件
- **智能过滤与安全验证**：精准识别补充数据文件（xlsx、csv、zip、docx、pdf 附表等），自动排除正文插图（gr/ga/fx 等）、参考文献链接与外部导航；取消生硬的 1 KB 限制，以文件签名和内容校验为准，安全保留微型 CSV/表格
- **自动组织与诊断证据**：按文章标题创建子文件夹，提供详细的候选发现规则与诊断原因
- **边界说明**：本技能专注于下载**外部独立的补充数据文件与附件**（包括独立下载的 HTML 附表），**不提取或解析正文排版内的内联 HTML 表格**（正文内联表格请使用网页正文提取工具）。

## 前置安装

首次使用前，需要安装 Scrapling 及其浏览器依赖：

```bash
pip install "scrapling[all]"
scrapling install
```

## 使用方法

```bash
python scripts/journal_downloader.py <url_or_doi> [-o output_dir] [options]
```

### 基本示例

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

### 高级选项

| 参数 | 说明 |
|------|------|
| `--headful` | 显示浏览器窗口（非无头模式），用于调试 |
| `--no-cloudflare` | 禁用 Cloudflare 绕过（对于无防护的网站） |
| `--real-chrome` | 使用系统安装的 Chrome，而非 Chromium |
| `--proxy` | 代理地址 `http://user:pass@host:port` |
| `--timeout` | 页面加载超时毫秒数（默认 60000） |
| `--wait-selector` | 等待特定 CSS 选择器出现后再抓取 |
| `--list-only` | 仅列出候选文件、URL、区块及匹配规则证据，不下载 |

### 调试示例

```bash
# 显示浏览器窗口排查问题
python scripts/journal_downloader.py "<url>" --headful --no-cloudflare

# 如果 Cloudflare 验证需要等待特定元素
python scripts/journal_downloader.py "<url>" --wait-selector "#article-body"
```

## 认证与会话支持区分

本技能包含两个互补的下载器，所支持的认证机制有所不同：

### 1. 集成版下载器 (`scripts/scansci_supp_downloader.py`)
- **定位**：深度集成 `scansci-pdf` 的高校与科研机构多级下载器。
- **支持认证**：
  - **机构 IP 识别（Tier A）**：校园网下直连 Elsevier Article API，秒级提取全部 MMC 并走 CDN 下载。
  - **日常浏览器克隆**：自动克隆本地 Chrome/Edge 的登录态与机构访问 Cookies。
  - **校园 WebVPN / CARSI**：支持 WebVPN 自动转链与 CARSI SSO 会话凭证注入。
  - **登录向导 Cookies**：自动读取 `scripts/login_publishers.py` 保存的会话。

### 2. 轻量版下载器 (`scripts/journal_downloader.py`)
- **定位**：独立的 Scrapling 极简下载器，无需安装配置复杂的 scansci-pdf 机构网络组件。
- **支持认证**：
  - **Cloudflare 隐身绕过**：内置 Turnstile/WAF 人机验证绕过。
  - **登录向导 Cookies**：运行 `python scripts/login_publishers.py` 完成出版社登录后，轻量版在页面抓取和文件下载中均会自动注入已保存的 Cookies。
  - *(注：轻量版不自动克隆 Chrome 本地目录，亦不包含 WebVPN/CARSI 代理)*。

### 手动登录向导

若需要保存出版社登录授权会话（如校外访问、Safari/Firefox 用户或无头服务器）：

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
| `scripts/supp_finder.py` | 统一的补充附件与表格候选识别、证据记录及文件验证模块 |
| `scripts/journal_downloader.py` | 基础轻量版页面分析与下载器（Scrapling StealthyFetcher + Cookie 复用） |
| `scripts/scansci_supp_downloader.py` | 高级集成版下载器（API/CDN/Cookie 多级策略 + 完整性动态渲染） |
| `scripts/login_publishers.py` | 出版社登录与 Cookie 配置向导 |
| `scripts/playwright_utils.py` | Playwright Chromium 自动定位工具 |
| `scripts/test_api.py` | Elsevier API 连通性测试 |
| `scripts/test_fetch.py` | 校园网直连与附件解析测试 |

## 常见问题

**Q: 显示 "No Cloudflare challenge found" ？**  
A: 这是正常信息，说明该网站当前没有触发 Cloudflare 验证。脚本仍会正常抓取。

**Q: 下载了多余的图片/PDF ？**  
A: 脚本已自动排除文章正文插图（`gr1.jpg` 等）和参考文献链接。如有新变种，可在 `supp_finder.py` 的排除规则中添加。

**Q: 小于 1 KB 的文件会不会被丢弃？**  
A: 不会。现已采用基于文件头签名与错误页检测的验证逻辑，只要是合法的 CSV、TSV 或文本数据，即使只有几十字节也会完整保留。
