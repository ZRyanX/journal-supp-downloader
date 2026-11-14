---
name: journal-supp-downloader
description: 从 DOI 或期刊文章链接识别、下载和校验独立的补充材料、附表及数据附件。默认直连并在受阻或漏检时按序自动分级接管浏览器（bsk → 本地 Chrome CDP → 独立有头 Chrome）。支持 Elsevier、Springer、Nature、Wiley、Silverchair/GSW、Atypon、RSC、Cambridge、Frontiers、Copernicus、PeerJ、MDPI、SciEngine、OJS/J-STAGE/中文期刊，以及 PMC、Figshare、ScienceBase、DiVA 等仓储与 Crossref 多出版商回退；可选复用 Tencent BrowserSkill。不用于提取正文内联表格。
---

# Journal Supplementary Data Downloader

下载文章的独立补充文件（xlsx、csv、zip、docx、PDF、独立 HTML 附表等），保留原始文件，并按文章标题组织输出。正文内联 HTML 表格不属于本技能范围。

## 选择入口

- **单篇常规下载**：`scripts/journal_downloader.py`，使用 Requests + Scrapling StealthyFetcher 抓取文章并识别附件。
- **高校、校园网或 Elsevier 多级下载**：`scripts/scansci_supp_downloader.py`，增加 Elsevier API/CDN、`scansci-pdf` 已保存凭据、WebVPN、CARSI 与隧道熔断支持。
- **需要网站现有登录态或应对高强度反爬**：两个入口在受阻/候选不完整时会自动探测已就绪的 BrowserSkill 或本地 Chrome CDP，最终回退至独立有头 Chrome；也可显式传入 `--bsk` / `--bsk-auto` / `--cdp`。阅读 [BrowserSkill 接入说明](references/browserskill.md) 了解配置、会话边界与故障诊断。

安装 Python 依赖及独立浏览器内核：

```bash
pip install -r requirements.txt
scrapling install
```

BrowserSkill 是可选的外部运行时，单独安装 `bsk` 和浏览器扩展；安装本技能不会自动安装它们。未启用 BrowserSkill 时，普通下载流程仍可使用。

## 执行流程

1. 接受 DOI 或文章 URL，按需求选择入口。可先用 `--list-only` 查看候选文件及其识别证据。
2. 使用最终着陆页 URL 和页面 `<base>` 解析相对附件地址（并自动修正正文误写的裸 DOI 链接如 `http://10.5066/...`），不把出版商相对路径拼接到 `doi.org`。
3. 检查候选数量与正文声明的附表/文件数（已过滤参考文献等噪声段落及整包压缩附件误报）。若被拦截、触发 SPA 空壳或结果不完整，自动按序升级至 OA 镜像（Unpaywall / OpenAlex）、`Tier 3-Auto` 浏览器接管、Crossref 多出版商备用站点回退（`[chooser-alt]`）与仓储子页面递归扩展（`RepoBrowserSession` 复用单浏览器会话解析 Figshare / ScienceBase / DiVA）。
4. 下载后结合魔数重命名（修正无后缀或 `.bin` 误命名）、ISO-8859-1 中文文件名修复及文件内容/错误页签名校验，微小但有效的 CSV 也应保留；不把登录页、WAF 挑战壳或 HTTP 错误页当成附件。
5. 汇报已下载数量、输出路径及剩余缺漏。没有发现附件不等于文章没有附件，出现认证/验证页时说明实际阻塞原因。

```bash
# 查看候选与匹配证据
python scripts/journal_downloader.py "<doi_or_url>" --list-only

# 单篇下载
python scripts/journal_downloader.py "<doi_or_url>" -o ./journal_downloads

# 校园网/机构环境，优先 API 和 CDN
python scripts/scansci_supp_downloader.py "<doi_or_url>" -o ./journal_downloads

# 优先用 BrowserSkill 读取文章；下载失败时复用同一会话
python scripts/journal_downloader.py "<doi_or_url>" --bsk

# 保留普通路径，仅在需要恢复时启动 BrowserSkill
python scripts/scansci_supp_downloader.py "<doi_or_url>" --bsk-auto

# 明确使用指定浏览器及用户提供的文章标签页
python scripts/journal_downloader.py "<doi_or_url>" --bsk-browser "<browser-id>" --bsk-tab "<tab-id>"
```

## BrowserSkill 的选择与生命周期

- 默认优先走常规直连/静态与轻量抓取，仅在 `Tier 3-Auto` 触发且检测到已连接的 `bsk` daemon 时自动按需兜底（不自动启动未运行的服务）；`--bsk` 优先用于文章页面，`--bsk-auto` 显式启用按需恢复，`--no-bsk` 显式禁用。
- 单独指定 `--bsk-browser` 或 `--bsk-tab` 会启用 BrowserSkill；与 `--bsk-auto` 合用时保留按需模式。它们不能与 `--no-bsk` 合用。
- 每次下载按需创建一个会话，页面读取、补查和附件下载共用。API/CDN 足以完成且不需浏览器补查时，不创建会话。
- 用户标签页借用后保留到本次任务结束，成功、提前返回、异常和键盘中断均执行归还与会话清理。不要在附件下载前手动归还。
- 指定浏览器或标签页失败时报告错误，不静默改用另一个浏览器。遵循扩展本身的借用确认流程。
- 只清理本次创建的会话，不用 `stop-all` 作为普通恢复手段。
- 可复用浏览器已有访问权限，但不保证通过 Cloudflare、验证码或机构登录；需要时由用户在浏览器中完成验证。

## 其他参数

两个下载器都支持：

| 参数 | 用途 |
|---|---|
| `-o`, `--output-dir` | 输出目录，默认 `./journal_downloads` |
| `--list-only` | 只列出候选与诊断，不下载附件 |
| `--headful` | 从初始页面抓取即显示独立抓取浏览器，便于排查 |
| `--cdp` / `--cdp-url` | 显式接入已经开放调试端口的 Chromium 浏览器 |
| `--no-cdp` | 禁用阻塞/缺漏后的本地 Chrome CDP 自动接管 |
| `--no-auto-headful` / `--no-auto-browser` | 禁用阻塞/缺漏后的独立有头 Chrome 自动回退 |
| `--oa` / `--no-oa` | 优先查询/禁用开放获取仓储镜像查询 |
| `--phase-timeout` | 单篇阶段（发现+下载）总预算秒数，默认 `150` |
| `--browser-goto-timeout` | 浏览器回退单页加载超时秒数，默认 `30` |
| `--browser-fallback-budget` | 浏览器回退阶段总预算秒数，默认 `60` |
| `--convert-markdown` | 用 MarkItDown 生成副本（含 Excel 多行表头展平与 ZIP 内表格转换），保留原始文件 |

轻量版另支持 `--timeout`（毫秒）、`--wait-selector`、`--proxy`、`--real-chrome`、`--no-cloudflare`。高级版另支持 `--no-api`、`--skip-cdn`、`--max-mmc`、`--max-misses`、`--no-vpn`、`--no-cookies`、`--proxy`、`--tunnel-fail-threshold`（默认 `3`）。精确参数以各脚本 `--help` 为准。

## 认证与恢复

- **自动分级浏览器接管与有头兜底**（快速 API / CDN / 请求与无头抓取均被拦截或结果不完整时按序自动触发，正常直连或已完成的文章不弹窗）：
  1. **优先接管已打开的日常有头浏览器（无额外弹窗、零空闲开销）**：若未禁用 `--no-bsk` 且 `bsk` 已就绪则优先复用已连接浏览器；否则若未禁用 `--no-cdp` 且检测到本地开放调试端口的 Chrome（`discover_active_cdp_port`）则自动挂载并继承实时登录态与挑战通过状态。
  2. **备用/开箱即用独立有头真 Chrome 回退**：当上述日常浏览器未开启或仍未取全且存在桌面图形环境时，若未传 `--no-auto-headful`，自动拉起 `headless=False`（自动检测本地 Chrome 启用 `real_chrome=True`、`network_idle=False`、`solve_cloudflare=True`）突破 Turnstile / Cloudflare / JS 动态渲染并延续到附件下载阶段。
- **BrowserSkill**：浏览器扩展与 `bsk` 通信，使用已登录的浏览器，无需导出 Cookie。连接故障先查看 `python scripts/browserskill_adapter.py status --json` 和 `bsk doctor`。
- **独立登录向导**：`python scripts/login_publishers.py` 保存会话到 `~/.journal_supp_downloader_profile/cookies.json`；两个下载器均可读取。
- **网络与本地 API 配置**：默认直连校园网/机构 IP，无需设置代理环境（未显式传入 `--proxy` 时自动忽略 `HTTP(S)_PROXY` / `ALL_PROXY` 等环境代理变量）；支持通过环境变量、仓库根目录 `.env`（模板见 `.env.example`，不提交仓库）或 `~/.journal_supp_downloader_profile/config.json` 配置 `ELSEVIER_API_KEY`、`IS_CAMPUS_NETWORK`、`ELSEVIER_INSTTOKEN`、`UNPAYWALL_EMAIL`、`CROSSREF_EMAIL`。
- **scansci-pdf 凭据**：仅高级版读取其机构、WebVPN/CARSI 配置；轻量版不读取这些配置。
- **多出版商回退与仓储穿透**：当主出版商（如 Lyell Collection `10.1144/` 无 IP 订阅）受限或无候选时，两个入口均支持通过 Crossref `[chooser-alt]` 切换同 DOI 的替代出版商宿主（如 GeoScienceWorld）；同时识别 Figshare（REST API + `__APOLLO_STATE__`）、ScienceBase（JSON + 裸 DOI 修正）、GSA（`10.1130/` → Crossref/Figshare）、DiVA（`diva-portal.org` / `diva2:` / `urn:nbn:se:` OAI-PMH 与镜像回退），并通过 `RepoBrowserSession` 复用浏览器上下文。
- **GeoScienceWorld / Silverchair 超时防护**：自动拦截阻塞渲染或下载的第三方挂起域名（如 `ajax.googleapis.com`），并优先使用 Playwright `context.request` 在同源浏览器上下文下载。
- **开放获取镜像**：可通过 Unpaywall/OpenAlex 查找 PMC/Europe PMC 等公开来源。公开副本不一定包含全部补充材料，仍需核对完整性。
- **动态补充区域**：轻量版可用 `--wait-selector` 等待实际附件元素；等待超时应视为恢复失败，不能当作内容已经就绪。

## 维护与验证

核心模块划分：
- `scripts/scansci_supp_downloader.py` / `scripts/journal_downloader.py`：高级版与轻量版 CLI 入口、多级调度与 Tier 3-Auto 级联编排。
- `scripts/supp_finder.py`：9+ 出版商专用补充材料提取器（Silverchair、Atypon、RSC、Cambridge、Frontiers、Copernicus、PeerJ、MDPI、OJS/J-STAGE/中文期刊、Nuxt/JSON、SciEngine 等）、SPA 空壳检测与完整性质检。
- `scripts/repository_resolver.py` & `scripts/oa_resolver.py`：Figshare/ScienceBase/GSA/DiVA 外部仓储穿透（含 `RepoBrowserSession`）、Crossref `[chooser-alt]` 与 Unpaywall/OpenAlex 公开源解析。
- `scripts/browserskill_adapter.py` & `scripts/playwright_utils.py`：BrowserSkill CLI 边界、CDP 自动探测、Playwright 直连上下文管理与第三方挂起域名拦截。
- `scripts/discovery_state.py` & `scripts/markitdown_converter.py`：发现状态追踪与可选 Markdown 转换（含 `.xlsx`/`.xls` 多行合并表头展平与 `.zip` 内嵌表格转换）。

保留适配层与外部 BrowserSkill 的边界，不复制其扩展或守护进程源码。

```bash
python -m pytest -q
```

自动测试覆盖模拟的 CLI 协议、会话清理、文件落盘、出版商结构解析、仓储穿透与下载恢复；真实出版商登录、浏览器扩展交互及大文件传输需要在已配置的浏览器中另行验证。
