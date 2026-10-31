# Pika H3 Flex Studio

在 Colab 上用 A100 跑官方 **Pika H3 Flex** 的本地工作台：网页选参数、串行排队、自动把成片下载到电脑。

不写 ComfyUI 工作流，直接驱动官方 CLI `pika-h3-pro`，用它的固定权重版本、适配器校验和六步采样。

---

## 一分钟理解

```
你的电脑                          隧道                      Colab A100
┌──────────────────────┐                    ┌──────────────────────────┐
│ 浏览器 127.0.0.1:3091 │                    │ server.py :8188          │
│   ↓ /api/*           │  mcp-mac.id96.vip  │   内存队列（不存盘）      │
│ local_web.py  代理 ───┼───────────────────►│   ↓                      │
│   ↓                  │                    │ pika-h3-pro（官方 CLI）   │
│ local_media.py 下载 ◄─┼────────────────────┤   ↓                      │
│   ↓                  │                    │ outputs/<id>-v<N>.mp4    │
│ outputs/<id>/v<N>.mp4│                    └──────────────────────────┘
│ studio.db（唯一的表） │
└──────────────────────┘
```

**状态只有一份**——在电脑的 SQLite 里。Colab 是纯执行器，重启只丢内存队列，跟 ComfyUI 重启一样。

---

## 快速开始

完整步骤见 **[docs/QUICKSTART.md](docs/QUICKSTART.md)**，这里只给最短路径。

**电脑**

```bash
python local_web.py        # 或双击 双击本地联调.bat
```

**Colab**（A100 运行时，Secrets 里有 `CF_TUNNEL_TOKEN`）

```python
import urllib.request
exec(urllib.request.urlopen(
    "https://raw.githubusercontent.com/lufeihb1996/pika/main/colab_one.py",
    timeout=60).read().decode("utf-8"))
```

看到 `隧道 已连接` 后，浏览器打开 <http://127.0.0.1:3091>。

---

## 文档

| 文档 | 内容 |
|---|---|
| **[docs/QUICKSTART.md](docs/QUICKSTART.md)** | 从 0 到出片；每次新会话怎么恢复；常见故障 |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | 三层结构、数据表、接口、文件命名、性能实测、已知限制 |

---

## 能力边界

| 项 | 范围 |
|---|---|
| 模式 | 文生 / 首帧 / 尾帧 / 首尾帧 / 多参考 |
| 分辨率 | 480p 或 768p（本地），11 种画幅 |
| 时长 | 5–15 秒（24fps，17n+5 帧网格） |
| 音频 | 32 kHz 立体声，混在 MP4 里 |
| 并行 | 单卡串行，一条一个 CLI 子进程 |
| 本地没有 | 1080p / 2K / 4K、音频分轨、提示词增强（那是 Pika 托管 API） |

---

## 目录

| 路径 | 作用 |
|---|---|
| `local_web.py` | 本地 3091：静态页 + `/api/*` 代理 + `/local-media/*` |
| `local_media.py` | 后台轮询、提交、下载；持有本地 SQLite |
| `jobstore.py` | 本地表结构：shots / takes / shot_assets |
| `studio/server.py` | Colab 端无状态服务 |
| `studio/progress_runner.py` | 包装官方 CLI：报进度、跳过回读校验 |
| `installer_runtime.py` | 选盘、建 venv、装依赖、GPU 预检 |
| `vercel-web/` | 可单独部署的静态前端 |
| `dist/` | 打包好的 Colab 启动器（单文件） |
| `colab_one.py` | Colab 一键启动（恢复环境 → 拉代码 → 下模型 → 启动） |
| `tests/` | 13 项单元测试 |

---

## 已知代价

- **模型 146.8 GB**，每次新 Colab 会话都要重下（30~40 分钟）。环境可以用 Google Drive 备份省 4 分钟，模型不行。
- **每条任务重新加载模型**（编码器 62 GB + 主模型 66 GB），显存装不下三个组件同时驻留，这是硬约束。
- **768p 长视频可能 OOM**：已去掉 CPU 卸载选项，显存不够时只能降分辨率或缩短时长。

性能数据、表结构、接口清单见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。
