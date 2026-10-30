# Generated from the verified standalone installer; setup only, no automatic generation.
"""复制到 Colab 运行：官方原生 CLI，独立环境，scratch 存储。"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
CODE_REV = '4bf8ea508891a432106e70f675d287c27f02306d'
MODEL_REV = '0ddf55ad7067ca1e76812253bd7b4600df87f759'
CONFIG = {'storage_root': '', 'task': 't2va', 'resolution': '480p', 'duration': 5, 'aspect': '16:9', 'seed': 42, 'cpu_offload': True, 'prompt_file': '', 'first_image': '', 'last_image': '', 'references': [], 'http_download': False}
GB = 10 ** 9

def run(args, *, cwd=None, env=None, log=None):
    args = list(map(str, args))
    print('$', ' '.join(args), flush=True)
    start = time.monotonic()
    if log is None:
        subprocess.run(args, cwd=cwd, env=env, check=True)
    else:
        with open(log, 'a', encoding='utf-8') as handle:
            proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', bufsize=1)
            try:
                for line in proc.stdout:
                    print(line, end='', flush=True)
                    handle.write(line)
                    handle.flush()
                rc = proc.wait()
                if rc:
                    raise RuntimeError(f'命令失败 exit={rc}；完整日志：{log}')
            except BaseException:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                raise
            finally:
                proc.stdout.close()
    print(f'阶段耗时：{time.monotonic() - start:.1f}s', flush=True)

def load_config():
    config = dict(CONFIG)
    parent = Path(__file__).resolve().parent if globals().get('__file__') else Path.cwd()
    cfg = parent / '01_官方生成配置.json'
    if cfg.is_file():
        supplied = json.loads(cfg.read_text(encoding='utf-8-sig'))
        if not isinstance(supplied, dict) or set(supplied) - set(config):
            raise ValueError('配置必须是对象，且只能使用 CONFIG 中的字段')
        config.update(supplied)
        print(f'配置来源：{cfg}（覆盖脚本顶部 CONFIG）', flush=True)
    expected = {'t2va': (False, False), 'i2va': (True, False), 'l2va': (False, True), 'fl2va': (True, True), 'ref2va': (False, False)}
    if config['task'] not in expected:
        raise ValueError('task 不支持')
    if config['resolution'] not in {'480p', '768p'}:
        raise ValueError('resolution 只支持 480p / 768p')
    if type(config['duration']) is not int or not 5 <= config['duration'] <= 15:
        raise ValueError('duration 必须是 5–15 的整数')
    if config['aspect'] not in {'21:9', '16:9', '3:2', '4:3', '5:4', '1:1', '4:5', '3:4', '2:3', '9:16', '9:21'}:
        raise ValueError('aspect 不支持')
    if type(config['seed']) is not int or not 0 <= config['seed'] < 2 ** 63:
        raise ValueError('seed 范围不正确')
    for key in ('cpu_offload', 'http_download'):
        if type(config[key]) is not bool:
            raise ValueError(f'{key} 必须是布尔值')
    if (bool(config['first_image']), bool(config['last_image'])) != expected[config['task']]:
        raise ValueError('首尾帧文件必须与 task 对应')
    for key in ('prompt_file', 'first_image', 'last_image'):
        if config[key] and (not Path(config[key]).is_file()):
            raise FileNotFoundError(f'{key}: {config[key]}')
    refs = config['references']
    if not isinstance(refs, list) or (config['task'] == 'ref2va') != bool(refs):
        raise ValueError('仅 ref2va 必须提供 references 列表')
    for ref in refs:
        kind, sep, path = ref.partition(':')
        if not sep or kind not in {'image', 'video', 'audio'} or (not Path(path).is_file()):
            raise ValueError(f'参考文件无效：{ref}')
    return config

def gpu_precheck():
    info = subprocess.check_output(['nvidia-smi', '--query-gpu=index,name,memory.total,memory.used,driver_version', '--format=csv,noheader,nounits'], text=True)
    print('GPU index, name, total MiB, used MiB, driver:\n' + info, flush=True)
    first = info.strip().splitlines()[0].split(',')
    if float(first[2]) < 75000:
        raise RuntimeError('当前 GPU 不是预期的 80GB 档，请确认运行时')
    if float(first[3]) > 4096:
        raise RuntimeError('GPU 已占用超过 4GiB。请先停止 ComfyUI / 其他推理并释放 notebook 的 GPU 模型，再重跑；本脚本不自动杀进程。')
    mem = dict((line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines()))
    available = int(mem['MemAvailable'].split()[0]) * 1024
    print(f'可用系统内存：{available / GB:.1f} GB', flush=True)
    if available < 110 * GB:
        raise RuntimeError('可用内存不足 110GB，请先释放其他模型；这是启动器保守余量，并非官方最低要求')

def select_storage(config):
    if config['storage_root']:
        chosen = Path(config['storage_root']).expanduser().resolve()
        if not chosen.is_dir():
            raise RuntimeError('storage_root 不存在，请填写实际挂载路径')
    else:
        data = json.loads(subprocess.check_output(['findmnt', '--json', '--output', 'TARGET,FSTYPE'], text=True))
        candidates = []

        def visit(items):
            for item in items:
                target = Path(item['target'])
                fs = item.get('fstype', '')
                if fs not in {'tmpfs', 'devtmpfs', 'proc', 'sysfs', 'squashfs'} and 'fuse' not in fs.lower():
                    try:
                        usage = shutil.disk_usage(target)
                        if usage.total >= 300 * GB and os.access(target, os.W_OK):
                            candidates.append((usage.free, target))
                    except OSError:
                        pass
                visit(item.get('children', []))
        visit(data['filesystems'])
        if not candidates:
            run(['df', '-hT'])
            raise RuntimeError('未检测到可写的 >=300GB 本地盘。请从 df 输出确认 scratch 路径并填写 storage_root；不会自动改用云盘。')
        chosen = max(candidates, key=lambda pair: pair[0])[1]
    usage = shutil.disk_usage(chosen)
    if str(chosen).startswith('/content/drive'):
        raise RuntimeError('请选择本地 scratch 存储')
    work = chosen / 'Pika_H3_Flex_Official'
    cached = 0
    if (work / 'hf').is_dir():
        for folder, _, files in os.walk(work / 'hf'):
            for name in files:
                path = Path(folder) / name
                if not path.is_symlink():
                    try:
                        cached += path.stat().st_size
                    except OSError:
                        pass
    required = max(25 * GB, 250 * GB - cached)
    print(f'存储：{chosen} | 空闲 {usage.free / GB:.1f} GB | HF 缓存 {cached / GB:.1f} GB', flush=True)
    if usage.free < required:
        raise RuntimeError(f'空间不足，当前预算要求约 {required / GB:.1f} GB 空闲')
    work.mkdir(exist_ok=True)
    return work

def secret(name):
    value = os.environ.get(name, '').strip()
    if not value:
        try:
            from google.colab import userdata
            value = (userdata.get(name) or '').strip()
        except Exception:
            pass
    return value

def ensure_venv(host_py, venv, env, log):
    py = venv / 'bin' / 'python'
    if py.exists():
        pip_check = subprocess.run([str(py), '-m', 'pip', '--version'], env=env, capture_output=True, text=True)
        if pip_check.returncode == 0:
            return py
    ensurepip = subprocess.run([str(host_py), '-m', 'ensurepip', '--version'], env=env, capture_output=True, text=True)
    if ensurepip.returncode != 0:
        if os.geteuid() != 0 or not shutil.which('apt-get'):
            raise RuntimeError('缺少 Python 3.12 venv 支持，请安装 python3.12-venv 后重跑')
        print('补齐 python3.12-venv，然后创建 / 修复虚拟环境', flush=True)
        run(['apt-get', 'update', '-qq'], env=env, log=log)
        run(['apt-get', 'install', '-y', '-qq', 'python3.12-venv'], env=env, log=log)
    run([host_py, '-m', 'venv', str(venv)], env=env, log=log)
    run([py, '-m', 'pip', '--version'], env=env, log=log)
    return py

def build_command(cli, repo, config, output):
    prompt = config['prompt_file'] or str(repo / 'examples' / 'prompts' / ('ref2va.txt' if config['task'] == 'ref2va' else 't2va.txt'))
    cmd = [str(cli), '--task', config['task'], '--model-repo', 'Pika-Labs/Pika-H3-Flex', '--revision', MODEL_REV, '--prompt-file', prompt, '--resolution', config['resolution'], '--duration', str(config['duration']), '--aspect', config['aspect'], '--seed', str(config['seed']), '--output', str(output)]
    if config['cpu_offload']:
        cmd.append('--cpu-offload')
    for key, flag in (('first_image', '--first-image'), ('last_image', '--last-image')):
        if config[key]:
            cmd.extend([flag, config[key]])
    for ref in config['references']:
        cmd.extend(['--ref', ref])
    return cmd

def prepare_environment():
    if sys.platform != 'linux':
        raise RuntimeError('请复制到 Colab Linux GPU 单元格运行')
    started = time.monotonic()
    config = dict(CONFIG)
    gpu_precheck()
    work = select_storage(config)
    for name in ('logs', 'outputs', 'tmp', 'pip-cache', 'hf'):
        (work / name).mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update(HF_HOME=str(work / 'hf'), HF_HUB_CACHE=str(work / 'hf' / 'hub'), HF_XET_CACHE=str(work / 'hf' / 'xet'), HF_HUB_DISABLE_XET='1' if config['http_download'] else '0', PIP_CACHE_DIR=str(work / 'pip-cache'), TMPDIR=str(work / 'tmp'), PYTHONUNBUFFERED='1', CUDA_VISIBLE_DEVICES='0', CUDA_MODULE_LOADING='LAZY', PYTORCH_CUDA_ALLOC_CONF='expandable_segments:True')
    token = secret('HF_TOKEN')
    if token:
        env['HF_TOKEN'] = token
    stamp = time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns() % 1000000)
    log = work / 'logs' / f'run-{stamp}.log'
    print(f'工作目录：{work}\n日志：{log}', flush=True)
    missing = [name for name in ('git', 'patch', 'ffmpeg', 'gcc', 'make') if not shutil.which(name)]
    if missing:
        if os.geteuid() != 0:
            raise RuntimeError('缺少系统工具：' + ', '.join(missing))
        run(['apt-get', 'update', '-qq'], env=env, log=log)
        run(['apt-get', 'install', '-y', '-qq', 'git', 'patch', 'ffmpeg', 'build-essential', 'python3-dev'], env=env, log=log)
    host_py = sys.executable if sys.version_info[:2] == (3, 12) else shutil.which('python3.12')
    if not host_py:
        raise RuntimeError('官方要求 Python 3.12，当前未找到；请选择提供 Python 3.12 的 Colab 运行时')
    repo = work / 'source'
    if not repo.exists():
        repo.mkdir()
        run(['git', 'init', str(repo)], env=env, log=log)
        run(['git', '-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/Pika-Labs/Pika-H3-Flex.git'], env=env, log=log)
    head = subprocess.run(['git', '-C', str(repo), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    if head != CODE_REV:
        dirty = subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'], text=True)
        if dirty.strip():
            raise RuntimeError('源码目录有本地改动，请保留后手动处理；不会覆盖')
        run(['git', '-C', str(repo), 'fetch', '--filter=blob:none', '--depth', '1', 'origin', CODE_REV], env=env, log=log)
        run(['git', '-C', str(repo), 'checkout', '--detach', CODE_REV], env=env, log=log)
    venv = work / 'venv-py312'
    py = ensure_venv(host_py, venv, env, log)
    env['PATH'] = str(venv / 'bin') + os.pathsep + env.get('PATH', '')
    fingerprint = hashlib.sha256((CODE_REV + str(py) + subprocess.check_output([str(py), '-V'], text=True)).encode() + (repo / 'requirements.txt').read_bytes()).hexdigest()
    marker = work / 'environment-ready.json'
    ready = False
    if marker.is_file():
        try:
            ready = json.loads(marker.read_text())['fingerprint'] == fingerprint
        except (ValueError, KeyError):
            pass
    if ready:
        check = subprocess.run([str(py), '-m', 'pip', 'check'], env=env, capture_output=True, text=True)
        patch = subprocess.run([str(py), 'tools/apply_diffusers_patches.py', '--check'], cwd=repo, env=env, capture_output=True, text=True)
        ready = check.returncode == 0 and patch.returncode == 0
    if not ready:
        run([py, '-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip', 'setuptools', 'wheel'], env=env, log=log)
        run([py, '-m', 'pip', 'install', '--disable-pip-version-check', '-r', repo / 'requirements.txt'], cwd=repo, env=env, log=log)
        run([py, 'tools/apply_diffusers_patches.py'], cwd=repo, env=env, log=log)
        run([py, '-m', 'pip', 'install', '--no-deps', '-e', repo], env=env, log=log)
        run([py, '-m', 'pip', 'check'], env=env, log=log)
    probe = "import torch; print('Torch:',torch.__version__,'CUDA:',torch.version.cuda); assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0)); x=torch.ones((32,32),device='cuda',dtype=torch.bfloat16); print((x@x).sum().item()); torch.cuda.synchronize()"
    run([py, '-c', probe], env=env, log=log)
    cli = venv / 'bin' / 'pika-h3-pro'
    run([cli, '--help'], cwd=repo, env=env, log=log)
    marker.write_text(json.dumps({'fingerprint': fingerprint, 'code_revision': CODE_REV}), encoding='utf-8')
    return (work, env, cli)
