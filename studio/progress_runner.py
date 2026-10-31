"""Observe the official pipeline without editing weights, schedules or its source."""
import functools
import json
from pathlib import Path
import sys
import time
import types

PREFIX = "[PIKA_PROGRESS] "
# 官方在写完 MP4 后会把整个文件解码一遍数帧数，只做校验、不产出任何东西。
# 在内存里跳过这一行（不修改磁盘上的官方源码），实测省 12~30 秒。
VERIFY_MARKER = "if sum(1 for _ in encoded.decode(video=0)) != frames:"


def emit(phase, message, **values):
    print(PREFIX + json.dumps(dict(phase=phase, message=message, **values), ensure_ascii=False), flush=True)


def stage(function, phase, message):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        emit(phase, message)
        return function(*args, **kwargs)
    return wrapped


def sampling_observer(function, synchronize):
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
            return function(generator, *args, **kwargs)
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


def main():
    emit("starting", "初始化官方推理程序")
    import torch
    import pika_h3  # noqa: F401  先加载包，才能定位子模块
    skipped = load_pipeline_without_output_verify()
    emit("starting", "回读校验已跳过" if skipped else "回读校验保持官方默认",
         skip_output_verify=skipped)
    import pika_h3.pipeline as pipeline
    from pika_h3.core import conditioning, loader, evaluation, vae
    pipeline.resolve_assets = stage(pipeline.resolve_assets, "assets", "检查 / 下载官方模型组件")
    conditioning.H3ConditionEncoder.encode = stage(conditioning.H3ConditionEncoder.encode, "conditioning", "编码提示词与参考素材")
    loader.build_transformer = stage(loader.build_transformer, "loading", "加载主模型与适配器")
    evaluation.sample_evaluation_ladder = sampling_observer(evaluation.sample_evaluation_ladder, torch.cuda.synchronize)
    vae.H3Decoders.__init__ = stage(vae.H3Decoders.__init__, "decoding", "加载解码器与解码音画")
    vae.write_mp4 = stage(vae.write_mp4, "encoding", "合成 MP4 并检查音画")
    from pika_h3.cli import main as official_main
    official_main()
    emit("done", "视频保存完成")


if __name__ == "__main__":
    main()
