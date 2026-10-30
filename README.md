# Pika H3 Flex Studio

手机与电脑可用的官方 Pika H3 Flex 视频工作台，参考 MOSS Mobile Studio 的任务流程，直接控制官方 CLI。网页可单独部署到 Vercel，任务服务与 GPU 运行在 Colab；网页的 /api 与 /media 经 Vercel 反向代理到固定 Cloudflare 隧道。

## 从 GitHub 启动（推荐）

仓库：<https://github.com/lufeihb1996/pika>

1. 把 `colab_start.py` 的全部内容粘进 Colab 一格，运行。
2. 它会 `git clone` 到 `/content/pika-src`、跑 `build_colab.py`、再启动工作台。
3. 之后本地改完代码 `git push`，回 Colab 重跑这一格即可，不用再复制 60KB 打包。

启动器按 `APP_VERSION`（构建时写成 `1.3.0-<commit>`）判断：源码有更新就重启服务，没有更新就只确保隧道在线。不会打断正在生成的任务。

## 已有官方脚本的会话

1. 让已有脚本的测试视频跑完，等待进程退出并释放显存。不要重启或删除会话，否则可能丢失 scratch 缓存。
2. 在同一个 Colab 笔记本新增代码格，粘贴 `dist/00_Colab_官方工作台_一键启动.py` 的完整内容，运行。
3. Colab Secrets 添加 `CF_TUNNEL_TOKEN` 并授权当前笔记本。可选 `HF_TOKEN`；默认无需密码；设置 `PIKA_STUDIO_PASSWORD` 可启用访问密码。
4. 本地联调时运行 `python local_web.py`，打开 http://127.0.0.1:3091，默认无需密码。部署后使用 Vercel 分配的固定项目域名。
5. 在网页填写提示词、选择模式与参数，提交后查看队列、日志、结果预览与 MP4 下载。

原脚本不用修改，不再用它提交后续生成。新启动格复用 `/mnt/local-scratch/Pika_H3_Flex_Official` 中的 source、venv-py312、hf 缓存。若前一条仍在使用 GPU，启动器会提示先等待。端口被旧 ComfyUI 占用时也会停止提示，不会广泛杀进程。

首次全新会话也可直接用同一启动器，或上传 `dist/01_Pika_官方网页工作台.ipynb` 到 Colab。缺少 Python 3.12 / venv 支持会检查或补装；按官方固定依赖与补丁安装。A100 GPU 生成仍需实际验证。

## 隧道

Cloudflare 已有固定域名对应的服务地址保持 `http://127.0.0.1:8188`。工作台占用原 ComfyUI 的端口，Vercel 页面通过同源反向代理访问隧道；请勿同时使用同一隧道端口运行其他后端。固定 HTTP/2 用于 cloudflared 到 Cloudflare 的连接。令牌由 Secrets 读取，通过子进程环境传入，不写入项目。

没有令牌或隧道连接失败时服务仍保留，查看启动输出 / `logs/studio-cloudflared.log`。补齐 Secret 后重跑启动格，可为已运行的工作台启动隧道。切换回 ComfyUI 时先停止工作台服务器，再恢复原服务，避免端口冲突。

## 功能与限制

- 五种模式：文生、首帧、尾帧、首尾帧、多参考。多参考最多 9 图 / 3 视频 / 3 音频，共 12 个；还需满足官方时长限制。
- 上传、串行队列（最多 8 个待完成任务）、取消、日志、历史、预览、Range 视频播放、原始 MP4 下载、复用参数。
- 默认 480p / 5 秒 / CPU 卸载；最高可尝试 768p / 15 秒，不保证 A100 最大档组合可运行。
- 直接调用官方 CLI，使用其固定模型版本、适配器校验与专用六步采样；没有自创替代调度。
- 每个任务一个官方 CLI 子进程，结束后释放模型；下次从磁盘加载，有加载开销。
- 进度条在官方采样时显示真实完成步数和估算的采样剩余时间。准备、下载与解码没有准确百分比，显示等待动画；采样百分比不代表整条任务的百分比。
- 作品和素材保存在当前 Colab 本地盘，运行时被回收会丢失。本机收藏使用 IndexedDB，仅属于此设备与浏览器，可能因清理网站数据被删除，请下载备份。
- 本机收藏在已打开的页面中可查看；此版本没有离线 Service Worker，不保证断网后能重新打开整个网站。
- 取消只终止本工作台的生成进程；重启服务后未完成任务标记中断，不自动重跑。
- 4K 与音频分轨为额外官方托管 API，此项目提供原生 480p / 768p 混合音频视频。

## 独立网页与本地联调

`vercel-web/` 是可单独部署的静态网页项目，`vercel.json` 将 `/api` 和 `/media` 反向代理到固定隧道。部署后域名由 Vercel 分配；未部署前没有固定公网网页地址。上传、登录 Cookie、Range 播放与大文件应在真实 Vercel 环境复核，Vercel 代理请求有超时限制。

本地运行 `python local_web.py` 打开 http://127.0.0.1:3091，以同源代理连接实际隧道。页面本身可离线打开；如果显示 Colab 未连接，检查本机到隧道的访问和 Cloudflare 安全规则。Cloudflare 1010 需查看 Cloudflare 防火墙事件，确认阻止来源和具体规则。

## 本地预览与维护

双击 `双击本地预览.bat` 或执行 `python preview.py`。地址 `http://127.0.0.1:3090`，默认无需密码。本地预览不会下载模型，也不会伪装成功生成。正式 Colab 使用不同的 Secret 或随机密码。

服务只依赖 Python 标准库，前端无需 npm。修改源代码后运行 `python build_colab.py` 重建独立启动文件与 notebook。`colab_launcher.py` 是含占位符的构建模板，不直接提交到 Colab。

项目本地测试验证接口与队列行为，不代表已在远程 Colab 上实际部署。首次运行后的具体错误请附日志核对。
