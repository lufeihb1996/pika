"""Install the isolated, headless INT8 refiner. No server or generation is started."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import sysconfig
import urllib.request

COMFY_REV = "b1a247c9cf9dbfda4b2c0a0c1d50950667eeaa0c"
REGEN_REV = "9b686c5ffd380e9d1351f9b8beef6d20d703ca3e"
UPSCALE_REV = "40316cf008b2fd8663263270669eb4da23f89d2c"
TABLE_SHA = "c02a6c11888297688c1e6278185ea1f947023acfc69f9003bbcdcec9a229a8e7"
MODELS = [
    ("Comfy-Org/MiniMax-H3", "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", "diffusion_models"),
    ("Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "text_encoders"),
    ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_video_vae_fp16.safetensors", "vae"),
    ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors", "vae"),
    ("Efficient-Large-Model/Open-H3-Regenerate-2K", "lora/open_h3_regenerate_2k_lora.safetensors", "loras"),
    ("Efficient-Large-Model/Open-H3-Regenerate-2K", "lora/sharp_curve_branches.safetensors", "loras"),
    ("Efficient-Large-Model/Open-H3-Regenerate-2K", "vae_fast/fast_h3_vae_noncausal_24l.safetensors", "vae"),
    ("Efficient-Large-Model/Open-H3-Regenerate-2K", "latent_upsampler/minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors", "latent_upscale_models"),
]


def run(*cmd):
    subprocess.run(list(map(str, cmd)), check=True)


def checkout(url, path, revision):
    if not (path / ".git").is_dir():
        run("git", "clone", "--no-checkout", "--filter=blob:none", url, path)
    run("git", "-C", path, "fetch", "--depth", "1", "origin", revision)
    run("git", "-C", path, "checkout", "--detach", revision)


def extract_table(models):
    from safetensors.torch import load_file, save_file
    import torch
    target = models / "diffusion_models/ref2va_adaln_t_table.safetensors"
    if target.is_file():
        raw = load_file(str(target))["adaln_t_table"].contiguous().numpy().tobytes()
        if hashlib.sha256(raw).hexdigest() == TABLE_SHA:
            return
    url = "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors"

    def read_range(start, end):
        headers = {"Range": f"bytes={start}-{end}"}
        if os.getenv("HF_TOKEN"):
            headers["Authorization"] = "Bearer " + os.environ["HF_TOKEN"]
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
            if response.status != 206 or not response.headers.get("Content-Range", "").startswith(f"bytes {start}-"):
                raise RuntimeError("Model host did not honor Range; refusing a full REF2VA download")
            data = response.read(end - start + 2)
        if len(data) != end - start + 1:
            raise RuntimeError("Incomplete model byte range")
        return data

    length = struct.unpack("<Q", read_range(0, 7))[0]
    if not 0 < length < 16 * 1024**2:
        raise ValueError("Invalid safetensors header size")
    header = json.loads(read_range(8, 7 + length))
    info = header["adaln_t_table"]
    if info["dtype"] != "F32" or info["shape"] != [1025, 8]:
        raise ValueError("Unexpected reference table")
    start, end = info["data_offsets"]
    raw = read_range(8 + length + start, 8 + length + end - 1)
    if hashlib.sha256(raw).hexdigest() != TABLE_SHA:
        raise ValueError("Reference table checksum mismatch")
    temporary = target.with_suffix(".part")
    save_file({"adaln_t_table": torch.frombuffer(bytearray(raw), dtype=torch.float32).reshape(1025, 8).clone()}, str(temporary))
    temporary.replace(target)


def ensure_venv(root):
    """Repair partial venvs too: a python symlink does not imply pip exists."""
    python = root / "venv/bin/python"
    ready = python.is_file() and subprocess.run(
        [str(python), "-m", "pip", "--version"], capture_output=True).returncode == 0
    if not ready:
        bootstrap = subprocess.run([sys.executable, "-m", "ensurepip", "--version"], capture_output=True)
        if bootstrap.returncode:
            if not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("apt-get"):
                raise RuntimeError("Missing venv support; install python%d.%d-venv" % sys.version_info[:2])
            package = "python%d.%d-venv" % sys.version_info[:2]
            print(f"补齐 {package}，修复独立精修环境", flush=True)
            run("apt-get", "update", "-qq")
            run("apt-get", "install", "-y", "-qq", package)
        run(sys.executable, "-m", "venv", "--system-site-packages", root / "venv")
        run(python, "-m", "pip", "--version")
    # A nested venv does not inherit the parent venv's packages automatically.
    # Read Pika's already-installed CUDA wheels; pip installs overrides locally.
    source = Path(sysconfig.get_path("purelib"))
    destination = root / ("venv/lib/python%d.%d/site-packages" % sys.version_info[:2])
    destination.mkdir(parents=True, exist_ok=True)
    if source.resolve() != destination.resolve():
        (destination / "pika_base_packages.pth").write_text(str(source) + "\n", encoding="utf-8")
    return python


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    python = ensure_venv(root)
    comfy = root / "engine"
    marker = root / "ready.json"
    fingerprint = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if not marker.is_file() or json.loads(marker.read_text()).get("fingerprint") != fingerprint:
        checkout("https://github.com/Comfy-Org/ComfyUI.git", comfy, COMFY_REV)
        checkout("https://github.com/rdh9616/ComfyUI-Open-H3-Regenerate.git", comfy / "custom_nodes/ComfyUI-Open-H3-Regenerate", REGEN_REV)
        checkout("https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler.git", comfy / "custom_nodes/Comfyui_Minimax_h3_latent_Upscaler", UPSCALE_REV)
        run(python, "-m", "pip", "install", "-r", comfy / "requirements.txt")
        for directory in (comfy / "custom_nodes").iterdir():
            requirements = directory / "requirements.txt"
            if requirements.is_file():
                run(python, "-m", "pip", "install", "-r", requirements)
        # Run downloads with the refiner's dependencies, not the launcher's environment.
        run(python, Path(__file__), "--root", root, "--models-only")
        marker.write_text(json.dumps({"fingerprint": fingerprint}), encoding="utf-8")
    return python, comfy


def download_models(root):
    from huggingface_hub import hf_hub_download, model_info
    models = root / "engine/models"
    revisions_file = root / "model-revisions.json"
    revisions = json.loads(revisions_file.read_text()) if revisions_file.is_file() else {}
    for repo, filename, subdir in MODELS:
        dest = models / subdir
        dest.mkdir(parents=True, exist_ok=True)
        if repo not in revisions:
            revisions[repo] = model_info(repo).sha
            revisions_file.write_text(json.dumps(revisions, indent=2), encoding="utf-8")
        print(f"检查 / 下载模型: {repo}/{filename}", flush=True)
        # local_dir resumes partial downloads and checks HF metadata on subsequent setup.
        downloaded = Path(hf_hub_download(repo, filename, revision=revisions[repo], local_dir=str(root / "downloads" / repo.replace("/", "--"))))
        target = dest / Path(filename).name
        if not target.exists():
            target.symlink_to(downloaded.resolve())
    extract_table(models)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--models-only", action="store_true")
    args = parser.parse_args()
    download_models(args.root) if args.models_only else prepare(args.root)
