"""Colab 一键启动：挂 Drive → 恢复环境 → 拉代码 → 预下载模型 → 启动工作台。

Colab 里只需要跑这一行：

    import urllib.request
    exec(urllib.request.urlopen(
        "https://raw.githubusercontent.com/lufeihb1996/pika/main/colab_one.py",
        timeout=60).read().decode("utf-8"))

默认预下载 REF2VA、FL2VA 两套权重和共用组件，约 216 GB，支持全部五种模式与 1080p 二采精修。
每次检查完整文件清单，已下载的文件由 Hugging Face 缓存复用。
"""
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
UPSCALER_REPO = "LBH-123-AI/Minimax_h3_latent_Upscaler"
UPSCALER_REV = "3f941d5d182014dd5c0a5e16330420ee2d4aa0c6"


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


def ensure_download_environment():
    if pathlib.Path(PY).is_file():
        return
    # A fresh session may have no Drive backup. Install before using venv Python.
    print("没有可复用的 Python 环境，先安装官方环境...", flush=True)
    installer = SRC / "installer_runtime.py"
    namespace = {"__name__": "pika_installer", "__file__": str(installer)}
    exec(compile(installer.read_text(encoding="utf-8"), str(installer), "exec"), namespace)
    namespace["CONFIG"] = dict(namespace["CONFIG"], storage_root=str(pathlib.Path(WORK).parent))
    namespace["prepare_environment"]()


def prefetch_models():
    print("检查 / 补齐 REF2VA + FL2VA：全新下载约 216 GB；已有任一完整套装时约补 69 GB。", flush=True)
    print("共用编码器和 VAE 只下载一份；已缓存文件会复用，中断后可重跑。", flush=True)
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
                  allow_patterns=["ref2va/*", "fl2va/*", "LICENSE", "NOTICE", "licenses/*"])
snapshot_download("{BASE_REPO}", revision="{BASE_REV}",
                  allow_patterns=["transformer_ref/*", "transformer/*", "text_encoder/*", "tokenizer/*",
                                  "processor/*", "vae/*", "audio_vae/*", "*.json",
                                  "LICENSE", "NOTICE", "licenses/*"])
snapshot_download("{UPSCALER_REPO}", revision="{UPSCALER_REV}",
                  allow_patterns=["minimax_h3_latent_upscaler_3d_conv_v1/*"])
print("两套模型 + 3D 潜变量二采超分权重准备完成：REF2VA 多参考；T2VA 文生；I2VA 首帧；L2VA 尾帧；FL2VA 首尾帧；支持 1080p 二采精修。", flush=True)
'''
    subprocess.run([PY, "-c", code], env=env, check=True)


def launch():
    code = LAUNCHER.read_text(encoding="utf-8")
    exec(compile(code, str(LAUNCHER), "exec"),
         {"__name__": "__main__", "__file__": str(LAUNCHER)})


def main():
    step("挂载 Google Drive")
    mount_drive()
    step("恢复运行环境")
    restore_env()
    step("同步代码")
    sync_code()
    ensure_download_environment()
    step("准备 REF2VA + FL2VA 模型")
    prefetch_models()
    step("启动工作台")
    launch()


if __name__ == "__main__":
    main()
