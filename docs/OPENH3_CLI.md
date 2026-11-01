# Pika 一采 + OpenH3 INT8 CLI 二采

新提交的二采任务默认 `refiner_backend=openh3`。普通一采保持官方 Pika CLI。
`progress_runner.py` 在导入 Pika / torch 之前路由：已有视频直接精修；一二采连跑先由独立
Pika 子进程保存 `.pass1.mp4` 并退出，再由另一个 Python 环境启动 OpenH3。无需 ComfyUI
HTTP 服务、网页或第二个隧道，但精修依赖固定版本的 ComfyUI Python 模型加载和采样库。

## 启动

原来的 `dist/00_Colab_官方工作台_一键启动.py` 链接继续使用。启动器默认先准备精修环境和
额外模型，再启动 API，避免首次二采下载超过客户端等待期限。首次准备需要较长下载时间
和额外磁盘空间；后续复用 `openh3/ready.json`。启动不提交 GPU 生成任务。
如只用原版 Pika，可将启动格中的 `PREPARE_OPENH3=False`；提交二采时显式选择 `pika`。

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
精修失败保留一采原片和已有输出。各阶段报告耗时，输出旁的 JSON 保存实际宽高和参数。
POSIX 子进程继承队列任务的进程组，沿用已有整组终止逻辑。

## 验证范围

`python -m unittest discover -s tests -v` 验证路由、队列参数冻结、独立子进程、
已有视频跳过一采、失败保留产物、尺寸计算与 Colab 打包。没有提交真实 GPU 生成。
Colab 上仍需验证模型加载、显存峰值、精修速度和画质；不能把隔离测试当成速度或画质实测。

来源：[OpenH3 节点实现](https://github.com/rdh9616/ComfyUI-Open-H3-Regenerate)、
[潜变量放大器](https://github.com/LBH-123-AI/Comfyui_Minimax_h3_latent_Upscaler)。
上游许可证随 git checkout 保留在精修环境的源码目录。
