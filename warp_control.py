#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WARP Control - opencode 出口 IP 管家

本地网页控制台：
  1. 自动检测并托管 WARP 代理后端（warp-cli / warp-plus / usque / 自定义命令）
  2. 把 WARP 的 SOCKS5 桥接成本地 HTTP 代理，供 opencode 使用
  3. 持续探测出口 IP，异常或 IP 长时间不变时自动重连换 IP
  4. 可监控 opencode 日志中的封禁关键词，命中后自动换 IP

仅依赖 Python 3 标准库。

用法：
    python warp_control.py [--port 8787] [--no-browser]
"""

import argparse
import ipaddress
import json
import os
import platform
import re
import select
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = os.path.join(APP_DIR, "config")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CONFIG_REL = os.path.join("config", "config.json")

DEFAULT_CONFIG = {
    "backend": "auto",
    "backend_path": "",
    "usque_config": "",
    # 留空 = 沿用 usque 配置文件里的端点。部分网络会屏蔽注册时分配的
    # 162.159.198.2，这里可指定同网段可达端点（如 162.159.198.1），
    # 每次启动后端时写入 usque 配置，避免重新注册后又变回被屏蔽的地址。
    "usque_endpoint": "",
    "socks_port": 40000,
    "http_port": 7891,
    "probe_interval": 20,
    "probe_timeout": 12,
    "auto_rotate": True,
    "rotate_after_failures": 3,
    "rotate_after_minutes": 0,
    "custom_command": "",
    "warp_plus_args": "",
    "builtin_mirror": "",
    "watch_log": "",
    "watch_pattern": "(429|403|blocked|forbidden|rate.?limit|banned|ip.?ban|too many requests)",
    "verify_ssl": True,
    # zen-gate 免费 AI 网关集成：随 WARP 一起启停，
    # 上游走 WARP 出口，撞限额自动换 IP（无限续航）
    "zen_enabled": True,
    "zen_path": os.path.join("bin", "zen-gate.exe"),
    # 留空 = 使用 zen-gate 自己的默认数据目录（Windows: %APPDATA%\zen-gate），
    # 这样能直接接管已经在跑的实例，并复用它的渠道 / 密钥 / 模型名单。
    "zen_data_dir": "",
    "zen_port": 8788,
    # 接管/拉起 zen-gate 时，把它的出口切到 WARP 的 HTTP 代理（停止时还原）。
    "zen_force_warp": True,
    # WARP 启动并探测成功后，自动通过 start-opencode.bat 启动 OpenCode
    # （已运行则直接复用，不会强制结束用户打开的 opencode）。
    "auto_start_opencode": False,
}

# --------------------------------------------------------------------------- #
# 路径约定：项目内的一切路径都相对 APP_DIR 存放。
#
# 目录被改名 / 移动 / 复制后，写死的绝对路径仍指向旧位置，Popen 会直接抛
# WinError 2，甚至出现「exe 在旧目录、配置在新目录」的串台。因此配置读写、
# 启动解析、日志展示都统一走下面这组函数：相对路径落盘，绝对路径只在
# 运行时由 __file__ 现算，绝不持久化。
# --------------------------------------------------------------------------- #

PATH_FIELDS = (
    "backend_path", "usque_config", "zen_path", "zen_data_dir", "watch_log",
)
# 旧版配置里绝对路径的锚点（项目内目录）
_PATH_ANCHORS = ("bin", "config", "asset")


def to_rel_path(path):
    """把项目内路径统一成相对 APP_DIR 的写法。

    - 相对路径：仅规范化分隔符，不拼接 APP_DIR。
    - 当前 APP_DIR 内的绝对路径：转成 relpath。
    - 另一份副本目录里的绝对路径（改名/移动前留下的旧值）：按 bin/
      config/ asset/ 锚点提取项目内层级，映射到当前 APP_DIR。
    - 项目外的绝对路径（如用户自己指定的外部程序）：原样保留。
    """
    p = (path or "").strip().strip('"').strip("'")
    if not p or "://" in p:
        return p
    norm = os.path.normpath(p)
    if os.path.isabs(norm):
        app = os.path.normcase(os.path.abspath(APP_DIR))
        nc = os.path.normcase(norm)
        if nc == app or nc.startswith(app + os.sep):
            rel = os.path.relpath(norm, APP_DIR)
        else:
            parts = nc.replace("\\", "/").split("/")
            anchor = -1
            for i, seg in enumerate(parts[:-1]):
                if seg in _PATH_ANCHORS:
                    anchor = i
                    break
            if anchor < 0:
                return p  # 项目外路径，无法相对化，原样保留
            rel = "/".join(parts[anchor:])
    else:
        rel = norm
    return rel.replace("/", os.sep)


def normalize_path_fields(cfg):
    """就地规范化配置里的路径字段，返回是否发生了改动。"""
    changed = False
    for key in PATH_FIELDS:
        old = cfg.get(key) or ""
        new = to_rel_path(old)
        if new != old:
            cfg[key] = new
            changed = True
    return changed


def show_cmd(cmd):
    """日志展示用：把命令里的项目内绝对路径显示为相对路径。"""
    if isinstance(cmd, (list, tuple)):
        return " ".join(to_rel_path(str(x)) for x in cmd)
    out = []
    for tok in re.split(r"(\s+)", str(cmd)):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        pre, core, post = "", tok, ""
        while core[:1] in ('"', "'"):
            pre, core = pre + core[0], core[1:]
        while core[-1:] in ('"', "'"):
            post, core = core[-1] + post, core[:-1]
        out.append(pre + to_rel_path(core) + post)
    return "".join(out)


# 两次“限额触发换 IP”之间的最小间隔，避免一整轮 429 把后端反复重启。
ZEN_ROTATE_COOLDOWN = 90.0

# zen-gate 日志中的限额 / 封禁信号：命中即视为当前出口 IP 的免费额度
# 已耗尽，由 warp_control 换一个 WARP 出口（免费额度按出口 IP 计量）。
# 不含 FreeTierError / FreeTier：那是车道级门禁，换 IP 也不会变。
ZEN_QUOTA_RE = re.compile(
    r"(status=429|ERR=quota|err=quota|FreeUsageLimitError|usage limit|rate.?limit|"
    r"免费车道整轮 429|暂时限流|限额|地区受限|RegionError|"
    r"blocked|banned|ip.?ban|too many requests)",
    re.I,
)

WARPCI_PATHS = [
    r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe",
    r"C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe",
    "/usr/bin/warp-cli",
    "/usr/local/bin/warp-cli",
    "/opt/cloudflare-warp/bin/warp-cli",
]

TRACE_URLS = [
    "https://www.cloudflare.com/cdn-cgi/trace",
    "https://cloudflare.com/cdn-cgi/trace",
    "https://1.1.1.1/cdn-cgi/trace",
]

NO_WINDOW = 0x08000000
NEW_GROUP = 0x00000200


# --------------------------------------------------------------------------- #
# 通用工具
# --------------------------------------------------------------------------- #

def log_print(msg):
    try:
        print(msg, flush=True)
    except Exception:
        pass


class Logger:
    """内存环形日志，支持前端增量拉取。"""

    def __init__(self, capacity=1500):
        self._items = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()

    def add(self, msg, level="info"):
        with self._lock:
            self._seq += 1
            item = {
                "id": self._seq,
                "t": time.strftime("%H:%M:%S"),
                "level": level,
                "msg": str(msg),
            }
            self._items.append(item)
        log_print("[%s] %s" % (item["t"], msg))
        return item

    def since(self, seq):
        with self._lock:
            return [i for i in self._items if i["id"] > seq]


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    migrated = False
    try:
        if os.path.isfile(CONFIG_PATH):
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
                migrated = normalize_path_fields(cfg)
    except Exception as e:
        log_print("读取配置失败，使用默认值：%s" % e)
    if migrated:
        # 老配置里的绝对路径（可能指向改名/移动前的旧目录）自动迁移一次，
        # 回写后以后启动都只认相对路径。
        save_config(cfg)
        log_print("配置中的绝对路径已转为相对路径并回写：%s" % CONFIG_REL)
    return cfg


def save_config(cfg):
    try:
        normalize_path_fields(cfg)
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log_print("写入配置失败：%s" % e)
        return False


def port_open(host, port, timeout=0.4):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(host, port, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_open(host, port, timeout=0.5):
            return True
        time.sleep(0.4)
    return False


def _listener_pids(port):
    """返回监听本机指定 TCP 端口的 PID 列表（尽力而为，失败时返回空列表）。"""
    port = int(port)
    pids = []
    if os.name == "nt":
        _, out = run_cmd(["netstat", "-ano", "-p", "tcp"], timeout=8)
        for line in (out or "").splitlines():
            m = re.match(
                r"^\s*TCP\s+\S+:%d\s+\S+\s+LISTENING\s+(\d+)\s*$" % port,
                line, re.I,
            )
            if m:
                pids.append(int(m.group(1)))
    else:
        _, out = run_cmd(
            ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"], timeout=8
        )
        for tok in (out or "").split():
            if tok.isdigit():
                pids.append(int(tok))
        if not pids:
            _, out = run_cmd(["ss", "-ltnp", "sport = :%d" % port], timeout=8)
            for m in re.finditer(r"pid=(\d+)", out or ""):
                pids.append(int(m.group(1)))
    return sorted(set(pids))


def _pid_name(pid):
    pid = int(pid)
    if os.name == "nt":
        _, out = run_cmd(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"], timeout=8
        )
        m = re.match(r'"([^"]+)"', (out or "").strip())
        return m.group(1) if m else ""
    _, out = run_cmd(["ps", "-p", str(pid), "-o", "comm="], timeout=8)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    return lines[0] if lines else ""


def _pid_alive(pid):
    pid = int(pid)
    if pid <= 0:
        return False
    if os.name == "nt":
        _, out = run_cmd(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"], timeout=8
        )
        return ('"%d"' % pid) in (out or "")
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _kill_pid(pid):
    pid = int(pid)
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        code, _ = run_cmd(["taskkill", "/F", "/T", "/PID", str(pid)], timeout=15)
        return code == 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.time() + 3
    while time.time() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return not _pid_alive(pid)


def _name_matches(name, allowed):
    def norm(value):
        base = os.path.basename((value or "").strip()).lower()
        return base[:-4] if base.endswith(".exe") else base

    target = norm(name)
    return bool(target) and any(target == norm(a) for a in allowed)


def _process_pairs():
    """返回本机进程的 (pid, name) 列表（尽力而为）。"""
    pairs = []
    if os.name == "nt":
        _, out = run_cmd(["tasklist", "/FO", "CSV", "/NH"], timeout=10)
        for line in (out or "").splitlines():
            m = re.match(r'"([^"]+)","(\d+)"', line.strip())
            if m:
                pairs.append((int(m.group(2)), m.group(1)))
    else:
        _, out = run_cmd(["ps", "-eo", "pid=,comm="], timeout=10)
        for line in (out or "").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                pairs.append((int(parts[0]), parts[1].strip()))
    return pairs


def kill_processes_by_name(allowed_names, logger):
    """结束所有名称匹配的后端进程，即使它没有监听端口。"""
    killed = []
    for pid, name in _process_pairs():
        if pid == os.getpid() or not _name_matches(name, allowed_names):
            continue
        logger.add("强制结束后端进程 %s（PID %d）" % (name, pid), "warn")
        _kill_pid(pid)
        killed.append(pid)
    return killed


def _proxy_connection_pids(port):
    """返回与本机指定端口建立 ESTABLISHED 连接的 PID 集合。"""
    port = int(port)
    pids = set()
    if os.name == "nt":
        _, out = run_cmd(["netstat", "-ano", "-p", "tcp"], timeout=8)
        for line in (out or "").splitlines():
            m = re.match(
                r"^\s*TCP\s+(\S+):(\d+)\s+(\S+):(\d+)\s+(\S+)\s+(\d+)\s*$",
                line,
            )
            if not m or m.group(5).upper() != "ESTABLISHED":
                continue
            if int(m.group(2)) != port and int(m.group(4)) != port:
                continue
            pids.add(int(m.group(6)))
    else:
        _, out = run_cmd(
            ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:ESTABLISHED", "-t"],
            timeout=8,
        )
        for tok in (out or "").split():
            if tok.isdigit():
                pids.add(int(tok))
    return pids


def opencode_status(http_port):
    """检测 start-opencode.bat 拉起的 opencode 进程及代理连接。"""
    pids = sorted(
        pid for pid, name in _process_pairs()
        if _name_matches(name, ("opencode", "opencode.exe"))
    )
    conn_pids = _proxy_connection_pids(http_port)
    active = [pid for pid in pids if pid in conn_pids]
    return {
        "running": bool(pids),
        "pids": pids,
        "count": len(pids),
        "proxy_connected": bool(active),
        "connections": len(active),
    }


def start_opencode_process(logger, http_port):
    """通过 start-opencode.bat 在新控制台启动 OpenCode。

    绝不结束用户已经打开的 opencode：检测到已有实例时直接复用现有窗口，
    跳过本次启动。自动启动与控制台按钮都走这里，两条路径都不会强杀。
    """
    before = opencode_status(http_port)
    existing = before.get("pids") or []
    bat = os.path.join(APP_DIR, "start-opencode.bat")
    if existing:
        msg = ("检测到 OpenCode 已在运行（PID %s），复用现有窗口，跳过启动；"
               "不会结束现有进程" % ", ".join(str(p) for p in existing))
        logger.add(msg)
        return {"ok": True, "message": msg, "skipped": True,
                "killed": [], "path": bat}

    exe = os.path.join(APP_DIR, "bin", "opencode.exe")
    if not os.path.isfile(bat):
        msg = "未找到启动脚本：%s" % bat
        if not os.path.isfile(exe):
            msg += "（且未找到 %s）" % exe
        logger.add(msg, "error")
        return {"ok": False, "message": msg, "killed": [], "path": bat}

    try:
        if os.name == "nt":
            CREATE_NEW_CONSOLE = 0x00000010
            # Do not redirect stdio: opencode is an interactive TUI and needs a real console.
            subprocess.Popen(
                ["cmd.exe", "/c", bat],
                cwd=APP_DIR,
                creationflags=CREATE_NEW_CONSOLE,
                close_fds=True,
            )
        else:
            subprocess.Popen(
                [bat],
                cwd=APP_DIR,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except Exception as e:
        msg = "通过 start-opencode.bat 启动 OpenCode 失败：%s" % e
        logger.add(msg, "error")
        return {"ok": False, "message": msg, "killed": [], "path": bat}

    msg = "已通过 start-opencode.bat 启动 OpenCode（新窗口）"
    logger.add(msg)
    return {"ok": True, "message": msg, "killed": [], "path": bat}


def reclaim_port(port, allowed_names, logger, force=False):
    """清理占用端口的残留后端进程。

    force=True 时无条件结束端口占用者（跳过自身进程）；
    否则只结束名称匹配 allowed_names 的进程，避免误杀用户的其他服务。
    """
    port = int(port)
    owners = _listener_pids(port)
    if not owners:
        return True

    killable = []
    for pid in owners:
        if pid == os.getpid():
            continue
        name = _pid_name(pid)
        if not force and not _name_matches(name, allowed_names):
            logger.add(
                "SOCKS5 端口 %d 已被 PID %d（%s）占用，且不是已知 WARP 后端；"
                "请结束该进程或在设置里更换端口"
                % (port, pid, name or "未知进程"),
                "error",
            )
            return False
        killable.append((pid, name))

    for pid, name in killable:
        logger.add(
            ("强制结束占用端口 %d 的进程 %s（PID %d）" if force
             else "发现残留后端进程 %s（PID %d）占用端口 %d，正在清理")
            % ((port, name or "未知进程", pid) if force
               else (name or "未知进程", pid, port)),
            "warn",
        )
        _kill_pid(pid)

    deadline = time.time() + 6
    while time.time() < deadline:
        if not port_open("127.0.0.1", port, timeout=0.3):
            return True
        time.sleep(0.3)

    logger.add("清理后端口 %d 仍被占用，请手动结束占用进程" % port, "error")
    return False


def fmt_dur(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return "%02d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def run_cmd(args, timeout=25, shell=False):
    kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=shell,
    )
    if os.name == "nt":
        kwargs["creationflags"] = NO_WINDOW
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        kwargs["startupinfo"] = si
    try:
        p = subprocess.run(args, **kwargs)
        return p.returncode, (p.stdout or "").strip()
    except FileNotFoundError:
        return 127, "找不到可执行文件：%s" % (args[0] if isinstance(args, list) else args)
    except subprocess.TimeoutExpired:
        return 124, "命令执行超时"
    except Exception as e:
        return 1, str(e)


def find_exec(names, extra_paths=()):
    for p in extra_paths:
        if p and os.path.isfile(p):
            return p
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return ""


def app_path(path):
    """把配置里的路径解析到当前程序目录；项目外的绝对路径原样保留。

    即使内存里残留指向旧目录/副本的绝对路径，也会先规范化成相对路径再
    拼到当前 APP_DIR，保证目录改名或复制后不会跑到别的目录去启动 exe。
    """
    path = (path or "").strip()
    if not path or "://" in path:
        return path
    rel = to_rel_path(path)
    return rel if os.path.isabs(rel) else os.path.join(APP_DIR, rel)


def detect_backends(include_builtin=False):
    """按优先级返回已安装的 WARP 后端。"""
    found = []

    p = find_exec(["warp-cli", "warp-cli.exe"], WARPCI_PATHS)
    if p:
        _, ver = run_cmd([p, "--version"], timeout=10)
        found.append({"key": "warp-cli", "path": p, "version": ver or "已安装"})

    p = find_exec(["warp-plus", "warp-plus.exe"])
    if p:
        _, ver = run_cmd([p, "--version"], timeout=10)
        found.append({"key": "warp-plus", "path": p, "version": ver or "已安装"})

    p = find_exec(["usque", "usque.exe"])
    if p:
        _, ver = run_cmd([p, "--version"], timeout=10)
        found.append({"key": "usque", "path": p, "version": ver or "已安装"})

    if include_builtin:
        dest = builtin_binary_path()
        found.append({
            "key": "builtin",
            "path": dest if os.path.isfile(dest) else "尚未下载（首次启动时自动下载）",
            "version": "内置 warp-plus（免安装）",
        })

    return found


# --------------------------------------------------------------------------- #
# 内置后端（自动下载官方 warp-plus）
# --------------------------------------------------------------------------- #

BUILTIN_DIR = os.path.join(APP_DIR, "bin")
BUILTIN_REPO = "https://github.com/bepass-org/warp-plus"
BUILTIN_FALLBACK_TAG = "v1.2.6"


def builtin_binary_path():
    exe = ".exe" if os.name == "nt" else ""
    return os.path.join(BUILTIN_DIR, "warp-plus" + exe)


def platform_asset_key():
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "windows":
        osname = "windows"
    elif system == "darwin":
        osname = "darwin"
    else:
        osname = "linux"

    arch = {
        "x86_64": "amd64", "amd64": "amd64",
        "aarch64": "arm64", "arm64": "arm64",
        "i386": "386", "i686": "386", "x86": "386",
    }.get(machine, "amd64")

    return "%s-%s" % (osname, arch)


def resolve_warp_plus_tag(timeout=10):
    """查询最新版本号；被限流或断网时回退到内置版本。"""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/bepass-org/warp-plus/releases/latest",
            headers={"User-Agent": "warp-control/1.0", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = (data or {}).get("tag_name")
        if tag and re.match(r"^v?[0-9]", tag):
            return tag
    except Exception:
        pass
    return BUILTIN_FALLBACK_TAG


def download_builtin(logger, mirror=""):
    """下载并解压官方 warp-plus 到 bin/，返回 (ok, path_or_error)。"""
    key = platform_asset_key()
    dest = builtin_binary_path()
    tmp = dest + ".download"
    try:
        os.makedirs(BUILTIN_DIR, exist_ok=True)
    except Exception as e:
        return False, "创建目录失败：%s" % e

    tag = resolve_warp_plus_tag()
    official = "%s/releases/download/%s/warp-plus_%s.zip" % (BUILTIN_REPO, tag, key)

    urls = []
    mirror = (mirror or "").strip().rstrip("/")
    if mirror:
        urls.append("%s/%s" % (mirror, official))
    urls.append(official)

    last_err = ""
    for url in urls:
        logger.add("开始下载内置后端：%s" % url)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "warp-control/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(tmp, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                got = 0
                last_log = 0.0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if time.time() - last_log >= 1.5:
                        last_log = time.time()
                        if total:
                            logger.add("下载中 %d%%（%.1f / %.1f MB）"
                                       % (got * 100 // total, got / 1048576.0, total / 1048576.0), "debug")
                        else:
                            logger.add("下载中 %.1f MB" % (got / 1048576.0), "debug")

            with zipfile.ZipFile(tmp) as z:
                member = ""
                for name in z.namelist():
                    if os.path.basename(name).lower() in ("warp-plus", "warp-plus.exe"):
                        member = name
                        break
                if not member:
                    raise RuntimeError("压缩包内未找到 warp-plus 可执行文件")
                with z.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)

            if os.path.getsize(dest) < 1024 * 512:
                raise RuntimeError("下载文件体积异常，可能被拦截或不是有效程序")

            if os.name != "nt":
                os.chmod(dest, 0o755)
            try:
                os.remove(tmp)
            except OSError:
                pass
            logger.add("内置后端已就绪：%s" % dest)
            return True, dest
        except Exception as e:
            last_err = str(e)
            logger.add("下载失败：%s" % last_err, "warn")
            try:
                os.remove(tmp)
            except OSError:
                pass

    return False, last_err or "未知错误"



# --------------------------------------------------------------------------- #
# SOCKS5 客户端
# --------------------------------------------------------------------------- #

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("连接被对端关闭")
        buf += chunk
    return buf


def socks5_connect(shost, sport, dhost, dport, timeout=15.0):
    """通过 SOCKS5 代理建立到目标的 TCP 连接。"""
    s = socket.create_connection((shost, int(sport)), timeout=timeout)
    s.settimeout(timeout)
    try:
        s.sendall(b"\x05\x01\x00")
        if _recv_exact(s, 2) != b"\x05\x00":
            raise OSError("SOCKS5 认证协商失败")

        try:
            ip = ipaddress.ip_address(dhost)
            atyp = b"\x01" if ip.version == 4 else b"\x04"
            addr = ip.packed
        except ValueError:
            try:
                name = dhost.encode("idna")
            except UnicodeError:
                name = dhost.encode("ascii", "ignore")
            if len(name) > 255:
                raise OSError("目标域名过长")
            atyp = b"\x03"
            addr = bytes([len(name)]) + name

        s.sendall(b"\x05\x01\x00" + atyp + addr + int(dport).to_bytes(2, "big"))

        ver, rep, _, ratyp = _recv_exact(s, 4)
        if ver != 5 or rep != 0:
            raise OSError("SOCKS5 连接被拒绝（code=%d）" % rep)
        if ratyp == 1:
            _recv_exact(s, 4)
        elif ratyp == 4:
            _recv_exact(s, 16)
        elif ratyp == 3:
            ln = _recv_exact(s, 1)[0]
            _recv_exact(s, ln)
        _recv_exact(s, 2)

        s.settimeout(None)
        return s
    except Exception:
        try:
            s.close()
        except Exception:
            pass
        raise


def _pipe(a, b, idle=180):
    """双向转发，任一端关闭即结束。"""
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], idle)
            if not r:
                return
            for s in r:
                try:
                    data = s.recv(65536)
                except OSError:
                    return
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


# --------------------------------------------------------------------------- #
# HTTP -> SOCKS5 桥接代理
# --------------------------------------------------------------------------- #

class HttpProxyBridge(threading.Thread):
    """把本地 HTTP 代理请求转发到上游 SOCKS5。"""

    daemon = True

    def __init__(self, listen_host, listen_port, socks_host, socks_port, logger):
        super().__init__(name="http-bridge")
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self.socks_host = socks_host
        self.socks_port = int(socks_port)
        self.log = logger
        self._stop = threading.Event()
        self._srv = None

    def run(self):
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.listen_host, self.listen_port))
            srv.listen(128)
            srv.settimeout(0.5)
            self._srv = srv
            while not self._stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        except Exception as e:
            self.log.add("HTTP 代理端口监听失败：%s" % e, "error")

    def stop(self):
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except Exception:
                pass

    # -- 处理单个客户端 -------------------------------------------------- #

    def _handle(self, conn):
        established = False
        try:
            conn.settimeout(30)
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                head += chunk
                if len(head) > 262144:
                    self._reply(conn, 431, "Request Header Too Large")
                    return

            header_block, _, rest = head.partition(b"\r\n\r\n")
            lines = header_block.split(b"\r\n")
            try:
                method, target, _version = lines[0].decode("latin1").split(" ", 2)
            except ValueError:
                self._reply(conn, 400, "Bad Request")
                return

            headers = []
            for ln in lines[1:]:
                if b":" in ln:
                    k, v = ln.split(b":", 1)
                    headers.append((k.strip().decode("latin1"), v.strip().decode("latin1")))

            if method.upper() == "CONNECT":
                host, port = self._split_host_port(target, 443)
                remote = socks5_connect(self.socks_host, self.socks_port, host, port)
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                established = True
                _pipe(conn, remote)
                remote.close()
                return

            # 普通 HTTP 绝对 URI 转发
            parts = urllib.parse.urlsplit(target)
            if not parts.hostname:
                host_header = ""
                for k, v in headers:
                    if k.lower() == "host":
                        host_header = v
                        break
                if not host_header:
                    self._reply(conn, 400, "Missing Host")
                    return
                host, port = self._split_host_port(host_header, 80)
                path = target if target.startswith("/") else "/" + target
            else:
                host = parts.hostname
                port = parts.port or (443 if parts.scheme == "https" else 80)
                path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))

            remote = socks5_connect(self.socks_host, self.socks_port, host, port)

            out = ["%s %s HTTP/1.1" % (method, path)]
            has_host = False
            for k, v in headers:
                low = k.lower()
                if low in ("proxy-connection", "connection", "keep-alive"):
                    continue
                if low == "host":
                    has_host = True
                out.append("%s: %s" % (k, v))
            if not has_host:
                out.append("Host: %s" % host)
            out.append("Connection: close")
            out.append("")
            out.append("")
            payload = "\r\n".join(out).encode("latin1")

            body = rest
            clen = 0
            for k, v in headers:
                if k.lower() == "content-length":
                    try:
                        clen = int(v)
                    except ValueError:
                        clen = 0
                    break
            while len(body) < clen:
                try:
                    chunk = conn.recv(min(65536, clen - len(body)))
                except OSError:
                    break
                if not chunk:
                    break
                body += chunk

            remote.sendall(payload + body)
            established = True
            conn.settimeout(None)
            _pipe(remote, conn)
            remote.close()
        except Exception:
            if not established:
                try:
                    self._reply(conn, 502, "Bad Gateway")
                except Exception:
                    pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @staticmethod
    def _split_host_port(value, default_port):
        value = value.strip()
        if value.startswith("["):
            host, _, port = value[1:].partition("]")
            return host, int(port[1:]) if port.startswith(":") else default_port
        if value.count(":") == 1:
            host, _, port = value.partition(":")
            try:
                return host, int(port)
            except ValueError:
                return host, default_port
        return value, default_port

    @staticmethod
    def _reply(conn, code, text):
        reason = {400: "Bad Request", 431: "Request Header Too Large", 502: "Bad Gateway"}.get(code, "Error")
        body = text.encode("utf-8", "replace")
        head = (
            "HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n" % (code, reason, len(body))
        )
        conn.sendall(head.encode("latin1") + body)


# --------------------------------------------------------------------------- #
# WARP 后端
# --------------------------------------------------------------------------- #

class BaseBackend:
    key = "base"

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.log = app.log
        self.path = ""
        self.proc = None
        self._recent = deque(maxlen=30)

    def available(self):
        return bool(self.path) and (os.path.isfile(self.path) or shutil.which(self.path))

    def start(self):
        raise NotImplementedError

    def stop(self):
        pass

    def restart(self):
        self.stop()
        time.sleep(1.2)
        return self.start()

    # 子进程管理
    def _spawn(self, cmd, shell=False):
        kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=shell,
            # 子进程的当前目录固定为项目根：脚本里可以放心用相对路径，
            # usque 默认读当前目录 config.json 的行为也不会跑偏。
            cwd=APP_DIR,
        )
        if os.name == "nt":
            kwargs["creationflags"] = NO_WINDOW | NEW_GROUP
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
        else:
            # 独立进程组，便于连同子进程一起结束
            kwargs["start_new_session"] = True
        self.proc = subprocess.Popen(cmd, **kwargs)
        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()

    def _drain(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self._recent.append(line)
                    self.log.add("[%s] %s" % (self.key, line), "debug")
        except Exception:
            pass

    def _prepare_port(self, port, allowed_names):
        """启动前强制清理占用端口的进程和同名残留后端。"""
        if allowed_names:
            kill_processes_by_name(allowed_names, self.log)
        if reclaim_port(port, allowed_names, self.log, force=True):
            return True, ""
        return False, (
            "SOCKS5 端口 %d 强制清理后仍被占用，无法启动 %s。\n"
            "请在任务管理器中结束占用进程，或在设置里更换端口。" % (int(port), self.key)
        )

    def _wait_owned(self, port, timeout, require_owner=True):
        """等待端口就绪，并确认监听者就是本次拉起的进程。

        仅判断端口是否可连会把“新进程绑定失败、旧进程仍在监听”误判为启动成功。
        require_owner=False 用于 shell 包装的自定义命令：监听者可能是子进程。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            p = self.proc
            if p is None:
                return False
            if p.poll() is not None:
                return False
            if port_open("127.0.0.1", port, timeout=0.5):
                if not require_owner:
                    return True
                owners = _listener_pids(port)
                if not owners or p.pid in owners:
                    return True
            time.sleep(0.4)
        return False

    def _startup_error(self, label, port):
        p = self.proc
        rc = p.poll() if p is not None else None
        parts = ["%s 启动后端口 %d 未就绪或进程已退出" % (label, int(port))]
        if rc is not None:
            parts.append("退出码：%s" % rc)
        tail = "\n".join(self._recent)
        if tail:
            parts.append("最近输出：\n" + tail)
        return "\n".join(parts)

    def _kill_proc(self):
        p = self.proc
        self.proc = None
        if not p:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                )
            else:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                else:
                    p.kill()
            except Exception:
                pass


class WarpCliBackend(BaseBackend):
    key = "warp-cli"

    def __init__(self, app, path):
        super().__init__(app)
        self.path = path

    def _run(self, *args, timeout=30):
        return run_cmd([self.path, "--accept-tos"] + list(args), timeout=timeout)

    def start(self):
        port = str(int(self.cfg["socks_port"]))

        code, out = self._run("mode", "proxy", timeout=15)
        if code != 0:
            self.log.add("warp-cli mode proxy 不受支持，尝试沿用当前模式：%s" % (out or code), "warn")

        code, out = self._run("proxy", "port", port, timeout=15)
        if code != 0:
            self.log.add("设置代理端口失败（可能为旧版本或已在代理模式）：%s" % (out or code), "warn")

        code, out = self._run("connect", timeout=40)
        if code != 0:
            hint = ""
            if re.search(r"registration|register|not registered", out or "", re.I):
                hint = "（请先执行：warp-cli --accept-tos registration new）"
            return False, "warp-cli connect 失败：%s%s" % (out or code, hint)

        if wait_for_port("127.0.0.1", self.cfg["socks_port"], 25):
            return True, "warp-cli 已连接，SOCKS5 监听 127.0.0.1:%s" % port

        code, out = self._run("status", timeout=15)
        return False, (
            "warp-cli 已执行 connect，但 SOCKS5 端口 %s 未就绪。当前状态：%s\n"
            "请确认已切换为代理模式（warp-cli mode proxy）" % (port, out or "未知")
        )

    def stop(self):
        self._run("disconnect", timeout=20)

    def status(self):
        return self._run("status", timeout=10)[1]


class WarpPlusBackend(BaseBackend):
    key = "warp-plus"

    def __init__(self, app, path):
        super().__init__(app)
        self.path = path

    def start(self):
        port = int(self.cfg["socks_port"])
        ok, msg = self._prepare_port(port, ("warp-plus", "warp-plus.exe"))
        if not ok:
            return False, msg

        cmd = [self.path, "-b", "127.0.0.1:%d" % port]
        extra = (self.cfg.get("warp_plus_args") or "").strip()
        if extra:
            try:
                cmd += shlex.split(extra, posix=(os.name != "nt"))
            except ValueError as e:
                return False, "warp-plus 额外参数解析失败：%s" % e

        self.log.add("启动命令：%s" % show_cmd(cmd))
        try:
            self._spawn(cmd)
        except Exception as e:
            return False, "warp-plus 启动失败：%s" % e

        if self._wait_owned(port, 40):
            return True, "warp-plus 已启动，SOCKS5 监听 127.0.0.1:%d" % port

        err = self._startup_error("warp-plus", port)
        self._kill_proc()
        return False, (
            err + "\n"
            "若日志显示正在注册或连接 Cloudflare，通常是网络无法直连其接口，请检查网络/代理设置。"
        )

    def stop(self):
        self._kill_proc()


class BuiltinBackend(WarpPlusBackend):
    """内置 warp-plus：本地没有时会自动下载官方程序。"""

    key = "builtin"

    def __init__(self, app):
        super().__init__(app, builtin_binary_path())

    def ensure(self):
        dest = builtin_binary_path()
        if os.path.isfile(dest) and os.path.getsize(dest) > 1024 * 512:
            self.path = dest
            return True, dest

        self.log.add("未发现内置后端，开始自动下载 warp-plus（仅首次需要联网）")
        ok, res = download_builtin(self.log, self.cfg.get("builtin_mirror") or "")
        if not ok:
            return False, (
                "内置后端下载失败：%s\n"
                "可手动下载 warp-plus 后放到：%s\n"
                "或在“设置”中填写下载镜像地址" % (res, BUILTIN_DIR)
            )
        self.path = res
        return True, res

    def start(self):
        ok, res = self.ensure()
        if not ok:
            return False, res
        self.log.add("使用内置后端：%s" % res)
        return super().start()


class UsqueBackend(BaseBackend):
    key = "usque"

    def __init__(self, app, path):
        super().__init__(app)
        self.path = path

    def config_path(self):
        return app_path((self.cfg.get("usque_config") or "").strip()) or app_path(
            os.path.join("config", "usque", "config.json")
        )

    def start(self):
        port = int(self.cfg["socks_port"])
        cfg_path = self.config_path()
        os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
        if not os.path.isfile(cfg_path):
            return False, (
                "未找到 usque 配置：%s\n"
                "首次使用需注册一次，请在项目根目录执行：\n"
                "  \"%s\" -c \"%s\" register -a" % (
                    to_rel_path(cfg_path), to_rel_path(self.path), to_rel_path(cfg_path)
                )
            )

        endpoint = (self.cfg.get("usque_endpoint") or "").strip()
        if endpoint:
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    ucfg = json.load(f)
                changed = []
                for key in ("endpoint_v4", "endpoint_h2_v4"):
                    if key in ucfg and ucfg[key] != endpoint:
                        ucfg[key] = endpoint
                        changed.append(key)
                if changed:
                    with open(cfg_path, "w", encoding="utf-8") as f:
                        json.dump(ucfg, f, ensure_ascii=False, indent=2)
                    self.log.add("usque 端点已设为 %s（%s）" % (endpoint, ", ".join(changed)))
            except Exception as e:
                return False, "无法写入 usque 端点 %s：%s" % (endpoint, e)

        ok, msg = self._prepare_port(port, ("usque", "usque.exe"))
        if not ok:
            return False, msg

        # usque v4 的 --bind 只接受地址，端口需用 --port 单独指定；
        # 新版默认从当前目录读取 config.json，这里显式指定配置文件避免冲突。
        cmd = [
            self.path, "-c", cfg_path, "socks",
            "--bind", "127.0.0.1", "--port", str(port),
        ]
        self.log.add("启动命令：%s" % show_cmd(cmd))
        try:
            self._spawn(cmd)
        except Exception as e:
            return False, "usque 启动失败：%s" % e

        if self._wait_owned(port, 25):
            return True, "usque 已启动，SOCKS5 监听 127.0.0.1:%d" % port

        err = self._startup_error("usque", port)
        self._kill_proc()
        return False, err + "\n请检查 usque 配置、端口占用，或重新执行 usque register"

    def stop(self):
        self._kill_proc()


class CustomBackend(BaseBackend):
    key = "custom"

    def __init__(self, app, command):
        super().__init__(app)
        self.command = (command or "").strip()
        self.path = self.command

    def _command_exe(self):
        cmd = self.command.strip()
        if cmd.startswith('"'):
            end = cmd.find('"', 1)
            if end > 0:
                return cmd[1:end]
        parts = cmd.split()
        return parts[0] if parts else ""

    def start(self):
        if not self.command:
            return False, "自定义命令为空，请在设置中填写，例如：warp-plus.exe -b 127.0.0.1:{port}"
        port = int(self.cfg["socks_port"])
        exe = self._command_exe()
        allowed = (os.path.basename(exe),) if exe else ()
        ok, msg = self._prepare_port(port, allowed)
        if not ok:
            return False, msg

        cmd = self.command.replace("{port}", str(port))
        self.log.add("启动命令：%s" % show_cmd(cmd))
        try:
            self._spawn(cmd, shell=True)
        except Exception as e:
            return False, "自定义命令启动失败：%s" % e

        if self._wait_owned(port, 25, require_owner=False):
            return True, "自定义命令已启动，SOCKS5 监听 127.0.0.1:%d" % port

        err = self._startup_error("自定义命令", port)
        self._kill_proc()
        return False, err

    def stop(self):
        self._kill_proc()


# --------------------------------------------------------------------------- #
# OpenCode Zen 入口（zen-gate）
# --------------------------------------------------------------------------- #

def zen_default_home():
    """zen-gate 自身使用的默认数据目录。"""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "zen-gate")
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "zen-gate")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "zen-gate")


def _zen_port(cfg, zen_mgr=None):
    if zen_mgr and getattr(zen_mgr, "port", None):
        return int(zen_mgr.port)
    return int((cfg or {}).get("zen_port") or 8788)


def _zen_local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _zen_main_key(cfg, zen_mgr=None, port=None):
    port = int(port or _zen_port(cfg, zen_mgr))
    # Running instance's admin state is the most authoritative source.
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/admin/api/state" % port,
            headers={"User-Agent": "warp-control/1.0"},
        )
        with _zen_local_opener().open(req, timeout=6) as resp:
            state = json.loads(resp.read().decode("utf-8", "replace"))
        key = (state.get("mainKey") or "").strip()
        if key:
            return key
    except Exception:
        pass
    candidates = []
    data_dir = app_path((cfg or {}).get("zen_data_dir") or "")
    if data_dir:
        candidates.append(os.path.join(data_dir, "config.json"))
    candidates.append(os.path.join(zen_default_home(), "config.json"))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            key = (data.get("mainKey") or data.get("masterKey") or "").strip()
            if key:
                return key
        except Exception:
            pass
    return ""


def sync_opencode_key(cfg, zen_mgr=None):
    """把 zen-gate 的 mainKey 同步到 config/zen-gate.key。

    config/opencode.json 里的 apiKey 写成 {file:zen-gate.key}，主密钥因此不会进版本库；
    这里在 zen-gate 启动/接管后自动补齐该文件，避免 OpenCode 报 401 invalid API key。
    返回 True 表示本次写入/更新了文件。
    """
    key = _zen_main_key(cfg, zen_mgr)
    if not key:
        return False
    path = os.path.join(APP_DIR, "config", "zen-gate.key")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if f.read().strip() == key:
                    return False
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(key)
        return True
    except Exception:
        return False


def zen_models_snapshot(cfg, zen_mgr=None):
    port = _zen_port(cfg, zen_mgr)
    base = "http://127.0.0.1:%d" % port
    try:
        req = urllib.request.Request(
            base + "/admin/api/state",
            headers={"User-Agent": "warp-control/1.0"},
        )
        with _zen_local_opener().open(req, timeout=8) as resp:
            state = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:
        return {
            "ok": False,
            "error": str(e),
            "port": port,
            "base_url": base + "/v1",
            "models": [],
            "stats": {},
            "channels": [],
            "has_key": bool(_zen_main_key(cfg, zen_mgr, port)),
        }
    models = state.get("models") or []
    if not isinstance(models, list):
        models = []
    stats = state.get("stats") or {}
    channels = state.get("channels") or []
    if not isinstance(channels, list):
        channels = []
    has_key = bool((state.get("mainKey") or "").strip() or _zen_main_key(cfg, zen_mgr, port))
    settings = state.get("settings") or {}
    if not isinstance(settings, dict):
        settings = {}
    return {
        "ok": True,
        "port": port,
        "base_url": base + "/v1",
        "main_key_present": bool((state.get("mainKey") or "").strip()),
        "has_key": has_key,
        "lane_fallback": bool(settings.get("laneFallbackToProviders")),
        "models": models,
        "stats": stats,
        "channels": channels,
    }


def zen_test_model(cfg, zen_mgr, model, prompt="", max_tokens=64):
    model = (model or "").strip()
    if not model:
        return {"ok": False, "error": "model 不能为空"}
    port = _zen_port(cfg, zen_mgr)
    main_key = _zen_main_key(cfg, zen_mgr, port)
    if not main_key:
        return {
            "ok": False,
            "model": model,
            "error": "未找到 zen-gate 主密钥（mainKey）。请确认 zen-gate 已配置且 WARP Control 能访问其数据目录/管理接口。",
        }
    prompt = (prompt or "Reply with exactly: OK").strip()
    if len(prompt) > 2000:
        prompt = prompt[:2000]
    try:
        max_tokens = int(max_tokens or 64)
    except Exception:
        max_tokens = 64
    max_tokens = max(1, min(max_tokens, 512))
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": False,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % port,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + main_key,
            "User-Agent": "warp-control/1.0",
        },
    )
    started = time.time()
    try:
        with _zen_local_opener().open(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        ms = int((time.time() - started) * 1000)
        text = ""
        try:
            choice = ((body.get("choices") or [{}])[0]) or {}
            msg = choice.get("message") or {}
            content = msg.get("content", choice.get("text", ""))
            if isinstance(content, list):
                text = "".join(
                    (part.get("text") or "") for part in content if isinstance(part, dict)
                )
            else:
                text = "" if content is None else str(content)
        except Exception:
            text = ""
        return {
            "ok": True,
            "model": model,
            "ms": ms,
            "text": text[:4000],
            "usage": body.get("usage") or {},
            "raw_model": body.get("model") or "",
            "id": body.get("id") or "",
        }
    except urllib.error.HTTPError as e:
        ms = int((time.time() - started) * 1000)
        raw = e.read().decode("utf-8", "replace") if hasattr(e, "read") else ""
        try:
            err = json.loads(raw) if raw else {"message": str(e)}
        except Exception:
            err = {"message": raw or str(e)}
        return {"ok": False, "model": model, "ms": ms, "status": getattr(e, "code", None), "error": err}
    except Exception as e:
        return {"ok": False, "model": model, "ms": int((time.time() - started) * 1000), "error": str(e)}


def zen_reprobe(cfg, zen_mgr=None):
    """让 zen-gate 重新探测模型可用性（等价于面板上的“重新探测”按钮）。"""
    port = _zen_port(cfg, zen_mgr)
    req = urllib.request.Request(
        "http://127.0.0.1:%d/admin/api/reprobe" % port,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "warp-control/1.0",
        },
    )
    try:
        with _zen_local_opener().open(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {"raw": raw[:300]}
        return {"ok": True, "port": port, "result": body}
    except urllib.error.HTTPError as e:
        return {"ok": False, "port": port, "error": "HTTP %s" % getattr(e, "code", "?")}
    except Exception as e:
        return {"ok": False, "port": port, "error": str(e)}


class ZenGate:
    """托管 zen-gate，并把 OpenCode Zen 的限额信号接到换 IP 上。

    zen-gate 把 OpenCode Zen 免费车道封装成本地 OpenAI / Anthropic 兼容入口；
    免费额度按出口 IP 计量，所以它的 {access} / {routing} 日志里出现
    status=429、err=quota 之类信号时，换一个 WARP 出口就能接着用。

    行为：
      * 端口已在监听：直接接管监控，不碰用户正在跑的实例；
      * 端口空闲：用 -no-tray 拉起，并让它继承 WARP 的 HTTP 代理；
      * 盯 <数据目录>/logs/<日期>.log，命中限额就通知 App 换 IP（带冷却）。
    """

    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.log = app.log
        self.proc = None
        self.owned = False
        self.attached = False
        self.port = int(self.cfg.get("zen_port") or 8788)
        self.quota_hits = 0
        self.last_hit = ""
        self.last_reason = ""
        self._stop = threading.Event()
        self._thread = None
        self._log_path = ""
        self._log_pos = None
        self._cooldown_until = 0.0
        self._run_checked = 0.0
        self._run_ok = False
        self._prev_proxy = None
        self._egress_patched = False
        self._last_unverified_log = 0.0

    # -- 路径与状态 ------------------------------------------------------ #

    def binary(self):
        p = app_path(self.cfg.get("zen_path") or "")
        return p if p and os.path.isfile(p) else ""

    def data_dir(self):
        return app_path(self.cfg.get("zen_data_dir") or "") or zen_default_home()

    def log_dirs(self):
        dirs = [os.path.join(self.data_dir(), "logs")]
        default = os.path.join(zen_default_home(), "logs")
        if default not in dirs:
            dirs.append(default)
        return dirs

    # -- 本机 HTTP / 进程探测 -------------------------------------------- #

    @staticmethod
    def _local_opener():
        # 管理接口只在 loopback；显式禁用环境代理，免得请求被 HTTP_PROXY 带走。
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _healthz(self, port, timeout=0.6):
        try:
            url = "http://127.0.0.1:%d/healthz" % int(port)
            with self._local_opener().open(url, timeout=timeout) as resp:
                data = json.loads(resp.read(512).decode("utf-8", "replace"))
            return isinstance(data, dict) and data.get("service") == "zen-gate"
        except Exception:
            return False

    @staticmethod
    def _zen_pids():
        if os.name != "nt":
            return set()
        code, out = run_cmd(
            ["tasklist", "/FI", "IMAGENAME eq zen-gate.exe", "/FO", "CSV", "/NH"],
            timeout=8,
        )
        return set(re.findall(r'"zen-gate\.exe","(\d+)"', out or "", re.I))

    @staticmethod
    def _local_listeners():
        out = ""
        if os.name == "nt":
            _, out = run_cmd(["netstat", "-ano", "-p", "tcp"], timeout=8)
        else:
            _, out = run_cmd(["netstat", "-ano", "-p", "tcp"], timeout=8)
        found = []
        for line in (out or "").splitlines():
            m = re.match(
                r"\s*TCP\s+(?:127\.0\.0\.1|\[::1\]):(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$",
                line, re.I,
            )
            if m:
                found.append((int(m.group(1)), m.group(2)))
        return found

    def resolve_port(self):
        """优先用配置端口；否则找出正在运行的 zen-gate 网关端口。"""
        cfg_port = int(self.cfg.get("zen_port") or 8788)
        candidates = [cfg_port]

        pids = self._zen_pids()
        if pids:
            for port, pid in self._local_listeners():
                if pid in pids and port not in candidates:
                    candidates.append(port)
        for port in (8788, 8787, 8789, 8790):
            if port not in candidates:
                candidates.append(port)

        for port in candidates:
            if self._healthz(port):
                if port != cfg_port:
                    self.log.add("zen-gate 实际监听 %d（配置为 %d），已自动对齐" % (port, cfg_port))
                self.port = port
                return port
        self.port = cfg_port
        return cfg_port

    def running(self):
        now = time.time()
        if now - self._run_checked >= 2.0:
            self._run_checked = now
            self._run_ok = self._healthz(self.port)
        return self._run_ok

    def status(self):
        return {
            "enabled": bool(self.cfg.get("zen_enabled")),
            "port": self.port,
            "running": self.running(),
            "owned": self.owned and self.proc is not None,
            "attached": self.attached,
            "egress": "warp" if self._egress_patched else "native",
            "quota_hits": self.quota_hits,
            "last_hit": self.last_hit,
            "last_reason": self.last_reason,
            "data_dir": self.data_dir(),
        }

    # -- 启停 ------------------------------------------------------------ #

    def start(self):
        self.port = self.resolve_port()
        self._run_checked = 0.0
        self._stop.clear()

        if self._healthz(self.port):
            self.attached = True
            self.log.add("已接管 zen-gate（127.0.0.1:%d），限额信号将触发换 IP" % self.port)
        else:
            ok, msg = self._spawn()
            if not ok:
                self.log.add(msg, "warn")
                return False
            self.owned = True
            self.log.add(msg)

        if self.cfg.get("zen_force_warp"):
            self._install_egress()

        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return True

    # -- 出口接管（zen-gate 管理接口） ----------------------------------- #

    def _admin_state(self):
        url = "http://127.0.0.1:%d/admin/api/state" % self.port
        with self._local_opener().open(url, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _admin_post(self, path, payload):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/admin/api/%s" % (self.port, path),
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self._local_opener().open(req, timeout=6) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _warp_verified(self):
        """WARP 出口是否已由探测确认：拿到过出口 IP 且 Cloudflare 判定 WARP=on。"""
        app = self.app
        egress = (getattr(app, "egress_ip", "") or "").strip()
        warp = (getattr(app, "warp", "") or "").strip().lower()
        return bool(egress) and warp == "on"

    def _install_egress(self):
        """把 zen-gate 的代理模式切到 WARP 的 HTTP 桥，停止时还原。"""
        proxy_url = "http://127.0.0.1:%d" % int(self.cfg["http_port"])
        if not self._warp_verified():
            now = time.time()
            if now - self._last_unverified_log >= 120:
                self._last_unverified_log = now
                self.log.add(
                    "WARP 出口尚未验证通过（未拿到出口 IP 或 WARP=off），暂不接管 zen-gate 出口",
                    "warn",
                )
            return
        if not port_open("127.0.0.1", int(self.cfg["http_port"])):
            self.log.add("WARP HTTP 代理未就绪，zen-gate 保持原出口", "warn")
            return
        try:
            settings = (self._admin_state() or {}).get("settings") or {}
            cur = (settings.get("proxyMode") or "env", settings.get("proxyURL") or "")
            if cur[0] == "custom" and cur[1].rstrip("/") == proxy_url:
                if not self._egress_patched:
                    self._prev_proxy = cur
                self.log.add("zen-gate 出口已是 WARP：%s" % proxy_url)
                return
            self._admin_post("settings", {"proxyMode": "custom", "proxyUrl": proxy_url})
            if not self._egress_patched:
                self._prev_proxy = cur
            self._egress_patched = True
            self.log.add("zen-gate 出口已切到 WARP：%s" % proxy_url)
        except Exception as e:
            self.log.add("切换 zen-gate 出口失败（换 IP 监控不受影响）：%s" % e, "warn")

    def _restore_egress(self):
        if not self._egress_patched:
            return
        mode, url = self._prev_proxy or ("env", "")
        try:
            payload = {"proxyMode": mode}
            if url:
                payload["proxyUrl"] = url
            self._admin_post("settings", payload)
            self.log.add("已还原 zen-gate 代理设置：%s %s" % (mode, url or ""))
        except Exception as e:
            self.log.add("还原 zen-gate 代理设置失败：%s" % e, "warn")
        self._egress_patched = False

    def retry_egress(self):
        """换 IP 成功后重试接管：WARP 之前没验证通过时，现在可能已经可用。"""
        if self._egress_patched or not self.cfg.get("zen_force_warp"):
            return
        self._install_egress()

    def _spawn(self):
        exe = self.binary()
        if not exe:
            return False, (
                "未找到 zen-gate（%s），跳过 OpenCode Zen 入口。"
                "可在设置里填写 zen-gate.exe 路径" % (to_rel_path(self.cfg.get("zen_path") or "") or "未配置")
            )

        proxy = "http://127.0.0.1:%d" % int(self.cfg["http_port"])
        env = os.environ.copy()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1,0.0.0.0"

        home = self.data_dir()
        try:
            os.makedirs(home, exist_ok=True)
            env["ZEN_GATE_HOME"] = home
        except Exception as e:
            return False, "创建 zen-gate 数据目录失败：%s" % e

        cmd = [exe, "-no-tray", "-port", str(self.port)]
        self.log.add("启动 zen-gate：%s（出口走 %s）" % (show_cmd(cmd), proxy))
        kwargs = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=APP_DIR,
            env=env,
        )
        if os.name == "nt":
            kwargs["creationflags"] = NO_WINDOW | NEW_GROUP
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kwargs["startupinfo"] = si
        else:
            kwargs["start_new_session"] = True

        try:
            self.proc = subprocess.Popen(cmd, **kwargs)
        except Exception as e:
            self.proc = None
            return False, "zen-gate 启动失败：%s" % e

        threading.Thread(target=self._drain, args=(self.proc,), daemon=True).start()

        if not wait_for_port("127.0.0.1", self.port, 25):
            self._kill_proc()
            return False, (
                "zen-gate 启动后 25 秒内未监听端口 %d，请检查端口是否被占用"
                "（控制台自身也在用 8787 时，请给 zen-gate 换一个端口）" % self.port
            )
        return True, "zen-gate 已启动，OpenCode Zen 入口：http://127.0.0.1:%d/v1" % self.port

    def _drain(self, proc):
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self.log.add("[zen-gate] %s" % line, "debug")
        except Exception:
            pass

    def _kill_proc(self):
        p = self.proc
        self.proc = None
        if not p:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=NO_WINDOW,
                )
            else:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except Exception:
                    p.terminate()
            p.wait(timeout=8)
        except Exception:
            try:
                if os.name != "nt":
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                else:
                    p.kill()
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        self._restore_egress()
        if self.owned:
            self._kill_proc()
        else:
            self.proc = None
        self.owned = False
        self.attached = False
        self._log_path = ""
        self._log_pos = None

    # -- 限额日志监控 ---------------------------------------------------- #

    def _today_log(self):
        name = time.strftime("%Y-%m-%d") + ".log"
        for d in self.log_dirs():
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
        return ""

    def _watch(self):
        while not self._stop.is_set():
            time.sleep(1.0)
            try:
                self._scan()
            except Exception:
                pass

    def _scan(self):
        path = self._today_log()
        if not path:
            return
        if path != self._log_path:
            # 第一次看到当天文件时从末尾开始，避免把启动前的历史 429 重放一遍。
            self._log_path = path
            try:
                self._log_pos = os.path.getsize(path)
            except OSError:
                self._log_pos = 0
            return

        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if self._log_pos is None or size < self._log_pos:
            self._log_pos = 0
        if size == self._log_pos:
            return

        # 用二进制读，避免文本模式 tell()/seek() 在 UTF-8 多字节字符上错位；
        # 只消费到最后一个换行，半行留到下次轮询。
        try:
            with open(path, "rb") as f:
                f.seek(self._log_pos)
                chunk = f.read()
        except OSError:
            return
        if not chunk:
            return
        end = chunk.rfind(b"\n") + 1
        if end <= 0:
            return
        self._log_pos += end
        for raw in chunk[:end].splitlines():
            self._on_line(raw.decode("utf-8", "replace"))

    def _on_line(self, line):
        if not ZEN_QUOTA_RE.search(line):
            return
        cat = ""
        m = re.search(r"\{([a-z]+)\}", line)
        if m:
            cat = m.group(1)
        # 只看请求 / 路由 / 上游三类的日志：{app} 里的公告、启动说明也会提到“限流”，
        # 不能当作额度耗尽的证据。
        if cat not in ("access", "routing", "upstream"):
            return
        if not self.cfg.get("auto_rotate"):
            return

        now = time.time()
        if now < self._cooldown_until:
            return
        if self.app.phase != "running" or self.app._rotating:
            return

        reason = line.strip()
        reason = reason[:200]
        self._cooldown_until = now + ZEN_ROTATE_COOLDOWN
        self.quota_hits += 1
        self.last_hit = time.strftime("%H:%M:%S")
        self.last_reason = reason
        self.log.add("OpenCode Zen 额度信号：%s" % reason, "warn")
        self.app.rotate("OpenCode Zen 免费额度耗尽，换出口 IP")


# --------------------------------------------------------------------------- #
# 出口 IP 探测
# --------------------------------------------------------------------------- #

def _parse_trace(text):
    data = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip()
    return data


def probe_via_proxy(port, timeout=12.0, verify=True):
    proxy = "http://127.0.0.1:%d" % int(port)
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})]
    if not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    opener = urllib.request.build_opener(*handlers)

    started = time.time()
    last_err = ""
    for url in TRACE_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "warp-control/1.0"})
            with opener.open(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
            data = _parse_trace(text)
            if data.get("ip"):
                return {
                    "ok": True,
                    "ip": data.get("ip", ""),
                    "loc": data.get("loc", ""),
                    "colo": data.get("colo", ""),
                    "warp": data.get("warp", ""),
                    "latency": int((time.time() - started) * 1000),
                }
            last_err = "返回内容缺少 IP"
        except Exception as e:
            last_err = str(e)
    return {
        "ok": False,
        "error": last_err or "探测失败",
        "latency": int((time.time() - started) * 1000),
    }


def probe_direct(timeout=8.0):
    try:
        req = urllib.request.Request(
            "https://www.cloudflare.com/cdn-cgi/trace",
            headers={"User-Agent": "warp-control/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _parse_trace(resp.read().decode("utf-8", "replace"))
        return {"ok": True, "ip": data.get("ip", ""), "loc": data.get("loc", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --------------------------------------------------------------------------- #
# 主应用
# --------------------------------------------------------------------------- #

class App:
    def __init__(self):
        self.cfg = load_config()
        self.log = Logger()
        self.lock = threading.RLock()
        self.proxy = None
        self.backend = None
        self.backend_name = ""
        self.zen = None
        self.phase = "stopped"

        self.egress_ip = ""
        self.loc = ""
        self.colo = ""
        self.warp = ""
        self.latency = None
        self.last_check = ""
        self.last_error = ""
        self.local_ip = ""
        self.consecutive_fail = 0
        self.last_ip = ""
        self.ip_since = 0.0
        self.rotate_count = 0
        self.started_at = 0.0
        self._rotating = False
        self._last_probe_ts = 0.0
        self._last_zen_egress_try = 0.0
        self._opencode_at = 0.0
        self._opencode_cache = {
            "running": False,
            "pids": [],
            "count": 0,
            "proxy_connected": False,
            "connections": 0,
        }
        self._opencode_auto_started = False
        self._opencode_auto_ts = 0.0
        self._opencode_auto_started = False
        self._opencode_auto_ts = 0.0
        self._tick = 0
        self._watch_path = ""
        self._watch_pos = None

        threading.Thread(target=self._probe_loop, daemon=True).start()
        threading.Thread(target=self._watch_loop, daemon=True).start()
        self.log.add("WARP Control 已启动，配置文件：%s" % CONFIG_REL)

    # -- 后端解析 -------------------------------------------------------- #

    def resolve_backend(self):
        cfg = self.cfg
        key = (cfg.get("backend") or "auto").lower()
        # 配置里存的是相对路径（或旧版遗留的绝对路径），一律按当前 APP_DIR 解析。
        path = app_path(cfg.get("backend_path") or "")

        if key == "custom":
            return CustomBackend(self, cfg.get("custom_command") or "")

        if key == "builtin":
            return BuiltinBackend(self)

        if key == "auto":
            detected = [d for d in detect_backends() if d["key"] != "builtin"]
            if not detected:
                self.log.add("未检测到本机 WARP 后端，改用内置后端（首次启动会自动下载）")
                return BuiltinBackend(self)
            key = detected[0]["key"]
            path = path or detected[0]["path"]
        elif not path:
            detected = {d["key"]: d["path"] for d in detect_backends()}
            path = detected.get(key, "")

        if key == "warp-cli":
            return WarpCliBackend(self, path) if path else None
        if key == "warp-plus":
            return WarpPlusBackend(self, path) if path else None
        if key == "usque":
            return UsqueBackend(self, path) if path else None
        return None

    # -- 启停 ------------------------------------------------------------ #

    def start(self):
        with self.lock:
            if self.phase in ("running", "starting", "rotating"):
                return {"ok": True, "message": "已在运行"}
            self.phase = "starting"
            self.last_error = ""
        threading.Thread(target=self._do_start, daemon=True).start()
        return {"ok": True, "message": "正在启动", "starting": True}

    def _do_start(self):
        try:
            backend = self.resolve_backend()
            if not backend:
                raise RuntimeError("未检测到可用的 WARP 后端，请在“设置”中指定程序路径，或改用自定义命令")

            self.backend = backend
            self.backend_name = backend.key
            self.log.add("使用后端：%s（%s）" % (
                backend.key, to_rel_path(getattr(backend, "path", "") or "") or "内置命令"))

            ok, msg = backend.start()
            if not ok:
                raise RuntimeError(msg)
            self.log.add(msg)

            with self.lock:
                if self.phase != "starting":
                    backend.stop()
                    return

            bridge = HttpProxyBridge(
                "127.0.0.1",
                int(self.cfg["http_port"]),
                "127.0.0.1",
                int(self.cfg["socks_port"]),
                self.log,
            )
            bridge.start()
            if not wait_for_port("127.0.0.1", int(self.cfg["http_port"]), 8):
                raise RuntimeError(
                    "HTTP 代理端口 %s 启动失败，可能被占用，请在设置中换一个端口" % self.cfg["http_port"]
                )
            self.proxy = bridge

            with self.lock:
                if self.phase != "starting":
                    bridge.stop()
                    self.proxy = None
                    backend.stop()
                    return
                self.started_at = time.time()
                self.consecutive_fail = 0
                self._opencode_auto_started = False
                self._opencode_auto_ts = 0.0
                self.phase = "running"
            self.log.add(
                "已就绪：SOCKS5 127.0.0.1:%s  ->  HTTP 127.0.0.1:%s"
                % (self.cfg["socks_port"], self.cfg["http_port"])
            )
            self.probe_once()
            if self.cfg.get("zen_enabled"):
                self._start_zen()
        except Exception as e:
            msg = str(e)
            with self.lock:
                self.phase = "error"
                self.last_error = msg
            self.log.add("启动失败：%s" % msg, "error")
            self._cleanup()

    def _start_zen(self):
        """WARP 就绪后拉起（或接管）zen-gate，作为 opencode 的 Zen 入口。"""
        with self.lock:
            if self.phase != "running":
                return
        zen = ZenGate(self)
        ok = zen.start()
        with self.lock:
            self.zen = zen
        if not ok and zen.running():
            # 启动失败但端口已通：可能另一个实例刚好起来了，仍保留监控。
            self.log.add("zen-gate 未由本程序拉起，但端口已监听，继续监控", "warn")
        try:
            if sync_opencode_key(self.cfg, zen):
                self.log.add("已同步 zen-gate 主密钥到 config/zen-gate.key（供 OpenCode 使用）")
        except Exception:
            pass

    def stop(self):
        with self.lock:
            if self.phase == "stopped":
                return {"ok": True, "message": "已是停止状态"}
            self.phase = "stopping"
        self._cleanup()
        with self.lock:
            self.phase = "stopped"
            self.started_at = 0.0
            self.latency = None
            self.consecutive_fail = 0
            self.last_error = ""
            self._opencode_auto_started = False
            self._opencode_auto_ts = 0.0
        self.log.add("已停止")
        return {"ok": True, "message": "已停止"}

    def _cleanup(self):
        if self.zen:
            try:
                self.zen.stop()
            except Exception:
                pass
            self.zen = None
        if self.proxy:
            self.proxy.stop()
            self.proxy = None
        if self.backend:
            try:
                self.backend.stop()
            except Exception:
                pass
            self.backend = None
            self.backend_name = ""

    def rotate(self, reason=""):
        with self.lock:
            if self._rotating or self.phase not in ("running", "starting"):
                return {"ok": False, "message": "当前状态不可换 IP"}
            self._rotating = True
            self.phase = "rotating"
        try:
            self.log.add("开始换 IP%s" % (("：" + reason) if reason else ""), "warn")
            if not self.backend:
                raise RuntimeError("后端未初始化")
            if self.backend.key == "warp-cli":
                self.backend.stop()
                time.sleep(1.5)
                ok, msg = self.backend.start()
            else:
                ok, msg = self.backend.restart()
            if not ok:
                raise RuntimeError(msg)
            self.log.add(msg)
            self.last_ip = ""
            self.ip_since = 0.0
            with self.lock:
                self.phase = "running"
            self.rotate_count += 1
            time.sleep(0.8)
            self.probe_once()
            zen = self.zen
            if zen is not None:
                try:
                    zen.retry_egress()
                except Exception:
                    pass
            return {"ok": True, "message": "换 IP 完成"}
        except Exception as e:
            with self.lock:
                self.phase = "error"
                self.last_error = str(e)
            self.log.add("换 IP 失败：%s" % e, "error")
            return {"ok": False, "message": str(e)}
        finally:
            self._rotating = False

    # -- 探测 ------------------------------------------------------------ #

    def probe_once(self):
        res = probe_via_proxy(
            int(self.cfg["http_port"]),
            timeout=float(self.cfg["probe_timeout"]),
            verify=bool(self.cfg["verify_ssl"]),
        )
        now = time.strftime("%H:%M:%S")
        with self.lock:
            self.last_check = now
            if res["ok"]:
                self.consecutive_fail = 0
                self.egress_ip = res["ip"]
                self.loc = res.get("loc", "")
                self.colo = res.get("colo", "")
                self.warp = res.get("warp", "")
                self.latency = res.get("latency")
                self.last_error = ""
                if res["ip"] != self.last_ip:
                    if self.last_ip:
                        self.log.add("出口 IP 变更：%s -> %s" % (self.last_ip, res["ip"]))
                    else:
                        self.log.add(
                            "当前出口 IP：%s（%s / %s，WARP=%s）"
                            % (res["ip"], res.get("loc", "?"), res.get("colo", "?"), res.get("warp", "?"))
                        )
                    self.last_ip = res["ip"]
                    self.ip_since = time.time()
            else:
                self.consecutive_fail += 1
                self.latency = None
                self.last_error = res.get("error", "")
                self.log.add(
                    "探测失败（连续 %d 次）：%s" % (self.consecutive_fail, self.last_error),
                    "warn" if self.consecutive_fail < 3 else "error",
                )
        return res

    def _probe_loop(self):
        while True:
            time.sleep(1)
            try:
                with self.lock:
                    if self.phase != "running":
                        continue
                    interval = max(5, int(self.cfg.get("probe_interval") or 20))
                if time.time() - self._last_probe_ts < interval:
                    continue
                self._last_probe_ts = time.time()
                self.probe_once()
                self._retry_zen_egress()
                self._tick += 1
                if self._tick % 5 == 1:
                    threading.Thread(target=self._probe_local, daemon=True).start()
                self._maybe_auto_rotate()
            except Exception:
                self.log.add("探测线程异常：%s" % traceback.format_exc(limit=2), "error")

    def _probe_local(self):
        res = probe_direct(timeout=8)
        if res.get("ok") and res.get("ip"):
            with self.lock:
                self.local_ip = res["ip"]

    def _retry_zen_egress(self):
        """WARP 验证通过后，尽快把 zen-gate 的出口切过来。

        启动时 WARP 可能还没完成握手，探测成功后这里会自动补一次接管。
        """
        zen = self.zen
        if zen is None or not self.cfg.get("zen_force_warp"):
            return
        if not self.egress_ip or (self.warp or "").lower() != "on":
            return
        now = time.time()
        if now - self._last_zen_egress_try < 15:
            return
        self._last_zen_egress_try = now
        try:
            zen.retry_egress()
        except Exception:
            pass
        self._maybe_auto_start_opencode()

    def _maybe_auto_start_opencode(self):
        """WARP 验证通过后，按配置自动通过 start-opencode.bat 启动 OpenCode。"""
        if not self.cfg.get("auto_start_opencode"):
            return
        if not self.egress_ip or (self.warp or "").lower() != "on":
            return
        if self._opencode_auto_started:
            return
        # Give zen-gate a moment to settle; avoid racing with its own startup.
        if time.time() - self.started_at < 3:
            return
        http_port = int(self.cfg.get("http_port") or 7891)
        try:
            running = opencode_status(http_port).get("running")
        except Exception:
            running = False
        # If opencode is already running, treat as satisfied to avoid killing user session.
        if running:
            self._opencode_auto_started = True
            self.log.add("检测到 OpenCode 已在运行，跳过自动启动")
            return
        now = time.time()
        if now - self._opencode_auto_ts < 30:
            return
        self._opencode_auto_ts = now
        self.log.add("WARP 已验证通过，自动启动 OpenCode（start-opencode.bat）…")
        res = start_opencode_process(self.log, http_port)
        if res.get("ok"):
            self._opencode_auto_started = True
        else:
            self.log.add("自动启动 OpenCode 未成功：%s" % (res.get("message") or "未知错误"), "warn")

    def _maybe_auto_rotate(self):
        cfg = self.cfg
        if not cfg.get("auto_rotate"):
            return
        fail_limit = max(1, int(cfg.get("rotate_after_failures") or 3))
        minutes = int(cfg.get("rotate_after_minutes") or 0)

        reason = ""
        if self.consecutive_fail >= fail_limit:
            reason = "连续 %d 次探测失败" % self.consecutive_fail
        elif minutes and self.last_ip and (time.time() - self.ip_since) >= minutes * 60:
            reason = "出口 IP 已保持 %d 分钟未变" % minutes

        if reason:
            self.rotate(reason)

    # -- 日志关键词监控 -------------------------------------------------- #

    def _watch_loop(self):
        while True:
            time.sleep(1.5)
            try:
                self._watch_once()
            except Exception:
                pass

    def _watch_once(self):
        rel = to_rel_path(self.cfg.get("watch_log") or "")
        path = app_path(rel)
        pattern = (self.cfg.get("watch_pattern") or "").strip()
        if not rel or not pattern or not os.path.isfile(path):
            self._watch_path = ""
            self._watch_pos = None
            return

        if rel != self._watch_path:
            self._watch_path = rel
            self._watch_pos = os.path.getsize(path)
            self.log.add("开始监控日志：%s" % rel)

        size = os.path.getsize(path)
        if self._watch_pos is None or size < self._watch_pos:
            self._watch_pos = 0

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self._watch_pos)
            hit = ""
            for line in f:
                if re.search(pattern, line, re.I):
                    hit = line.strip()[:200]
                    break
            self._watch_pos = f.tell()

        if hit:
            self.log.add("日志命中封禁关键词：%s" % hit, "warn")
            if self.cfg.get("auto_rotate") and self.phase == "running":
                self.rotate("日志命中封禁关键词")

    def reset_watch(self):
        self._watch_path = ""
        self._watch_pos = None

    def _opencode_snapshot(self, http_port):
        now = time.time()
        if now - self._opencode_at < 2.0:
            return dict(self._opencode_cache)
        try:
            data = opencode_status(http_port)
        except Exception:
            data = dict(self._opencode_cache)
        self._opencode_at = now
        self._opencode_cache = data
        return dict(data)

    # -- 状态 ------------------------------------------------------------ #

    def snapshot(self):
        cfg = self.cfg
        with self.lock:
            socks_port = int(cfg.get("socks_port") or 40000)
            http_port = int(cfg.get("http_port") or 7891)
            now = time.time()
            zen = self.zen.status() if self.zen else {
                "enabled": bool(cfg.get("zen_enabled")),
                "port": int(cfg.get("zen_port") or 8788),
                "running": False,
                "owned": False,
                "attached": False,
                "egress": "native",
                "quota_hits": 0,
                "last_hit": "",
                "last_reason": "",
                "data_dir": zen_default_home(),
            }
            opencode = self._opencode_snapshot(http_port)
            return {
                "phase": self.phase,
                "busy": self.phase in ("starting", "stopping", "rotating"),
                "running": self.phase == "running",
                "backend": self.backend_name,
                "socks_port": socks_port,
                "http_port": http_port,
                "socks_up": port_open("127.0.0.1", socks_port),
                "http_up": port_open("127.0.0.1", http_port),
                "egress_ip": self.egress_ip,
                "loc": self.loc,
                "colo": self.colo,
                "warp": self.warp,
                "latency": self.latency,
                "last_check": self.last_check,
                "last_error": self.last_error,
                "local_ip": self.local_ip,
                "uptime": fmt_dur(now - self.started_at) if self.started_at else "",
                "rotate_count": self.rotate_count,
                "consecutive_fail": self.consecutive_fail,
                "auto_rotate": bool(cfg.get("auto_rotate")),
                "ip_age": int(now - self.ip_since) if self.ip_since else 0,
                "opencode": opencode,
                "zen": zen,
            }


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #

APP = None


class Handler(BaseHTTPRequestHandler):
    server_version = "WarpControl/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        try:
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path
            query = urllib.parse.parse_qs(parts.query)

            if path in ("/", "/index.html"):
                self._send(200, HTML_PAGE, "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if path.startswith("/asset/"):
                name = os.path.basename(urllib.parse.unquote(path[len("/asset/"):]))
                if not name or name.startswith("."):
                    self._json({"error": "not found"}, 404)
                    return
                full = os.path.join(APP_DIR, "asset", name)
                if not os.path.isfile(full):
                    self._json({"error": "not found"}, 404)
                    return
                ext = os.path.splitext(name)[1].lower()
                ctype = {
                    ".png": "image/png",
                    ".jpg": "image/jpeg",
                    ".jpeg": "image/jpeg",
                    ".gif": "image/gif",
                    ".webp": "image/webp",
                    ".svg": "image/svg+xml",
                    ".ico": "image/x-icon",
                }.get(ext, "application/octet-stream")
                with open(full, "rb") as f:
                    self._send(200, f.read(), ctype)
                return
            if path == "/api/status":
                self._json(APP.snapshot())
                return
            if path == "/api/config":
                self._json({"config": APP.cfg, "config_path": CONFIG_REL})
                return
            if path == "/api/logs":
                try:
                    since = int((query.get("since") or ["0"])[0])
                except ValueError:
                    since = 0
                self._json({"items": APP.log.since(since)})
                return
            if path == "/api/builtin":
                dest = builtin_binary_path()
                self._json({
                    "exists": os.path.isfile(dest),
                    "path": dest,
                    "size": os.path.getsize(dest) if os.path.isfile(dest) else 0,
                    "platform": platform_asset_key(),
                })
                return
            if path == "/api/detect":
                self._json({"backends": detect_backends(include_builtin=True)})
                return
            if path == "/api/zen/models":
                self._json(zen_models_snapshot(APP.cfg, getattr(APP, "zen", None)))
                return
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path
            body = self._read_json()

            if path == "/api/start":
                self._json(APP.start())
                return
            if path == "/api/stop":
                self._json(APP.stop())
                return
            if path == "/api/rotate":
                self._json(APP.rotate("手动触发"))
                return
            if path == "/api/probe":
                res = APP.probe_once()
                self._json({"ok": res["ok"], "probe": res, "status": APP.snapshot()})
                return
            if path == "/api/detect":
                self._json({"backends": detect_backends(include_builtin=True)})
                return
            if path == "/api/builtin-download":
                if getattr(APP, "_downloading", False):
                    self._json({"ok": True, "message": "正在下载中，请查看日志"})
                    return
                APP._downloading = True

                def _job():
                    try:
                        ok, res = download_builtin(APP.log, APP.cfg.get("builtin_mirror") or "")
                        APP.log.add(
                            "内置后端下载完成" if ok else ("内置后端下载失败：%s" % res),
                            "info" if ok else "error",
                        )
                    finally:
                        APP._downloading = False

                threading.Thread(target=_job, daemon=True).start()
                self._json({"ok": True, "message": "已开始下载，请到“日志”页查看进度"})
                return
            if path == "/api/config":
                updated = self._apply_config(body)
                self._json(updated)
                return
            if path == "/api/zen/test":
                model = (body.get("model") or "").strip()
                prompt = body.get("prompt") or "Reply with exactly: OK"
                max_tokens = body.get("max_tokens") or 64
                self._json(zen_test_model(APP.cfg, getattr(APP, "zen", None), model, prompt, max_tokens))
                return
            if path == "/api/zen/reprobe":
                self._json(zen_reprobe(APP.cfg, getattr(APP, "zen", None)))
                return
            if path == "/api/opencode/start":
                http_port = int(APP.cfg.get("http_port") or 7891)
                self._json(start_opencode_process(APP.log, http_port))
                return
            if path == "/api/clear-logs":
                self._json({"ok": True})
                return
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _apply_config(self, body):
        allowed = list(DEFAULT_CONFIG.keys())
        old_ports = (APP.cfg.get("socks_port"), APP.cfg.get("http_port"))
        old_zen = (APP.cfg.get("zen_enabled"), APP.cfg.get("zen_path"),
                   APP.cfg.get("zen_port"), APP.cfg.get("zen_data_dir"))
        for k in allowed:
            if k not in body:
                continue
            v = body[k]
            default = DEFAULT_CONFIG[k]
            try:
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v)
                elif isinstance(default, str):
                    v = str(v).strip()
            except (TypeError, ValueError):
                continue
            APP.cfg[k] = v

        if APP.cfg["socks_port"] == APP.cfg["http_port"]:
            APP.cfg["http_port"] = int(APP.cfg["socks_port"]) + 1

        save_config(APP.cfg)
        APP.reset_watch()
        APP.log.add("配置已保存")

        new_ports = (APP.cfg.get("socks_port"), APP.cfg.get("http_port"))
        new_zen = (APP.cfg.get("zen_enabled"), APP.cfg.get("zen_path"),
                   APP.cfg.get("zen_port"), APP.cfg.get("zen_data_dir"))
        msg = "配置已保存"
        if old_ports != new_ports and APP.phase != "stopped":
            msg = "配置已保存，端口变更需重启后生效"
            APP.log.add(msg, "warn")
        elif old_zen != new_zen and APP.phase != "stopped":
            msg = "配置已保存，zen-gate 变更需重启 WARP Control 后生效"
            APP.log.add(msg, "warn")
        return {"ok": True, "message": msg, "config": APP.cfg}


# --------------------------------------------------------------------------- #
# 前端页面
# --------------------------------------------------------------------------- #

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WARP Control - opencode 出口 IP 管家</title>
<style>
  :root{
    --bg:#070a11; --panel:#0e141f; --panel2:#121a27; --line:#1d2838;
    --txt:#e6edf7; --dim:#8492a6; --accent:#22d3ee; --accent2:#3b82f6;
    --ok:#34d399; --warn:#fbbf24; --err:#f87171; --mono:ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    background:
      radial-gradient(1200px 600px at 10% -10%, rgba(59,130,246,.16), transparent 60%),
      radial-gradient(900px 500px at 100% 0%, rgba(34,211,238,.12), transparent 55%),
      var(--bg);
    color:var(--txt);font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
    min-height:100vh;padding-bottom:48px;
  }
  .wrap{max-width:1180px;margin:0 auto;padding:22px 20px}
  header{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
  .brand{display:flex;align-items:center;gap:10px}
  .logo{width:34px;height:34px;border-radius:10px;display:grid;place-items:center;
    background:linear-gradient(135deg,var(--accent2),var(--accent));color:#04121a;font-weight:800;font-size:15px}
  h1{font-size:19px;font-weight:650;letter-spacing:.3px}
  .sub{color:var(--dim);font-size:12.5px;margin-left:2px}
  .actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
  .pill{font-size:12px;padding:5px 11px;border-radius:999px;border:1px solid var(--line);
    background:#0b111b;color:var(--dim);display:inline-flex;align-items:center;gap:6px}
  .pill::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--dim)}
  .pill.running{color:#b8f5e2;border-color:rgba(52,211,153,.35);background:rgba(52,211,153,.08)}
  .pill.running::before{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  .pill.starting,.pill.stopping,.pill.rotating{color:#ffe6a8;border-color:rgba(251,191,36,.35);background:rgba(251,191,36,.08)}
  .pill.starting::before,.pill.stopping::before,.pill.rotating::before{background:var(--warn);animation:blink 1s infinite}
  .pill.error{color:#ffc9c9;border-color:rgba(248,113,113,.4);background:rgba(248,113,113,.08)}
  .pill.error::before{background:var(--err)}
  @keyframes blink{50%{opacity:.25}}
  .btn{border:1px solid var(--line);background:#131c28;color:var(--txt);padding:8px 15px;border-radius:9px;
    font-size:13px;cursor:pointer;transition:.15s;font-family:inherit}
  .btn:hover:not(:disabled){border-color:#2f4056;background:#18222f;transform:translateY(-1px)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .btn.primary{background:linear-gradient(135deg,var(--accent2),#0ea5c6);border:none;color:#04121a;font-weight:650}
  .btn.ghost{background:transparent}
  .tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin-bottom:18px;overflow-x:auto}
  .tab{background:none;border:none;color:var(--dim);padding:10px 15px;font-size:13.5px;cursor:pointer;
    border-bottom:2px solid transparent;font-family:inherit;white-space:nowrap}
  .tab:hover{color:var(--txt)}
  .tab.active{color:var(--accent);border-bottom-color:var(--accent)}
  .page{display:none}
  .page.active{display:block;animation:fade .22s ease}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

  .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:12px;margin-bottom:16px}
  .stat{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);
    border-radius:13px;padding:14px 15px;position:relative;overflow:hidden}
  .stat .label{font-size:11.5px;color:var(--dim);letter-spacing:.4px;margin-bottom:7px}
  .stat .value{font-size:19px;font-weight:600;font-family:var(--mono);word-break:break-all;line-height:1.25}
  .stat .hint{font-size:11px;color:var(--dim);margin-top:5px}
  .stat.accent::after{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:linear-gradient(var(--accent),var(--accent2))}
  .green{color:var(--ok)} .yellow{color:var(--warn)} .red{color:var(--err)} .cyan{color:var(--accent)}

  .cols{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media(max-width:860px){.cols{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:17px}
  .card h3{font-size:13.5px;font-weight:600;margin-bottom:13px;display:flex;align-items:center;gap:7px}
  .card h3 span.tag{font-size:10.5px;color:var(--dim);border:1px solid var(--line);border-radius:5px;padding:2px 6px;font-weight:400}
  .kv{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 0;border-bottom:1px dashed #182232;font-size:13px}
  .kv:last-child{border-bottom:none}
  .kv .k{color:var(--dim);font-size:12.5px}
  .kv .v{font-family:var(--mono);font-size:12.5px;display:flex;align-items:center;gap:8px}
  .copy{border:1px solid var(--line);background:#0d141e;color:var(--dim);border-radius:6px;
    font-size:11px;padding:3px 8px;cursor:pointer;font-family:inherit}
  .copy:hover{color:var(--accent);border-color:rgba(34,211,238,.4)}
  pre.code{background:#05080e;border:1px solid var(--line);border-radius:10px;padding:13px;
    font-family:var(--mono);font-size:12px;line-height:1.65;color:#b9c7db;overflow:auto;white-space:pre}
  .code-head{display:flex;justify-content:space-between;align-items:center;margin:12px 0 7px;font-size:11.5px;color:var(--dim)}
  .code-head:first-child{margin-top:0}

  .form{display:grid;grid-template-columns:1fr 1fr;gap:14px}
  @media(max-width:760px){.form{grid-template-columns:1fr}}
  .field{display:flex;flex-direction:column;gap:6px}
  .field.full{grid-column:1/-1}
  .field label{font-size:12.5px;color:var(--dim)}
  .field input[type=text],.field input[type=number],.field select,.field input[type=password]{
    background:#0a1119;border:1px solid var(--line);border-radius:8px;padding:9px 11px;color:var(--txt);
    font-size:13px;font-family:inherit;outline:none;transition:.15s;width:100%}
  .field input:focus,.field select:focus{border-color:var(--accent2);box-shadow:0 0 0 3px rgba(59,130,246,.12)}
  .field .tip{font-size:11px;color:#61708a;line-height:1.5}
  .check{display:flex;align-items:center;gap:9px;font-size:13px;color:var(--txt);padding-top:4px;cursor:pointer}
  .check input{width:16px;height:16px;accent-color:var(--accent2);cursor:pointer}
  .row-actions{display:flex;gap:10px;margin-top:18px;flex-wrap:wrap}
  .detected{margin-top:10px;font-size:12.5px;color:var(--dim);line-height:1.9}
  .detected b{color:var(--accent)}

  #logBox{background:#04070c;border:1px solid var(--line);border-radius:12px;padding:12px;
    height:min(62vh,620px);overflow:auto;font-family:var(--mono);font-size:12px;line-height:1.7}
  .log-line{display:flex;gap:9px;white-space:pre-wrap;word-break:break-word}
  .log-line .t{color:#4b5b71;flex:0 0 auto}
  .log-line .m{flex:1}
  .log-line.debug .m{color:#6b7b91}
  .log-line.info .m{color:#c3d0e2}
  .log-line.warn .m{color:var(--warn)}
  .log-line.error .m{color:var(--err)}
  .log-toolbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
  .muted{color:var(--dim);font-size:12.5px}
  .help{font-size:13.5px;line-height:1.85;color:#b6c3d6}
  .help h3{color:var(--txt);font-size:14px;margin:20px 0 8px}
  .help h3:first-child{margin-top:0}
  .help code{background:#0a1119;border:1px solid var(--line);border-radius:5px;padding:1px 6px;
    font-family:var(--mono);font-size:12px;color:var(--accent)}
  .help ul{padding-left:20px}
  .help li{margin-bottom:5px}
  .toast{position:fixed;left:50%;bottom:26px;transform:translate(-50%,20px);background:#152030;
    border:1px solid #26364b;color:var(--txt);padding:11px 20px;border-radius:10px;font-size:13px;
    opacity:0;pointer-events:none;transition:.25s;z-index:99;box-shadow:0 12px 32px rgba(0,0,0,.45)}
  .toast.show{opacity:1;transform:translate(-50%,0)}
  .banner{margin-bottom:14px;padding:11px 14px;border-radius:10px;font-size:12.5px;display:none;
    border:1px solid rgba(248,113,113,.35);background:rgba(248,113,113,.08);color:#ffc9c9}
  .banner.show{display:block}
  .banner.info{border-color:rgba(251,191,36,.35);background:rgba(251,191,36,.07);color:#ffe6a8}
  .mini{margin-top:6px}
  .mini .log-line{font-size:11.8px}
  .model-table{width:100%;border-collapse:collapse;font-size:12.5px}
  .model-table th,.model-table td{padding:9px 8px;border-bottom:1px solid #182232;text-align:left;vertical-align:top}
  .model-table th{color:var(--dim);font-size:11.5px;font-weight:600;white-space:nowrap}
  .model-table td{color:#c8d4e6}
  .model-table .mono{font-family:var(--mono);font-size:11.5px;word-break:break-all}
  .badge{display:inline-block;padding:2px 7px;border-radius:999px;font-size:10.5px;border:1px solid var(--line);background:#0b111b;color:var(--dim);white-space:nowrap}
  .badge.ok{color:#b8f5e2;border-color:rgba(52,211,153,.35);background:rgba(52,211,153,.08)}
  .badge.warn{color:#ffe6a8;border-color:rgba(251,191,36,.35);background:rgba(251,191,36,.08)}
  .badge.err{color:#ffc9c9;border-color:rgba(248,113,113,.35);background:rgba(248,113,113,.08)}
  .btn.sm{padding:5px 10px;font-size:11.5px;border-radius:7px}
  .modal-mask{position:fixed;inset:0;background:rgba(3,7,12,.72);display:none;align-items:center;
    justify-content:center;z-index:120;padding:20px}
  .modal-mask.show{display:flex}
  .modal{background:#0c1420;border:1px solid var(--line);border-radius:14px;max-width:520px;width:100%;
    box-shadow:0 24px 60px rgba(0,0,0,.55);overflow:hidden}
  .modal-head{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;
    border-bottom:1px solid var(--line)}
  .modal-head h3{margin:0;font-size:15px;color:var(--txt)}
  .modal-x{background:none;border:0;color:var(--dim);font-size:22px;line-height:1;cursor:pointer;padding:0 4px}
  .modal-x:hover{color:var(--txt)}
  .modal-body{padding:16px}
  .modal-foot{display:flex;justify-content:flex-end;padding:0 16px 16px}
  .tip-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px}
  .tip-grid figure{margin:0;background:#070c13;border:1px solid var(--line);border-radius:10px;
    padding:10px;text-align:center}
  .tip-grid img{width:100%;height:auto;border-radius:6px;display:block;background:#fff}
  .tip-grid figcaption{margin-top:8px;font-size:11.5px;color:var(--dim)}
  @media(max-width:520px){.tip-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <div class="logo">W</div>
      <div>
        <h1>WARP Control</h1>
        <div class="sub">opencode 出口 IP 管家 · 本地 SOCKS5 → HTTP 代理</div>
      </div>
    </div>
    <div class="actions">
      <span class="pill" id="phasePill">已停止</span>
      <button class="btn primary" id="btnStart">启动</button>
      <button class="btn" id="btnStop">停止</button>
      <button class="btn ghost" id="btnRotate">立即换 IP</button>
      <button class="btn ghost" id="btnStartOpencode">启动 OpenCode</button>
      <button class="btn ghost" id="btnTip">打赏作者</button>
    </div>
  </header>

  <nav class="tabs">
    <button class="tab active" data-tab="dash">仪表盘</button>
    <button class="tab" data-tab="models">模型</button>
    <button class="tab" data-tab="cfg">设置</button>
    <button class="tab" data-tab="log">日志</button>
    <button class="tab" data-tab="help">帮助</button>
  </nav>

  <!-- 仪表盘 -->
  <section class="page active" id="page-dash">
    <div class="banner" id="errBanner"></div>
    <div class="stats">
      <div class="stat accent">
        <div class="label">WARP 出口 IP</div>
        <div class="value" id="sIp">—</div>
        <div class="hint" id="sIpHint">尚未探测</div>
      </div>
      <div class="stat">
        <div class="label">归属地 / 节点</div>
        <div class="value" id="sLoc">—</div>
        <div class="hint" id="sWarp">WARP 状态未知</div>
      </div>
      <div class="stat">
        <div class="label">代理延迟</div>
        <div class="value" id="sLatency">—</div>
        <div class="hint" id="sCheck">最近检测 —</div>
      </div>
      <div class="stat">
        <div class="label">后端</div>
        <div class="value" id="sBackend">—</div>
        <div class="hint" id="sUptime">运行时长 —</div>
      </div>
      <div class="stat">
        <div class="label">SOCKS5 端口</div>
        <div class="value" id="sSocks">—</div>
        <div class="hint" id="sSocksState">—</div>
      </div>
      <div class="stat">
        <div class="label">HTTP 端口</div>
        <div class="value" id="sHttp">—</div>
        <div class="hint" id="sHttpState">—</div>
      </div>
      <div class="stat">
        <div class="label">OpenCode 状态</div>
        <div class="value" id="sOpencode">—</div>
        <div class="hint" id="sOpencodeHint">start-opencode.bat</div>
      </div>
    </div>

    <div class="cols">
      <div class="card">
        <h3>代理地址 <span class="tag">仅监听 127.0.0.1</span></h3>
        <div class="kv">
          <span class="k">HTTP 代理（opencode 用）</span>
          <span class="v"><span id="vHttp">—</span><button class="copy" data-copy="vHttp">复制</button></span>
        </div>
        <div class="kv">
          <span class="k">SOCKS5 代理</span>
          <span class="v"><span id="vSocks">—</span><button class="copy" data-copy="vSocks">复制</button></span>
        </div>
        <div class="kv">
          <span class="k">本机直连出口 IP</span>
          <span class="v" id="vLocal">—</span>
        </div>
        <div class="kv">
          <span class="k">当前 IP 已保持</span>
          <span class="v" id="vIpAge">—</span>
        </div>
        <div class="kv">
          <span class="k">OpenCode 状态</span>
          <span class="v" id="vOpencode">—</span>
        </div>
        <div class="kv">
          <span class="k">自动换 IP</span>
          <span class="v" id="vAuto">—</span>
        </div>
        <div class="kv">
          <span class="k">OpenCode Zen 入口</span>
          <span class="v" id="vZen">—</span>
        </div>
        <div class="kv">
          <span class="k">Zen 限额触发换 IP</span>
          <span class="v" id="vZenHits">—</span>
        </div>
        <div class="kv">
          <span class="k">累计换 IP 次数</span>
          <span class="v" id="vRotate">0</span>
        </div>
      </div>

      <div class="card">
        <h3>opencode 接入 <span class="tag">复制后重启终端</span></h3>
        <div class="code-head"><span>Windows CMD</span><button class="copy" data-copy="cmdBlock">复制</button></div>
        <pre class="code" id="cmdBlock"></pre>
        <div class="code-head"><span>PowerShell</span><button class="copy" data-copy="psBlock">复制</button></div>
        <pre class="code" id="psBlock"></pre>
        <div class="tip muted" style="margin-top:10px;line-height:1.7">
          提示：本程序把 SOCKS5 桥接成标准 HTTP 代理，Node/opencode 无需额外支持 socks5。
          如果 opencode 仍走直连，请确认是在设置了环境变量的同一个终端里启动。
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>组件下载 <span class="tag">GitHub · 加速</span></h3>
      <div class="tip muted" style="margin-bottom:10px;line-height:1.7">
        加速链接使用 <code>https://xiake.pro/github/?_360safeparam=503890531</code> 作为 GitHub 加速入口（将目标 GitHub 地址作为 <code>url</code> 参数传入）。若加速入口参数格式变化，可直接使用 GitHub 原始/发布页链接。
      </div>
      <div style="overflow:auto">
        <table class="model-table">
          <thead><tr><th>组件</th><th>仓库</th><th>发布页</th><th>加速打开</th></tr></thead>
          <tbody id="dlBody"></tbody>
        </table>
      </div>
    </div>

    <div class="card mini" style="margin-top:14px">
      <h3>最近事件</h3>
      <div id="miniLog"></div>
    </div>
  </section>

  <!-- 模型 -->
  <section class="page" id="page-models">
    <div class="log-toolbar">
      <span class="muted" id="modelsHint">从 zen-gate 读取模型列表、统计与测试结果</span>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input type="text" id="modelSearch" placeholder="搜索模型 ID / 名称 / 渠道" style="background:#0a1119;border:1px solid var(--line);border-radius:8px;padding:7px 10px;color:var(--txt);font-size:12.5px;min-width:220px;outline:none">
        <button class="btn ghost" id="btnModelsRefresh">刷新</button>
        <button class="btn ghost" id="btnModelsReprobe" title="让 zen-gate 重新探测各模型可用性（约需数十秒）">重新探测</button>
      </div>
    </div>
    <div class="banner" id="modelsErr"></div>
    <div class="banner info" id="modelsNote"></div>
    <div class="stats" id="modelsStats" style="margin-bottom:14px"></div>
    <div class="cols" style="margin-bottom:14px">
      <div class="card">
        <h3>模型测试 <span class="tag">经 zen-gate /v1</span></h3>
        <div class="form" style="grid-template-columns:1fr">
          <div class="field">
            <label>模型</label>
            <input type="text" id="testModel" list="modelList" placeholder="例如 zen/glm-5.3-flash-free 或 beam-free">
            <datalist id="modelList"></datalist>
          </div>
          <div class="field">
            <label>测试 Prompt</label>
            <textarea id="testPrompt" rows="3" style="background:#0a1119;border:1px solid var(--line);border-radius:8px;padding:9px 11px;color:var(--txt);font-size:13px;font-family:inherit;outline:none;resize:vertical">Reply with exactly: OK</textarea>
          </div>
          <div class="field">
            <label>max_tokens</label>
            <input type="number" id="testMaxTokens" min="1" max="512" value="64">
          </div>
        </div>
        <div class="row-actions"><button class="btn primary" id="btnModelTest">发送测试</button></div>
        <pre class="code" id="modelTestOut" style="margin-top:12px;min-height:92px;white-space:pre-wrap">等待测试…</pre>
      </div>
      <div class="card">
        <h3>统计概览 <span class="tag">zen-gate stats</span></h3>
        <div id="statsDetail" class="muted" style="font-size:12.5px;line-height:1.8">加载后显示今日/累计请求、失败、token 与最近错误。</div>
      </div>
    </div>
    <div class="card">
      <h3>模型列表 <span class="tag" id="modelsCount">0</span></h3>
      <div style="overflow:auto">
        <table class="model-table" id="modelsTable">
          <thead><tr>
            <th>模型</th><th>状态</th><th>渠道</th><th>能力</th><th>上下文</th><th>探测</th><th>操作</th>
          </tr></thead>
          <tbody id="modelsBody"><tr><td colspan="7" class="muted">点击“刷新”加载</td></tr></tbody>
        </table>
      </div>
    </div>
  </section>

  <!-- 设置 -->
  <section class="page" id="page-cfg">
    <div class="card">
      <h3>WARP 后端</h3>
      <div class="form">
        <div class="field">
          <label>后端选择</label>
          <select id="fBackend">
            <option value="auto">自动检测（推荐，无本地 WARP 时用内置）</option>
            <option value="builtin">内置 warp-plus（免安装，首次自动下载）</option>
            <option value="warp-cli">Cloudflare WARP 官方客户端（warp-cli）</option>
            <option value="warp-plus">本机 warp-plus</option>
            <option value="usque">usque</option>
            <option value="custom">自定义命令</option>
          </select>
          <div class="tip">自动模式：优先本机 warp-cli / warp-plus / usque，都没有则使用内置后端并自动下载。</div>
        </div>
        <div class="field">
          <label>程序路径（留空则自动查找）</label>
          <input type="text" id="fPath" placeholder="例如 bin\usque.exe">
          <div class="tip">相对项目根目录；目录改名或移动后仍有效，不要填绝对路径。</div>
        </div>
        <div class="field full">
          <label>自定义命令（可选，{port} 会替换为 SOCKS5 端口）</label>
          <input type="text" id="fCustom" placeholder="例如 warp-plus.exe -b 127.0.0.1:{port} --gool">
          <div class="tip">选择“自定义命令”后端时必填；支持任意能提供本地 SOCKS5 的程序。</div>
        </div>
        <div class="field full">
          <label>warp-plus 额外参数（warp-plus / 内置后端生效）</label>
          <input type="text" id="fPlusArgs" placeholder="例如 --gool --scan">
        </div>
        <div class="field full">
          <label>内置后端下载镜像（可选）</label>
          <input type="text" id="fMirror" placeholder="例如 https://ghfast.top">
          <div class="tip">GitHub 访问不畅时填写镜像前缀；留空则直连官方地址。</div>
        </div>
      </div>
      <div class="row-actions">
        <button class="btn" id="btnDetect">检测本机后端</button>
        <button class="btn" id="btnBuiltin">下载 / 更新内置后端</button>
        <span class="muted" id="detectInfo"></span>
      </div>
      <div class="detected" id="detectList"></div>
    </div>

    <div class="card" style="margin-top:14px">
      <h3>端口与探测</h3>
      <div class="form">
        <div class="field">
          <label>上游 SOCKS5 端口</label>
          <input type="number" id="fSocks" min="1" max="65535">
          <div class="tip">warp-cli 代理模式默认 40000。</div>
        </div>
        <div class="field">
          <label>本地 HTTP 代理端口</label>
          <input type="number" id="fHttp" min="1" max="65535">
          <div class="tip">opencode 使用的 HTTP_PROXY 端口，默认 7891。</div>
        </div>
        <div class="field">
          <label>探测间隔（秒）</label>
          <input type="number" id="fInterval" min="5" max="3600">
        </div>
        <div class="field">
          <label>探测超时（秒）</label>
          <input type="number" id="fTimeout" min="3" max="120">
        </div>
        <div class="field">
          <label class="check"><input type="checkbox" id="fVerify"> 校验证书（关闭仅在证书异常时使用）</label>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <h3>自动换 IP</h3>
      <div class="form">
        <div class="field full">
          <label class="check"><input type="checkbox" id="fAuto"> 启用自动换 IP</label>
        </div>
        <div class="field">
          <label>连续探测失败多少次后换 IP</label>
          <input type="number" id="fFail" min="1" max="50">
          <div class="tip">代理不通 / 请求被重置时触发。</div>
        </div>
        <div class="field">
          <label>出口 IP 保持多少分钟后强制更换（0 = 关闭）</label>
          <input type="number" id="fMinutes" min="0" max="1440">
          <div class="tip">用于避免同一出口 IP 使用过久被风控。</div>
        </div>
        <div class="field full">
          <label>opencode 日志文件（可选）</label>
          <input type="text" id="fWatchLog" placeholder="例如 logs\opencode.log">
          <div class="tip">相对项目根目录；监控该文件新增内容，命中关键词即自动换 IP。</div>
        </div>
        <div class="field full">
          <label>封禁关键词（正则，不区分大小写）</label>
          <input type="text" id="fWatchPattern">
        </div>
      </div>
      <div class="row-actions">
        <button class="btn primary" id="btnSave">保存配置</button>
        <span class="muted" id="saveInfo"></span>
      </div>
    </div>

    <div class="card" style="margin-top:14px">
      <h3>OpenCode Zen 入口 <span class="tag">zen-gate</span></h3>
      <div class="form">
        <div class="field full">
          <label class="check"><input type="checkbox" id="fZen"> 随 WARP 一起启停 zen-gate</label>
          <div class="tip">zen-gate 把 OpenCode Zen 免费车道封装成本地 OpenAI / Anthropic 兼容入口。
            opencode 的 baseURL 指向 <code>http://127.0.0.1:端口/v1</code>，API Key 用 zen-gate 面板里的主密钥。</div>
        </div>
        <div class="field full">
          <label class="check"><input type="checkbox" id="fZenWarp"> 强制 zen-gate 走 WARP 出口（停止时还原原设置）</label>
          <label class="check"><input type="checkbox" id="fAutoStartOpencode"> WARP 验证通过后自动启动 OpenCode（通过 <code>start-opencode.bat</code>；不会强制结束已打开的 opencode，已在运行则直接复用）</label>
          <div class="tip">接管已在运行的实例时，会通过 zen-gate 的管理接口把代理模式改为
            <code>http://127.0.0.1:HTTP端口</code>；这样 Zen 免费额度按 WARP 出口 IP 计量。</div>
        </div>
        <div class="field">
          <label>zen-gate 程序路径</label>
          <input type="text" id="fZenPath" placeholder="bin\zen-gate.exe">
          <div class="tip">相对路径按程序目录解析。</div>
        </div>
        <div class="field">
          <label>zen-gate 端口</label>
          <input type="number" id="fZenPort" min="1" max="65535">
          <div class="tip">默认 8788；留空写入端口时程序也会自动识别正在运行的实例。</div>
        </div>
        <div class="field full">
          <label>zen-gate 数据目录（可选）</label>
          <input type="text" id="fZenData" placeholder="留空 = %APPDATA%\zen-gate">
          <div class="tip">留空会直接接管已在运行的实例，并复用它的渠道、密钥和模型名单。</div>
        </div>
        <div class="field full">
          <div class="tip">
            触发规则：监控 zen-gate 的 access / routing 日志，命中 <code>status=429</code>、<code>err=quota</code>
            等限额信号时自动换一个 WARP 出口 IP（受上方“自动换 IP”总开关约束，90 秒内只触发一次）。
          </div>
        </div>
      </div>
    </div>
  </section>

  <!-- 日志 -->
  <section class="page" id="page-log">
    <div class="log-toolbar">
      <span class="muted">实时输出，最多保留 1500 条</span>
      <button class="btn ghost" id="btnClear">清空显示</button>
    </div>
    <div id="logBox"></div>
  </section>

  <!-- 帮助 -->
  <section class="page" id="page-help">
    <div class="card help">
      <h3>它解决什么问题</h3>
      <p>opencode 直连时出口 IP 固定，容易被服务端按 IP 限流或封禁。本程序托管 Cloudflare WARP，
         让 opencode 的请求从 WARP 出口发出；并在检测到异常时自动重连 WARP 更换出口 IP。
         它同时把 WARP 的 SOCKS5 转成标准 HTTP 代理，避免 opencode / Node 不支持 socks5 的问题。</p>

      <h3>前置条件（任选其一）</h3>
      <ul>
        <li><b>内置后端（推荐，免安装）</b>：后端选择“内置 warp-plus”或保持“自动检测”。
            首次启动时程序会从官方仓库下载 <code>warp-plus</code> 到本目录的 <code>bin/</code>，
            之后不再需要联网下载。GitHub 访问不畅时可在设置里填写下载镜像。</li>
        <li><b>Cloudflare WARP 官方客户端</b>：安装后确认 <code>warp-cli</code> 可用。首次使用可能需要
            <code>warp-cli --accept-tos registration new</code>。</li>
        <li><b>本机 warp-plus</b>：已有 <code>warp-plus.exe</code> 时，在“设置”里指定路径即可。</li>
        <li><b>usque</b>：先执行 <code>usque register</code>，再让本程序托管。</li>
      </ul>

      <h3>使用步骤</h3>
      <ul>
        <li>1. “设置”里选后端（默认自动检测，没有本地 WARP 时自动用内置后端），保存。</li>
        <li>2. 回到“仪表盘”点“启动”。首次使用内置后端会先下载，可在“日志”页看进度。</li>
        <li>3. 等待状态变为“运行中”且显示出口 IP。</li>
        <li>4. 复制页面上的 CMD 或 PowerShell 环境变量，在同一个终端里启动 opencode。</li>
        <li>5. 需要换 IP 时点“立即换 IP”；开启自动换 IP 后，失败或超时会自动更换。</li>
      </ul>

      <h3>opencode 侧配置示例</h3>
      <pre class="code">:: Windows CMD
set HTTP_PROXY=http://127.0.0.1:7891
set HTTPS_PROXY=http://127.0.0.1:7891
opencode</pre>
      <pre class="code"># PowerShell
$env:HTTP_PROXY="http://127.0.0.1:7891"
$env:HTTPS_PROXY="http://127.0.0.1:7891"
opencode</pre>

      <h3>OpenCode Zen 入口</h3>
      <ul>
        <li>zen-gate 把 OpenCode Zen 免费车道封装成本地 OpenAI / Anthropic 兼容接口。
            开启“随 WARP 一起启停 zen-gate”后，本程序会在 WARP 就绪时拉起它（或接管已在运行的实例），
            并把它的出口切到 WARP 的 HTTP 代理（<code>127.0.0.1:7891</code>），停止时还原原设置。</li>
        <li>免费额度按出口 IP 计量：zen-gate 的 access / routing 日志出现
            <code>status=429</code>、<code>err=quota</code> 等限额信号时，本程序会自动换一个 WARP 出口 IP，
            opencode 重试即可继续使用（受“自动换 IP”总开关约束，90 秒内只触发一次）。</li>
        <li>opencode 侧把 provider 的 baseURL 指向 <code>http://127.0.0.1:8788/v1</code>，
            API Key 填 zen-gate 面板里的主密钥。zen-gate 端口与 WARP Control 控制台端口分开：
            控制台默认 8787，zen-gate 默认 8788。</li>
      </ul>

      <h3>注意事项</h3>
      <ul>
        <li>只监听 127.0.0.1，不会把代理暴露到局域网。</li>
        <li>warp-cli 代理模式依赖客户端未被系统全局模式占用；如提示注册，请先完成注册。</li>
        <li>自动换 IP 会短暂中断连接，建议在空闲时触发。</li>
        <li>代理链路异常时，先看“日志”页的具体报错，再检查端口是否被占用。</li>
      </ul>
    </div>
  </section>
</div>
<div class="toast" id="toast"></div>

<div class="modal-mask" id="tipModal" role="dialog" aria-modal="true" aria-labelledby="tipTitle">
  <div class="modal">
    <div class="modal-head">
      <h3 id="tipTitle">打赏作者</h3>
      <button class="modal-x" id="tipClose" aria-label="关闭">×</button>
    </div>
    <div class="modal-body">
      <div class="tip muted">如果 WARP Control 对你有帮助，欢迎扫码打赏支持作者，感谢！</div>
      <div class="tip-grid">
        <figure>
          <img src="asset/1.jpg" alt="打赏二维码 1" loading="lazy">
          <figcaption>扫码打赏</figcaption>
        </figure>
        <figure>
          <img src="asset/2.png" alt="打赏二维码 2" loading="lazy">
          <figcaption>扫码打赏</figcaption>
        </figure>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn primary" id="tipOk">知道了</button>
    </div>
  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));
let lastLogId = 0;
let cfgData = null;
let currentStatus = null;

async function api(path, method, body){
  const opt = {method: method || 'GET', headers: {'Content-Type': 'application/json'}};
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch(e) { data = {error: text}; }
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(msg){
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 2200);
}

function setText(id, val){
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function ghAccel(url){
  return 'https://xiake.pro/github/?_360safeparam=503890531&url=' + encodeURIComponent(url);
}

function renderDownloads(){
  const body = document.getElementById('dlBody');
  if (!body) return;
  const items = [
    {name:'OpenCode', repo:'anomalyco/opencode', home:'https://github.com/anomalyco/opencode', rel:'https://github.com/anomalyco/opencode/releases'},
    {name:'zen-gate', repo:'LAGcomcom/zen-gate', home:'https://github.com/LAGcomcom/zen-gate', rel:'https://github.com/LAGcomcom/zen-gate/releases'},
    {name:'warp-plus', repo:'bepass-org/warp-plus', home:'https://github.com/bepass-org/warp-plus', rel:'https://github.com/bepass-org/warp-plus/releases'},
    {name:'usque', repo:'Diniboy1123/usque', home:'https://github.com/Diniboy1123/usque', rel:'https://github.com/Diniboy1123/usque/releases'}
  ];
  body.innerHTML = items.map(it => {
    const h = escHtml(it.home), r = escHtml(it.rel);
    const ah = escHtml(ghAccel(it.home)), ar = escHtml(ghAccel(it.rel));
    return '<tr>'
      + '<td><b>' + escHtml(it.name) + '</b></td>'
      + '<td class="mono"><a href="' + h + '" target="_blank" rel="noopener">' + escHtml(it.repo) + '</a></td>'
      + '<td><a href="' + r + '" target="_blank" rel="noopener">GitHub 发布页</a></td>'
      + '<td><a href="' + ah + '" target="_blank" rel="noopener">加速仓库</a> · <a href="' + ar + '" target="_blank" rel="noopener">加速发布页</a></td>'
      + '</tr>';
  }).join('');
}

const TIP_DATE_KEY = 'wc_tip_prompt_date';
function tipTodayKey(){
  const d = new Date();
  return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
}
function showTip(){
  const m = $('#tipModal');
  if (!m) return;
  m.classList.add('show');
  // 记录“今天已提示”，关闭后当天不再自动弹出。
  try { localStorage.setItem(TIP_DATE_KEY, tipTodayKey()); } catch (e) {}
}
function hideTip(){
  const m = $('#tipModal');
  if (m) m.classList.remove('show');
}
function maybeShowDailyTip(){
  let seen = '';
  try { seen = localStorage.getItem(TIP_DATE_KEY) || ''; } catch (e) {}
  if (seen !== tipTodayKey()) setTimeout(showTip, 900);
}

function phaseLabel(p){
  return ({running:'运行中', stopped:'已停止', starting:'启动中', stopping:'停止中',
           rotating:'换 IP 中', error:'异常'})[p] || p || '未知';
}

function render(s){
  currentStatus = s;
  const pill = $('#phasePill');
  pill.textContent = phaseLabel(s.phase);
  pill.className = 'pill ' + (s.phase || 'stopped');

  $('#btnStart').disabled = s.busy || s.phase === 'running';
  $('#btnStop').disabled = s.busy || s.phase === 'stopped';
  $('#btnRotate').disabled = s.busy || s.phase !== 'running';

  setText('sIp', s.egress_ip || '—');
  const ipHint = s.egress_ip
    ? ('WARP=' + (s.warp || '未知') + ' · 检测于 ' + (s.last_check || '—'))
    : (s.phase === 'running' ? '正在等待首次探测…' : '尚未探测');
  setText('sIpHint', ipHint);
  $('#sIp').className = 'value ' + (s.egress_ip ? 'green' : '');

  const loc = [s.loc, s.colo].filter(Boolean).join(' / ') || '—';
  setText('sLoc', loc);
  setText('sWarp', s.warp === 'on' ? '已通过 WARP 出网' : (s.warp ? 'WARP=' + s.warp : 'WARP 状态未知'));

  setText('sLatency', s.latency ? s.latency + ' ms' : '—');
  $('#sLatency').className = 'value ' +
    (s.latency == null ? '' : (s.latency < 400 ? 'green' : (s.latency < 900 ? 'yellow' : 'red')));
  setText('sCheck', '最近检测 ' + (s.last_check || '—'));

  setText('sBackend', s.backend || '—');
  setText('sUptime', s.uptime ? '运行时长 ' + s.uptime : '运行时长 —');

  setText('sSocks', s.socks_port || '—');
  setText('sSocksState', s.socks_up ? '端口已监听' : '端口未监听');
  $('#sSocksState').className = 'hint ' + (s.socks_up ? 'green' : 'red');

  setText('sHttp', s.http_port || '—');
  setText('sHttpState', s.http_up ? '端口已监听' : '端口未监听');
  $('#sHttpState').className = 'hint ' + (s.http_up ? 'green' : 'red');

  const oc = s.opencode || {};
  const ocText = oc.running
    ? ('运行中' + (oc.count > 1 ? ' ×' + oc.count : ''))
    : '未运行';
  setText('sOpencode', ocText);
  $('#sOpencode').className = 'value ' + (oc.running ? 'green' : '');
  setText('sOpencodeHint', oc.running
    ? (oc.proxy_connected ? ('已连接代理' + (oc.connections ? ' · ' + oc.connections : '')) : '未检测到代理连接')
    : 'start-opencode.bat');
  $('#sOpencodeHint').className = 'hint ' + (oc.running ? (oc.proxy_connected ? 'green' : 'yellow') : '');
  setText('vOpencode', oc.running
    ? (ocText + (oc.pids && oc.pids.length ? ' · PID ' + oc.pids.join(', ') : '') +
       (oc.proxy_connected ? ' · 经代理' : ''))
    : '未检测到 opencode 进程');
  $('#vOpencode').className = 'v ' + (oc.running ? (oc.proxy_connected ? 'green' : 'yellow') : '');

  const httpAddr = 'http://127.0.0.1:' + s.http_port;
  const socksAddr = 'socks5://127.0.0.1:' + s.socks_port;
  setText('vHttp', httpAddr);
  setText('vSocks', socksAddr);
  setText('vLocal', s.local_ip || '—');
  setText('vIpAge', s.ip_age ? Math.floor(s.ip_age / 60) + ' 分钟' : '—');
  setText('vAuto', s.auto_rotate ? ('已开启（失败 ' + (s.consecutive_fail || 0) + ' 次）') : '未开启');
  $('#vAuto').className = 'v ' + (s.auto_rotate ? 'green' : '');
  const zen = s.zen || {};
  const zenState = !zen.enabled ? '已关闭'
    : (zen.running
        ? ('http://127.0.0.1:' + zen.port + '/v1' + (zen.attached ? '（接管）' : '') +
           (zen.egress === 'warp' ? ' · 经 WARP' : ''))
        : '未运行');
  setText('vZen', zenState);
  $('#vZen').className = 'v ' + (zen.running ? 'green' : (zen.enabled ? 'yellow' : ''));
  setText('vZenHits', zen.quota_hits
    ? (zen.quota_hits + ' 次' + (zen.last_hit ? ' · 最近 ' + zen.last_hit : ''))
    : '尚未触发');
  $('#vZenHits').className = 'v ' + (zen.quota_hits ? 'yellow' : '');
  setText('vRotate', String(s.rotate_count || 0));

  const cmd = 'set HTTP_PROXY=' + httpAddr + '\r\nset HTTPS_PROXY=' + httpAddr + '\r\nopencode';
  const ps = '$env:HTTP_PROXY="' + httpAddr + '"\r\n$env:HTTPS_PROXY="' + httpAddr + '"\r\nopencode';
  setText('cmdBlock', cmd);
  setText('psBlock', ps);

  const banner = $('#errBanner');
  if (s.last_error && s.phase !== 'running') {
    banner.textContent = '错误：' + s.last_error;
    banner.classList.add('show');
  } else if (s.last_error) {
    banner.textContent = '最近一次探测失败：' + s.last_error;
    banner.classList.add('show');
  } else {
    banner.classList.remove('show');
  }
}

async function refresh(){
  try {
    render(await api('/api/status'));
  } catch(e) {
    $('#phasePill').className = 'pill error';
    $('#phasePill').textContent = '连接失败';
  }
}

function appendLog(item, box, mini){
  const el = document.createElement('div');
  el.className = 'log-line ' + (item.level || 'info');
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = item.t;
  const m = document.createElement('span');
  m.className = 'm';
  m.textContent = item.msg;
  el.appendChild(t);
  el.appendChild(m);
  const nearBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  box.appendChild(el);
  while (box.childElementCount > (mini ? 7 : 800)) box.removeChild(box.firstChild);
  if (nearBottom || mini) box.scrollTop = box.scrollHeight;
}

async function pollLogs(){
  try {
    const d = await api('/api/logs?since=' + lastLogId);
    for (const it of d.items) {
      lastLogId = Math.max(lastLogId, it.id);
      appendLog(it, $('#logBox'), false);
      appendLog(it, $('#miniLog'), true);
    }
  } catch(e) { /* 忽略轮询错误 */ }
}

async function act(name){
  try {
    const r = await api('/api/' + name, 'POST', {});
    if (r.message) toast(r.message);
    await refresh();
  } catch(e) {
    toast('操作失败：' + e.message);
  }
}

function escHtml(s){
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
let modelsCache = null;
function fmtNum(n){ return (n == null || n === '') ? '—' : String(n); }
function modelCaps(m){
  const a = [];
  if (m.vision) a.push('vision');
  if (m.reasoning) a.push('reasoning');
  if (m.systemOne) a.push('system1');
  if (Array.isArray(m.efforts) && m.efforts.length) a.push('effort');
  return a;
}
function stateBadge(state){
  const s = String(state || 'unknown').toLowerCase();
  let cls = 'badge';
  if (s === 'ok' || s === 'active' || s === 'healthy' || s === 'up') cls += ' ok';
  else if (s === 'error' || s === 'down' || s === 'blocked' || s === 'dead') cls += ' err';
  else if (s && s !== 'unknown') cls += ' warn';
  return '<span class="' + cls + '">' + escHtml(state || 'unknown') + '</span>';
}
function renderModelStats(data){
  const box = $('#modelsStats');
  const detail = $('#statsDetail');
  if (!data || !data.ok) {
    box.innerHTML = '';
    detail.textContent = data && data.error ? ('zen-gate 不可用：' + data.error) : 'zen-gate 不可用';
    return;
  }
  const models = data.models || [];
  const stats = data.stats || {};
  const days = stats.days || {};
  const today = new Date().toISOString().slice(0,10);
  const day = days[today] || {};
  let totalReq = 0, totalFail = 0, totalOut = 0;
  Object.values(days).forEach(d => { totalReq += (+d.requests||0); totalFail += (+d.failed||0); totalOut += (+d.output||0); });
  const cards = [
    ['模型数', models.length, data.base_url || ('127.0.0.1:' + data.port)],
    ['今日请求', day.requests || 0, '失败 ' + (day.failed || 0)],
    ['累计请求', totalReq, '失败 ' + totalFail],
    ['累计输出 tokens', totalOut, '按 zen-gate 统计']
  ];
  box.innerHTML = cards.map(c => '<div class="stat"><div class="label">'+escHtml(c[0])+'</div><div class="value">'+escHtml(c[1])+'</div><div class="hint">'+escHtml(c[2])+'</div></div>').join('');
  const note = $('#modelsNote');
  if (note) {
    const throttled = models.filter(m => String(m.state || '').toLowerCase() === 'throttled').length;
    if (data.lane_fallback && throttled > 0) {
      note.innerHTML = 'zen 免费通道当前有 <b>' + throttled + '</b> 个模型被限流（FreeUsageLimitError）。'
        + 'zen-gate 已开启 <code>laneFallbackToProviders</code>，调用这些模型会自动回落到 kilo / openrouter 免费通道，仍然可以正常使用；'
        + '下表状态是 zen 原始通道的探测结果，不代表最终不可用。';
      note.classList.add('show');
    } else if (!data.lane_fallback && throttled > 0) {
      note.innerHTML = 'zen 免费通道当前有 <b>' + throttled + '</b> 个模型被限流，且未开启通道回落（laneFallbackToProviders）。'
        + '在 zen-gate 面板设置里开启“通道回落到提供商”后，可自动改用 kilo / openrouter 免费通道。';
      note.classList.add('show');
    } else {
      note.classList.remove('show');
      note.innerHTML = '';
    }
  }
  const recent = (stats.recent || []).slice(-8).reverse();
  let html = '<div><b>今日</b>：请求 ' + (+day.requests||0) + '，失败 ' + (+day.failed||0) + '，输入 ' + (+day.input||0) + '，输出 ' + (+day.output||0) + '</div>';
  html += '<div><b>累计</b>：请求 ' + totalReq + '，失败 ' + totalFail + '，输入 tokens ' + Object.values(days).reduce((a,d)=>a+(+d.input||0),0) + '，输出 tokens ' + totalOut + '</div>';
  if (recent.length) {
    html += '<div style="margin-top:8px"><b>最近记录</b></div><div class="mono" style="font-size:11.5px;color:#9fb0c8">';
    recent.forEach(r => {
      html += escHtml((r.ok?'OK':'ERR') + ' ' + (r.model||'') + (r.errCode?' ['+r.errCode+']':'') + (r.output?' out='+r.output:'')) + '<br>';
    });
    html += '</div>';
  }
  detail.innerHTML = html;
}
function renderModelsTable(){
  const q = ($('#modelSearch') ? $('#modelSearch').value : '').trim().toLowerCase();
  const data = modelsCache;
  const body = $('#modelsBody');
  const dl = $('#modelList');
  if (!data || !data.ok) {
    body.innerHTML = '<tr><td colspan="7" class="muted">'+escHtml(data && data.error ? data.error : '未加载')+'</td></tr>';
    if (dl) dl.innerHTML = '';
    setText('modelsCount','0');
    return;
  }
  const models = (data.models || []).filter(m => {
    if (!q) return true;
    const hay = [m.id,m.name,m.channel,(m.channels||[]).join(','),m.state,m.detail].join(' ').toLowerCase();
    return hay.indexOf(q) >= 0;
  });
  setText('modelsCount', String(models.length) + ' / ' + String((data.models||[]).length));
  if (dl) dl.innerHTML = (data.models||[]).map(m => '<option value="'+escHtml(m.id)+'">'+escHtml((m.name||m.id)+'')+'</option>').join('');
  if (!models.length) { body.innerHTML = '<tr><td colspan="7" class="muted">无匹配模型</td></tr>'; return; }
  body.innerHTML = models.map(m => {
    const caps = modelCaps(m).map(c=>'<span class="badge">'+escHtml(c)+'</span>').join(' ');
    const ch = escHtml(m.channel || (m.channels||[]).join(',') || '—');
    const ctx = (m.contextWindow ? (Math.round(m.contextWindow/1024)+'k ctx') : '') + (m.maxOutput ? (' / '+Math.round(m.maxOutput/1024)+'k out') : '');
    const probe = (m.latencyMs ? (m.latencyMs+' ms') : '') + (m.detail ? ('<div class="muted" style="font-size:11px;max-width:240px;white-space:normal">'+escHtml(String(m.detail).slice(0,120))+'</div>') : '');
    const id = escHtml(m.id||'');
    const name = escHtml(m.name||m.id||'');
    return '<tr>'
      + '<td><div class="mono">'+id+'</div><div class="muted" style="font-size:11.5px">'+name+'</div></td>'
      + '<td>'+stateBadge(m.state)+'</td>'
      + '<td>'+ch+'</td>'
      + '<td>'+(caps||'<span class="muted">—</span>')+'</td>'
      + '<td class="mono" style="font-size:11.5px">'+escHtml(ctx||'—')+'</td>'
      + '<td style="font-size:11.5px">'+(probe||'<span class="muted">—</span>')+'</td>'
      + '<td><button class="btn sm ghost" data-pick-model="'+id+'">测试</button></td>'
      + '</tr>';
  }).join('');
  body.querySelectorAll('[data-pick-model]').forEach(b => b.addEventListener('click', () => {
    $('#testModel').value = b.dataset.pickModel || '';
    window.scrollTo({top:0,behavior:'smooth'});
  }));
}
async function loadModels(force){
  if (!force && modelsCache && modelsCache.ok && modelsCache._at && Date.now()-modelsCache._at < 15000) {
    renderModelStats(modelsCache); renderModelsTable(); return;
  }
  const hint = $('#modelsHint');
  const err = $('#modelsErr');
  if (hint) hint.textContent = '正在读取 zen-gate 模型与统计…';
  if (err) { err.classList.remove('show'); err.textContent = ''; }
  try {
    const d = await api('/api/zen/models');
    d._at = Date.now();
    modelsCache = d;
    renderModelStats(d);
    renderModelsTable();
    if (hint) hint.textContent = d.ok
      ? ('zen-gate 127.0.0.1:' + d.port + ' · ' + ((d.models||[]).length) + ' 个模型')
      : 'zen-gate 不可用';
    if (!d.ok && err) { err.textContent = '读取失败：' + (d.error || '未知错误'); err.classList.add('show'); }
  } catch(e) {
    if (hint) hint.textContent = '读取失败';
    if (err) { err.textContent = '读取失败：' + e.message; err.classList.add('show'); }
  }
}
async function reprobeModels(){
  const btn = $('#btnModelsReprobe');
  const hint = $('#modelsHint');
  btn.disabled = true;
  if (hint) hint.textContent = '已请求 zen-gate 重新探测，约数十秒后自动刷新…';
  try {
    const r = await api('/api/zen/reprobe', 'POST', {});
    if (!r.ok) throw new Error(r.error || '未知错误');
    toast('已开始重新探测，稍后自动刷新');
    setTimeout(() => loadModels(true), 25000);
  } catch(e) {
    toast('重新探测失败：' + e.message);
    if (hint) hint.textContent = '重新探测失败：' + e.message;
  } finally {
    btn.disabled = false;
  }
}
async function runModelTest(){
  const model = ($('#testModel').value || '').trim();
  if (!model) { toast('请输入或选择模型'); return; }
  const prompt = $('#testPrompt').value || 'Reply with exactly: OK';
  const max_tokens = parseInt($('#testMaxTokens').value || '64', 10) || 64;
  const out = $('#modelTestOut');
  const btn = $('#btnModelTest');
  btn.disabled = true;
  out.textContent = '正在测试 ' + model + ' …';
  try {
    const r = await api('/api/zen/test', 'POST', {model, prompt, max_tokens});
    if (r.ok) {
      let t = 'OK · ' + (r.ms||0) + ' ms · ' + (r.raw_model || model);
      if (r.usage && (r.usage.total_tokens || r.usage.prompt_tokens || r.usage.completion_tokens)) {
        t += '\nusage: ' + JSON.stringify(r.usage);
      }
      t += '\n\n' + (r.text || '(空响应)');
      out.textContent = t;
      toast('模型测试成功');
    } else {
      out.textContent = 'FAIL · ' + (r.ms||0) + ' ms · ' + model + '\n' + JSON.stringify(r.error || r, null, 2);
      toast('模型测试失败');
    }
  } catch(e) {
    out.textContent = '请求失败：' + e.message;
    toast('模型测试请求失败');
  } finally {
    btn.disabled = false;
  }
}

async function startOpencode(){
  const oc = (currentStatus && currentStatus.opencode) ? currentStatus.opencode : {};
  let msg = '将启动 OpenCode（通过 start-opencode.bat，新控制台窗口），不会强制结束任何已打开的 opencode。';
  if (oc.running && oc.pids && oc.pids.length) {
    msg = '检测到 OpenCode 已在运行（PID ' + oc.pids.join(', ') + '），将直接复用现有窗口并跳过启动，不会结束这些进程。';
  } else if (oc.running) {
    msg = '检测到 OpenCode 已在运行，将直接复用现有窗口并跳过启动，不会结束它。';
  }
  msg += '\n\n是否继续？';
  if (!confirm(msg)) return;
  const btn = $('#btnStartOpencode');
  btn.disabled = true;
  try {
    const r = await api('/api/opencode/start', 'POST', {});
    toast(r.message || (r.ok ? '已启动 OpenCode' : '启动失败'));
    setTimeout(refresh, 1200);
    setTimeout(refresh, 3500);
  } catch(e) {
    toast('启动 OpenCode 失败：' + e.message);
  } finally {
    btn.disabled = false;
  }
}

async function loadConfig(){
  const d = await api('/api/config');
  cfgData = d.config;
  const c = d.config;
  $('#fBackend').value = c.backend || 'auto';
  $('#fPath').value = c.backend_path || '';
  $('#fCustom').value = c.custom_command || '';
  $('#fPlusArgs').value = c.warp_plus_args || '';
  $('#fMirror').value = c.builtin_mirror || '';
  $('#fSocks').value = c.socks_port;
  $('#fHttp').value = c.http_port;
  $('#fInterval').value = c.probe_interval;
  $('#fTimeout').value = c.probe_timeout;
  $('#fVerify').checked = !!c.verify_ssl;
  $('#fAuto').checked = !!c.auto_rotate;
  $('#fFail').value = c.rotate_after_failures;
  $('#fMinutes').value = c.rotate_after_minutes;
  $('#fWatchLog').value = c.watch_log || '';
  $('#fWatchPattern').value = c.watch_pattern || '';
  $('#fZen').checked = !!c.zen_enabled;
  $('#fZenWarp').checked = !!c.zen_force_warp;
  $('#fAutoStartOpencode').checked = !!c.auto_start_opencode;
  $('#fZenPath').value = c.zen_path || '';
  $('#fZenPort').value = c.zen_port;
  $('#fZenData').value = c.zen_data_dir || '';
  setText('saveInfo', '配置文件：' + d.config_path);
}

async function saveConfig(){
  const body = {
    backend: $('#fBackend').value,
    backend_path: $('#fPath').value.trim(),
    custom_command: $('#fCustom').value.trim(),
    warp_plus_args: $('#fPlusArgs').value.trim(),
    builtin_mirror: $('#fMirror').value.trim(),
    socks_port: Number($('#fSocks').value),
    http_port: Number($('#fHttp').value),
    probe_interval: Number($('#fInterval').value),
    probe_timeout: Number($('#fTimeout').value),
    verify_ssl: $('#fVerify').checked,
    auto_rotate: $('#fAuto').checked,
    rotate_after_failures: Number($('#fFail').value),
    rotate_after_minutes: Number($('#fMinutes').value),
    watch_log: $('#fWatchLog').value.trim(),
    watch_pattern: $('#fWatchPattern').value.trim(),
    zen_enabled: $('#fZen').checked,
    zen_force_warp: $('#fZenWarp').checked,
    auto_start_opencode: $('#fAutoStartOpencode').checked,
    zen_path: $('#fZenPath').value.trim(),
    zen_port: Number($('#fZenPort').value),
    zen_data_dir: $('#fZenData').value.trim()
  };
  try {
    const r = await api('/api/config', 'POST', body);
    toast(r.message || '已保存');
    setText('saveInfo', r.message || '已保存');
    await refresh();
  } catch(e) {
    toast('保存失败：' + e.message);
  }
}

async function detect(){
  const info = $('#detectInfo');
  info.textContent = '检测中…';
  try {
    const r = await api('/api/detect', 'POST', {});
    const list = $('#detectList');
    list.innerHTML = '';
    if (!r.backends.length) {
      list.textContent = '未检测到任何后端。可选择“内置 warp-plus”，程序会自动下载。';
      info.textContent = '';
      return;
    }
    for (const b of r.backends) {
      const div = document.createElement('div');
      const label = b.key === 'builtin' ? '内置 warp-plus' : b.key;
      div.innerHTML = '<b>' + label + '</b> · ' + b.version +
        '<br><span style="color:#61708a">' + b.path + '</span>';
      list.appendChild(div);
    }
    info.textContent = '共检测到 ' + r.backends.length + ' 项';
    if (!$('#fPath').value && r.backends[0].key !== 'builtin') {
      $('#fPath').value = r.backends[0].path;
    }
  } catch(e) {
    info.textContent = '检测失败：' + e.message;
  }
}

async function downloadBuiltin(){
  try {
    const r = await api('/api/builtin-download', 'POST', {});
    toast(r.message || '已开始下载');
    $$('.tab').forEach(x => x.classList.remove('active'));
    $$('.page').forEach(x => x.classList.remove('active'));
    document.querySelector('.tab[data-tab="log"]').classList.add('active');
    $('#page-log').classList.add('active');
  } catch(e) {
    toast('下载失败：' + e.message);
  }
}

function copyFrom(id){
  const el = document.getElementById(id);
  if (!el) return;
  const text = el.textContent;
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => toast('已复制'), () => toast('复制失败，请手动选择'));
  } else {
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); toast('已复制'); } catch(e) { toast('复制失败'); }
    document.body.removeChild(ta);
  }
}

$$('.tab').forEach(t => t.addEventListener('click', () => {
  $$('.tab').forEach(x => x.classList.remove('active'));
  $$('.page').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  $('#page-' + t.dataset.tab).classList.add('active');
  if (t.dataset.tab === 'models') loadModels(false).catch(() => {});
}));

$$('.copy').forEach(b => b.addEventListener('click', () => copyFrom(b.dataset.copy)));
$('#btnStart').addEventListener('click', () => act('start'));
$('#btnStop').addEventListener('click', () => act('stop'));
$('#btnRotate').addEventListener('click', () => act('rotate'));
$('#btnDetect').addEventListener('click', detect);
$('#btnBuiltin').addEventListener('click', downloadBuiltin);
$('#btnSave').addEventListener('click', saveConfig);
$('#btnClear').addEventListener('click', () => { $('#logBox').innerHTML = ''; });
$('#btnModelsRefresh').addEventListener('click', () => loadModels(true));
$('#btnModelsReprobe').addEventListener('click', reprobeModels);
$('#btnModelTest').addEventListener('click', runModelTest);
$('#modelSearch').addEventListener('input', () => { if (modelsCache) renderModelsTable(); });
$('#btnStartOpencode').addEventListener('click', startOpencode);
$('#btnTip').addEventListener('click', showTip);
$('#tipClose').addEventListener('click', hideTip);
$('#tipOk').addEventListener('click', hideTip);
$('#tipModal').addEventListener('click', e => { if (e.target === $('#tipModal')) hideTip(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideTip(); });

(async function init(){
  renderDownloads();
  maybeShowDailyTip();
  await loadConfig().catch(e => toast('读取配置失败：' + e.message));
  await refresh();
  await pollLogs();
  setInterval(refresh, 2000);
  setInterval(pollLogs, 2000);
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

PREVIEW_MOCK = r"""
<script>
(function () {
  var STATUS = {
    phase: "running", busy: false, running: true, backend: "builtin",
    socks_port: 40000, http_port: 7891, socks_up: true, http_up: true,
    egress_ip: "104.28.198.37", loc: "JP", colo: "NRT", warp: "on",
    latency: 286, last_check: "00:22:41", last_error: "",
    local_ip: "124.174.33.195", uptime: "00:42:07", rotate_count: 2,
    consecutive_fail: 0, auto_rotate: true, ip_age: 734
  };
  var CONFIG = {
    backend: "builtin", backend_path: "", socks_port: 40000, http_port: 7891,
    probe_interval: 20, probe_timeout: 12, auto_rotate: true,
    rotate_after_failures: 3, rotate_after_minutes: 0,
    custom_command: "", warp_plus_args: "", builtin_mirror: "",
    watch_log: "", watch_pattern: "(429|403|blocked|forbidden|rate.?limit|banned|ip.?ban|too many requests)",
    verify_ssl: true
  };
  var LOGS = [
    {id: 1, t: "00:21:03", level: "info", msg: "使用后端：builtin（内置 warp-plus）"},
    {id: 2, t: "00:21:05", level: "info", msg: "warp-plus 已启动，SOCKS5 监听 127.0.0.1:40000"},
    {id: 3, t: "00:21:05", level: "info", msg: "已就绪：SOCKS5 127.0.0.1:40000  ->  HTTP 127.0.0.1:7891"},
    {id: 4, t: "00:21:06", level: "info", msg: "当前出口 IP：104.28.198.37（JP / NRT，WARP=on）"},
    {id: 5, t: "00:22:41", level: "info", msg: "探测正常，延迟 286 ms"},
    {id: 6, t: "00:24:10", level: "warn", msg: "开始换 IP：手动触发"},
    {id: 7, t: "00:24:14", level: "info", msg: "出口 IP 变更：104.28.198.37 -> 104.28.211.66"},
    {id: 8, t: "00:24:14", level: "info", msg: "当前出口 IP：104.28.211.66（JP / NRT，WARP=on）"}
  ];

  function reply(obj) {
    return Promise.resolve(new Response(JSON.stringify(obj), {
      status: 200, headers: {"Content-Type": "application/json"}
    }));
  }

  window.fetch = function (url) {
    var u = String(url);
    if (u.indexOf("/api/status") >= 0) return reply(STATUS);
    if (u.indexOf("/api/config") >= 0) return reply({config: CONFIG, config_path: "（静态预览，未连接真实后端）"});
    if (u.indexOf("/api/logs") >= 0) {
      var m = u.match(/since=(\d+)/);
      var since = m ? Number(m[1]) : 0;
      return reply({items: LOGS.filter(function (i) { return i.id > since; })});
    }
    if (u.indexOf("/api/detect") >= 0) {
      return reply({backends: [
        {key: "builtin", path: "（静态预览）bin/warp-plus.exe", version: "内置 warp-plus（免安装）"}
      ]});
    }
    if (u.indexOf("/api/builtin") >= 0) {
      return reply({exists: true, path: "bin/warp-plus.exe", size: 8123456, platform: "windows-amd64"});
    }
    return reply({ok: true, message: "静态预览：仅展示界面，未连接真实后端"});
  };

  document.addEventListener("DOMContentLoaded", function () {
    var badge = document.createElement("div");
    badge.textContent = "静态预览 · 非实时数据";
    badge.style.cssText = "position:fixed;right:14px;bottom:14px;z-index:999;padding:6px 12px;" +
      "border-radius:999px;background:rgba(251,191,36,.12);border:1px solid rgba(251,191,36,.4);" +
      "color:#fbbf24;font-size:11.5px;font-family:system-ui,sans-serif";
    document.body.appendChild(badge);
  });
})();
</script>
"""


def build_preview_html():
    return HTML_PAGE.replace("<body>", "<body>\n" + PREVIEW_MOCK, 1)


def main():
    global APP
    parser = argparse.ArgumentParser(description="WARP Control - opencode 出口 IP 管家")
    parser.add_argument("--host", default="127.0.0.1", help="控制台监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8787, help="控制台端口，默认 8787")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--auto-start-opencode", action="store_true", help="WARP 启动并验证通过后，自动通过 start-opencode.bat 启动 OpenCode")
    parser.add_argument("--export-preview", action="store_true", help="导出静态预览页 preview.html 后退出")
    args = parser.parse_args()

    if args.export_preview:
        out = os.path.join(APP_DIR, "preview.html")
        with open(out, "w", encoding="utf-8") as f:
            f.write(build_preview_html())
        log_print("已导出静态预览：%s" % out)
        return 0

    APP = App()
    if getattr(args, "auto_start_opencode", False):
        APP.cfg["auto_start_opencode"] = True
        save_config(APP.cfg)
        APP.log.add("已启用：WARP 验证通过后自动启动 OpenCode")

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        log_print("控制台端口 %d 无法监听：%s" % (args.port, e))
        return 1

    server.daemon_threads = True
    url = "http://%s:%d/" % (args.host, args.port)
    APP.log.add("控制台地址：%s" % url)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        APP.log.add("正在退出…")
        APP._cleanup()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
