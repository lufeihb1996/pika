"""Private resident refiner, behind the existing per-job CLI and queue.

Only a local Unix socket is exposed. Disconnecting the job proxy aborts the
worker's process group, preserving the queue's existing cancellation semantics.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback


def worker_version():
    digest = hashlib.sha256()
    for name in ("openh3_worker.py", "openh3_cli.py"):
        digest.update(Path(__file__).with_name(name).read_bytes())
    return digest.hexdigest()


def worker_root(output):
    return Path(os.getenv("PIKA_OPENH3_HOME", str(Path(output).resolve().parent.parent / "openh3"))).resolve()


def send(connection, data):
    connection.sendall((json.dumps(data, ensure_ascii=False) + "\n").encode("utf-8"))


def connect(root, timeout=3):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(root / "worker.sock"))
        reader = connection.makefile("r", encoding="utf-8")
        hello = json.loads(reader.readline())
        if hello.get("kind") != "hello":
            raise RuntimeError("Invalid refiner worker greeting")
        return connection, reader, hello
    except BaseException:
        connection.close()
        raise


def stop_resident(root):
    """Called before Pika is loaded. Never evict an actively serving worker."""
    if not hasattr(socket, "AF_UNIX"):
        return
    try:
        connection, reader, _ = connect(root, timeout=3)
    except (FileNotFoundError, ConnectionRefusedError):
        return
    except TimeoutError:
        raise RuntimeError("OpenH3 正在服务另一个任务，不能同时加载 Pika") from None
    try:
        send(connection, {"op": "shutdown"})
        reply = json.loads(reader.readline())
        if reply.get("kind") != "bye":
            raise RuntimeError("OpenH3 未确认释放显存")
        # The bye is sent just before process exit; wait for the owning PID too.
        deadline = time.monotonic() + 20
        pid = reply.get("pid")
        while pid and Path(f"/proc/{pid}/cmdline").exists():
            try:
                if not Path(f"/proc/{pid}/cmdline").read_bytes():
                    break  # zombie: CUDA context has already been released
            except FileNotFoundError:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("等待 OpenH3 进程释放显存超时；未启动 Pika")
            time.sleep(.1)
    finally:
        reader.close()
        connection.close()


def ensure_running(python, engine, root):
    from filelock import FileLock
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "worker-launch.lock"), timeout=60):
        try:
            connection, reader, hello = connect(root)
        except (FileNotFoundError, ConnectionRefusedError):
            hello = None
        else:
            reader.close()
            connection.close()
        if hello and hello["version"] == worker_version():
            return
        if hello:
            stop_resident(root)
        address = root / "worker.sock"
        if address.exists():
            import stat
            if not stat.S_ISSOCK(address.stat().st_mode):
                raise RuntimeError("Refusing to replace a non-socket worker path")
            address.unlink()
        with (root / "worker.log").open("ab") as log:
            process = subprocess.Popen([str(python), "-u", str(Path(__file__).resolve()),
                "--root", str(root), "--engine", str(engine)], stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
        (root / "worker.pid").write_text(str(process.pid), encoding="ascii")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"OpenH3 常驻进程启动失败，查看 {root / 'worker.log'}")
            try:
                connection, reader, _ = connect(root, timeout=1)
                reader.close()
                connection.close()
                return
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
                time.sleep(.1)
        raise RuntimeError("OpenH3 常驻进程启动超时")


def request(root, data, output=None):
    """Proxy progress to the existing server's stdout parser; no GPU imports."""
    output = output or sys.stdout
    connection, reader, hello = connect(root)
    connection.settimeout(1800)
    if hello["version"] != worker_version():
        reader.close()
        connection.close()
        raise RuntimeError("OpenH3 worker version mismatch")
    try:
        send(connection, data)
        while True:
            line = reader.readline()
            if not line:
                raise RuntimeError("OpenH3 常驻进程已退出；旧产物保留，下次任务会重新加载")
            event = json.loads(line)
            if event.get("kind") == "log":
                output.write(event["text"])
                output.flush()
            elif event.get("kind") == "result":
                if not event["ok"]:
                    raise RuntimeError(event.get("error") or "OpenH3 refinement failed")
                return event
    finally:
        reader.close()
        connection.close()


def run_resident(python, engine, root, arguments):
    ensure_running(python, engine, root)
    return request(root, {"op": "refine", "args": arguments})


def abort_worker():
    # The daemon owns its own session; also terminate a live FFmpeg child.
    if os.name == "posix" and os.getpgrp() == os.getpid():
        os.killpg(os.getpgrp(), signal.SIGTERM)
    os._exit(85)


class JobOutput:
    """One stable stream for logging handlers, with a different client per job."""
    def __init__(self, fallback):
        self.fallback = fallback
        self.connection = None
        self.lock = threading.Lock()
        self.encoding = "utf-8"
        self.errors = "replace"

    def write(self, text):
        if not text:
            return 0
        with self.lock:
            if self.connection is None:
                self.fallback.write(text)
                self.fallback.flush()
            else:
                try:
                    send(self.connection, {"kind": "log", "text": text})
                except (BrokenPipeError, ConnectionResetError, OSError):
                    abort_worker()
        return len(text)

    def flush(self):
        self.fallback.flush()

    def isatty(self):
        return False

    def fileno(self):
        return self.fallback.fileno()


def watch_disconnect(connection, finished):
    while not finished.wait(.2):
        try:
            ready, _, _ = select.select([connection], [], [], .2)
            if ready and connection.recv(1, socket.MSG_PEEK) == b"":
                if not finished.is_set():
                    abort_worker()
                return
        except (OSError, ValueError):
            if not finished.is_set():
                abort_worker()
            return


def serve(root, engine, refine_fn=None, ready=None):
    """Serial worker. refine_fn is injectable for GPU-free protocol tests."""
    if refine_fn is None:
        from openh3_cli import refine as refine_fn
    address = root / "worker.sock"
    root.mkdir(parents=True, exist_ok=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(address))
    os.chmod(address, 0o600)
    listener.listen(4)
    version = worker_version()
    stream = JobOutput(sys.__stderr__)
    if ready:
        ready.set()
    try:
        while True:
            connection, _ = listener.accept()
            with connection:
                send(connection, {"kind": "hello", "pid": os.getpid(), "version": version})
                with connection.makefile("r", encoding="utf-8") as reader:
                    line = reader.readline(128 * 1024)
                    if not line:
                        continue
                    payload = json.loads(line)
                if payload.get("op") == "shutdown":
                    send(connection, {"kind": "bye", "pid": os.getpid()})
                    return
                if payload.get("op") not in {"refine", "warm"}:
                    send(connection, {"kind": "result", "ok": False, "error": "Unknown worker request"})
                    continue
                allowed = {"source", "output", "prompt_file", "resolution", "start_sigma", "seed"}
                warm = payload["op"] == "warm"
                values = dict(source=str(root/"warm.source"), output=str(root/"warm.output"),
                    prompt_file=str(root/"warm.prompt"), resolution="1080p", start_sigma=.78, seed=0) if warm else payload.get("args", {})
                if set(values) != allowed:
                    send(connection, {"kind": "result", "ok": False, "error": "Invalid refiner arguments"})
                    continue
                args = argparse.Namespace(engine=engine, warm_only=warm, **values)
                for name in ("source", "output", "prompt_file"):
                    setattr(args, name, Path(getattr(args, name)))
                finished = threading.Event()
                monitor = threading.Thread(target=watch_disconnect, args=(connection, finished), daemon=True)
                stream.connection = connection
                original_out, original_err = sys.stdout, sys.stderr
                sys.stdout = sys.stderr = stream
                monitor.start()
                failure = None
                try:
                    refine_fn(args)
                except Exception as exc:
                    traceback.print_exc()
                    failure = str(exc)
                finally:
                    finished.set()
                    monitor.join(timeout=1)
                    sys.stdout, sys.stderr = original_out, original_err
                    stream.connection = None
                send(connection, {"kind": "result", "ok": failure is None, "error": failure,
                                  "pid": os.getpid()})
                if failure is not None:
                    return  # avoid carrying a failed CUDA context into another job
    finally:
        listener.close()
        address.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    args = parser.parse_args()
    serve(args.root.resolve(), args.engine.resolve())
