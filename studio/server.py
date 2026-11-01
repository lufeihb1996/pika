"""Pika official CLI queue and same-origin mobile studio. Python standard library only."""
from __future__ import annotations
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

MODEL_REV = "0ddf55ad7067ca1e76812253bd7b4600df87f759"
APP_VERSION = "1.3.0"
TASKS = {"t2va": (False, False), "i2va": (True, False), "l2va": (False, True),
         "fl2va": (True, True), "ref2va": (False, False)}
ASPECTS = ["16:9", "9:16", "1:1", "21:9", "3:2", "4:3", "5:4", "4:5", "3:4", "2:3", "9:21"]
EXTENSIONS = {"image": {".png", ".jpg", ".jpeg", ".webp"},
              "video": {".mp4", ".mov", ".webm"}, "audio": {".wav", ".mp3", ".flac", ".m4a"}}
ID = re.compile(r"^[0-9a-f]{32}$")
MAX_UPLOAD = 200 * 1024**2


class Studio:
    def __init__(self, root, cli, preview=False):
        self.root, self.cli, self.preview = Path(root), Path(cli), preview
        for folder in ("jobs", "outputs", "uploads"):
            (self.root / folder).mkdir(parents=True, exist_ok=True)
        self.password = os.getenv("PIKA_STUDIO_PASSWORD", "")
        self.session_key = secrets.token_bytes(32)
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.jobs = {}
        self.active = None
        self.proc = None
        self.failures = {}
        # 无状态执行器：任务只活在内存里。重启后队列清空，已生成的 MP4 仍在 outputs/。
        self.thread = threading.Thread(target=self.worker, daemon=True)
        self.thread.start()

    def save(self, job):
        """无状态实现：这里不落盘，保留调用点是为了 worker 的代码不用到处改。"""

    def session(self):
        expiry = str(int(time.time()) + 7 * 86400)
        return expiry + "." + hmac.new(self.session_key, expiry.encode(), hashlib.sha256).hexdigest()

    def valid_session(self, token):
        try:
            expiry, sig = token.split(".", 1)
            expected = hmac.new(self.session_key, expiry.encode(), hashlib.sha256).hexdigest()
            return int(expiry) > time.time() and hmac.compare_digest(sig, expected)
        except (ValueError, AttributeError):
            return False

    def upload_path(self, value, expected=None):
        if not isinstance(value, str) or not ID.fullmatch(value):
            raise ValueError("素材标识无效，请重新上传")
        paths = list((self.root / "uploads").glob(value + ".*"))
        paths = [p for p in paths if p.suffix != ".part"]
        if len(paths) != 1:
            raise ValueError("素材不存在，请重新上传")
        path = paths[0]
        if expected and path.suffix.lower() not in EXTENSIONS[expected]:
            raise ValueError("素材类型与任务不匹配")
        return path

    def validate(self, data):
        prompt = data.get("prompt", "")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 8000:
            raise ValueError("请输入 1–8000 字符的提示词")
        task = data.get("task", "t2va")
        if not isinstance(task, str) or task not in TASKS:
            raise ValueError("生成模式不支持")
        res = data.get("resolution", "480p")
        duration = data.get("duration", 5)
        aspect = data.get("aspect", "16:9")
        if not isinstance(res, str) or res not in {"480p", "768p"} or type(duration) is not int or not 5 <= duration <= 15 or not isinstance(aspect, str) or aspect not in ASPECTS:
            raise ValueError("请选择有效的分辨率、时长与画幅")
        seed = data.get("seed")
        if seed in (None, "", -1):
            seed = secrets.randbelow(2**32)
        if type(seed) is not int or not 0 <= seed <= 2**53 - 1:
            raise ValueError("种子须为 0 至 9007199254740991 的整数；留空随机")
        # 不做 CPU 卸载：transformer 常驻显存，速度优先。768p 长视频若显存不足就降回 480p。
        offload = False
        first, last = data.get("first_image", ""), data.get("last_image", "")
        if (bool(first), bool(last)) != TASKS[task]:
            raise ValueError("请按所选模式上传首尾帧")
        for value in (first, last):
            if value:
                self.upload_path(value, "image")
        refs = data.get("references", [])
        if not isinstance(refs, list) or (task == "ref2va") != bool(refs) or len(refs) > 12:
            raise ValueError("多参考模式需 1–12 个素材，其他模式不要附带参考素材")
        counts = {"image": 0, "video": 0, "audio": 0}
        cleaned = []
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("kind"), str) or ref.get("kind") not in counts:
                raise ValueError("参考素材类型无效")
            kind = ref["kind"]
            self.upload_path(ref.get("id"), kind)
            counts[kind] += 1
            cleaned.append({"kind": kind, "id": ref["id"]})
        if counts["image"] > 9 or counts["video"] > 3 or counts["audio"] > 3 or (refs and not counts["image"] + counts["video"]):
            raise ValueError("最多 9 图 / 3 视频 / 3 音频，共 12 个；至少一张图片或一个视频")
        source_video = data.get("source_video") or ""
        if source_video:
            self.upload_path(source_video, "video")
        second_pass = bool(data.get("second_pass", False)) or bool(source_video)
        refiner_backend = data.get("refiner_backend", os.getenv("PIKA_REFINER_BACKEND", "openh3"))
        if refiner_backend not in {"openh3", "pika"}:
            raise ValueError("二采后端请选择 openh3 或 pika")
        raw_sigma = data.get("start_sigma", 0.78)
        if type(raw_sigma) is bool or not isinstance(raw_sigma, (int, float)) or not 0.1 <= float(raw_sigma) <= 0.99:
            raise ValueError("二采重噪强度 start_sigma 需在 0.1–0.99 之间")
        start_sigma = round(float(raw_sigma), 4)
        raw_scale = data.get("upscale_factor", 2.0)
        if type(raw_scale) is bool or not isinstance(raw_scale, (int, float)) or not 1.2 <= float(raw_scale) <= 3.0:
            raise ValueError("二采放大倍率 upscale_factor 需在 1.2–3.0 之间")
        upscale_factor = round(float(raw_scale), 3)
        return dict(prompt=prompt.strip(), task=task, resolution=res, duration=duration,
                    aspect=aspect, seed=seed, cpu_offload=offload, first_image=first,
                    last_image=last, references=cleaned, source_video=source_video,
                    second_pass=second_pass, refiner_backend=refiner_backend,
                    start_sigma=start_sigma, upscale_factor=upscale_factor)

    def submit(self, data):
        if self.preview or not self.cli.is_file():
            raise RuntimeError("当前为本地界面预览；请在 Colab 启动官方生成服务")
        params = self.validate(data)
        # 从已有镜头提交：沿用同一个 shot，生成新 take；新建镜头才生成新 id。
        shot_id = data.get("shot_id")
        version = data.get("version")
        if not isinstance(shot_id, str) or not ID.fullmatch(shot_id):
            shot_id = secrets.token_hex(16)
        if type(version) is not int or version < 1:
            version = 1
        with self.lock:
            existing = self.jobs.get(shot_id)
            if existing and existing["status"] in {"queued", "running", "cancelling"}:
                raise ValueError("这个镜头正在生成，请先取消再提交")
            if sum(j["status"] in {"queued", "running", "cancelling"} for j in self.jobs.values()) >= 8:
                raise ValueError("队列已满，请等待或取消任务")
            job = dict(id=shot_id, version=version, created=time.time(),
                       status="queued", stage="等待 GPU", params=params)
            self.jobs[shot_id] = job
        self.wake.set()
        return job

    def output_path(self, job):
        """每个版本一个文件：outputs/<shot_id>-v<version>.mp4。"""
        return self.root / "outputs" / f"{job['id']}-v{int(job.get('version') or 1)}.mp4"

    def log_path(self, job):
        return self.root / "jobs" / f"{job['id']}-v{int(job.get('version') or 1)}.log"

    def command(self, job):
        p = job["params"]
        prompt = self.root / "jobs" / f"{job['id']}-v{int(job.get('version') or 1)}.txt"
        prompt.write_text(p["prompt"], encoding="utf-8")
        output = self.output_path(job)
        cmd = [str(self.cli.parent / "python"), str(Path(__file__).with_name("progress_runner.py")), "--task", p["task"], "--model-repo", "Pika-Labs/Pika-H3-Flex",
               "--revision", MODEL_REV, "--prompt-file", str(prompt), "--resolution", p["resolution"],
               "--duration", str(p["duration"]), "--aspect", p["aspect"], "--seed", str(p["seed"]), "--output", str(output)]
        for key, flag in (("first_image", "--first-image"), ("last_image", "--last-image")):
            if p[key]:
                cmd.extend([flag, str(self.upload_path(p[key], "image"))])
        for ref in p["references"]:
            cmd.extend(["--ref", ref["kind"] + ":" + str(self.upload_path(ref["id"], ref["kind"]))])
        if p.get("source_video"):
            cmd.extend(["--source-video", str(self.upload_path(p["source_video"], "video"))])
        if p.get("second_pass") or p.get("source_video"):
            cmd.extend(["--second-pass", "--start-sigma", str(p.get("start_sigma", 0.78)),
                        "--upscale-factor", str(p.get("upscale_factor", 2.0)),
                        "--refiner-backend", p.get("refiner_backend", "pika")])
        return cmd

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs[job_id]
            if job["status"] == "queued":
                job.update(status="cancelled", stage="已取消", finished=time.time())
            elif job["status"] == "running":
                job.update(status="cancelling", stage="正在释放进程与显存")
                proc = self.proc
                if proc and proc.poll() is None:
                    self.terminate(proc)
                    def force_stop():
                        try:
                            proc.wait(timeout=12)
                        except subprocess.TimeoutExpired:
                            self.terminate(proc, force=True)
                    threading.Thread(target=force_stop, daemon=True).start()
            self.save(job)
            return job

    @staticmethod
    def terminate(proc, force=False):
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
            elif force:
                proc.kill()
            else:
                proc.terminate()
        except ProcessLookupError:
            pass

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=3)

    def worker(self):
        while not self.stopped.is_set():
            self.wake.wait(1)
            self.wake.clear()
            with self.lock:
                pending = sorted((j for j in self.jobs.values() if j["status"] == "queued"), key=lambda j: j["created"])
                if not pending:
                    continue
                job = pending[0]
                self.active = job["id"]
                job.update(status="running", started=time.time(), stage="准备官方组件 / 下载权重")
                self.save(job)
            log = self.log_path(job)
            try:
                cmd = self.command(job)
                env = os.environ.copy()
                for key in ("PIKA_STUDIO_PASSWORD", "CF_TUNNEL_TOKEN"):
                    env.pop(key, None)
                env["PYTHONUNBUFFERED"] = "1"
                with self.lock:
                    if job["status"] == "cancelling":
                        raise InterruptedError("任务已取消")
                    self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                 text=True, encoding="utf-8", errors="replace", env=env,
                                                 start_new_session=(os.name == "posix"), bufsize=1)
                with open(log, "a", encoding="utf-8") as handle:
                    for line in self.proc.stdout:
                        handle.write(line)
                        handle.flush()
                        if line.startswith("[PIKA_PROGRESS] "):
                            try:
                                progress = json.loads(line[len("[PIKA_PROGRESS] "):])
                                with self.lock:
                                    if job["status"] == "running":
                                        job["progress"] = progress
                                        job["stage"] = progress["message"]
                                        self.save(job)
                                continue
                            except (ValueError, KeyError):
                                pass
                        lower = line.lower()
                        stage = None
                        if "[cond]" in lower:
                            stage = "编码提示词与参考素材"
                        elif "loading weights" in lower:
                            stage = "加载 Qwen3-VL 视觉语言编码器权重"
                        elif "loading checkpoint shards" in lower:
                            # 主模型是 7 个分片，VAE 解码器是 3 个（视频 2 + 音频 1），用分母区分。
                            counts = re.findall(r"(\d+)/(\d+)", line)
                            total = int(counts[-1][1]) if counts else 0
                            if total == 3:
                                stage = "加载视频与音频解码器"
                            elif total > 0:
                                stage = f"加载 Pika-H3 主模型分片（{total} 个）"
                            else:
                                stage = "加载模型分片"
                        elif "%|" in lower or "it/s" in lower or "s/it" in lower:
                            stage = "官方程序运行中（详见日志）"
                        elif "decod" in lower:
                            stage = "解码音画"
                        if stage:
                            with self.lock:
                                if job["status"] == "running":
                                    job["stage"] = stage
                    rc = self.proc.wait()
                    self.proc.stdout.close()
                with self.lock:
                    if job["status"] == "cancelling":
                        job.update(status="cancelled", stage="已取消")
                    elif rc == 0 and self.output_path(job).is_file():
                        video = self.output_path(job)
                        job.update(status="done", stage="生成完成",
                                   video_file=str(video), video_bytes=video.stat().st_size)
                        job["progress"] = {"phase": "done", "message": "生成完成"}
                    else:
                        job.update(status="failed", stage=f"官方程序退出（{rc}），请查看日志", error=self.tail(job["id"])[-6000:])
            except Exception as exc:
                with self.lock:
                    cancelled = job["status"] == "cancelling"
                    job.update(status="cancelled" if cancelled else "failed", stage="已取消" if cancelled else str(exc))
            finally:
                with self.lock:
                    job["finished"] = time.time()
                    self.save(job)
                    self.active, self.proc = None, None
                self.wake.set()

    def tail(self, job_id):
        job = self.jobs.get(job_id)
        if job:
            path = self.log_path(job)
        else:
            files = sorted((self.root / "jobs").glob(f"{job_id}-v*.log"))
            path = files[-1] if files else self.root / "jobs" / (job_id + ".log")
        if not path.is_file():
            return "等待任务启动…"
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 24000))
            return f.read().decode("utf-8", errors="replace")


class Handler(BaseHTTPRequestHandler):
    server_version = "PikaStudio/1.0"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        # Do not log cookies, prompts, bodies or passwords.
        if len(args) > 1 and str(args[1]).startswith("5"):
            super().log_message(fmt, *args)

    def json(self, obj, status=200, cookie=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self):
        if not self.app.password:
            return True
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            return self.app.valid_session(jar["pika_session"].value) if "pika_session" in jar else False
        except cookies.CookieError:
            return False

    def body(self):
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > 100000:
            raise ValueError("请求正文过大或为空")
        data = json.loads(self.rfile.read(size))
        if not isinstance(data, dict):
            raise ValueError("请求必须为 JSON 对象")
        return data

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def dispatch(self):
        try:
            self.route()
        except (ValueError, json.JSONDecodeError) as exc:
            self.json({"error": str(exc)}, 400)
        except KeyError:
            self.json({"error": "任务不存在"}, 404)
        except RuntimeError as exc:
            self.json({"error": str(exc)}, 503)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            self.json({"error": "服务内部异常，请查看 Colab 服务日志"}, 500)

    def route(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        post = self.command == "POST"
        if post and self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.json({"error": "不接受跨站提交"}, 403)
        if path == "/api/login" and post:
            now = time.time()
            ip = self.client_address[0]
            with self.app.lock:
                attempts = [t for t in self.app.failures.get(ip, []) if now-t < 60]
                if len(attempts) >= 10:
                    return self.json({"error": "尝试次数过多，请一分钟后重试"}, 429)
                password = self.body().get("password", "")
                if not isinstance(password, str) or not hmac.compare_digest(password.encode(), self.app.password.encode()):
                    self.app.failures[ip] = attempts + [now]
                    return self.json({"error": "访问密码不正确"}, 401)
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return self.json({"ok": True}, cookie=f"pika_session={self.app.session()}; Path=/; HttpOnly; SameSite=Strict; Max-Age=604800{secure}")
        if path == "/api/health":
            auth = self.authenticated()
            info = {"service": "pika-h3-flex-studio", "version": APP_VERSION, "online": True, "authenticated": auth,
                    "auth_required": bool(self.app.password)}
            if auth:
                info.update(preview=self.app.preview, engine_ready=self.app.cli.is_file() and not self.app.preview,
                            active=self.app.active, aspects=ASPECTS)
            return self.json(info)
        if path.startswith(("/api/", "/media/")):
            if not self.authenticated():
                return self.json({"error": "请先输入访问密码"}, 401)
        if path == "/api/logout" and post:
            return self.json({"ok": True}, cookie="pika_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0")
        if path == "/api/jobs" and not post:
            with self.app.lock:
                jobs = sorted(self.app.jobs.values(), key=lambda j: j["created"], reverse=True)[:100]
                return self.json({"jobs": jobs})
        if path == "/api/jobs" and post:
            return self.json(self.app.submit(self.body()), 201)
        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/(cancel|log)", path)
        if match:
            job_id, action = match.groups()
            if job_id not in self.app.jobs:
                raise KeyError(job_id)
            if action == "cancel" and post:
                return self.json(self.app.cancel(job_id))
            if action == "log" and not post:
                return self.json({"log": self.app.tail(job_id)})
        if path == "/api/uploads" and post:
            return self.upload(query)
        if path == "/api/self-update" and post:
            import urllib.request
            url = f"https://raw.githubusercontent.com/lufeihb1996/pika/main/studio/progress_runner.py?{int(time.time())}"
            req = urllib.request.Request(url, headers={"User-Agent": "PikaStudio/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                code = r.read().decode("utf-8")
            Path(__file__).with_name("progress_runner.py").write_text(code, encoding="utf-8")
            return self.json({"ok": True, "bytes": len(code)})
        match = re.fullmatch(r"/media/([0-9a-f]{32})", path)
        if match and not post:
            job = self.app.jobs.get(match[1])
            if job and job["status"] == "done":
                return self.file(self.app.output_path(job), "video/mp4", "download" in query)
            # 服务重启后内存里没有任务，但文件还在磁盘上，按版本号取最新的。
            files = sorted((self.app.root / "outputs").glob(match[1] + "-v*.mp4"))
            legacy = self.app.root / "outputs" / (match[1] + ".mp4")
            target = files[-1] if files else (legacy if legacy.is_file() else None)
            if not target:
                raise ValueError("视频尚未完成")
            return self.file(target, "video/mp4", "download" in query)
        self.json({"error": "接口不存在"}, 404)

    def upload(self, query):
        name = query.get("name", [""])[0]
        ext = Path(name).suffix.lower()
        kind = next((k for k, extensions in EXTENSIONS.items() if ext in extensions), None)
        if not kind:
            raise ValueError("不支持该文件格式")
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= MAX_UPLOAD:
            raise ValueError("单个素材需小于 200MiB，且不能为空")
        free = shutil.disk_usage(self.app.root).free
        if free < size + 5 * 1024**3:
            raise ValueError("本地磁盘余量不足，请先下载并清理旧文件")
        upload_id = secrets.token_hex(16)
        target = self.app.root / "uploads" / (upload_id + ext)
        part = target.with_suffix(".part")
        self.connection.settimeout(120)
        try:
            with part.open("wb") as out:
                remaining = size
                while remaining:
                    block = self.rfile.read(min(1024**2, remaining))
                    if not block:
                        raise ValueError("上传中断，请重试")
                    out.write(block)
                    remaining -= len(block)
            part.replace(target)
        finally:
            part.unlink(missing_ok=True)
        return self.json({"id": upload_id, "kind": kind, "name": name, "size": size}, 201)

    def file(self, path, content_type=None, download=False):
        if not path.is_file():
            return self.json({"error": "文件不存在"}, 404)
        size = path.stat().st_size
        start, end, status = 0, size-1, 200
        byte_range = self.headers.get("Range")
        if byte_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", byte_range)
            if not match or not any(match.groups()):
                return self.json({"error": "不支持该 Range"}, 416)
            a, b = match.groups()
            if not a:
                start = max(0, size-int(b))
            else:
                start = int(a)
                end = min(size-1, int(b)) if b else size-1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(end-start+1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="pika-{path.name}"')
        self.end_headers()
        with path.open("rb") as handle:
            handle.seek(start)
            left = end-start+1
            while left:
                block = handle.read(min(1024**2, left))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=os.getenv("PIKA_WORK", "/mnt/local-scratch/Pika_H3_Flex_Official"))
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    cli = root / "venv-py312" / "bin" / "pika-h3-pro"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.app = Studio(root, cli, args.preview)
    print(f"Pika Studio listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with server.app.lock:
            if server.app.proc and server.app.proc.poll() is None:
                server.app.terminate(server.app.proc)
        server.server_close()


if __name__ == "__main__":
    main()
