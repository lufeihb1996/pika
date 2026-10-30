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
APP_VERSION = "1.3.0-5ba28b0-dirty"
PORT = 8188  # 复用原来 ComfyUI 的隧道服务端口。
STORAGE_ROOT = ""  # 空白自动检测 scratch；也可填 /mnt/local-scratch。
HTTP_DOWNLOAD = False  # Xet 下载异常时设 True，后续生成进程走普通 HTTP。
BUNDLE_GZ_B64 = "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"


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
    token = get_secret("CF_TUNNEL_TOKEN")
    if not token:
        print("⚠️ 服务已启动，但缺少 CF_TUNNEL_TOKEN。请在 Colab Secrets 添加并授权后重跑本格。", flush=True)
        return False
    pidfile = work / "studio-cloudflared.pid"
    if live_owned(pidfile, "cloudflared"):
        print("⏩ 本工作台隧道进程已在运行，连接状态见 studio-cloudflared.log", flush=True)
        return True
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
        if current.get("version") == APP_VERSION:
            print("⏩ 已有同版本 Pika 工作台在线，无需密码，保留当前任务。", flush=True)
            start_tunnel(work, namespace["secret"])
            print(f"🌐 在电脑上打开工作台：{STUDIO_URL}\n后端隧道：{PUBLIC_URL}", flush=True)
            return
        # Only update our own idle server. Do not interrupt GPU work or queued jobs.
        for path in (work / "jobs").glob("*.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("status") in {"running", "queued", "cancelling"}:
                raise RuntimeError("更新进度条前，请等当前队列完成或在网页取消任务，然后重跑这格；不会中断生成。")
        pid = live_owned(work / "studio-server.pid", str(work / "studio-app" / "server.py"))
        if not pid:
            raise RuntimeError("检测到旧版服务但无法确认归属，请先手动停止旧工作台服务器")
        import signal
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            if not health():
                break
            time.sleep(.25)
        else:
            raise RuntimeError("旧版服务尚未停止，请稍后重跑")
        print("✅ 已停止本工作台的空闲旧版服务，正在更新。", flush=True)
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=1):
            raise RuntimeError(f"{PORT} 已被其他程序占用，请先停止旧 ComfyUI，再运行工作台；本脚本不会杀其他进程。")
    except OSError:
        pass
    work, env, cli = namespace["prepare_environment"]()
    app_dir = work / "studio-app"
    for name, content in payload.items():
        target = app_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
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
