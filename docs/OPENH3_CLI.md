# Pika 一采 + OpenH3 INT8 CLI 二采

新提交的二采任务默认 `refiner_backend=openh3`。普通一采保持官方 Pika CLI。
`progress_runner.py` 在导入 Pika / torch 之前路由：已有视频直接精修；一二采连跑先由独立
Pika 子进程保存 `.pass1.mp4` 并退出，再由独立环境的常驻进程执行 OpenH3。无需 ComfyUI
HTTP 服务、网页或第二个隧道，但精修依赖固定版本的 ComfyUI Python 模型加载和采样库。

## 启动

原来的 `dist/00_Colab_官方工作台_一键启动.py` 链接继续使用。启动器默认先准备精修环境和
额外模型，再启动 API，避免首次二采下载超过客户端等待期限。首次准备需要较长下载时间
和额外磁盘空间；后续复用 `openh3/ready.json`。启动不提交 GPU 生成任务。
如只用原版 Pika，可将启动格中的 `PREPARE_OPENH3=False`；提交二采时显式选择 `pika`。

云盘只用于恢复 Pika 环境：已挂载的
`/content/drive/MyDrive/ComfyUI_Models/Pika_H3_Flex/env.tar.gz`。本地环境完整时直接复用。
模型从 Hugging Face 检查 / 下载，不从云盘复制 B15 模型。精修环境会补齐缺失的
`python3.12-venv`，并修复创建到一半的虚拟环境；沿用现有 Pika CUDA 包，新增依赖安装到精修环境。

精修单独安装 `torch 2.11.0+cu130` 及匹配的 torchvision / torchaudio；Pika 原环境保持不变。
固定版 ComfyUI 的 `quant_ops.py` 会在 CUDA <13 时禁用 Kitchen CUDA 后端，因此不能直接
沿用 Pika 的 cu129 Torch 做高效 INT8 精修。启动器会检查并补齐，CLI 也会拒绝静默使用旧构建。
这保证优化后端可启用，不代表已经完成相同片段的速度或画质对照。

后端请求示例字段：

```json
{"second_pass": true, "refiner_backend": "openh3", "start_sigma": 0.78, "upscale_factor": 2.0}
```

已有任务缺少后端字段时，继续使用旧 Pika 路径。新任务入队保存后端字段，不随环境变量
变化。也可设置 `PIKA_REFINER_BACKEND=pika` 作为新任务默认值。

独立命令（先运行 setup，Linux / CUDA）：

```bash
python studio/openh3_setup.py --root /mnt/local-scratch/Pika_H3_Flex_Official/openh3
/mnt/local-scratch/Pika_H3_Flex_Official/openh3/venv/bin/python studio/openh3_cli.py \
  --engine /mnt/local-scratch/Pika_H3_Flex_Official/openh3/engine \
  --source /absolute/draft.mp4 --output /absolute/refined.mp4 \
  --prompt-file /absolute/prompt.txt --resolution 1080p --start-sigma 0.78 --seed 42
```

## 模型及精修行为

模型清单唯一维护在 `studio/openh3_setup.py:MODELS`：FL2VA pruned INT8、Qwen3-VL INT8、
视频 FP16 / 音频 FP32 VAE、Regenerate LoRA、Sharp 分支、3D 放大器、Fast VAE。
REF2VA 仅 Range 提取并校验 32KB 曲线表，服务器不支持 Range 时直接报错，不下载完整底模。
首次下载解析出的 HF 提交号记录在 `openh3/model-revisions.json`，之后保持这些版本。
Pika 的 BF16 权重不替代上述 INT8 模型。下载缓存与精修环境均位于 `openh3/`；设置
`PIKA_OPENH3_HOME` 可改变位置。不要在任务运行期间更改该目录。

旧界面倍率映射成目标像素预算：≤1.6 为 720p、≤2.1 为 1080p、≤2.35 为 1.5K、
其余为 2K。保持源画幅；16:9 的 1080p 输出为 1920×1080，竖屏则为 1080×1920。
计算尺寸向上对齐到 32，解码后中心裁掉对齐边缘。其它画幅按同等像素量计算偶数尺寸。
不再直接把源片宽高乘二。原片按 24fps 重采样，补齐 H3 帧网格后精修，最后去掉补帧；
单次最多约 15 秒，不静默截短长片。三步 Euler、CFG 1、默认 sigma 0.78、Sharp 强度 0.65。
本实现没有旧独立脚本的人脸区域蒙版，也不使用 Pika 参考图作为二采条件；画面和人物来源
是草稿视频，提示词来自当前任务，因此需要验证身份保持效果。

Fast VAE 解码后由 FFmpeg 复制原音轨，画面编码到临时文件；FFmpeg 成功后原子替换目标。
精修失败保留一采原片和已有输出。各阶段报告耗时，输出旁的 JSON 保存实际宽高、参数及 worker PID。

## 连续二采的常驻与缓存

`openh3_worker.py` 通过私有 Unix socket（0600 权限）串行服务现有 CLI 任务，不开放新的 HTTP 端口。
相邻二采任务复用一个进程。FL2VA 主模型、Regenerate / Sharp 补丁、Qwen、视频与音频 VAE 只首次
从磁盘加载；Fast VAE 和放大器沿用插件内存缓存。进程保留模型对象，GPU 由显存管理器按余量安排，
取消了各阶段无条件卸载全部模型的调用；不强制整套模型永远驻留 GPU。

最近两份原片的编码潜变量按文件内容 SHA256 缓存，最近 16 份提示词条件按原文缓存；每个任务
克隆自己的输入潜变量，重新创建种子、噪声、sigma 和 guider。输出尺寸、步数、LoRA 强度保持不变。
首次仍是冷加载；后续日志报告 `cache_hit` 和 `worker_pid`，可据此核验真实复用。

切回 Pika 一采或旧 Pika 二采时，CLI 先退出空闲常驻进程并等待显存释放。队列仍按原逻辑终止
每个任务的 CLI 进程组；常驻进程监测代理 socket，代理退出就终止自己的进程组（含 FFmpeg），
释放 GPU。异常任务也退出常驻进程，下一任务重新加载；不复用失败后的 CUDA 上下文。
源码变更、环境升级会更换空闲 worker。没有新增暂停、恢复、重置或中断 API。

## 验证范围

`python -m unittest discover -s tests -v` 验证路由、队列参数冻结、独立子进程、
已有视频跳过一采、失败保留产物、尺寸计算与 Colab 打包。Linux socket 专项在 Colab 上以模拟
推理验证同进程 / 同对象复用、任务失败、代理断开取消；没有为此提交真实 GPU 视频生成。
Colab 上仍需验证模型加载、显存峰值、精修速度和画质；不能把隔离测试当成速度或画质实测。

来源：[OpenH3 节点实现](https://github.com/rdh9616/ComfyUI-Open-H3-Regenerate)、
[潜变量放大器](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler)。
上游许可证随 git checkout 保留在精修环境的源码目录。
