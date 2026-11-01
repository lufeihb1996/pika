"""Headless INT8 OpenH3 runner; imports model libraries but starts no HTTP server."""
import argparse
from contextlib import contextmanager
import gc
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time


def emit(phase, message, **data):
    print("[PIKA_PROGRESS] " + json.dumps(dict(phase=phase, message=message, **data), ensure_ascii=False), flush=True)


@contextmanager
def stage(phase, message):
    started = time.monotonic()
    emit(phase, message)
    yield
    emit(phase, message + " · 完成", stage_seconds=round(time.monotonic() - started, 3))


def target_size(width, height, resolution):
    """Keep aspect; labels describe pixel budgets, not a width multiplier."""
    budgets = {"720p": 1280 * 720, "1080p": 1920 * 1080, "1.5k": 2304 * 1296, "2k": 2560 * 1440}
    if width <= 0 or height <= 0:
        raise ValueError("Video dimensions must be positive")
    scale = math.sqrt(budgets[resolution] / (width * height))
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def resolution_for_scale(scale):
    return "720p" if scale <= 1.6 else "1080p" if scale <= 2.1 else "1.5k" if scale <= 2.35 else "2k"


def run_job(clean, source, sigma, scale):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--prompt", default="")
    parser.add_argument("--seed", type=int, default=42)
    args, _ = parser.parse_known_args(clean)
    output = args.output.resolve()
    if source and not Path(source).is_file():
        raise FileNotFoundError(source)
    if source and Path(source).resolve() == output:
        raise ValueError("Refinement output must differ from the source video")
    root = Path(os.getenv("PIKA_OPENH3_HOME", str(output.parent.parent / "openh3"))).resolve()
    from openh3_setup import prepare
    with stage("assets", "准备独立 OpenH3 INT8 精修环境与模型"):
        python, comfy = prepare(root)
    if source:
        source = Path(source).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
    else:
        source = output.with_name(output.stem + ".pass1.mp4")
        argv = list(clean)
        argv[argv.index("--output") + 1] = str(source)
        with stage("starting", "Pika CLI 一采，完成后退出并释放显存"):
            subprocess.run([sys.executable, str(Path(__file__).with_name("progress_runner.py")), *argv], check=True,
                           env=dict(os.environ, PIKA_REFINER_BACKEND="pika"))
    prompt = args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else args.prompt
    output.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = output.with_suffix(".refine.txt")
    prompt_path.write_text(prompt, encoding="utf-8")
    subprocess.run([str(python), str(Path(__file__).resolve()), "--engine", str(comfy), "--source", str(source),
                    "--output", str(output), "--prompt-file", str(prompt_path), "--seed", str(args.seed),
                    "--resolution", resolution_for_scale(scale), "--start-sigma", str(sigma)], check=True)


def refine(args):
    args.engine = args.engine.resolve()
    args.source = args.source.resolve()
    args.output = args.output.resolve()
    args.prompt_file = args.prompt_file.resolve()
    if args.source == args.output:
        raise ValueError("Refinement output must differ from the source video")
    if not 0.1 <= args.start_sigma <= 0.99:
        raise ValueError("start_sigma must be between 0.1 and 0.99")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    os.chdir(args.engine)
    sys.path.insert(0, str(args.engine.resolve()))
    # Comfy's argument parser must not see this CLI's arguments.
    sys.argv = [sys.argv[0], "--disable-auto-launch", "--reserve-vram", "2"]
    import asyncio
    import numpy as np
    import torch
    import comfy_kitchen as kitchen
    if kitchen.int8_attention_is_available():
        sys.argv += ["--use-ck-attention", "--fast", "fp16_accumulation"]
        emit("starting", "OpenH3 启用 Kitchen INT8 attention 与 FP16 accumulation")
    import av
    import nodes
    import comfy.model_management as mm
    import comfy.samplers
    import comfy.utils
    from comfy_extras import nodes_custom_sampler as samplers

    async def load_plugins():
        for name in ("Comfyui_Minimax_h3_latent_Upscaler", "ComfyUI-Open-H3-Regenerate"):
            if not await nodes.load_custom_node(str(args.engine / "custom_nodes" / name)):
                raise RuntimeError(f"Failed to load headless refiner library: {name}")
    asyncio.run(load_plugins())
    regen = sys.modules[nodes.NODE_CLASS_MAPPINGS["OpenH3RegenLora"].__module__]
    # Upstream table inversion accepts rows; spatial conditioning can have leading axes.
    original_invert = regen.core.invert_table
    def invert(table, coords):
        shape = coords.shape[:-1]
        unique, inverse = torch.unique(coords.reshape(-1, coords.shape[-1]), dim=0, return_inverse=True)
        parts, error = [], 0.0
        for block in unique.split(128):
            values, distance = original_invert(table, block)
            parts.append(values)
            error = max(error, distance)
        return torch.cat(parts)[inverse].reshape(shape), error
    regen.core.invert_table = invert

    def result(value):
        return value.result if hasattr(value, "result") else value

    with torch.inference_mode():
        with stage("conditioning", "读取原片并准备 24fps 精修输入"):
            with av.open(str(args.source)) as container:
                stream = container.streams.video[0]
                rate = float(stream.average_rate or stream.guessed_rate or 24)
                if stream.duration and float(stream.duration * stream.time_base) > 15.3:
                    raise ValueError("OpenH3 单次精修支持约 15 秒；请先拆分长视频")
                images = torch.from_numpy(np.stack([frame.to_ndarray(format="rgb24") for frame in container.decode(video=0)])).float() / 255
            # Keep source composition; the old fixed 16:9 draft preset would crop other aspects.
            images, count, width, height = regen.OpenH3RegenPrepareDraft().prepare(images, "keep (snap to 32)", 0, rate)
            if count > 365:
                raise ValueError("OpenH3 单次精修支持约 15 秒；请先拆分长视频")
            width_out, height_out = target_size(width, height, args.resolution)
            compute_width = math.ceil(width_out / 32) * 32
            compute_height = math.ceil(height_out / 32) * 32
            emit("conditioning", f"OpenH3 输入 {width}×{height} → 输出 {width_out}×{height_out}，{count}帧",
                 input_width=width, input_height=height, output_width=width_out, output_height=height_out, frames=count)
            vae = nodes.VAELoader().load_vae("minimax_h3_video_vae_fp16.safetensors")[0]
            latent = nodes.VAEEncode().encode(vae, images)[0]
            del images, vae
            mm.unload_all_models()
            gc.collect()
            mm.soft_empty_cache()
        with stage("conditioning", "INT8 Qwen 编码精修提示词"):
            clip = nodes.CLIPLoader().load_clip("qwen3vl_32b_minimax_h3_int8_convrot.safetensors", "minimax")[0]
            cond = nodes.CLIPTextEncode().encode(clip, args.prompt_file.read_text(encoding="utf-8"))[0]
            del clip
            mm.unload_all_models()
            gc.collect()
            mm.soft_empty_cache()
        with stage("upscaling", "OpenH3 3D 潜变量放大"):
            cls = nodes.NODE_CLASS_MAPPINGS["MinimaxH3LatentUpscaler3D"]
            mode = sys.modules[cls.__module__].UpscaleMode.TARGET_DIMENSIONS
            latent = result(cls.execute(latent, "minimax_h3_latent_upscaler_3d_conv_v1_bf16.safetensors",
                                       {"mode": mode, "width": compute_width, "height": compute_height}, align=32,
                                       enable_temporal_chunking=True, force_unload=True, device="cuda", precision="bf16"))[0]
            audio_vae = nodes.VAELoader().load_vae("minimax_h3_audio_vae_fp32.safetensors")[0]
            latent = regen.OpenH3RegenRefineLatent().build(latent, regen.AUDIO_MODES[0], audio_vae=audio_vae)[0]
            del audio_vae
            mm.unload_all_models()
        with stage("loading", "加载独立 FL2VA INT8 + OpenH3 Regenerate + Sharp"):
            model = nodes.UNETLoader().load_unet("minimax_h3_fl2va_pruned_int8_convrot.safetensors", "default")[0]
            model = regen.OpenH3RegenLora().apply(model, "open_h3_regenerate_2k_lora.safetensors",
                        "sharp_curve_branches.safetensors", "ref2va_adaln_t_table.safetensors", 1.0, 0.65, "bf16")[0]
            sigmas = regen.OpenH3RegenSigmas().sigmas(3, args.start_sigma)[0]
            guider = result(samplers.BasicGuider.execute(model, cond))[0]
            noise = result(samplers.RandomNoise.execute(args.seed))[0]
        started = time.monotonic()
        def progress(current, total, preview=None, **kwargs):
            torch.cuda.synchronize()
            emit("sampling", f"OpenH3 INT8 精修 {current}/{total}", completed=current, total=total,
                 sampling_seconds=round(time.monotonic() - started, 2))
        comfy.utils.set_progress_bar_global_hook(progress)
        emit("sampling", "OpenH3 INT8 · Euler 三步精修", completed=0, total=3)
        refined = result(samplers.SamplerCustomAdvanced.execute(noise, guider, comfy.samplers.sampler_object("euler"), sigmas, latent))[0]
        del guider, model, noise, latent, cond
        mm.unload_all_models()
        gc.collect()
        mm.soft_empty_cache()
        with stage("decoding", "OpenH3 Fast VAE 解码"):
            pixels = regen.OpenH3RegenFastVAEDecode().decode(refined, "fast_h3_vae_noncausal_24l.safetensors", False)[0][:count]
            y, x = (pixels.shape[1] - height_out) // 2, (pixels.shape[2] - width_out) // 2
            if y < 0 or x < 0 or len(pixels) != count:
                raise RuntimeError("Fast VAE returned unexpected dimensions or frame count")
            pixels = pixels[:, y:y + height_out, x:x + width_out, :3]
        with stage("encoding", "编码精修画面并接回原片音轨"):
            temporary = args.output.with_name(args.output.stem + ".refining.mp4")
            cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width_out}x{height_out}",
                   "-r", "24", "-i", "pipe:0", "-i", str(args.source), "-map", "0:v:0", "-map", "1:a:0?",
                   "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-c:a", "copy", "-t", str(count / 24),
                   "-movflags", "+faststart", str(temporary)]
            process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            try:
                for frame in pixels:
                    process.stdin.write((frame.clamp(0, 1).cpu().numpy() * 255).round().astype(np.uint8).tobytes())
                process.stdin.close()
                if process.wait() != 0:
                    raise RuntimeError("FFmpeg failed to encode refinement")
                temporary.replace(args.output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                temporary.unlink(missing_ok=True)
        args.output.with_suffix(".json").write_text(json.dumps(dict(backend="openh3_int8", width=width_out, height=height_out,
              frames=count, steps=3, start_sigma=args.start_sigma, seed=args.seed), indent=2), encoding="utf-8")
        emit("done", "OpenH3 INT8 精修完成")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--resolution", choices=("720p", "1080p", "1.5k", "2k"), default="1080p")
    parser.add_argument("--start-sigma", type=float, default=0.78)
    parser.add_argument("--seed", type=int, default=42)
    refine(parser.parse_args())
