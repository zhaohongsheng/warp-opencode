# WARP Control

> **opencode 出口 IP 管家**：托管 Cloudflare WARP，把 SOCKS5 桥接成标准 HTTP 代理，出口异常时自动换 IP，并可一并接管 [zen-gate](https://github.com/LAGcomcom/zen-gate) 免费 AI 网关。

[English](README.en.md) | **简体中文**

![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-0078D6?style=flat-square)
![python](https://img.shields.io/badge/Python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![deps](https://img.shields.io/badge/dependencies-stdlib%20only-34D399?style=flat-square)
![listen](https://img.shields.io/badge/listen-127.0.0.1-22D3EE?style=flat-square)

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [功能特性](#功能特性)
- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [接入 opencode](#接入-opencode)
- [WARP 后端](#warp-后端)
- [自动换 IP](#自动换-ip)
- [OpenCode Zen 入口（zen-gate）](#opencode-zen-入口zen-gate)
- [配置项](#配置项)
- [Web 控制台与 HTTP API](#web-控制台与-http-api)
- [命令行参数](#命令行参数)
- [目录结构](#目录结构)
- [常见问题](#常见问题)
- [安全说明](#安全说明)
- [致谢与许可](#致谢与许可)

---

## 它解决什么问题

opencode 直连时出口 IP 固定，容易被上游按 IP 限流、封禁，或者免费额度很快耗尽。WARP Control 做了三件事：

1. **托管 WARP**：自动检测并启动本机的 `warp-cli` / `warp-plus` / `usque`，也可以使用内置的 `warp-plus`（首次启动自动下载），或任意能提供本地 SOCKS5 的自定义命令。
2. **桥接代理**：把 WARP 提供的 SOCKS5 转成标准 HTTP 代理，解决 opencode / Node / Bun 对 `socks5://` 支持不完整的问题。
3. **自动换 IP**：持续探测出口 IP；探测失败、IP 保持过久、opencode 日志命中封禁关键词、或 zen-gate 免费额度耗尽时，自动重连 WARP 更换出口 IP。

整个程序只有一个 Python 文件，**仅依赖 Python 3 标准库**。

## 功能特性

- **多后端托管**：`auto` / `builtin` / `warp-cli` / `warp-plus` / `usque` / `custom` 六种模式。
- **免安装后端**：内置 `warp-plus`，首次使用时从官方 Release 下载到 `bin/`，支持镜像加速。
- **SOCKS5 → HTTP 桥接**：完整支持 `CONNECT`（HTTPS）与普通 HTTP 绝对 URI 转发。
- **出口探测**：通过 Cloudflare `cdn-cgi/trace` 获取出口 IP、归属地、Colo 和 WARP 状态，并统计延迟。
- **自动换 IP**：连续探测失败、IP 保持超时、日志命中关键词、Zen 额度信号均可触发，带冷却保护。
- **日志监控**：可盯住 opencode 日志文件的新增内容，命中正则即换 IP。
- **zen-gate 集成**：随 WARP 一起启停 zen-gate，强制其走 WARP 出口，并在额度耗尽时换 IP 续命。
- **本地控制台**：深色 Web 界面，实时状态、日志、配置修改、一键启停 / 换 IP。
- **只监听本机**：控制台、HTTP 代理、zen-gate 默认全部绑定 `127.0.0.1`。

## 工作原理

```text
┌────────────┐   HTTP_PROXY   ┌──────────────────┐   SOCKS5   ┌──────────┐
│  opencode  │ ─────────────► │  WARP Control    │ ─────────► │  WARP    │ ──► Internet
│  (Node/Bun)│  127.0.0.1:7891│  HTTP→SOCKS5 桥  │ 127.0.0.1:40000        │
└────────────┘                └──────────────────┘            └──────────┘
                                      ▲
                                      │ 出口探测 / 换 IP 控制
                                      │
┌────────────┐   HTTP (强制)   ┌──────┴───────────┐
│  zen-gate  │ ─────────────► │  WARP Control    │
│  :8788/v1  │                │  代理桥 + 日志监控│
└────────────┘                └──────────────────┘

Web 控制台：http://127.0.0.1:8787
```

默认端口：

| 组件 | 默认地址 | 说明 |
| --- | --- | --- |
| Web 控制台 | `http://127.0.0.1:8787` | 本程序自己的管理界面 |
| HTTP 代理 | `http://127.0.0.1:7891` | 给 opencode / zen-gate 使用 |
| 上游 SOCKS5 | `127.0.0.1:40000` | WARP 后端监听端口 |
| zen-gate | `http://127.0.0.1:8788/v1` | OpenAI / Anthropic 兼容入口 |

## 环境要求

- **Python 3.8+**（无需安装任何第三方包）。
- 任选其一作为 WARP 后端：
  - 内置 `warp-plus`（推荐，免安装，首次自动下载）；
  - [Cloudflare WARP 官方客户端](https://1.1.1.1/)（提供 `warp-cli`）；
  - 本机 `warp-plus`；
  - `usque`（需先注册）；
  - 任意能提供本地 SOCKS5 的命令。
- 可选：`bin/zen-gate.exe` 或系统里已安装的 zen-gate。

> Windows 用户可直接使用 `start.bat`，脚本会自动寻找可用的 Python 3。

## 快速开始

### Windows 一键启动

1. 安装 Python 3.8+（安装时勾选 **Add Python to PATH**）。
2. 双击 `start.bat`。脚本会先检查 `bin/` 下的 `opencode.exe`、`usque.exe`、`warp-plus.exe`、`zen-gate.exe`，缺失的会通过 GitHub 加速镜像自动下载，然后再依次尝试 `py -3`、`python` 和常见安装目录启动程序。
3. 浏览器自动打开 `http://127.0.0.1:8787`。
4. 在「设置」里选择后端（默认 `auto`），保存。
5. 回到「仪表盘」点「启动」，等待状态变为「运行中」并显示出口 IP。
6. 复制页面上的环境变量，在**同一个终端**里启动 opencode，或直接运行 `start-opencode.bat`。

### 手动启动

```bash
# 默认监听 127.0.0.1:8787，并自动打开浏览器
python warp_control.py

# 指定控制台端口
python warp_control.py --port 9000

# 不自动打开浏览器
python warp_control.py --no-browser

# 导出静态预览页 preview.html（不启动服务）
python warp_control.py --export-preview
```

停止程序：在控制台点「停止」，然后关闭终端（Windows 下 `Ctrl+C`）。

## 接入 opencode

代理就绪后，在启动 opencode 的终端里设置环境变量：

**Windows CMD**

```bat
set HTTP_PROXY=http://127.0.0.1:7891
set HTTPS_PROXY=http://127.0.0.1:7891
opencode
```

**PowerShell**

```powershell
$env:HTTP_PROXY="http://127.0.0.1:7891"
$env:HTTPS_PROXY="http://127.0.0.1:7891"
opencode
```

**macOS / Linux**

```bash
export HTTP_PROXY=http://127.0.0.1:7891
export HTTPS_PROXY=http://127.0.0.1:7891
opencode
```

也可以直接使用仓库里的 `start-opencode.bat`：它会从 `config/config.json` 读取 `http_port`，自动设置大小写两套代理变量与 `ALL_PROXY`，并保留 `NO_PROXY` 以放行本机流量。

```bat
:: 使用 config/config.json 中的 http_port
start-opencode.bat

:: 临时覆盖代理地址
set OPENCODE_PROXY=http://127.0.0.1:1080
start-opencode.bat
```

如果 opencode 仍显示直连出口 IP，请确认：

- 环境变量是在**启动 opencode 的同一个终端**里设置的；
- 设置后重新打开了终端；
- 没有其他代理配置覆盖 `HTTP_PROXY` / `HTTPS_PROXY`。

### 模型 Provider 示例

`config/opencode.json` 是一个 OpenAI 兼容 provider 的示例配置，可把 `baseURL` 指向 zen-gate 或其他本地网关：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "free": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "free-router",
      "options": {
        "baseURL": "http://127.0.0.1:8788/v1",
        "apiKey": "{file:zen-gate.key}"
      },
      "models": {
        "auto": { "name": "auto" }
      }
    }
  },
  "model": "free/auto"
}
```

`{file:zen-gate.key}` 是 opencode 的变量替换语法，路径相对于配置文件所在目录，读取 `config/zen-gate.key` 的内容作为 API Key。

- `config/zen-gate.key` 已被 `.gitignore` 忽略，不会把主密钥提交到版本库；
- WARP Control 启动 zen-gate 时会自动把主密钥写入该文件，无需手动创建；
- 也可以直接写明文密钥，但那样就有泄露风险，不建议提交到仓库。

## WARP 后端

| 后端 | 说明 | 前置条件 |
| --- | --- | --- |
| `auto` | 按 `warp-cli` → `warp-plus` → `usque` 顺序检测，都没有则使用内置后端 | 无 |
| `builtin` | 内置 `warp-plus`，首次启动自动下载到 `bin/` | 首次需要联网（可用镜像） |
| `warp-cli` | Cloudflare WARP 官方客户端，使用代理模式 | 已安装并完成注册 |
| `warp-plus` | 使用本机已有的 `warp-plus` | 已安装或指定路径 |
| `usque` | 使用 `usque` 提供 SOCKS5 | 已执行 `usque register` |
| `custom` | 执行自定义命令，`{port}` 会替换为 SOCKS5 端口 | 命令能提供本地 SOCKS5 |

**warp-cli 首次使用**：如果提示未注册，先执行：

```bash
warp-cli --accept-tos registration new
```

**usque 首次使用**：

```bash
bin\usque.exe -c "config\usque\config.json" register -a
```

**内置后端下载失败**：在「设置」里填写下载镜像（如 `https://ghfast.top`），或手动下载 `warp-plus` 放到 `bin/` 目录。

## 自动换 IP

开启「自动换 IP」后，以下任一条件会触发后端重连并更换出口：

| 触发条件 | 配置项 | 默认值 |
| --- | --- | --- |
| 连续探测失败 | `rotate_after_failures` | `3` 次 |
| 出口 IP 保持过久 | `rotate_after_minutes` | `0`（关闭） |
| opencode 日志命中关键词 | `watch_log` + `watch_pattern` | 空 / 内置正则 |
| zen-gate 免费额度耗尽 | 自动监控 zen-gate 日志 | 90 秒冷却 |
| 手动点击「立即换 IP」 | — | — |

换 IP 会短暂中断代理连接，建议在空闲时触发。zen-gate 的额度信号只统计 `{access}` / `{routing}` / `{upstream}` 三类日志，避免把启动公告里的「限流」误判为额度耗尽。

## OpenCode Zen 入口（zen-gate）

[zen-gate](https://github.com/LAGcomcom/zen-gate) 把 OpenCode Zen 免费车道封装成本地 OpenAI / Anthropic 兼容接口。WARP Control 与它的集成方式：

1. **随 WARP 启停**：WARP 就绪后自动拉起 zen-gate（`-no-tray -port 8788`），或接管已在运行的实例。
2. **强制走 WARP**：通过 zen-gate 管理接口把代理模式切到 `http://127.0.0.1:7891`，停止时还原原设置。
3. **额度耗尽换 IP**：监控 zen-gate 日志，命中 `status=429`、`err=quota`、`rate limit` 等信号时自动换 WARP 出口。免费额度按出口 IP 计量，换 IP 后 opencode 重试即可继续。
4. **数据目录复用**：`zen_data_dir` 留空时使用 zen-gate 默认目录（Windows 为 `%APPDATA%\zen-gate`），可直接复用已有渠道、密钥和模型名单。

opencode 侧把 provider 的 `baseURL` 指向 `http://127.0.0.1:8788/v1`，API Key 填 zen-gate 面板里的主密钥即可。

> 注意：控制台默认端口是 `8787`，zen-gate 默认是 `8788`，两者不要冲突。zen-gate 的免费池提示词可能被上游记录，请勿提交机密内容。

## 配置项

配置文件为程序目录下的 `config/config.json`，也可以在 Web 控制台「设置」页修改。修改端口或 zen-gate 相关配置后需要重启 WARP Control 才会生效。

### 路径约定（重要）

项目内所有路径一律**相对项目根目录**存放：`backend_path`、`usque_config`、`zen_path`、`zen_data_dir`、`watch_log`，以及两个启动脚本里的路径。整个目录可以随意改名、移动、复制，不会因为配置里残留旧目录的绝对路径而启动失败。

- 控制台日志里的启动命令、错误提示、`/api/config` 返回的路径都显示为相对路径；绝对路径只在运行时由 `warp_control.py` 所在目录现算，**不会写进配置文件**。
- 从旧版本升级时，配置中残留的绝对路径（例如 `F:\xxx\warp-control\bin\usque.exe`）会在启动时自动转为相对路径并回写一次，日志会提示「配置中的绝对路径已转为相对路径」。
- 例外：系统级外部程序的位置无法相对化，仍按绝对路径处理 —— `warp-cli` 的系统安装位置、`start.bat` 探测 Python 安装位置、以及用户主动填写的项目外程序 / 日志路径。
- 启动脚本用 `cd /d "%~dp0"` 把当前目录切到脚本所在目录，之后全部使用相对路径；`%~dp0` 是动态基准，不包含写死的盘符或目录名。

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `backend` | `auto` | 后端类型：`auto` / `builtin` / `warp-cli` / `warp-plus` / `usque` / `custom` |
| `backend_path` | `""` | 后端程序路径，相对项目根目录（如 `bin/usque.exe`）；留空则自动查找 |
| `usque_config` | `""` | usque 配置文件路径，相对项目根目录；留空使用 `config/usque/config.json` |
| `usque_endpoint` | `""` | 指定 MASQUE 端点 IP；启动后端时写入 usque 配置的 `endpoint_v4` / `endpoint_h2_v4`，留空则沿用注册时的端点 |
| `socks_port` | `40000` | 上游 SOCKS5 端口 |
| `http_port` | `7891` | 本地 HTTP 代理端口 |
| `probe_interval` | `20` | 出口探测间隔（秒） |
| `probe_timeout` | `12` | 单次探测超时（秒） |
| `auto_rotate` | `true` | 是否启用自动换 IP |
| `rotate_after_failures` | `3` | 连续失败多少次后换 IP |
| `rotate_after_minutes` | `0` | IP 保持多少分钟后强制更换，`0` 为关闭 |
| `custom_command` | `""` | 自定义后端命令，`{port}` 会替换为 SOCKS5 端口 |
| `warp_plus_args` | `""` | 传给 warp-plus 的额外参数，如 `--gool --scan` |
| `builtin_mirror` | `""` | 内置后端下载镜像前缀 |
| `watch_log` | `""` | 要监控的 opencode 日志文件路径，相对项目根目录 |
| `watch_pattern` | 见下方 | 日志封禁关键词正则 |
| `verify_ssl` | `true` | 探测时是否校验证书 |
| `zen_enabled` | `true` | 是否随 WARP 启停 zen-gate |
| `zen_path` | `bin/zen-gate.exe` | zen-gate 程序路径，相对项目根目录 |
| `zen_data_dir` | `""` | zen-gate 数据目录，可填相对项目根目录的路径；留空使用其默认目录 |
| `zen_port` | `8788` | zen-gate 监听端口 |
| `zen_force_warp` | `true` | 是否强制 zen-gate 走 WARP 出口 |

默认日志关键词：

```regex
(429|403|blocked|forbidden|rate.?limit|banned|ip.?ban|too many requests)
```

## Web 控制台与 HTTP API

控制台包含四个页面：

- **仪表盘**：出口 IP、归属地、延迟、后端、端口状态、OpenCode（`start-opencode`）运行/代理连接状态、代理地址、一键启停 / 换 IP、最近事件。
- **设置**：后端、端口、探测、自动换 IP、日志监控、zen-gate 集成。
- **日志**：实时输出，内存保留最近 1500 条。
- **帮助**：使用步骤与常见配置示例。

控制台同时暴露一组本地 JSON API：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/status` | 当前运行状态与出口信息 |
| `GET` | `/api/config` | 读取配置 |
| `POST` | `/api/config` | 保存配置 |
| `POST` | `/api/start` | 启动 WARP 与代理桥 |
| `POST` | `/api/stop` | 停止 |
| `POST` | `/api/rotate` | 立即换 IP |
| `POST` | `/api/probe` | 立即探测一次 |
| `GET` | `/api/logs?since=<id>` | 增量拉取日志 |
| `GET` | `/api/detect` | 检测本机 WARP 后端 |
| `POST` | `/api/builtin-download` | 下载 / 更新内置后端 |
| `GET` | `/api/builtin` | 查询内置后端状态 |

## 命令行参数

```text
python warp_control.py [--host 127.0.0.1] [--port 8787] [--no-browser] [--export-preview]
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 控制台监听地址 |
| `--port` | `8787` | 控制台端口 |
| `--no-browser` | 关闭 | 启动后不自动打开浏览器 |
| `--export-preview` | 关闭 | 导出静态预览页 `preview.html` 后退出 |

## 目录结构

```text
warp-control/
├─ warp_control.py          # 主程序（控制台 + 代理桥 + 后端托管）
├─ config/                  # 所有配置统一放这里
│  ├─ config.json           # 运行配置
│  ├─ opencode.json         # opencode provider 配置
│  ├─ zen-gate.key          # zen-gate 主密钥（自动生成，不入库）
│  └─ usque/config.json     # usque 注册配置（含设备密钥，不入库）
├─ start.bat                # Windows 一键启动（自动寻找 Python）
├─ start-opencode.bat       # 带代理环境变量启动 opencode
├─ start-with-opencode.bat  # 启动控制台并在 WARP 就绪后自动拉起 opencode
├─ asset/                   # 打赏二维码等静态资源
├─ preview.html             # 静态界面预览
├─ bin/                     # 内置后端 / zen-gate / opencode 可执行文件
│  ├─ warp-plus.exe
│  ├─ usque.exe
│  ├─ zen-gate.exe
│  └─ opencode.exe
```

## 常见问题

**找不到 Python / `start.bat` 一闪而过**

`start.bat` 会先探测 Python，找不到会自动下载 3.12.10 并静默安装（用户级、无需管理员）；若自动安装失败，再手动安装 Python 3.8+ 并勾选 Add Python to PATH，然后在终端里运行 `python warp_control.py` 查看具体报错。

**内置后端下载失败**

GitHub 直连不畅时，在设置里填写 `builtin_mirror`（如 `https://ghfast.top`），或手动下载 `warp-plus` 放到 `bin/`。

`start.bat` 自动补全依赖时默认依次尝试 `gh-proxy.com`、`ghfast.top`、`ghproxy.net`。如需换源，在运行前设置环境变量（结尾必须带 `/`）：

```bat
set WARP_GH_MIRROR=https://ghfast.top/
start.bat
```

**启动失败：`[WinError 2] 系统找不到指定的文件`**

几乎都是配置里残留了旧目录的绝对路径，导致目录改名 / 移动 / 复制后 exe 仍指向原位置，甚至出现「exe 在旧目录、配置在新目录」的串台。检查 `config/config.json` 的 `backend_path` 是否为相对路径（如 `bin\usque.exe`）；程序启动时会自动把绝对路径规范化并回写一次。也可对照控制台日志里的「启动命令」，现在它显示为相对路径，一眼就能看出路径是否落在当前目录。

**`warp-cli connect` 失败**

先完成注册：`warp-cli --accept-tos registration new`。另外 `warp-cli` 代理模式不能与系统全局 WARP 模式同时占用。

**端口被占用 / 后端残留进程**

启动 WARP 后端前会强制清理：先结束同名残留后端进程（如 `usque.exe` / `warp-plus.exe`），再强制释放 SOCKS 端口的占用者，然后启动并确认监听端口的 PID 属于本次进程。若强制清理后端口仍被占用，日志会报错，此时请在任务管理器中结束占用进程，或修改 `socks_port` / `http_port` / `zen_port` 后重启。控制台自身默认占用 `8787`。

**usque 一直重连 / MASQUE 连接超时**

日志出现 `Failed to connect tunnel: ... no recent network activity`、探测连续 `urlopen error timed out` 时，说明隧道根本没建立，换 IP 多少次都没用。

部分网络会屏蔽注册时分配的 `162.159.198.2`。在 `config/config.json` 里设置 `usque_endpoint` 为同网段可达端点（本机实测 `162.159.198.1` 可用），重启 WARP Control 即可；程序每次启动后端都会把它写进 `config/usque/config.json`，重新注册后也不会退回去。修改端点不会影响已注册的账号信息。

排查端点可用 `usque.exe -c <临时配置副本> socks --bind 127.0.0.1 --port 40001`，再用 `curl --socks5-hostname 127.0.0.1:40001 https://www.cloudflare.com/cdn-cgi/trace` 验证，返回 `warp=on` 即为可用。注意 TCP 通不代表 QUIC 通，别盲选 `--http2`。

**opencode 仍然走直连**

确认代理变量与 opencode 在同一个终端生效；使用 `start-opencode.bat`；检查是否有其他代理软件覆盖环境变量。

**zen-gate 一直 429**

确认「自动换 IP」已开启；zen-gate 额度信号有 90 秒冷却，避免一轮 429 反复重启后端；若换 IP 后仍受限，可能是车道级门禁而非 IP 额度。

**opencode 里 zen 的模型用不了**

按顺序排查：

1. `config/opencode.json` 的 `baseURL` 必须是 `http://127.0.0.1:8788/v1`（zen-gate 的实际端口），指向不存在的端口时 opencode 会直接报连接失败；
2. `apiKey` 必须等于 zen-gate 的主密钥（`%APPDATA%\zen-gate\config.json` 的 `mainKey`），密钥错误会返回 `401 invalid API key`；推荐写成 `{file:zen-gate.key}`，由 WARP Control 自动同步；
3. 控制台「模型」页显示 `throttled` / `FreeUsageLimitError` 表示 **OpenCode Zen 免费车道额度已耗尽**，不是 IP 被封；在 zen-gate 面板开启「通道回落到提供商」（`laneFallbackToProviders`）后，调用这些模型会自动改用 kilo / openrouter 免费通道，实测仍可正常对话；
4. `beam-free` 报 `RegionError` 属于地区限制，换 IP 不一定能绕过；
5. 改完配置要重启 opencode（关闭终端重新执行 `start-opencode.bat`）才会生效。

**探测报证书错误**

仅在证书链异常的环境下，把 `verify_ssl` 设为 `false`。

## 安全说明

- 控制台、HTTP 代理和 zen-gate 默认只监听 `127.0.0.1`，不会暴露到局域网。
- 控制台**没有登录认证**，请勿通过端口转发或反向代理把它暴露到公网。
- zen-gate 的管理接口固定只接受本机请求，但仍建议设置强 API Key。
- 自动换 IP 会短暂中断连接，请避免在长任务执行期间手动触发。
- 请遵守 Cloudflare WARP、OpenCode Zen 及各上游服务的条款，仅将本项目用于合规用途。

## 致谢与许可

- [zen-gate](https://github.com/LAGcomcom/zen-gate)：OpenCode Zen 本地网关（`zen-gate/` 目录为其独立项目）。
- [warp-plus](https://github.com/bepass-org/warp-plus)：免安装 WARP 后端。
- [usque](https://github.com/Diniboy1123/usque)：WARP 的另一种实现。
- Cloudflare WARP：出口网络。

本仓库未单独声明开源许可证；`zen-gate`、`warp-plus`、`usque` 等第三方组件遵循其各自仓库的许可证。
# warp-opencode
# warp-opencode
# warp-opencode
