---
name: journal-supp-downloader
description: 从 DOI 或期刊文章链接识别、下载和校验独立的补充材料、附表及数据附件。支持 Elsevier、Springer、Nature、Wiley、PMC 等；可选复用 Tencent BrowserSkill 的已登录浏览器会话。不用于提取正文内联表格。
---

# Journal Supplementary Data Downloader

下载文章的独立补充文件（xlsx、csv、zip、docx、PDF、独立 HTML 附表等），保留原始文件，并按文章标题组织输出。正文内联 HTML 表格不属于本技能范围。

## 选择入口

- **单篇常规下载**：`scripts/journal_downloader.py`，使用 Scrapling 抓取文章并识别附件。
- **高校、校园网或 Elsevier 多级下载**：`scripts/scansci_supp_downloader.py`，增加 Elsevier API/CDN、scansci-pdf 已保存凭据、WebVPN 和 CARSI 支持。
- **需要网站现有登录态**：为上述入口增加 `--bsk` 或 `--bsk-auto`。阅读 [BrowserSkill 接入说明](references/browserskill.md) 了解配置、会话边界与故障诊断。

安装 Python 依赖及独立浏览器内核：

```bash
pip install -r requirements.txt
scrapling install
```

BrowserSkill 是可选的外部运行时，单独安装 `bsk` 和浏览器扩展；安装本技能不会自动安装它们。未启用 BrowserSkill 时，普通下载流程仍可使用。

## 执行流程

1. 接受 DOI 或文章 URL，按需求选择入口。可先用 `--list-only` 查看候选文件及其识别证据。
2. 使用最终着陆页 URL 和页面 `<base>` 解析相对附件地址，不能把出版商相对路径拼接到 `doi.org`。
3. 检查候选数量与页面声明的附件数。发现补充区块却没有文件、数量不足或动态内容未加载时，使用同一认证会话补查。
4. 下载后以文件签名、内容和错误页特征验证；微小但有效的 CSV 也应保留。不要把登录页、验证码页或 HTTP 错误页当成附件。
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

- 默认不调用 BrowserSkill；`--bsk` 优先用于文章页面，`--bsk-auto` 允许失败/缺漏恢复。`--no-bsk` 显式禁用。
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
| `--convert-markdown` | 用 MarkItDown 生成副本，保留原始文件 |

轻量版另支持 `--timeout`（毫秒）、`--wait-selector`、`--proxy`、`--real-chrome`、`--no-cloudflare`。高级版另支持 `--no-api`、`--skip-cdn`、`--max-mmc`、`--max-misses`、`--no-vpn`、`--no-cookies`。精确参数以各脚本 `--help` 为准。

## 认证与恢复

- **自动分级浏览器接管与有头兜底**（快速 API / CDN / 请求与无头抓取均被拦截或结果不完整时按序自动触发，正常直连或已完成的文章不弹窗）：
  1. **优先接管已打开的日常有头浏览器（无额外弹窗、零空闲开销）**：若未禁用 `--no-bsk` 且 `bsk` 已就绪则优先复用已连接浏览器；否则若未禁用 `--no-cdp` 且检测到本地开放调试端口的 Chrome（`discover_active_cdp_port`）则自动挂载并继承实时登录态与挑战通过状态。
  2. **备用/开箱即用独立有头真 Chrome 回退**：当上述日常浏览器未开启或仍未取全且存在桌面图形环境时，若未传 `--no-auto-headful`，自动拉起 `headless=False`（自动检测本地 Chrome 启用 `real_chrome=True`、`network_idle=False`、`solve_cloudflare=True`）突破 Turnstile / Cloudflare / JS 动态渲染并延续到附件下载阶段。
- **BrowserSkill**：浏览器扩展与 `bsk` 通信，使用已登录的浏览器，无需导出 Cookie。连接故障先查看 `python scripts/browserskill_adapter.py status --json` 和 `bsk doctor`。
- **独立登录向导**：`python scripts/login_publishers.py` 保存会话到 `~/.journal_supp_downloader_profile/cookies.json`；两个下载器均可读取。
- **网络与本地 API 配置**：默认直连校园网/机构 IP，无需设置代理环境（未显式传入 `--proxy` 时会忽略环境变量中的 `HTTP(S)_PROXY` / `ALL_PROXY`，不污染全局环境变量）；支持通过环境变量、仓库根目录 `.env`（模板见 `.env.example`，不提交仓库）或 `~/.journal_supp_downloader_profile/config.json` 配置 `ELSEVIER_API_KEY`、`IS_CAMPUS_NETWORK`、`ELSEVIER_INSTTOKEN`、`UNPAYWALL_EMAIL`、`CROSSREF_EMAIL`。
- **scansci-pdf 凭据**：仅高级版读取其机构、WebVPN/CARSI 配置；轻量版不读取这些配置。
- **GSA / DiVA 专用通道**：当 GeoScienceWorld（`10.1130/`）受反爬阻塞或未返回候选时，自动回退 Crossref → figshare API（含 `translate.goog` 镜像回退）；识别 `diva-portal.org` / `diva2:` / `urn:nbn:se:` 时启用 DiVA OAI-PMH 与镜像回退。
- **开放获取镜像**：可通过 Unpaywall/OpenAlex 查找 PMC/Europe PMC 等公开来源。公开副本不一定包含全部补充材料，仍需核对完整性。
- **动态补充区域**：轻量版可用 `--wait-selector` 等待实际附件元素；等待超时应视为恢复失败，不能当作内容已经就绪。

## 维护与验证

共享识别与校验逻辑位于 `scripts/supp_finder.py`；BrowserSkill CLI 边界和会话管理位于 `scripts/browserskill_adapter.py`。保留适配层与外部 BrowserSkill 的边界，不复制其扩展或守护进程源码。

```bash
python -m pytest -q
```

自动测试覆盖模拟的 CLI 协议、会话清理、文件落盘和下载恢复；真实出版商登录、浏览器扩展交互及大文件传输需要在已配置的浏览器中另行验证。
