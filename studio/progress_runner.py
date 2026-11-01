"""Observe the official pipeline without editing weights, schedules or its source, plus Pass-2 1080p refinement."""
import functools
import gc
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import types

PREFIX = "[PIKA_PROGRESS] "
# 官方在写完 MP4 后会把整个文件解码一遍数帧数，只做校验、不产出任何东西。
# 在内存里跳过这一行（不修改磁盘上的官方源码），实测省 12~30 秒。
VERIFY_MARKER = "if sum(1 for _ in encoded.decode(video=0)) != frames:"

UPSCALER_REPO = "LBH-123-AI/Minimax_h3_latent_Upscaler"
UPSCALER_REV = "3f941d5d182014dd5c0a5e16330420ee2d4aa0c6"
UPSCALER_FILE = "minimax_h3_latent_upscaler_3d_conv_v1/minimax_h3_latent_upscaler_3d_conv_v1_fp16.safetensors"


def emit(phase, message, **values):
    print(PREFIX + json.dumps(dict(phase=phase, message=message, **values), ensure_ascii=False), flush=True)


def stage(function, phase, message):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        emit(phase, message)
        return function(*args, **kwargs)
    return wrapped


def sampling_observer(function, synchronize, capture=None):
    @functools.wraps(function)
    def wrapped(generator, *args, **kwargs):
        total = len(kwargs["sigmas_v"]) - 1
        count = 0
        started = time.monotonic()
        emit("sampling", "开始官方采样", completed=0, total=total)

        def completed_step(module, inputs, output):
            nonlocal count
            # Report completion after GPU work finishes, not merely CPU dispatch.
            synchronize()
            count += 1
            elapsed = time.monotonic() - started
            emit("sampling", f"采样 {count}/{total}", completed=count, total=total,
                 sampling_seconds=round(elapsed, 2),
                 sampling_eta_seconds=round(elapsed / count * (total-count), 1))

        handle = generator.register_forward_hook(completed_step)
        try:
            result = function(generator, *args, **kwargs)
            if isinstance(capture, dict) and isinstance(result, tuple) and len(result) == 2:
                capture["video"] = result[0].detach().cpu()
                capture["audio"] = result[1].detach().cpu()
            return result
        finally:
            handle.remove()
    return wrapped


def load_pipeline_without_output_verify():
    """在内存里替换 pipeline 源码，跳过回读校验那一行。

    只影响本进程的模块对象，磁盘上的官方代码不变，git status 不会变脏。
    找不到标记时返回 False，照常跑官方原版。
    """
    import importlib.util
    spec = importlib.util.find_spec("pika_h3.pipeline")
    if spec is None or not spec.origin:
        return False
    source = Path(spec.origin).read_text(encoding="utf-8")
    if VERIFY_MARKER not in source:
        return False
    patched = source.replace(VERIFY_MARKER, "if False:  # 回读校验已在内存中跳过")
    module = types.ModuleType("pika_h3.pipeline")
    module.__file__ = spec.origin
    module.__package__ = "pika_h3"
    exec(compile(patched, spec.origin, "exec"), module.__dict__)
    sys.modules["pika_h3.pipeline"] = module
    return True


def file_sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def latent_cache_path(output_path, video_path):
    folder = Path(output_path).resolve().parent / ".latents"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{file_sha256(video_path)}.pt"


def save_latent_cache(output_path, video_latent, audio_latent):
    import torch
    output_path = Path(output_path)
    if not output_path.is_file() or video_latent is None or audio_latent is None:
        return
    try:
        cache_file = latent_cache_path(output_path, output_path)
        v = video_latent[0] if video_latent.ndim == 5 else video_latent
        a = audio_latent[0] if audio_latent.ndim == 3 else audio_latent
        torch.save({"video": v.detach().to(torch.float16).cpu(),
                    "audio": a.detach().to(torch.float32).cpu()}, cache_file)
    except Exception as exc:
        print(f"[latent-cache] skip saving cache: {exc}", flush=True)


def build_refine_ladder(start_sigma=0.78, video_shift=12.0, audio_shift=3.0):
    """Build the 3-step Pika H3 consistency re-noising ladder starting at start_sigma."""
    start_sigma = max(0.1, min(0.99, float(start_sigma)))
    q0 = start_sigma / (video_shift - (video_shift - 1.0) * start_sigma)
    knot0 = q0 * 1000.0
    tail = [k for k in (250.0, 100.0, 40.0, 0.0) if k < knot0 - 5.0]
    if not tail or tail[-1] != 0.0:
        tail.append(0.0)
    knots = [knot0] + tail
    base = [k / 1000.0 for k in knots]
    sigmas_v = [(video_shift * s) / (1.0 + (video_shift - 1.0) * s) if s > 0 else 0.0 for s in base]
    sigmas_a = [(audio_shift * s) / (1.0 + (audio_shift - 1.0) * s) if s > 0 else 0.0 for s in base]
    return sigmas_v, sigmas_a


def extract_pass2_args(argv):
    """Strip --second-pass, --source-video, --start-sigma, --upscale-factor from argv before official CLI parser."""
    clean = []
    second_pass = False
    source_video = ""
    start_sigma = 0.78
    upscale_factor = 2.0
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--second-pass":
            second_pass = True
            i += 1
        elif arg == "--source-video" and i + 1 < len(argv):
            source_video = argv[i + 1]
            second_pass = True
            i += 2
        elif arg == "--start-sigma" and i + 1 < len(argv):
            start_sigma = float(argv[i + 1])
            i += 2
        elif arg == "--upscale-factor" and i + 1 < len(argv):
            upscale_factor = float(argv[i + 1])
            i += 2
        else:
            clean.append(arg)
            i += 1
    return clean, second_pass, source_video, start_sigma, upscale_factor


def _detect_upscaler_arch(sd):
    mid_ch = int(sd["in_conv.weight"].shape[0])
    in_ch = int(sd["in_conv.weight"].shape[1])
    block_ids = set()
    has_temporal_conv = False
    for k in sd.keys():
        if k.startswith("blocks."):
            parts = k.split(".")
            if parts[1].isdigit():
                block_ids.add(int(parts[1]))
            if "temporal.conv." in k:
                has_temporal_conv = True
    return {
        "channels": in_ch,
        "mid_channels": mid_ch,
        "num_blocks": (max(block_ids) + 1) if block_ids else 6,
        "temporal_mode": "conv" if has_temporal_conv else "none",
        "has_attn": any(k.startswith("attn.") for k in sd.keys()),
    }


def _build_latent_resizer_3d(sd):
    import torch
    import torch.nn as nn
    import torch.nn.functional as F

    arch = _detect_upscaler_arch(sd)

    class AttnBlock3D(nn.Module):
        def __init__(self, channels, num_heads=8):
            super().__init__()
            self.norm = nn.GroupNorm(32, channels)
            self.num_heads = num_heads
            self.head_dim = channels // num_heads
            self.qkv = nn.Conv3d(channels, channels * 3, 1)
            self.proj = nn.Conv3d(channels, channels, 1)

        def forward(self, x):
            B, C, T, H, W = x.shape
            h = self.norm(x)
            qkv = self.qkv(h).reshape(B, 3, self.num_heads, self.head_dim, T * H * W)
            q, k, v = qkv[:, 0], qkv[:, 1], qkv[:, 2]
            q = q.permute(0, 1, 3, 2)
            k = k.permute(0, 1, 3, 2)
            v = v.permute(0, 1, 3, 2)
            attn = F.scaled_dot_product_attention(q, k, v)
            attn = attn.permute(0, 1, 3, 2).reshape(B, C, T, H, W)
            return x + self.proj(attn)

    class TemporalConv(nn.Module):
        def __init__(self, channels):
            super().__init__()
            self.norm = nn.GroupNorm(32, channels)
            self.conv = nn.Sequential(
                nn.SiLU(),
                nn.Conv3d(channels, channels, kernel_size=(3, 1, 1), padding=(1, 0, 0), groups=channels),
                nn.SiLU(),
                nn.Conv3d(channels, channels, kernel_size=1),
            )

        def forward(self, x):
            return x + self.conv(self.norm(x))

    class ResBlockEmb3D(nn.Module):
        def __init__(self, channels, emb_dim, temporal_mode="conv"):
            super().__init__()
            self.norm1 = nn.GroupNorm(32, channels)
            self.conv1 = nn.Conv3d(channels, channels, 3, padding=1)
            self.norm2 = nn.GroupNorm(32, channels)
            self.conv2 = nn.Conv3d(channels, channels, 3, padding=1)
            self.emb_proj = nn.Sequential(nn.SiLU(), nn.Linear(emb_dim, channels * 2))
            self.act = nn.SiLU()
            self.temporal = TemporalConv(channels) if temporal_mode == "conv" else None

        def forward(self, x, emb):
            h = self.conv1(self.act(self.norm1(x)))
            emb_out = self.emb_proj(emb)[:, :, None, None, None]
            scale, shift = emb_out.chunk(2, dim=1)
            h = self.norm2(h) * (1 + scale) + shift
            h = x + self.conv2(self.act(h))
            if self.temporal is not None:
                h = self.temporal(h)
            return h

    class LatentResizer3D(nn.Module):
        def __init__(self, channels=24, mid_channels=512, num_blocks=6, temporal_mode="conv", has_attn=True):
            super().__init__()
            emb_dim = mid_channels // 4
            self.scale_mlp = nn.Sequential(
                nn.Linear(1, emb_dim),
                nn.SiLU(),
                nn.Linear(emb_dim, emb_dim),
            )
            self.in_conv = nn.Conv3d(channels, mid_channels, 3, padding=1)
            self.blocks = nn.ModuleList([
                ResBlockEmb3D(mid_channels, emb_dim, temporal_mode=temporal_mode) for _ in range(num_blocks)
            ])
            self.attn = AttnBlock3D(mid_channels, num_heads=8) if has_attn else None
            self.mid_idx = num_blocks // 2
            self.out_norm = nn.GroupNorm(32, mid_channels)
            self.out_conv = nn.Conv3d(mid_channels, channels, 3, padding=1)
            self.act = nn.SiLU()
            self.residual_gain = nn.Parameter(torch.ones(1))

        def _forward_core(self, x_up, scale_emb):
            h = self.in_conv(x_up)
            for i, block in enumerate(self.blocks):
                h = block(h, scale_emb)
                if self.attn is not None and i == self.mid_idx - 1:
                    h = self.attn(h)
            return x_up + self.residual_gain * self.out_conv(self.act(self.out_norm(h)))

        def forward(self, x, scale=2.0, target_size=None, chunk_size=8, chunk_overlap=2):
            B, C, T, H, W = x.shape
            if target_size is None:
                target_size = (T, int(round(H * scale)), int(round(W * scale)))
            x_up = F.interpolate(x, size=target_size, mode="trilinear", align_corners=False)
            scale_t = torch.tensor([[scale]], device=x.device, dtype=x.dtype).expand(B, 1)
            scale_emb = self.scale_mlp(scale_t)
            if T <= chunk_size:
                return self._forward_core(x_up, scale_emb)
            step = chunk_size - chunk_overlap
            out = torch.zeros_like(x_up)
            weight = torch.zeros((1, 1, T, 1, 1), device=x.device, dtype=x.dtype)
            for start in range(0, T, step):
                end = min(start + chunk_size, T)
                chunk_len = end - start
                pred = self._forward_core(x_up[:, :, start:end], scale_emb)
                w = torch.ones((1, 1, chunk_len, 1, 1), device=x.device, dtype=x.dtype)
                if start > 0 and chunk_overlap > 0:
                    ramp_len = min(chunk_overlap, chunk_len)
                    ramp = torch.linspace(0, 1, ramp_len + 2, device=x.device, dtype=x.dtype)[1:-1]
                    w[:, :, :ramp_len] = ramp.view(1, 1, -1, 1, 1)
                if end < T and chunk_overlap > 0:
                    ramp_len = min(chunk_overlap, chunk_len)
                    ramp = torch.linspace(1, 0, ramp_len + 2, device=x.device, dtype=x.dtype)[1:-1]
                    w[:, :, -ramp_len:] = ramp.view(1, 1, -1, 1, 1)
                out[:, :, start:end] += pred * w
                weight[:, :, start:end] += w
                if end == T:
                    break
            return out / weight.clamp(min=1e-6)

    model = LatentResizer3D(**arch)
    model.load_state_dict(sd, strict=True)
    return model


def upscale_video_latent(video_external, scale=2.0):
    """Upscale normalized [1, F, 24, H, W] video latent by 2.0x using LatentResizer3D."""
    import torch
    from huggingface_hub import hf_hub_download
    from safetensors.torch import load_file

    ckpt = hf_hub_download(UPSCALER_REPO, UPSCALER_FILE, revision=UPSCALER_REV)
    raw_sd = load_file(ckpt, device="cpu")
    if any(k.startswith("upscaler.") for k in raw_sd.keys()):
        raw_sd = {k[len("upscaler."):]: v for k, v in raw_sd.items() if k.startswith("upscaler.")}

    if video_external.ndim == 4:
        video_external = video_external.unsqueeze(0)
    B, F_lat, C, H, W = video_external.shape
    eff_mult = float(scale)
    if H * W > 1800:
        eff_mult = max(1.15, min(eff_mult, math.sqrt(1600.0 / (H * W)) * eff_mult))
    H_out = max(2, int(round(H * eff_mult / 2.0)) * 2)
    W_out = max(2, int(round(W * eff_mult / 2.0)) * 2)
    effective_scale = math.sqrt((H_out / H) * (W_out / W))

    device = torch.device("cuda")
    model = _build_latent_resizer_3d(raw_sd).to(device=device, dtype=torch.float16).eval()
    s_norm = video_external.permute(0, 2, 1, 3, 4).contiguous().to(device=device, dtype=torch.float16)
    with torch.no_grad():
        up = model(s_norm, scale=float(effective_scale), target_size=(F_lat, H_out, W_out))
    video_up = up.permute(0, 2, 1, 3, 4).contiguous().to(dtype=torch.bfloat16).cpu()
    del model, raw_sd, s_norm, up
    gc.collect()
    torch.cuda.empty_cache()
    return video_up


def run_pass2_refine(args, source_video, start_sigma=0.78, upscale_factor=2.0, cached_pair=None):
    """Execute Single-Video Pass-2 full-BF16 3-step refinement."""
    import torch
    import pika_h3.pipeline as pipeline
    from pika_h3.cli import parse_refs
    from pika_h3.core import conditioning, evaluation, loader, vae
    from pika_h3.core.media import decode_video_reference
    from pika_h3.core.schedules import audio_frames
    from pika_h3.core.sampling import add_noise
    from pika_h3.core.wrapper import PikaH3Wrapper

    prompt = args.prompt_file.read_text(encoding="utf-8").strip() if args.prompt_file else (args.prompt or "").strip()
    refs = parse_refs(args.ref)
    assets = pipeline.resolve_assets(args.task, args.model_repo, args.revision, args.base_repo, args.base_revision)
    device = torch.device("cuda")
    dtype = torch.bfloat16

    encoder = conditioning.H3ConditionEncoder(assets.base, device, dtype)

    video_low = None
    audio_low = None
    if cached_pair is not None and cached_pair[0] is not None and cached_pair[1] is not None:
        video_low, audio_low = cached_pair
    elif source_video and Path(source_video).is_file():
        cache_file = latent_cache_path(args.output, source_video)
        if cache_file.is_file():
            emit("conditioning", "命中一采潜变量无损缓存 (.latents)")
            blob = torch.load(cache_file, map_location="cpu", weights_only=True)
            video_low = blob["video"].unsqueeze(0).to(dtype)
            audio_low = blob["audio"].unsqueeze(0).to(dtype)
        else:
            emit("conditioning", "编码一采原片潜变量")
            frames_t, waveform, sr = decode_video_reference(Path(source_video))
            vae_enc, _ = encoder._load_vaes()
            if hasattr(vae_enc, "enable_tiling"):
                vae_enc.enable_tiling()
            h_in = max(16, (int(frames_t.shape[-2]) // 16) * 16)
            w_in = max(16, (int(frames_t.shape[-1]) // 16) * 16)
            video_low = encoder.encode_video_latent(frames_t, height=h_in, width=w_in).unsqueeze(0).cpu()
            dur_in = max(1, round((video_low.shape[1] - 1) * 4 / 24))
            if waveform is not None:
                audio_low = encoder.encode_audio_latent(waveform, sr, duration=dur_in).unsqueeze(0).cpu()
            else:
                audio_low = torch.zeros((1, audio_frames(dur_in), 128), dtype=dtype)
    else:
        raise ValueError("二采精修缺少一采原片或潜变量输入")

    tier = "720p" if upscale_factor <= 1.6 else ("1K/1080p" if upscale_factor <= 2.1 else ("1.5K" if upscale_factor <= 2.35 else "2K"))
    emit("upscaling", f"二采 {tier} · 3D 潜变量 {upscale_factor:g}x 等比放大")
    video_up = upscale_video_latent(video_low, scale=upscale_factor)
    _, F_lat, _, H_out, W_out = video_up.shape
    height = H_out * 16
    width = W_out * 16
    frames = (F_lat - 1) * 4 + 1
    duration = max(1, round((frames - 1) / 24))
    target_a_len = audio_frames(duration)
    if audio_low.shape[1] < target_a_len:
        pad = torch.zeros((1, target_a_len - audio_low.shape[1], audio_low.shape[2]), dtype=audio_low.dtype)
        audio_low = torch.cat([audio_low, pad], dim=1)
    elif audio_low.shape[1] > target_a_len:
        audio_low = audio_low[:, :target_a_len, :]

    req = pipeline.Request(
        task=args.task,
        prompt=prompt,
        negative_prompt=args.negative_prompt,
        resolution="768p",
        duration=duration,
        aspect=args.aspect,
        seed=args.seed,
        first_image=args.first_image,
        last_image=args.last_image,
        references=refs,
        cpu_offload=True,
        reference_short_edge=1024,
        output=args.output,
    )
    cond = encoder.encode(req, width=width, height=height, frames=frames)
    encoder.free()

    # Load full BF16 transformer + Pika adapter with block-level group offload for 1080p VRAM safety
    model = loader.build_transformer(assets.base, assets.transformer_subfolder, assets.adapter,
                                     device=device, cpu_offload=True)
    wrapper = PikaH3Wrapper(model, cond, width=width, height=height, frames=frames, device=device, dtype=dtype)

    sigmas_v, sigmas_a = build_refine_ladder(start_sigma=start_sigma)
    gen = torch.Generator(device="cpu").manual_seed((int(args.seed) + 1000) % (2**63 - 1))
    eps_v = torch.randn(video_up.shape, generator=gen, dtype=torch.float32).to(device=device, dtype=dtype)
    eps_a = torch.randn(audio_low.shape, generator=gen, dtype=torch.float32).to(device=device, dtype=dtype)
    noisy_video = add_noise(video_up.to(device=device, dtype=dtype), eps_v, sigmas_v[0])
    noisy_audio = add_noise(audio_low.to(device=device, dtype=dtype), eps_a, sigmas_a[0])
    del eps_v, eps_a, video_up

    video_refined, _ = evaluation.sample_evaluation_ladder(
        wrapper,
        seed=args.seed,
        sigmas_v=sigmas_v,
        sigmas_a=sigmas_a,
        video_re_noise=0.5,
        audio_re_noise=0.0,
        noise=(noisy_video, noisy_audio),
    )

    del wrapper, model, cond, noisy_video, noisy_audio
    gc.collect()
    torch.cuda.empty_cache()

    decoders = vae.H3Decoders(assets.base, device, dtype)
    if hasattr(decoders.vae, "enable_tiling"):
        decoders.vae.enable_tiling()
    video_u8 = decoders.decode_video(video_refined)
    audio_wav = decoders.decode_audio(audio_low.to(device=device, dtype=dtype))
    decoders.free()

    vae.write_mp4(args.output, video_u8, audio_wav, fps=24, audio_rate=44100)
    save_latent_cache(args.output, video_refined, audio_low)
    meta = {
        "task": args.task,
        "prompt": prompt,
        "resolution": f"{width}x{height}",
        "duration": duration,
        "frames": frames,
        "seed": args.seed,
        "second_pass": True,
        "start_sigma": start_sigma,
        "upscale_factor": upscale_factor,
    }
    args.output.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    emit("starting", "初始化官方推理程序")
    import torch
    import pika_h3  # noqa: F401  先加载包，才能定位子模块
    skipped = load_pipeline_without_output_verify()
    emit("starting", "回读校验已跳过" if skipped else "回读校验保持官方默认",
         skip_output_verify=skipped)
    import pika_h3.pipeline as pipeline
    from pika_h3.core import conditioning, loader, evaluation, vae
    last_latents = {}
    pipeline.resolve_assets = stage(pipeline.resolve_assets, "assets", "检查 / 下载官方模型组件")
    conditioning.H3ConditionEncoder.encode = stage(conditioning.H3ConditionEncoder.encode, "conditioning", "编码提示词与参考素材")
    loader.build_transformer = stage(loader.build_transformer, "loading", "加载主模型与适配器")
    evaluation.sample_evaluation_ladder = sampling_observer(evaluation.sample_evaluation_ladder, torch.cuda.synchronize, capture=last_latents)
    vae.H3Decoders.__init__ = stage(vae.H3Decoders.__init__, "decoding", "加载解码器与解码音画")
    vae.write_mp4 = stage(vae.write_mp4, "encoding", "合成 MP4 并检查音画")

    clean_argv, second_pass, source_video, start_sigma, upscale_factor = extract_pass2_args(sys.argv[1:])
    from pika_h3.cli import parser as official_parser, main as official_main
    args = official_parser().parse_args(clean_argv)

    if second_pass and source_video:
        run_pass2_refine(args, source_video=Path(source_video), start_sigma=start_sigma, upscale_factor=upscale_factor)
    elif second_pass:
        pass1_out = args.output.with_name(args.output.stem + ".pass1.mp4")
        pass1_argv = []
        i = 0
        while i < len(clean_argv):
            if clean_argv[i] == "--output" and i + 1 < len(clean_argv):
                pass1_argv.extend(["--output", str(pass1_out)])
                i += 2
            else:
                pass1_argv.append(clean_argv[i])
                i += 1
        sys.argv = [sys.argv[0]] + pass1_argv
        official_main()
        save_latent_cache(pass1_out, last_latents.get("video"), last_latents.get("audio"))
        try:
            run_pass2_refine(args, source_video=pass1_out, start_sigma=start_sigma, upscale_factor=upscale_factor,
                             cached_pair=(last_latents.get("video"), last_latents.get("audio")))
        finally:
            pass1_out.unlink(missing_ok=True)
            pass1_out.with_suffix(".json").unlink(missing_ok=True)
    else:
        sys.argv = [sys.argv[0]] + clean_argv
        official_main()
        save_latent_cache(args.output, last_latents.get("video"), last_latents.get("audio"))
    emit("done", "视频保存完成")


if __name__ == "__main__":
    main()
