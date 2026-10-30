"""Bundle the local app and setup helper as one copyable Colab cell, without credentials.

每次构建把 git 版本号写进 APP_VERSION，这样 Colab 重跑启动格时能判断代码是否更新，
不会因为版本号固定为 1.3.0 而跳过重启服务。
"""
import base64
import gzip
import json
from pathlib import Path
import py_compile
import shutil
import subprocess
import time

root = Path(__file__).resolve().parent
WATCHED = ("studio", "jobstore.py", "local_media.py", "local_web.py", "colab_launcher.py")
BASE_VERSION = 'APP_VERSION = "1.3.0"'


def build_version():
    try:
        rev = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                      cwd=root, text=True).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--", *WATCHED],
                                        cwd=root, text=True).strip()
        return f"1.3.0-{rev}" + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "1.3.0-" + time.strftime("%Y%m%d-%H%M")


version = build_version()
stamp = f'APP_VERSION = "{version}"'

files = {"installer_runtime.py": (root / "installer_runtime.py").read_text(encoding="utf-8")}
# Colab 只当执行器：只带后端 .py，前端和本地数据库不再进包。
for file in sorted((root / "studio").rglob("*.py")):
    files[file.relative_to(root / "studio").as_posix()] = file.read_text(encoding="utf-8")
files["server.py"] = files["server.py"].replace(BASE_VERSION, stamp)

bundle = base64.b64encode(gzip.compress(json.dumps(files, ensure_ascii=False).encode(), mtime=0)).decode()
source = (root / "colab_launcher.py").read_text(encoding="utf-8")
source = source.replace("__BUNDLE_PLACEHOLDER__", bundle).replace(BASE_VERSION, stamp)

out = root / "dist"
out.mkdir(exist_ok=True)
target = out / "00_Colab_官方工作台_一键启动.py"
target.write_text(source, encoding="utf-8")
py_compile.compile(str(target), doraise=True)
notebook = {
    "nbformat": 4, "nbformat_minor": 5,
    "metadata": {"colab": {"name": "Pika_H3_Flex_官方网页工作台.ipynb"}, "kernelspec": {"name": "python3", "display_name": "Python 3"}},
    "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": [
            "# Pika H3 Flex 官方网页工作台\n",
            "选择 A100 80GB / 高内存运行时。若已有测试正在生成，请先等它结束。\n",
            "Colab Secrets 添加并授权 `CF_TUNNEL_TOKEN`；可选 `HF_TOKEN`。工作台无需密码。\n",
            "运行下一格，通过你的独立工作台网页连接后端。默认无需密码；已有环境与模型缓存会复用。\n",
            "Cloudflare Tunnel 服务地址应为 `http://127.0.0.1:8188`。首次模型下载发生在网页提交任务后。"
        ]},
        {"cell_type": "code", "metadata": {}, "execution_count": None, "outputs": [], "source": source.splitlines(keepends=True)}
    ]
}
(out / "01_Pika_官方网页工作台.ipynb").write_text(json.dumps(notebook, ensure_ascii=False, indent=1), encoding="utf-8")
web = root / "vercel-web"
(web / "public").mkdir(parents=True, exist_ok=True)
for f in (root / "studio" / "public").iterdir():
    if f.is_file():
        shutil.copy2(f, web / "public" / f.name)
print(f"Built {target} ({len(source.encode())/1024:.1f} KiB); {len(files)} bundled files; version {version}")
