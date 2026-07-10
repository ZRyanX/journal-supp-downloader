---
name: journal-supp-downloader
description: 使用 Scrapling 从学术期刊网站（Elsevier、Springer、Nature 等）自动下载补充数据/附件。适用于具有 Cloudflare 防护的期刊网站。支持 DOI 链接和直接文章链接。
---

# Journal Supplementary Data Downloader

本技能提供了一套完整的流程，用于从学术期刊网站（如 Elsevier ScienceDirect、SpringerLink、Nature 等）自动下载文章的补充数据文件（Supplementary Data、Appendix、MMC 文件等）。

## 核心能力

- **Cloudflare 绕过**：使用 Scrapling 的 `StealthyFetcher` 自动绕过 Cloudflare Turnstile/Interstitial 检测
- **DOI 支持**：直接接受 DOI 链接，自动跟随重定向到期刊网站
- **智能过滤**：精准识别补充数据文件（xlsx、csv、zip、docx 等），自动排除文章插图、参考文献链接、外部导航链接
- **自动组织**：按文章标题创建子文件夹，同一文献的所有补充文件归入一处

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

# 仅列出补充文件链接，不下载
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

### 调试示例

```bash
# 显示浏览器窗口排查问题
python scripts/journal_downloader.py "<url>" --headful --no-cloudflare

# 如果 Cloudflare 验证需要等待特定元素
python scripts/journal_downloader.py "<url>" --wait-selector "#article-body"
```

## 认证与会话配置

### 自动会话克隆（默认，无需操作）

如果你日常使用 Chrome 或 Edge 浏览器且已登录相关数据库，下载器脚本在运行时会自动安全克隆当前浏览器的登录状态。

### 手动登录向导（备选方案）

如果自动克隆失效、使用 Safari/Firefox，或在服务器环境运行，可运行出版社登录向导：

```bash
# 交互式登录向导
python scripts/login_publishers.py
```

该向导会：
1. 启动可见浏览器窗口，打开 Elsevier、Springer、Nature 等出版社网站
2. 你在浏览器中完成机构登录/授权
3. 关闭窗口后自动保存 Cookies 会话

后续运行 `journal_downloader.py` 或 `scansci_supp_downloader.py` 时将自动注入已保存的 Cookies。

## 工作流程

1. **获取页面**：`StealthyFetcher` 启动无头浏览器，解决 Cloudflare 挑战（如有），跟随 DOI 重定向，加载文章页面
2. **扫描链接**：使用组合策略定位补充数据：
   - 策略 1：CSS 选择器匹配 Elsevier MMC 链接（`mmc1.xlsx` 等）
   - 策略 2：在 Supplementary Material / Appendix 标题区域下查找文件链接
   - 策略 3：扫描所有包含 `mmc` 关键字的链接
3. **过滤判断**：
   - ✅ 保留：MMC 文件、supplementary/supp 命名文件、数据扩展名文件
   - ❌ 排除：文章插图（`gr1.jpg` 等）、参考文献链接（scholar.google.com）、外部导航、页面锚点
4. **下载文件**：用 HTTP `Fetcher` 下载数据文件（不需要浏览器）

## 支持的期刊平台

- **Elsevier / ScienceDirect**：识别 `mmc*.xlsx`、`mmc*.docx` 等 MMC 文件
- **Springer / SpringerLink**：识别 supplementary 区域的文件链接
- **Nature / Nature.com**：识别 Supplementary Information 链接
- **Wiley / Wiley Online Library**：识别补充数据链接
- **Taylor & Francis**：识别补充文件链接
- **其他使用 Cloudflare 的期刊网站**

## 工具脚本

| 脚本 | 说明 |
|------|------|
| `scripts/journal_downloader.py` | 基础页面分析下载器（Scrapling StealthyFetcher） |
| `scripts/scansci_supp_downloader.py` | 高级集成版下载器（API/CDN/Cookie 多级策略） |
| `scripts/login_publishers.py` | 出版社登录与 Cookie 配置向导 |
| `scripts/playwright_utils.py` | Playwright Chromium 自动定位工具 |
| `scripts/test_api.py` | Elsevier API 连通性测试 |
| `scripts/test_fetch.py` | 校园网直连与附件解析测试 |

## 常见问题

**Q: 显示 "No Cloudflare challenge found" ？**  
A: 这是正常信息，说明该网站当前没有触发 Cloudflare 验证。脚本仍会正常抓取。

**Q: 下载了多余的图片/PDF ？**  
A: 脚本已过滤文章插图和参考文献链接。如果仍有误匹配，可以在 `EXCLUDE_URL_PATTERNS` 中添加排除规则。

**Q: 云防护绕过失败？**  
A: 尝试 `--headful --real-chrome --timeout 120000` 增大超时并用真实 Chrome 浏览器。
