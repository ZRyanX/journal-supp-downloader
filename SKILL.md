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
| `--headful` | 显示独立抓取浏览器，便于排查 |
| `--cdp` / `--cdp-url` | 显式接入已经开放调试端口的 Chromium 浏览器 |
| `--oa` / `--no-oa` | 优先查询/禁用开放获取仓储镜像查询 |
| `--convert-markdown` | 用 MarkItDown 生成副本，保留原始文件 |

轻量版另支持 `--timeout`（毫秒）、`--wait-selector`、`--proxy`、`--real-chrome`、`--no-cloudflare`。高级版另支持 `--no-api`、`--skip-cdn`、`--max-mmc`、`--max-misses`、`--no-vpn`、`--no-cookies`。精确参数以各脚本 `--help` 为准。

## 认证与恢复

- **BrowserSkill**：浏览器扩展与 `bsk` 通信，使用已登录的浏览器，无需导出 Cookie。连接故障先查看 `python scripts/browserskill_adapter.py status --json` 和 `bsk doctor`。
- **独立登录向导**：`python scripts/login_publishers.py` 保存会话到 `~/.journal_supp_downloader_profile/cookies.json`；两个下载器均可读取。
- **scansci-pdf 凭据**：仅高级版读取其机构、WebVPN/CARSI 配置；轻量版不读取这些配置。
- **开放获取镜像**：可通过 Unpaywall/OpenAlex 查找 PMC/Europe PMC 等公开来源。公开副本不一定包含全部补充材料，仍需核对完整性。
- **动态补充区域**：轻量版可用 `--wait-selector` 等待实际附件元素；等待超时应视为恢复失败，不能当作内容已经就绪。

## 维护与验证

共享识别与校验逻辑位于 `scripts/supp_finder.py`；BrowserSkill CLI 边界和会话管理位于 `scripts/browserskill_adapter.py`。保留适配层与外部 BrowserSkill 的边界，不复制其扩展或守护进程源码。

```bash
python -m pytest -q
```

自动测试覆盖模拟的 CLI 协议、会话清理、文件落盘和下载恢复；真实出版商登录、浏览器扩展交互及大文件传输需要在已配置的浏览器中另行验证。
