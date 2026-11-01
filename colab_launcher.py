# -*- coding: utf-8 -*-
"""A100 官方 Pika 工作台一键启动：复用模型缓存，启动网页后端，无需密码。"""
from __future__ import annotations
import base64
import gzip
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

PUBLIC_URL = "https://mcp-mac.id96.vip"
STUDIO_URL = "http://127.0.0.1:3091"  # 电脑上的本地独立网页；部署后可换为项目域名。
APP_VERSION = "1.3.0"
PORT = 8188  # 复用原来 ComfyUI 的隧道服务端口。
STORAGE_ROOT = ""  # 空白自动检测 scratch；也可填 /mnt/local-scratch。
HTTP_DOWNLOAD = False  # Xet 下载异常时设 True，后续生成进程走普通 HTTP。
PREPARE_OPENH3 = True  # 启动时准备独立 INT8 二采，避免首次任务等待下载而超时。
ENV_BACKUP = "/content/drive/MyDrive/ComfyUI_Models/Pika_H3_Flex/env.tar.gz"
BUNDLE_GZ_B64 = "__BUNDLE_PLACEHOLDER__"


def health():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=3) as response:
            return json.load(response)
    except Exception:
        return {}


def live_owned(pidfile, expected):
    try:
        pid = int(pidfile.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        return pid if expected in cmdline else None
    except (OSError, ValueError):
        return None


def start_tunnel(work, get_secret):
    pidfile = work / "studio-cloudflared.pid"
    if live_owned(pidfile, "cloudflared"):
        print("⏩ 本工作台隧道进程已在运行，连接状态见 studio-cloudflared.log", flush=True)
        return True
    token = get_secret("CF_TUNNEL_TOKEN")
    if not token:
        print("⚠️ 服务已启动，但缺少 CF_TUNNEL_TOKEN。请在 Colab Secrets 添加并授权后重跑本格。", flush=True)
        return False
    binary = shutil.which("cloudflared")
    if not binary:
        local = Path("/content/cloudflared")
        if not local.is_file() or local.stat().st_size < 1024**2:
            partial = local.with_suffix(".part")
            machine = os.uname().machine
            arch = "arm64" if machine in {"aarch64", "arm64"} else "amd64"
            url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
            subprocess.run(["curl", "-fL", "--retry", "2", "--connect-timeout", "15", "--max-time", "120",
                            "-o", str(partial), url], check=True)
            partial.replace(local)
        local.chmod(0o755)
        binary = str(local)
    tunnel_env = os.environ.copy()
    tunnel_env["TUNNEL_TOKEN"] = token
    log = work / "logs" / "studio-cloudflared.log"
    with log.open("w", encoding="utf-8") as fp:
        # 使用环境变量传入令牌，避免在命令行与日志打印中暴露。
        proc = subprocess.Popen([binary, "tunnel", "--protocol", "http2", "run"],
                                stdout=fp, stderr=subprocess.STDOUT, env=tunnel_env, start_new_session=True)
    pidfile.write_text(str(proc.pid))
    start = time.monotonic()
    while time.monotonic()-start < 30:
        text = log.read_text(encoding="utf-8", errors="replace")
        if proc.poll() is not None:
            print("⚠️ 隧道退出，工作台仍在本地运行。日志：\n" + text[-8000:], flush=True)
            return False
        if "Registered tunnel connection" in text:
            print("✅ Cloudflare HTTP/2 隧道已注册连接", flush=True)
            return True
        time.sleep(.5)
    print(f"⚠️ 隧道连接尚未确认，请查看 {log}；本地工作台已就绪。", flush=True)
    return False


def restore_pika_environment(work):
    if (work / "environment-ready.json").is_file() and (work / "venv-py312/bin/python").is_file():
        print("Pika 环境已在本地，复用现有环境；模型由 HF 缓存 / 下载补齐。", flush=True)
        return
    backup = Path(ENV_BACKUP)
    if backup.is_file():
        work.mkdir(parents=True, exist_ok=True)
        print(f"从已挂载云盘恢复 Pika 环境: {backup}", flush=True)
        subprocess.run(["tar", "-xzf", str(backup), "-C", str(work)], check=True)
    else:
        print("未找到已挂载的 Pika 环境备份，使用环境安装器；不从云盘读取模型。", flush=True)


def main():
    if sys.platform != "linux":
        raise RuntimeError("本文件在 Colab A100 Linux 中运行；本地看界面请运行 preview.py")
    payload = json.loads(gzip.decompress(base64.b64decode(BUNDLE_GZ_B64)))
    namespace = {"__name__": "pika_installer", "__file__": "/content/pika-studio-installer.py"}
    exec(compile(payload.pop("installer_runtime.py"), "pika-studio-installer.py", "exec"), namespace)
    config = dict(namespace["CONFIG"], storage_root=STORAGE_ROOT, http_download=HTTP_DOWNLOAD)
    namespace["CONFIG"] = config
    current = health()
    if current.get("service") == "pika-h3-flex-studio":
        work = namespace["select_storage"](config)
        app_dir = work / "studio-app"
        for name, content in payload.items():
            target = app_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        if current.get("version") == APP_VERSION:
            print("⏩ 已有同版本 Pika 工作台在线，已同步最新脚本并保留当前任务。", flush=True)
            start_tunnel(work, namespace["secret"])
            print(f"🌐 在电脑上打开工作台：{STUDIO_URL}\n后端隧道：{PUBLIC_URL}", flush=True)
            return
        pid = live_owned(work / "studio-server.pid", str(work / "studio-app" / "server.py"))
        if pid:
            import signal
            os.kill(pid, signal.SIGTERM)
            for _ in range(40):
                if not health():
                    break
                time.sleep(.25)
            print("✅ 已停止旧版服务，正在启动新版服务。", flush=True)
        else:
            print("⚡ 已直接热更新 studio-app/progress_runner.py，当前运行中任务不受影响。", flush=True)
            start_tunnel(work, namespace["secret"])
            return
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            raise RuntimeError(f"{PORT} 已被其他程序占用，请先停止旧 ComfyUI，再运行工作台；本脚本不会杀其他进程。")
    except OSError:
        pass
    restore_pika_environment(namespace["select_storage"](config))
    work, env, cli = namespace["prepare_environment"]()
    app_dir = work / "studio-app"
    for name, content in payload.items():
        target = app_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if PREPARE_OPENH3:
        print("准备 OpenH3 INT8 二采（独立环境，无 HTTP 服务；首次需下载额外模型）...", flush=True)
        subprocess.run([str(cli.parent / "python"), str(app_dir / "openh3_setup.py"),
                        "--root", str(work / "openh3")], check=True, env=env)
    env.pop("PIKA_STUDIO_PASSWORD", None)
    env.pop("CF_TUNNEL_TOKEN", None)
    log = work / "logs" / "studio-server.log"
    with log.open("a", encoding="utf-8") as fp:
        proc = subprocess.Popen([str(cli.parent / "python"), str(app_dir / "server.py"),
                                 "--root", str(work), "--port", str(PORT)],
                                stdout=fp, stderr=subprocess.STDOUT, env=env, start_new_session=True)
    (work / "studio-server.pid").write_text(str(proc.pid))
    for _ in range(120):
        if proc.poll() is not None:
            print(log.read_text(encoding="utf-8", errors="replace")[-10000:], flush=True)
            raise RuntimeError("工作台服务提前退出")
        if health().get("service") == "pika-h3-flex-studio":
            break
        time.sleep(.5)
    else:
        print(log.read_text(encoding="utf-8", errors="replace")[-10000:], flush=True)
        raise RuntimeError("工作台启动超时")
    print("✅ 工作台已启动，生成任务在网页提交，不会自动跑示例。", flush=True)
    print("🌐 工作台无需登录，打开网页即可使用。", flush=True)
    try:
        connected = start_tunnel(work, namespace["secret"])
    except Exception as exc:
        connected = False
        print(f"⚠️ 隧道准备失败：{exc}。本地工作台仍在运行。", flush=True)
    print(f"🌐 在电脑上打开工作台：{STUDIO_URL}\n后端隧道：{PUBLIC_URL}", flush=True)
    print(f"隧道 {'已连接' if connected else '尚待确认'}；Cloudflare 服务地址应为 http://127.0.0.1:{PORT}", flush=True)
    print(f"模型缓存复用：{work / 'hf'}\n服务日志：{log}\n生成视频：{work / 'outputs'}", flush=True)


if __name__ == "__main__":
    main()
