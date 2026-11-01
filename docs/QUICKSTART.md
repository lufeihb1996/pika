# 快速手册

从零到出片，以及每次新会话怎么恢复。

---

## 0. 一次性的准备

### Colab Secrets

左侧钥匙图标 → 添加：

| 名字 | 必需 | 用途 |
|---|---|---|
| `CF_TUNNEL_TOKEN` | ✅ | Cloudflare 隧道令牌 |
| `HF_TOKEN` | 可选 | Hugging Face 许可（模型要授权时才需要） |

**把右侧的授权开关打开**，否则脚本读不到。

### 电脑

确认代理软件开着（`HTTPS_PROXY`，通常是 `127.0.0.1:5732`）。代码走系统代理，直连 Cloudflare 要 14 秒，走代理 1.5 秒。

---

## 1. 起本地服务

```powershell
cd D:\ai\Pika_H3_Flex_Studio
python local_web.py
```

或双击 `双击本地联调.bat`。

浏览器打开 <http://127.0.0.1:3091>。此时页面能打开，但顶部是红点（Colab 没连上）。

---

## 2. 起 Colab

新建笔记本 → 运行时选 **A100 GPU** → 跑这一格：

```python
import urllib.request
exec(urllib.request.urlopen(
    "https://raw.githubusercontent.com/lufeihb1996/pika/main/colab_one.py",
    timeout=60).read().decode("utf-8"))
```

它会依次：

```
挂 Google Drive                 第一次会弹授权
恢复环境                        Drive 有备份就解压
拉代码                         git clone
下 REF + FL2VA 共约 216 GB     共用组件只下一份，已有文件复用，中断后重跑补齐
起 server + 隧道
```

### 时间预期

| 你的状态 | 总耗时 |
|---|---|
| 会话还在、模型已下 | 20 秒 |
| 会话没了、Drive 有环境备份 | 环境恢复 + 两套模型约 216 GB 下载，耗时随带宽变化 |
| 会话没了、Drive 也没备份 | 额外安装环境，再下载两套模型 |

### 成功的标志

```
✅ 工作台已启动，生成任务在网页提交，不会自动跑示例。
✅ Cloudflare HTTP/2 隧道已注册连接
隧道 已连接；Cloudflare 服务地址应为 http://127.0.0.1:8188
```

---

## 3. 出第一条

回电脑页面 **Ctrl+Shift+R** 强刷，顶部变绿点。

右侧面板：

1. 模式 **REF2VA**，上传至少一张图片或一个视频参考
2. 分辨率 **480p**、时长 **5 秒**
3. 提示词（REF2VA 六段式，见下）
4. 点「加入队列」

默认下载 REF 与普通主模型、两种适配器和一份共用组件，合计约 216 GB，五种模式全部准备。已有 REF 或普通版完整缓存时，只需补另一套约 69 GB；不清理旧文件。REF2VA 用于多参考生成，I2VA 用于首帧接续，FL2VA 用于首尾帧引导，T2VA 用于纯文字生成，L2VA 用于尾帧引导。

### 提示词格式

REF2VA 使用六段结构，参考图按上传顺序对应 `<Picture 1>` 等标签：

```text
subject_definitions:
...

summary:
...

retention_analysis:
...

detailed_description:
[Shot 1] ...

overall_soundscape:
...

non_diegetic_music:
...
```

文生 / 首尾帧模式使用三段式：

```
integrated_multimodal_description:
[Shot 1] ...

overall_soundscape:
...

non_diegetic_music:
...
```

- 场景用**英文**写，只有台词保留中文
- 5 秒只写一个镜头、一个动作回合
- 声音一定要写，不写就是随机配

---

## 4. 看进度

| 阶段 | 显示 |
|---|---|
| 检查模型缓存 | 检查 / 下载官方模型组件 |
| 加载文本编码器 | 编码提示词与参考素材 |
| 加载主模型 | 加载 Pika-H3 主模型分片 |
| 采样 | 采样 1/6 … 6/6（有真实步数） |
| 解码 | 加载视频与音频解码器 |
| 编码 | 合成 MP4 |

采样阶段有百分比和剩余时间，其余阶段只有文字（官方不打进度）。

---

## 5. 拿片

完成后自动下载到：

```
D:\ai\Pika_H3_Flex_Studio\outputs\<shot_id>\v1.mp4
```

页面上：

- **▶ 播放**：读本地文件，支持拖进度条
- **⬇ 另存**：浏览器下载

---

## 每次新会话怎么恢复

只需要跑第 2 步那一格。它会自动判断：

| 检查 | 行为 |
|---|---|
| 环境在不在 | 不在就从 Drive 解压 |
| 模型在不在 | 不在就下载 |
| 代码版本变没变 | 变了就重启服务 |

**不用手动做任何事。**

---

## 改代码后怎么更新

```powershell
# 本地
python build_colab.py          # 重新打包
python -m unittest discover -s tests -q
git add -A && git commit -m "..." && git push
```

然后回 Colab 重跑第 2 步那一格。启动器看到版本号变了会重启服务；有任务在跑会拒绝并提示等。

---

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| 红点「Colab 离线」 | 隧道断 / 代理没开 | 浏览器开 `https://mcp-mac.id96.vip/api/health`，530 就是 Colab 服务没了 |
| 点生成没反应 | Colab 还是旧版 | 重跑第 2 步，确认打印的版本号是最新 commit |
| 一直「等待 GPU」 | 前一条在跑 | 单卡串行，等它结束 |
| 卡在「加载主模型分片」 | 冷缓存，正常 | 第一条 40~60 秒，第二条 25 秒 |
| `CUDA out of memory` | 768p 长视频 | 降 480p 或缩短时长（已无 CPU 卸载选项） |
| 下载失败 HTTP 530 | 隧道断了 | 检查 Colab 是否还在跑 |
| 启动格报「请等当前队列完成」 | 有任务在跑 | 等它跑完再重跑启动格 |

---

## 成本提醒

- Colab 免费/Pro 的会话会被回收，`/mnt/local-scratch` 清空 → 默认两套模型约 216 GB 要重下。
- 环境（venv + 源码）可以备份到 Google Drive 省 4 分钟，见 [ARCHITECTURE.md](ARCHITECTURE.md#环境备份)。
- 默认无密码，隧道域名固定——测试阶段别外传域名。
