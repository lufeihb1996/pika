# 架构与实现

给要改代码的人看。快速上手请读 [QUICKSTART.md](QUICKSTART.md)。

---

## 1. 三层结构

```
┌──────────────── 电脑 ────────────────┐      ┌─ Cloudflare ─┐      ┌────── Colab ──────┐
│                                      │      │              │      │                   │
│ 浏览器 ──► local_web.py :3091        │      │  固定域名     │      │  server.py :8188  │
│            ├─ 静态页（vercel-web）    │      │  mcp-mac...  │      │   内存队列         │
│            ├─ /api/* → 隧道           │◄────►│              │◄────►│   ↓               │
│            └─ /local-media/* 本地文件 │      └──────────────┘      │  pika-h3-pro      │
│                                      │                            │   ↓               │
│ local_media.py（后台线程）            │                            │ outputs/          │
│   ├─ 每 3.5s 轮询 /api/jobs          │                            │   <id>-v<N>.mp4   │
│   ├─ 提交任务、分配版本号             │                            └───────────────────┘
│   ├─ 下载成片到 outputs/<id>/v<N>.mp4│
│   └─ 持有 SQLite（唯一状态源）        │
└──────────────────────────────────────┘
```

**设计原则：状态在本地，Colab 只算。**

Colab 重启等同 ComfyUI 重启——内存队列清空，已生成的文件留在磁盘，本地镜头列表和已下载视频不受影响。

---

## 2. 一次生成的完整链路

```
① 浏览器 POST /api/jobs  {shot_id, 提示词, 参数}
        ↓
② local_web 拦截（不转发），交给 local_media.submit()
   - 校验参数
   - store.next_version(shot_id) 分配版本号
   - 写 takes 表（status=queued）
   - 把 {shot_id, version, params} POST 到隧道
        ↓
③ Colab server.py：
   - 同一 shot 有任务在跑 → 拒绝
   - 建内存 job（status=queued）
        ↓
④ worker 取队首 → 起子进程：
   python progress_runner.py --task t2va --prompt-file jobs/<id>-v1.txt
          --resolution 480p --duration 5 --output outputs/<id>-v1.mp4 ...
        ↓
⑤ progress_runner 调官方 CLI：
   - 加载文本编码器 62 GB → 编码提示词 → 释放
   - 加载主模型 66 GB + LoRA → 六步采样
   - 释放主模型 → 加载 VAE → 解码 → 写 MP4
   - （回读校验在内存中跳过）
        ↓
⑥ 本地每 3.5s GET /api/jobs → merge_remote()
   - 同 shot + 同 version → 更新状态/进度/时间
   - 连续两轮看不到该任务 → 标 interrupted
        ↓
⑦ status=done → 下载 GET /media/<id>?v=N
   → 写 outputs/<id>/v1.mp4.part → 改名
   → takes.download_status=ready, video_path=...
```

---

## 3. 本地数据库

`outputs/studio.db`（SQLite，WAL 模式）。

### shots —— 镜头

```
id  created  status  stage
task  prompt  resolution  duration  aspect  seed  cpu_offload
first_image  last_image  hidden  local_draft  error
editor_task  editor_prompt  editor_resolution  editor_duration
editor_aspect  editor_seed  editor_cpu_offload  editor_first_image  editor_last_image
```

### takes —— 这个镜头的第 N 次生成

```
shot_id  version                      主键
status  stage  created  started  finished  error
prompt  resolution  duration  aspect  seed  cpu_offload     每次生成的参数快照
progress_phase  progress_message  progress_completed  progress_total
sampling_seconds  sampling_eta_seconds
video_path  video_bytes
download_status  download_error  downloaded_bytes  total_bytes
```

### shot_assets —— 首帧/尾帧/参考素材

```
shot_id  role('first'|'last'|'reference'|'editor_*')  position  kind  upload_id
```

**版本号由本地分配**：`SELECT MAX(version) FROM takes WHERE shot_id=?` + 1。Colab 不存版本，收到就用。

---

## 4. 文件命名

| 位置 | 路径 |
|---|---|
| 电脑 | `outputs/<shot_id>/v<N>.mp4` |
| Colab | `outputs/<shot_id>-v<N>.mp4` |
| Colab 日志 | `jobs/<shot_id>-v<N>.log` |
| Colab 提示词 | `jobs/<shot_id>-v<N>.txt` |

同一个镜头重新生成 = 新版本，旧版本文件保留。

---

## 5. 接口

### Colab（`studio/server.py`，无状态）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 版本、在线状态 |
| GET | `/api/jobs` | 内存队列（含 shot_id、version、status、progress） |
| POST | `/api/jobs` | 提交：`{shot_id, version, prompt, task, resolution, duration, aspect, seed, ...}` |
| POST | `/api/jobs/<id>/cancel` | 取消 |
| GET | `/api/jobs/<id>/log` | 尾部日志 |
| GET | `/media/<id>?v=N` | 取 MP4（支持 Range；内存没有时按文件名兜底） |
| POST | `/api/uploads?name=x.png` | 上传参考素材（≤200 MiB） |

### 本地（`local_web.py`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/local/state` | 本地全部镜头 + 下载状态 |
| POST | `/api/jobs` | 提交（本地分配版本号后转发） |
| POST | `/api/local/shots` | 新建草稿 |
| POST | `/api/local/shots/<id>/save` | 保存参数 |
| POST | `/api/local/shots/<id>/delete` | 移出列表（软删） |
| POST | `/api/local/jobs/<id>/retry` | 重新下载 |
| GET | `/local-media/<id>` | 播放本地 MP4（支持 Range） |

其余 `/api/*` 直接代理到隧道。

---

## 6. 模型构成

最小可用集 **146.8 GB**（13 个大文件）：

| 组件 | 文件数 | 大小 | 来源 |
|---|---|---|---|
| `transformer/` 主模型 | 7 | 66.28 GB | MiniMax H3 |
| `text_encoder/` Qwen3-VL 32B | 2 | 66.71 GB | Qwen（阿里） |
| `vae/` 视频 VAE | 2 | 10.42 GB | MiniMax H3 |
| `audio_vae/` | 1 | 0.61 GB | MiniMax H3 |
| `fl2va/adapter.safetensors` | 1 | 2.77 GB | **Pika 后训练** |
| tokenizer / processor / 配置 | 若干 | ~0.02 GB | — |

**没下的**（按需触发）：

| 组件 | 大小 | 什么时候下 |
|---|---|---|
| `transformer_ref/` | 66.28 GB | 第一次用 REF2VA |
| `ref2va/adapter.safetensors` | 2.77 GB | 同上 |
| `comfy/loras/` | 3.64 GB | 只有 ComfyUI 用 |

### 精度

| 组件 | 精度 |
|---|---|
| 文本编码器 | bf16 |
| 主模型 | bf16 |
| LoRA | 存储 fp32、计算 bf16 |
| 视频 / 音频 VAE | fp32（官方明确保持） |

---

## 7. 性能实测（A100 80GB）

### 480p / 5 秒

| 阶段 | 耗时 | 占比 |
|---|---|---|
| 资源检查 | 8.2s | 5.6% |
| 文本编码器加载 + 编码 | 26.1s | 17.6% |
| 主模型加载 | 27.5s | 18.6% |
| **采样（六步）** | **29.2s** | **19.7%** |
| 解码 + 编码 MP4 | 57.0s | 38.5% |
| **合计** | **148s** | |

### 768p / 5 秒

| 阶段 | 耗时 |
|---|---|
| 资源检查 | 8.2s |
| 文本编码器 | 24.6s |
| 主模型 | 26.5s |
| **采样** | **113.8s** |
| 解码 + 编码 | 126.7s |
| **合计** | **300s** |

**结论：采样只占 20%（480p），瓶颈在加载和解码。** 换 H100 采样能快一半，总时间只快 9%。

---

## 8. 已做的优化

| 优化 | 效果 | 实现位置 |
|---|---|---|
| 关闭 CPU 卸载 | 305~378s → **148s** | `server.py` 不再传 `--cpu-offload` |
| 走系统 HTTPS 代理 | 下载 262s → **2.8s** | `local_media.open_connection()` |
| 跳过 MP4 回读校验 | 解码段省 12~30s | `progress_runner.py` 内存替换源码 |
| 连续两次未见才标中断 | 修复误判 | `local_media.merge_remote()` |
| 分片文案按分母区分 | 7=主模型、3=解码器 | `server.py` 日志解析 |

### 为什么不能常驻模型

```
文本编码器 62.1 GiB + 主模型 61.7 GiB = 123.8 GiB
A100 可用 74.5 GiB
```

装不下，所以官方只能分阶段加载、用完释放。这是硬约束，不是实现问题。

---

## 9. 已知限制

| 限制 | 说明 |
|---|---|
| 分辨率 | 本地只有 480p / 768p；1080p 以上是托管 API |
| 时长 | 5–15 秒，24fps，帧数按 17n+5 |
| 显存 | 768p 超过 5 秒可能 OOM（已无 CPU 卸载选项） |
| 并行 | 单卡串行；官方包不做单请求多卡 |
| 模型加载 | 每条任务重新加载，占 36% 时间 |
| 环境持久性 | Colab scratch 被回收 → 模型重下 |
| 前端 | Colab 不再提供页面（只有 API）；手机/外网用需部署 `vercel-web/` |

---

## 10. 环境备份

`venv-py312` + `source` 可打包存 Google Drive，省 4 分钟重装（**模型不能这样存**，见下）。

**保存**（装好环境后）：

```python
from google.colab import drive
drive.mount('/content/drive')

import os, subprocess
work = "/mnt/local-scratch/Pika_H3_Flex_Official"
dst = "/content/drive/MyDrive/ComfyUI_Models/Pika_H3_Flex"
os.makedirs(dst, exist_ok=True)

subprocess.run(f"tar -C {work} -czf {dst}/env.tar.gz venv-py312 source environment-ready.json",
               shell=True, check=True)
```

`colab_one.py` 会自动检测并恢复。

**为什么模型不能这样存**：146.8 GB 压缩后仍约 140 GB，上传 47 分钟、恢复 30 分钟，与直接从 Hugging Face 下载（约 27 分钟）相比没有优势，还多占 Drive 空间。

---

## 11. 维护

```powershell
python build_colab.py                     # 打包 dist/，写入版本号 1.3.0-<commit>
python -m unittest discover -s tests -q   # 13 项测试
git add -A && git commit -m "..." && git push
```

### 版本号机制

`build_colab.py` 把 `APP_VERSION` 从 `1.3.0` 改成 `1.3.0-<git short hash>`（有未提交改动时加 `-dirty`）。

启动器比较 `/api/health` 返回的版本：

| 情况 | 行为 |
|---|---|
| 相同 | 只确保隧道在线 |
| 不同 | 检查有无 running 任务 → 有则拒绝，无则重启 |

### 打包内容

| 文件 | 是否进包 |
|---|---|
| `installer_runtime.py` | ✅ |
| `studio/server.py` | ✅ |
| `studio/progress_runner.py` | ✅ |
| `studio/public/*` | ❌（Colab 不发页面） |
| `jobstore.py` | ❌（只有本地用） |

打包结果约 **26 KiB / 3 个文件**。
