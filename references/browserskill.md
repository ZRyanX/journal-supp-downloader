# BrowserSkill 接入与诊断

本项目通过外部 `bsk` CLI 接入 [Tencent/BrowserSkill](https://github.com/Tencent/BrowserSkill)，不内置浏览器扩展或 daemon。需要使用已有登录态、借用文章标签页或诊断浏览器下载失败时阅读本说明。

## 配置

按 [官方安装说明](https://github.com/Tencent/BrowserSkill#quick-start) 安装 CLI 和 Chrome/Edge 扩展，启用扩展连接。然后检查：

```bash
bsk --version
bsk doctor
python scripts/browserskill_adapter.py status --json
```

`ready: true` 表示已识别到运行中的 daemon 和浏览器连接；不表示已经登录出版商或拥有文章访问权限。状态命令的退出码仅在三者（CLI、daemon、浏览器连接）都可用时为 0。上游 `bsk status` 可能按需启动 daemon。

适配器按顺序从 `BSK_BIN` / `BSK_PATH`、`BSK_INSTALL_DIR`、`BSK_HOME/bin`、PATH 和常见用户目录发现程序。不修改全局 PATH，不自动安装或重启共享 daemon。

## 模式

| 参数 | 行为 |
|---|---|
| 无 BrowserSkill 参数 | 不调用 BrowserSkill；原有 HTTP/Scrapling/CDP 策略独立运行 |
| `--bsk` / `--browserskill` | 页面抓取优先使用 BrowserSkill；高级版仍先尝试 API/CDN |
| `--bsk-auto` | 普通路径失败或附件候选不完整时按需使用 |
| `--no-bsk` | 显式禁用，与其他模式和浏览器选择参数互斥 |
| `--bsk-browser <id-or-label>` | 绑定指定浏览器；单独使用会启用 `--bsk` |
| `--bsk-tab <id>` | 借用指定文章标签页，整个任务复用；单独使用会启用 `--bsk` |

`--bsk-auto` 可与浏览器/标签页选择参数合用。`--no-bsk` 只控制 BrowserSkill，不控制独立的 CDP 功能。

```bash
python scripts/browserskill_adapter.py sessions
python scripts/browserskill_adapter.py tabs --scope user
python scripts/journal_downloader.py "<doi>" --bsk-auto
python scripts/scansci_supp_downloader.py "<doi>" --bsk-browser "<browser-id>" --bsk-tab "<tab-id>"
```

使用用户提供或明确选择的标签页 ID。借用失败时不会改为在其他标签页导航。浏览器扩展可能要求用户确认；不要修改扩展设置规避确认。

## 会话与文件处理

下载入口为每次运行创建一个按需启动的会话管理器。页面读取、快速通道完成后的附件补查、后续下载均通过它复用会话。正常完成、提前返回、异常和 Ctrl+C 都执行清理；进程被强制终止时无法保证执行 Python 清理代码。

借用标签页的操作使用显式 `--tab-id`，不依赖用户或其他操作改变后的活动标签页。结束时归还借用标签页并停止本次会话。若停止失败，诊断信息包含需清理的会话 ID，并保留退出时重试机会；不停止其他会话或共享 daemon。

下载策略：

1. 常规 HTTP 请求优先；401/403、网络错误或无效文件均可进入已启用的浏览器恢复路径。
2. 页内 `fetch` 仅处理不超过 **8 MiB** 的小文件，检查响应长度与实际累计字节数，并在超限时取消读取。它仍受 CORS、Cookie 和站点策略限制。
3. 超限、CORS 错误或无效内容时，通过页面临时链接元素调用原生 `bsk download`。该命令的目标是元素引用/CSS 选择器，不能直接传 URL。结束后删除临时链接。
4. 下载先进入独立临时目录；检查实际文件存在、非空，再经过补充文件内容校验，最后替换目标文件。失败不提交临时文件，不将登录页登记为成功下载。

页内传输使用 Base64，不能称为大文件流式下载。大附件依赖上游原生下载能力；远程 BrowserSkill 模式的文件传输不属于当前保证范围。

## 兼容性与诊断边界

适配实现依据上游源码提交 `e8777ab` 检查了 `status`、`evaluate`、`get-html`、`download` 的命令和返回格式。这里记录的是协议参考，不是已完成真实浏览器验证的版本范围。

- `status` 当前返回 `daemon_version`、`protocol_version`、`browsers` 等字段；同时保留部分旧格式解析。
- `evaluate` 即使退出码为 0，也可能返回 `ok: false`；适配器会将其视为失败，且不重放可能有副作用的脚本。
- `get-html --json` 的 `truncated` 标记不可忽略；截断内容会触发完整 DOM 提取回退。
- 页面元数据中的最终 URL 用于解析附件相对链接。抓取函数返回的 200 是 DOM 提取成功标记，并非实际网络 HTTP 状态。
- 能识别的验证/拒绝访问页面会报错，但此检测并非完整的付费墙或登录页识别系统。

| 现象 | 排查方向 |
|---|---|
| 找不到 `bsk` | CLI 是否安装、当前环境的 PATH，或设置 `BSK_BIN` |
| daemon 存在但 `ready: false` | 浏览器是否打开、扩展是否启用并连接 |
| 指定浏览器/标签页失败 | 用 `bsk browsers`、`tabs --scope user` 核对实例和 ID |
| JS 执行错误 | 检查具体错误，必要时查看命令帮助及上游版本 |
| 人机验证或机构登录页 | 用户在浏览器中完成操作后重试，不承诺自动通过 |
| 下载“成功”但没有有效文件 | 检查是否为登录页、CORS 限制、浏览器未触发下载或上游传输失败 |

完整回归测试：`python -m pytest -q`。另需用真实配置验证：已登录文章、需借用的用户标签页、跨域附件、大于 8 MiB 的附件，以及中断后的标签页归还。
