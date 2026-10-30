"""Colab 一键启动：挂 Drive → 恢复环境 → 拉代码 → 预下载模型 → 启动工作台。

Colab 里只需要跑这一行：

    import urllib.request
    exec(urllib.request.urlopen(
        "https://raw.githubusercontent.com/lufeihb1996/pika/main/colab_one.py",
        timeout=60).read().decode("utf-8"))

启动器本身不下载模型（怕没确认就吃掉 141 GiB），这里补上：模型已存在就跳过。
"""
import glob
import os
import pathlib
import shutil
import subprocess
import sys

WORK = "/mnt/local-scratch/Pika_H3_Flex_Official"
BACKUP = "/content/drive/MyDrive/ComfyUI_Models/Pika_H3_Flex/env.tar.gz"
REPO = "https://github.com/lufeihb1996/pika.git"
SRC = pathlib.Path("/content/pika-src")
PY = f"{WORK}/venv-py312/bin/python"
LAUNCHER = SRC / "dist" / "00_Colab_官方工作台_一键启动.py"

ADAPTER_REV = "0ddf55ad7067ca1e76812253bd7b4600df87f759"
BASE_REPO = "MiniMaxAI/MiniMax-H3"
BASE_REV = "42ed227ee7df40d41602854ae760620d6eb651fe"


def step(title):
    print(f"\n=== {title} ===", flush=True)


def mount_drive():
    if os.path.isdir("/content/drive/MyDrive"):
        print("Drive 已挂载")
        return
    from google.colab import drive
    drive.mount("/content/drive")


def restore_env():
    if os.path.exists(f"{WORK}/environment-ready.json"):
        print("环境已在本地，跳过恢复")
        return
    if not os.path.exists(BACKUP):
        print(f"没有环境备份（{BACKUP}），启动器会重新安装")
        return
    os.makedirs(WORK, exist_ok=True)
    print("解压环境备份，约 1~3 分钟...")
    subprocess.run(f"tar -C {WORK} -xzf {BACKUP}", shell=True, check=True)
    print("环境已恢复")


def sync_code():
    if (SRC / ".git").is_dir():
        subprocess.run(["git", "-C", str(SRC), "fetch", "--depth", "1", "origin", "main"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(SRC), "reset", "--hard", "origin/main"],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        if SRC.exists():
            shutil.rmtree(SRC)
        subprocess.run(["git", "clone", "--depth", "1", REPO, str(SRC)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rev = subprocess.check_output(["git", "-C", str(SRC), "rev-parse", "--short", "HEAD"],
                                  text=True).strip()
    print(f"源码版本 {rev}")
    if not LAUNCHER.is_file():
        subprocess.run([sys.executable, "build_colab.py"], cwd=str(SRC), check=True)


def model_ready():
    return bool(glob.glob(
        f"{WORK}/hf/hub/models--{BASE_REPO.replace('/', '--')}/snapshots/*/transformer/*.safetensors"))


def prefetch_models():
    if model_ready():
        print("模型已在本地，跳过下载")
        return
    print("下载模型约 141 GiB，30~40 分钟，别关页面...", flush=True)
    env = dict(os.environ,
               HF_HOME=f"{WORK}/hf", HF_HUB_CACHE=f"{WORK}/hf/hub",
               HF_XET_CACHE=f"{WORK}/hf/xet")
    try:
        from google.colab import userdata
        token = userdata.get("HF_TOKEN")
        if token:
            env["HF_TOKEN"] = token
    except Exception:
        pass
    code = f'''
from huggingface_hub import snapshot_download
snapshot_download("Pika-Labs/Pika-H3-Flex", revision="{ADAPTER_REV}",
                  allow_patterns=["fl2va/*", "LICENSE", "NOTICE", "licenses/*"])
snapshot_download("{BASE_REPO}", revision="{BASE_REV}",
                  allow_patterns=["transformer/*", "text_encoder/*", "tokenizer/*",
                                  "processor/*", "vae/*", "audio_vae/*", "*.json",
                                  "LICENSE", "NOTICE", "licenses/*"])
print("模型下载完成", flush=True)
'''
    subprocess.run([PY, "-c", code], env=env, check=True)


def launch():
    code = LAUNCHER.read_text(encoding="utf-8")
    exec(compile(code, str(LAUNCHER), "exec"),
         {"__name__": "__main__", "__file__": str(LAUNCHER)})


step("挂载 Google Drive")
mount_drive()
step("恢复运行环境")
restore_env()
step("同步代码")
sync_code()
step("准备模型")
prefetch_models()
step("启动工作台")
launch()
