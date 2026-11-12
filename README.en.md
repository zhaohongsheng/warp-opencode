# WARP Control

> **Egress IP manager for opencode**: runs Cloudflare WARP, bridges SOCKS5 to a standard HTTP proxy, rotates the egress IP automatically, and can manage the [zen-gate](https://github.com/LAGcomcom/zen-gate) free AI gateway alongside it.

**English** | [简体中文](README.md)

![platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-0078D6?style=flat-square)
![python](https://img.shields.io/badge/Python-3.8%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![deps](https://img.shields.io/badge/dependencies-stdlib%20only-34D399?style=flat-square)
![listen](https://img.shields.io/badge/listen-127.0.0.1-22D3EE?style=flat-square)

---

## Contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Using with opencode](#using-with-opencode)
- [WARP backends](#warp-backends)
- [Automatic IP rotation](#automatic-ip-rotation)
- [OpenCode Zen entry (zen-gate)](#opencode-zen-entry-zen-gate)
- [Configuration](#configuration)
- [Web console and HTTP API](#web-console-and-http-api)
- [CLI options](#cli-options)
- [Project layout](#project-layout)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)
- [Credits and license](#credits-and-license)

---

## Why this exists

When opencode connects directly, its egress IP is fixed. Upstream services can rate-limit or ban that IP, and free quotas tied to the IP can run out quickly. WARP Control does three things:

1. **Runs WARP for you** — detects and starts a local `warp-cli` / `warp-plus` / `usque`, uses a bundled `warp-plus` (downloaded on first run), or runs any custom command that exposes a local SOCKS5 port.
2. **Bridges the proxy** — converts WARP's SOCKS5 endpoint into a standard HTTP proxy, so opencode / Node / Bun do not need full `socks5://` support.
3. **Rotates the egress IP** — probes the public IP continuously and reconnects WARP when probes fail, the IP has been stable too long, opencode logs match a ban pattern, or the zen-gate free quota is exhausted.

The whole program is a single Python file and **uses only the Python 3 standard library**.

## Features

- **Multiple managed backends**: `auto`, `builtin`, `warp-cli`, `warp-plus`, `usque`, and `custom`.
- **Zero-install backend**: bundled `warp-plus`, downloaded from the official release into `bin/` on first use, with optional mirror support.
- **SOCKS5 → HTTP bridge**: supports both `CONNECT` (HTTPS) and plain HTTP absolute-URI forwarding.
- **Egress probing**: reads IP, location, colo, and WARP status from Cloudflare `cdn-cgi/trace`, and measures latency.
- **Automatic rotation**: triggered by consecutive probe failures, IP age, log keyword hits, or Zen quota signals, with cooldown protection.
- **Log watching**: tails an opencode log file and rotates when a line matches a configurable regex.
- **zen-gate integration**: starts/stops zen-gate with WARP, forces its egress through WARP, and rotates the IP when the free quota runs out.
- **Local web console**: dark UI with live status, logs, config editing, and one-click start/stop/rotate.
- **Loopback only**: console, HTTP proxy, and zen-gate bind to `127.0.0.1` by default.

## How it works

```text
┌────────────┐   HTTP_PROXY   ┌──────────────────┐   SOCKS5   ┌──────────┐
│  opencode  │ ─────────────► │  WARP Control    │ ─────────► │  WARP    │ ──► Internet
│  (Node/Bun)│  127.0.0.1:7891│  HTTP→SOCKS5     │ 127.0.0.1:40000        │
└────────────┘                └──────────────────┘            └──────────┘
                                      ▲
                                      │ probe / rotate
                                      │
┌────────────┐   HTTP (forced)  ┌──────┴───────────┐
│  zen-gate  │ ─────────────►  │  WARP Control    │
│  :8788/v1  │                 │  bridge + watcher│
└────────────┘                 └──────────────────┘

Web console: http://127.0.0.1:8787
```

Default ports:

| Component | Default address | Purpose |
| --- | --- | --- |
| Web console | `http://127.0.0.1:8787` | Management UI |
| HTTP proxy | `http://127.0.0.1:7891` | Used by opencode / zen-gate |
| Upstream SOCKS5 | `127.0.0.1:40000` | WARP backend listener |
| zen-gate | `http://127.0.0.1:8788/v1` | OpenAI / Anthropic-compatible endpoint |

## Requirements

- **Python 3.8+** (no third-party packages).
- One WARP backend:
  - bundled `warp-plus` (recommended, zero-install, auto-download);
  - [Cloudflare WARP client](https://1.1.1.1/) providing `warp-cli`;
  - a local `warp-plus`;
  - `usque` (register first);
  - any command that exposes a local SOCKS5 port.
- Optional: `bin/zen-gate.exe` or an existing zen-gate installation.

> On Windows, just run `start.bat`; it locates a working Python 3 automatically.

## Quick start

### Windows one-click

1. Install Python 3.8+ and enable **Add Python to PATH**.
2. Double-click `start.bat`. It first checks `bin/` for `opencode.exe`, `usque.exe`, `warp-plus.exe`, and `zen-gate.exe`, downloads any missing file through a GitHub mirror, then launches the app via `py -3`, `python`, or a common install directory.
3. The browser opens `http://127.0.0.1:8787`.
4. In **Settings**, choose a backend (default `auto`) and save.
5. On **Dashboard**, click **Start** and wait for the status to become **Running** with an egress IP.
6. Copy the environment variables shown on the page and start opencode in the **same terminal**, or run `start-opencode.bat`.

### Manual start

```bash
# Listen on 127.0.0.1:8787 and open the browser
python warp_control.py

# Custom console port
python warp_control.py --port 9000

# Do not open the browser
python warp_control.py --no-browser

# Export a static preview page and exit
python warp_control.py --export-preview
```

To stop: click **Stop** in the console, then close the terminal (`Ctrl+C` on Windows).

## Using with opencode

Once the proxy is running, set the environment variables in the terminal that launches opencode:

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

You can also use the included `start-opencode.bat`. It reads `http_port` from `config/config.json`, sets both upper- and lower-case proxy variables plus `ALL_PROXY`, and keeps `NO_PROXY` so local traffic bypasses the proxy.

```bat
:: Use http_port from config/config.json
start-opencode.bat

:: Override the proxy address
set OPENCODE_PROXY=http://127.0.0.1:1080
start-opencode.bat
```

If opencode still shows your direct egress IP:

- make sure the variables are set in the **same terminal** that starts opencode;
- reopen the terminal after setting them;
- check that no other proxy configuration overrides `HTTP_PROXY` / `HTTPS_PROXY`.

### Model provider example

`config/opencode.json` is a sample OpenAI-compatible provider configuration. Point `baseURL` at zen-gate or another local gateway:

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

`{file:zen-gate.key}` is opencode's variable substitution syntax. The path is relative to the config file directory, so it reads `config/zen-gate.key` as the API key.

- `config/zen-gate.key` is gitignored, so the master key never lands in version control;
- WARP Control writes the key there automatically when it starts zen-gate;
- You can inline the key instead, but then be careful never to commit it.

## WARP backends

| Backend | Description | Prerequisite |
| --- | --- | --- |
| `auto` | Tries `warp-cli` → `warp-plus` → `usque`, falls back to the bundled backend | None |
| `builtin` | Bundled `warp-plus`, downloaded into `bin/` on first start | Internet on first run (mirror supported) |
| `warp-cli` | Official Cloudflare WARP client in proxy mode | Installed and registered |
| `warp-plus` | Existing local `warp-plus` | Installed or path specified |
| `usque` | `usque` providing SOCKS5 | `usque register` completed |
| `custom` | Custom command; `{port}` is replaced with the SOCKS5 port | Command exposes local SOCKS5 |

**First use of warp-cli** — if it reports that the client is not registered:

```bash
warp-cli --accept-tos registration new
```

**First use of usque**:

```bash
bin\usque.exe -c "config\usque\config.json" register -a
```

**Bundled backend download fails** — set `builtin_mirror` in Settings (for example `https://ghfast.top`), or download `warp-plus` manually and place it in `bin/`.

## Automatic IP rotation

With automatic rotation enabled, any of the following triggers a backend restart and a new egress IP:

| Trigger | Setting | Default |
| --- | --- | --- |
| Consecutive probe failures | `rotate_after_failures` | `3` |
| Egress IP unchanged for too long | `rotate_after_minutes` | `0` (off) |
| opencode log matches a pattern | `watch_log` + `watch_pattern` | empty / built-in regex |
| zen-gate free quota exhausted | zen-gate log monitoring | 90 s cooldown |
| Manual **Rotate now** | — | — |

Rotation briefly interrupts the proxy, so trigger it when idle. For zen-gate, only `{access}`, `{routing}`, and `{upstream}` log lines count, so announcements mentioning "rate limit" are not mistaken for quota exhaustion.

## OpenCode Zen entry (zen-gate)

[zen-gate](https://github.com/LAGcomcom/zen-gate) wraps the OpenCode Zen free lane as a local OpenAI / Anthropic-compatible API. WARP Control integrates with it as follows:

1. **Start/stop with WARP** — launches zen-gate (`-no-tray -port 8788`) once WARP is ready, or attaches to an instance that is already running.
2. **Force traffic through WARP** — switches zen-gate's proxy mode to `http://127.0.0.1:7891` through its admin API, and restores the previous setting on stop.
3. **Rotate on quota exhaustion** — watches zen-gate logs for `status=429`, `err=quota`, `rate limit`, and similar signals, then rotates the WARP egress IP. The free quota is counted per egress IP, so retrying after rotation usually resumes service.
4. **Reuse the data directory** — when `zen_data_dir` is empty, zen-gate's default directory is used (on Windows: `%APPDATA%\zen-gate`), preserving existing channels, keys, and model lists.

On the opencode side, set the provider `baseURL` to `http://127.0.0.1:8788/v1` and use the master key from the zen-gate panel.

> Note: the console defaults to `8787` and zen-gate to `8788`; keep them distinct. Prompts sent to zen-gate's free pool may be recorded upstream, so do not submit confidential content.

## Configuration

The configuration lives in `config/config.json` next to the program, and can also be edited on the **Settings** page. Changing ports or zen-gate settings requires restarting WARP Control.

### Path convention (important)

Every path inside the project is stored **relative to the project root**: `backend_path`, `usque_config`, `zen_path`, `zen_data_dir`, `watch_log`, and the paths in both launcher scripts. The folder can be renamed, moved, or copied freely without breaking startup.

- Startup commands, error messages and `/api/config` all display relative paths. Absolute paths are computed at runtime from the directory containing `warp_control.py` and are **never persisted** to the config file.
- When upgrading, absolute paths left over from an older config (for example `F:\xxx\warp-control\bin\usque.exe`) are normalized to relative paths and written back once at startup.
- Exception: system-wide external locations cannot be made relative — the `warp-cli` install path, the Python install locations probed by `start.bat`, and any program/log path outside the project that you enter yourself.
- The launchers run `cd /d "%~dp0"` first and then use relative paths only; `%~dp0` is a dynamic base, not a hard-coded drive or folder name.

| Key | Default | Description |
| --- | --- | --- |
| `backend` | `auto` | Backend type: `auto` / `builtin` / `warp-cli` / `warp-plus` / `usque` / `custom` |
| `backend_path` | `""` | Backend executable path, relative to the project root (e.g. `bin/usque.exe`); auto-detected when empty |
| `usque_config` | `""` | usque config path, relative to the project root; defaults to `config/usque/config.json` |
| `usque_endpoint` | `""` | Pin the MASQUE endpoint IP; written to `endpoint_v4` / `endpoint_h2_v4` on each backend start. Empty keeps the registered endpoint |
| `socks_port` | `40000` | Upstream SOCKS5 port |
| `http_port` | `7891` | Local HTTP proxy port |
| `probe_interval` | `20` | Egress probe interval (seconds) |
| `probe_timeout` | `12` | Per-probe timeout (seconds) |
| `auto_rotate` | `true` | Enable automatic IP rotation |
| `rotate_after_failures` | `3` | Rotate after this many consecutive failures |
| `rotate_after_minutes` | `0` | Force rotation after the IP has been stable this many minutes; `0` disables |
| `custom_command` | `""` | Custom backend command; `{port}` becomes the SOCKS5 port |
| `warp_plus_args` | `""` | Extra warp-plus arguments, e.g. `--gool --scan` |
| `builtin_mirror` | `""` | Download mirror prefix for the bundled backend |
| `watch_log` | `""` | opencode log file to watch, relative to the project root |
| `watch_pattern` | see below | Ban keyword regex for log watching |
| `verify_ssl` | `true` | Verify certificates during probes |
| `zen_enabled` | `true` | Start/stop zen-gate together with WARP |
| `zen_path` | `bin/zen-gate.exe` | zen-gate executable, relative to the project root |
| `zen_data_dir` | `""` | zen-gate data directory; may be relative to the project root; empty means its default |
| `zen_port` | `8788` | zen-gate listening port |
| `zen_force_warp` | `true` | Force zen-gate egress through WARP |

Default log pattern:

```regex
(429|403|blocked|forbidden|rate.?limit|banned|ip.?ban|too many requests)
```

## Web console and HTTP API

The console has four pages:

- **Dashboard**: egress IP, location, latency, backend, port status, OpenCode (`start-opencode`) process/proxy-connection status, proxy addresses, start/stop/rotate, recent events.
- **Settings**: backend, ports, probing, automatic rotation, log watching, zen-gate integration.
- **Logs**: live output, keeping the latest 1500 entries in memory.
- **Help**: usage steps and configuration examples.

It also exposes a local JSON API:

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/status` | Current state and egress info |
| `GET` | `/api/config` | Read configuration |
| `POST` | `/api/config` | Save configuration |
| `POST` | `/api/start` | Start WARP and the proxy bridge |
| `POST` | `/api/stop` | Stop |
| `POST` | `/api/rotate` | Rotate the IP now |
| `POST` | `/api/probe` | Probe once |
| `GET` | `/api/logs?since=<id>` | Incremental logs |
| `GET` | `/api/detect` | Detect local WARP backends |
| `POST` | `/api/builtin-download` | Download / update the bundled backend |
| `GET` | `/api/builtin` | Bundled backend status |

## CLI options

```text
python warp_control.py [--host 127.0.0.1] [--port 8787] [--no-browser] [--export-preview]
```

| Option | Default | Description |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Console bind address |
| `--port` | `8787` | Console port |
| `--no-browser` | off | Do not open the browser automatically |
| `--export-preview` | off | Export `preview.html` and exit |

## Project layout

```text
warp-control/
├─ warp_control.py          # Main program (console + proxy bridge + backend manager)
├─ config/                  # All configuration files
│  ├─ config.json           # Runtime configuration
│  ├─ opencode.json         # opencode provider config
│  ├─ zen-gate.key          # zen-gate master key (generated, gitignored)
│  └─ usque/config.json     # usque registration config (device key, gitignored)
├─ start.bat                # Windows one-click launcher (finds Python)
├─ start-opencode.bat       # Launch opencode with proxy env vars
├─ start-with-opencode.bat  # Start the console and auto-launch opencode once WARP is ready
├─ asset/                   # Static assets such as the donation QR codes
├─ preview.html             # Static UI preview
├─ bin/                     # Bundled backend / zen-gate / opencode executables
│  ├─ warp-plus.exe
│  ├─ usque.exe
│  ├─ zen-gate.exe
│  └─ opencode.exe
```

## Troubleshooting

**Python not found / `start.bat` closes immediately**

`start.bat` detects Python first and, if missing, downloads Python 3.12.10 and installs it silently per-user (no admin needed). If that fails, install Python 3.8+ manually with Add Python to PATH, then run `python warp_control.py` in a terminal to see the error.

**Bundled backend download fails**

Set `builtin_mirror` in Settings (for example `https://ghfast.top`), or download `warp-plus` manually into `bin/`.

When `start.bat` fills in missing dependencies, it tries `gh-proxy.com`, `ghfast.top`, then `ghproxy.net`. To use another mirror, set the environment variable before launching (the trailing `/` is required):

```bat
set WARP_GH_MIRROR=https://ghfast.top/
start.bat
```

**Startup fails with `[WinError 2] The system cannot find the file specified`**

This almost always means an absolute path from an older directory survived in the config. After renaming, moving, or copying the folder, the exe still points at the old location — sometimes even mixing the old folder's exe with the new folder's config. Check that `backend_path` in `config/config.json` is relative (e.g. `bin\usque.exe`); startup normalizes any leftover absolute paths and writes them back once. The logged "startup command" now shows relative paths too, so a wrong base directory is obvious.

**`warp-cli connect` fails**

Register first: `warp-cli --accept-tos registration new`. Also, warp-cli proxy mode cannot coexist with the system-wide WARP mode.

**Port in use / stale backend process**

Before starting the WARP backend, WARP Control force-cleans the environment: it kills stale backend processes with known names (for example `usque.exe` / `warp-plus.exe`), forcefully frees the configured SOCKS port owner, starts the backend, and verifies that the listening PID belongs to the process it just launched. If the port is still occupied after force cleanup, the log reports an error; stop the owner in Task Manager or change `socks_port` / `http_port` / `zen_port` and restart. The console itself uses `8787` by default.

**usque keeps reconnecting / MASQUE connection times out**

`Failed to connect tunnel: ... no recent network activity` plus repeated `urlopen error timed out` probes means the tunnel never came up; rotating IPs will not help.

Some networks block the registered `162.159.198.2`. Set `usque_endpoint` in `config/config.json` to a reachable endpoint in the same range (`162.159.198.1` verified working here) and restart WARP Control. The program rewrites it into `config/usque/config.json` on every backend start, so re-registering will not revert it. The registered account is unaffected.

To test an endpoint: `usque.exe -c <temp config copy> socks --bind 127.0.0.1 --port 40001`, then `curl --socks5-hostname 127.0.0.1:40001 https://www.cloudflare.com/cdn-cgi/trace`; `warp=on` means it works. TCP reachability does not imply QUIC reachability, and `--http2` is not a universal fallback.

**opencode still connects directly**

Make sure the proxy variables are active in the same terminal, use `start-opencode.bat`, and check that other proxy software is not overriding the environment.

**zen-gate keeps returning 429**

Confirm automatic rotation is enabled. Zen quota signals have a 90-second cooldown to avoid restarting the backend in a 429 loop. If rotating does not help, the restriction may be lane-level rather than IP-level.

**Zen models do not work in opencode**

Check in this order:

1. `baseURL` in `config/opencode.json` must be `http://127.0.0.1:8788/v1` (zen-gate's real port); a dead port makes opencode fail with a connection error;
2. `apiKey` must match zen-gate's master key (`mainKey` in `%APPDATA%\zen-gate\config.json`); a wrong key returns `401 invalid API key`. Prefer `{file:zen-gate.key}`, which WARP Control keeps in sync;
3. `throttled` / `FreeUsageLimitError` on the Models page means the **OpenCode Zen free lane quota is exhausted**, not that the IP is blocked. Enable "fall back to providers" (`laneFallbackToProviders`) in zen-gate and requests are re-routed to the kilo / openrouter free lanes, which still work in practice;
4. `beam-free` returning `RegionError` is a regional restriction that rotation may not bypass;
5. Restart opencode (close the terminal and rerun `start-opencode.bat`) after changing the config.

**Certificate error during probing**

Only in environments with a broken certificate chain, set `verify_ssl` to `false`.

## Security notes

- The console, HTTP proxy, and zen-gate listen on `127.0.0.1` by default and are not exposed to the LAN.
- The console has **no authentication**. Do not expose it through port forwarding or a reverse proxy.
- zen-gate's admin API accepts local requests only, but you should still set a strong API key.
- Rotation briefly interrupts connections; avoid triggering it during long-running tasks.
- Follow the terms of Cloudflare WARP, OpenCode Zen, and upstream providers. Use this project only for compliant purposes.

## Credits and license

- [zen-gate](https://github.com/LAGcomcom/zen-gate) — local OpenCode Zen gateway (`zen-gate/` is its own project).
- [warp-plus](https://github.com/bepass-org/warp-plus) — zero-install WARP backend.
- [usque](https://github.com/Diniboy1123/usque) — another WARP implementation.
- Cloudflare WARP — egress network.

This repository does not declare a separate open-source license. Third-party components such as `zen-gate`, `warp-plus`, and `usque` remain under their respective licenses.
