"""Observe the official pipeline without editing weights, schedules or its source."""
import functools
import json
import time

PREFIX = "[PIKA_PROGRESS] "


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


def main():
    emit("starting", "初始化官方推理程序")
    import torch
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
    emit("done", "视频保存与校验完成")


if __name__ == "__main__":
    main()
