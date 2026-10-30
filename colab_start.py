"""Pika H3 Flex 工作台启动格：从 GitHub 拉最新源码、构建、启动。

用法：把本文件全部内容粘进 Colab 一格运行。
之后本地改完代码只要 git push，回 Colab 重跑这一格即可，不用再复制几万字符的打包。
启动器会读 APP_VERSION：源码有更新就重启服务；没有更新就只确保隧道在线。
"""
import pathlib
import subprocess
import sys

REPO = "https://github.com/lufeihb1996/pika.git"
SRC = pathlib.Path("/content/pika-src")
LAUNCHER = SRC / "dist" / "00_Colab_官方工作台_一键启动.py"


def run(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


if not (SRC / ".git").is_dir():
    print(f"克隆 {REPO} → {SRC}")
    run(["git", "clone", "--depth", "1", REPO, str(SRC)])
else:
    run(["git", "-C", str(SRC), "fetch", "--depth", "1", "origin", "main"])
    run(["git", "-C", str(SRC), "reset", "--hard", "origin/main"])

rev = subprocess.check_output(["git", "-C", str(SRC), "rev-parse", "--short", "HEAD"],
                              text=True).strip()
print(f"源码版本 {rev}")

run([sys.executable, "build_colab.py"], cwd=str(SRC))

code = LAUNCHER.read_text(encoding="utf-8")
exec(compile(code, str(LAUNCHER), "exec"), {"__name__": "__main__", "__file__": str(LAUNCHER)})
