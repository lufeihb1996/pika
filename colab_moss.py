"""只下载 MOSS 生图权重；不启动服务，不安装或修改 PyTorch。

可在 Colab 单独 exec 本文件，也由 colab_one.py 自动调用。
MOSS_MODELS_DIR 可指定 ComfyUI 的 models 目录；默认放在 Pika 的 scratch 盘。
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys


MOSS_REPO = "Comfy-Org/MOSS-Image"
MOSS_REV = "530a4c94f92c35ef6818b1b035a983fe9dcf5f54"
# 与原三合一脚本使用相同的 SFT INT8、Qwen3.5-9B INT8、FLUX.2 VAE。
MOSS_FILES = (
    ("diffusion_models/moss_image_sft_int8_convrot.safetensors", 5413698958),
    ("text_encoders/qwen3.5_9b_int8_convrot.safetensors", 9748121532),
    ("vae/flux2-vae.safetensors", 336211292),
)


def default_models_dir():
    if Path("/mnt/local-scratch").is_dir():
        return Path("/mnt/local-scratch/Pika_H3_Flex_Official/comfy-models")
    return Path("/content/ComfyUI/models")


def download_moss(models_dir=None):
    root = Path(models_dir or os.environ.get("MOSS_MODELS_DIR") or default_models_dir())
    root.mkdir(parents=True, exist_ok=True)
    # 原三合一脚本中的 Qwen 权重走 HTTP，避免 Xet 冷门分块重建卡住。
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    if importlib.util.find_spec("huggingface_hub") is None:
        subprocess.run([sys.executable, "-m", "pip", "install", "huggingface_hub>=0.34,<2"], check=True)
    from huggingface_hub import constants, hf_hub_download
    # 单独 exec 时，notebook 可能已经导入过 huggingface_hub。
    constants.HF_HUB_DISABLE_XET = True

    token = os.environ.get("HF_TOKEN")
    if not token:
        try:
            from google.colab import userdata
            token = userdata.get("HF_TOKEN")
        except Exception:
            pass
    total = sum(size for _, size in MOSS_FILES)
    print(f"MOSS 生图三件套：{total / 10**9:.2f} GB，目录：{root}", flush=True)
    for filename, expected in MOSS_FILES:
        print(f"检查 / 补齐：{filename}", flush=True)
        downloaded = Path(hf_hub_download(
            repo_id=MOSS_REPO, revision=MOSS_REV, filename=filename,
            local_dir=str(root), token=token or None,
        ))
        if downloaded.stat().st_size != expected:
            # 旧文件或损坏的本地缓存不能只因“文件存在”就视为下载完成。
            downloaded = Path(hf_hub_download(
                repo_id=MOSS_REPO, revision=MOSS_REV, filename=filename,
                local_dir=str(root), token=token or None, force_download=True,
            ))
        if downloaded.stat().st_size != expected:
            raise RuntimeError(f"MOSS 文件不完整：{filename}；请重跑下载脚本。")
        print(f"已就绪：{filename} ({expected / 10**9:.2f} GB)", flush=True)
    print("MOSS 权重准备完成。生图需要另行启动支持 MOSS 的 ComfyUI；Pika 服务只负责视频。", flush=True)
    return root


if __name__ == "__main__":
    download_moss()
