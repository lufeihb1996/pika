"""Persistent local task tracker and bounded automatic video downloader."""
from __future__ import annotations
import http.client
import json
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

from jobstore import JobStore

JOB_ID = re.compile(r'^[0-9a-f]{32}$')
USER_AGENT = 'PikaStudioLocal/1.0'


def proxy_address(host):
    """读取系统 HTTPS_PROXY；NO_PROXY 命中时返回 None（测试里的 127.0.0.1 就走直连）。"""
    no_proxy = os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or ''
    for item in no_proxy.replace(';', ',').split(','):
        item = item.strip().lstrip('.')
        if item and (host == item or host.endswith('.' + item)):
            return None
    raw = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or ''
    if not raw:
        return None
    url = urlsplit(raw if '://' in raw else 'http://' + raw)
    if not url.hostname:
        return None
    return url.hostname, url.port or 8080


def open_connection(url, timeout=30):
    """到 upstream 的连接。系统有 HTTPS_PROXY 时走 CONNECT 隧道：
    本机直连 Cloudflare 要 10–15 秒且常常超时，经代理只要 1.5 秒。"""
    host = url.hostname
    port = url.port or (443 if url.scheme == 'https' else 80)
    cls = http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
    proxy = proxy_address(host)
    last = None
    for _ in range(3):
        conn = cls(proxy[0], proxy[1], timeout=timeout) if proxy else cls(host, port, timeout=timeout)
        if proxy:
            conn.set_tunnel(host, port)
        try:
            conn.connect()
            conn.sock.settimeout(timeout)
            return conn
        except (OSError, http.client.HTTPException) as exc:
            last = exc
            conn.close()
    raise last


class Downloads:
    def __init__(self, upstream, root, user_agent=USER_AGENT, interval=3.5, retry_delays=(0, 2, 5, 10)):
        self.upstream, self.root = upstream, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.user_agent, self.interval, self.retry_delays = user_agent, interval, retry_delays
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.pending = queue.Queue()
        self.scheduled = set()
        self.jobs = {}
        self.online = False
        self.last_error = ''
        self.last_checked = None
        self.connections = set()
        self.busy = threading.Event()
        self.store = JobStore(self.root / 'studio.db', migrate_dir=self.root)
        for job in self.store.all():
            if JOB_ID.fullmatch(str(job.get('id', ''))):
                self.jobs[job['id']] = job
        for job in self.jobs.values():
            target = self.root / job['id'] / f"v{int(job.get('version') or 1)}.mp4"
            if target.is_file() and target.stat().st_size > 0:
                known = job.get('download', {}).get('total_bytes')
                if known and known != target.stat().st_size:
                    job['download'] = dict(status='failed', error='本地文件长度不符，请重试下载')
                else:
                    size = target.stat().st_size
                    job['download'] = dict(status='ready', downloaded_bytes=size, total_bytes=size, bytes_per_second=0)
                    job['video_path'] = str(target)
            elif job.get('download', {}).get('status') in {'ready', 'downloading'}:
                job['download'] = dict(status='queued')
        self.threads = [threading.Thread(target=self.worker, daemon=True) for _ in range(2)]
        self.threads.append(threading.Thread(target=self.monitor, daemon=True))
        for thread in self.threads:
            thread.start()
        for job in list(self.jobs.values()):
            self.schedule(job['id'])

    def connection(self, timeout=60):
        conn = open_connection(self.upstream, timeout)
        with self.lock:
            self.connections.add(conn)
        return conn

    def close_connection(self, conn):
        conn.close()
        with self.lock:
            self.connections.discard(conn)

    def save(self, job):
        self.store.put(job)

    def submit(self, data):
        """本地分配版本号、写库，再把任务推给 Colab。Colab 不存任何东西。"""
        if not isinstance(data, dict):
            raise ValueError('请求体必须是对象')
        params = self.shot_params(data)
        if not params['prompt'].strip():
            raise ValueError('请先填写提示词')
        shot_id = data.get('shot_id')
        with self.lock:
            if not (isinstance(shot_id, str) and JOB_ID.fullmatch(shot_id)):
                shot_id = secrets.token_hex(16)
                job = dict(id=shot_id, created=time.time(), status='queued', stage='等待 GPU',
                           params=params, version=1, download={'status': 'pending'})
                self.jobs[shot_id] = job
            else:
                job = self.jobs.get(shot_id)
                if not job:
                    raise ValueError('镜头不存在')
                if job.get('status') in {'queued', 'running', 'cancelling'}:
                    raise ValueError('这个镜头正在生成，请先取消再提交')
                # 草稿提交沿用它占用的版本号，其它镜头顺延到下一版。
                if job.get('local_draft') and job.get('status') == 'draft':
                    version = job.get('version') or 1
                else:
                    version = self.store.next_version(shot_id)
                job.update(status='queued', stage='等待 GPU', params=params, version=version,
                           download={'status': 'pending'})
                for key in ('started', 'finished', 'error', 'video_path', 'video_bytes'):
                    job.pop(key, None)
            self.save(job)
            version = job['version']
        payload = dict(params, shot_id=shot_id, version=version)
        try:
            reply = self._post('/api/jobs', payload)
        except Exception as exc:
            with self.lock:
                failed = self.jobs[shot_id]
                failed.update(status='failed', stage=str(exc), error=str(exc))
                self.save(failed)
            raise
        with self.lock:
            job = self.jobs[shot_id]
            job['status'] = reply.get('status') or 'queued'
            job['stage'] = reply.get('stage') or job.get('stage')
            self.save(job)
            return json.loads(json.dumps(job))

    def _post(self, path, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        conn = self.connection(timeout=30)
        try:
            conn.request('POST', path, body=body, headers={
                'User-Agent': self.user_agent, 'Content-Type': 'application/json',
                'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            raw = response.read(2 * 1024**2)
            if response.status not in {200, 201}:
                try:
                    message = json.loads(raw.decode('utf-8')).get('error', '')
                except (ValueError, UnicodeDecodeError):
                    message = ''
                raise ValueError(message or f'Colab 返回 HTTP {response.status}')
            return json.loads(raw.decode('utf-8'))
        finally:
            self.close_connection(conn)

    def merge_remote(self, remote_jobs):
        """远端只报内存里的任务状态；这里合并进本地 shots/takes。"""
        seen = set()
        for remote in remote_jobs:
            jid = str(remote.get('id', ''))
            if not JOB_ID.fullmatch(jid):
                continue
            seen.add(jid)
            with self.lock:
                job = self.jobs.get(jid)
                if not job or job.get('version') != remote.get('version'):
                    continue
                job['status'] = remote.get('status') or job.get('status')
                job['stage'] = remote.get('stage') or job.get('stage')
                for key in ('started', 'finished'):
                    if remote.get(key) is not None:
                        job[key] = remote[key]
                if remote.get('error'):
                    job['error'] = remote['error']
                if remote.get('progress'):
                    job['progress'] = remote['progress']
                self.save(job)
            self.schedule(jid)
        # 本地还在跑、远端已经没有：Colab 重启或服务丢了内存队列。
        with self.lock:
            stale = [j for j in self.jobs.values()
                     if j.get('status') in {'queued', 'running', 'cancelling'}
                     and j.get('id') not in seen]
            for job in stale:
                job.update(status='interrupted', stage='Colab 服务重启，任务未完成')
                self.save(job)
        return len(stale)

    def schedule(self, jid, retry=False):
        with self.lock:
            job = self.jobs.get(jid)
            if not job or job.get('hidden') or job.get('status') != 'done' or jid in self.scheduled:
                return
            status = job.get('download', {}).get('status')
            if status == 'ready' or (status == 'failed' and not retry):
                return
            self.scheduled.add(jid)
            job['download'] = {**job.get('download', {}), 'status': 'queued', 'error': ''}
            self.save(job)
            self.pending.put(jid)

    def update(self, jid, **values):
        with self.lock:
            job = self.jobs[jid]
            job['download'] = {**job.get('download', {}), **values}
            self.save(job)

    def state(self):
        with self.lock:
            jobs = sorted((j for j in self.jobs.values() if not j.get('hidden')), key=lambda j: j.get('created', 0), reverse=True)
            return json.loads(json.dumps(dict(local=True, engine_online=self.online, error=self.last_error,
                                             last_checked=self.last_checked, output_directory=str(self.root), jobs=jobs)))

    def create_shot(self, params):
        params = self.shot_params(params)
        with self.lock:
            job = dict(id=secrets.token_hex(16), status='draft', stage='草稿 · 尚未提交生成',
                       created=time.time(), params=params, version=1,
                       download={'status': 'pending'}, local_draft=True)
            self.jobs[job['id']] = job
            self.save(job)
            return json.loads(json.dumps(job))

    @staticmethod
    def shot_params(params):
        if not isinstance(params, dict):
            raise ValueError('镜头参数必须是对象')
        defaults=dict(task='t2va', prompt='', resolution='480p', duration=5, aspect='16:9',
                      seed=None, cpu_offload=None, first_image='', last_image='', references=[])
        defaults.update({k:v for k,v in params.items() if k in defaults})
        if defaults['cpu_offload'] is None:
            # 480p 任意时长、768p 5 秒都不需要卸载；768p 更长的档位才默认开启。
            defaults['cpu_offload'] = defaults['resolution'] == '768p' and defaults['duration'] > 5
        if not isinstance(defaults['prompt'], str) or len(defaults['prompt'])>8000:
            raise ValueError('提示词长度不正确')
        return defaults

    def edit_shot(self, jid, params):
        params = self.shot_params(params)
        with self.lock:
            job=self.jobs[jid]
            if job['status'] in {'running','queued','cancelling'}:
                raise ValueError('已提交任务的参数已冻结，请先取消，或新建镜头')
            if job['status']=='draft':
                job['params']=params
            else:
                # Preserve original parameters associated with an existing successful video.
                job['editor_params']=params
            self.save(job)
            return json.loads(json.dumps(job))

    def delete_shot(self, jid):
        with self.lock:
            job=self.jobs[jid]
            if job['status'] in {'running','queued','cancelling'}:
                raise ValueError('请先取消正在生成或排队的任务，再移出列表')
            job['hidden']=True
            self.save(job)

    def refresh(self):
        conn = self.connection()
        remote_jobs = []
        try:
            conn.request('GET', '/api/jobs', headers={'User-Agent': self.user_agent, 'Accept-Encoding': 'identity'})
            response = conn.getresponse()
            if response.status != 200:
                raise RuntimeError(f'Colab 接口 HTTP {response.status}')
            data = json.loads(response.read(8 * 1024**2).decode('utf-8'))
            if not isinstance(data.get('jobs'), list):
                raise ValueError('隧道返回的不是 Pika 任务列表')
            remote_jobs = data['jobs']
            with self.lock:
                self.online, self.last_error, self.last_checked = True, '', time.time()
        finally:
            self.close_connection(conn)
        # 远端是无状态的，只报内存里的任务；本地负责落库和判断中断。
        self.merge_remote(remote_jobs)

    def monitor(self):
        while not self.stop.is_set():
            self.wake.clear()
            try:
                self.refresh()
            except Exception as exc:
                with self.lock:
                    self.online, self.last_error, self.last_checked = False, str(exc), time.time()
            # 下载期间把轮询降到 12 秒，别和下载抢同一条隧道。
            self.wake.wait(12 if self.busy.is_set() else self.interval)

    def worker(self):
        while not self.stop.is_set():
            try:
                jid = self.pending.get(timeout=.3)
            except queue.Empty:
                continue
            try:
                for attempt, delay in enumerate(self.retry_delays):
                    if self.stop.wait(delay):
                        break
                    try:
                        self.download(jid)
                        break
                    except Exception as exc:
                        final = attempt == len(self.retry_delays)-1
                        if final:
                            # 先放开排队标记，再写 failed：否则外部马上重试会被当成重复任务丢掉。
                            with self.lock:
                                self.scheduled.discard(jid)
                        self.update(jid, status='failed' if final else 'retrying', error=str(exc),
                                    bytes_per_second=0, attempt=attempt+1)
            finally:
                with self.lock:
                    self.scheduled.discard(jid)
                self.pending.task_done()

    def download(self, jid):
        job = self.jobs.get(jid) or {}
        version = int(job.get('version') or 1)
        folder = self.root / jid
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f'v{version}.mp4'
        part = folder / f'v{version}.part'
        offset = part.stat().st_size if part.exists() else 0
        self.update(jid, status='downloading', downloaded_bytes=offset, bytes_per_second=0, error='')
        self.busy.set()
        conn = self.connection()
        try:
            headers = {'User-Agent': self.user_agent, 'Accept-Encoding': 'identity'}
            if offset:
                headers['Range'] = f'bytes={offset}-'
            conn.request('GET', f'/media/{jid}?v={version}', headers=headers)
            response = conn.getresponse()
            if response.status == 416:
                part.unlink(missing_ok=True)
                raise RuntimeError('断点失效，下次尝试从头下载')
            if response.status not in {200, 206}:
                raise RuntimeError(f'下载 HTTP {response.status}')
            if 'video/mp4' not in response.getheader('Content-Type', '').lower():
                raise RuntimeError('服务器没有返回 MP4 视频')
            length = int(response.getheader('Content-Length', '0'))
            if length <= 0:
                raise RuntimeError('服务器未提供有效文件长度')
            if response.status == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.getheader('Content-Range', ''))
                if not match or int(match[1]) != offset:
                    raise RuntimeError('断点响应范围不正确')
                total, mode = int(match[3]), 'ab'
            else:
                offset, total, mode = 0, length, 'wb'
            self.update(jid, total_bytes=total, downloaded_bytes=offset)
            received = 0
            started = previous = time.monotonic()
            with part.open(mode) as handle:
                while not self.stop.is_set():
                    block = response.read1(64 * 1024)
                    if not block:
                        break
                    handle.write(block)
                    received += len(block)
                    now = time.monotonic()
                    if now - previous >= .25:
                        handle.flush()
                        self.update(jid, downloaded_bytes=offset + received,
                                    bytes_per_second=round(received / max(.001, now - started)))
                        previous = now
            if self.stop.is_set():
                raise RuntimeError('本地服务停止，下载已保留断点')
            if received != length or part.stat().st_size != total:
                raise RuntimeError(f'下载未完整：{part.stat().st_size}/{total} 字节，已保留断点')
            part.replace(target)
            with self.lock:
                self.jobs[jid]['video_path'] = str(target)
            self.update(jid, status='ready', downloaded_bytes=total, total_bytes=total,
                        bytes_per_second=0, error='', finished=time.time())
        finally:
            self.close_connection(conn)
            self.busy.clear()

    def close(self):
        self.stop.set()
        self.wake.set()
        with self.lock:
            connections = list(self.connections)
        for conn in connections:
            conn.close()
        for thread in self.threads:
            thread.join(timeout=1)
        self.store.close()
