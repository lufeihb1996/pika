"""Local video studio. Persistent monitoring, automatic downloads and local-only playback."""
import argparse
import http.client
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit
from local_media import Downloads, open_connection

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'vercel-web' / 'public'
CACHE_DIR = ROOT / 'outputs'
DEFAULT_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
HOP = {'connection','transfer-encoding','keep-alive','proxy-authenticate','proxy-authorization','te','trailer','upgrade'}
_manager_lock = threading.Lock()

class Handler(SimpleHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, *args):
        pass

    @property
    def downloads(self):
        with _manager_lock:
            if not getattr(self.server, 'downloads', None):
                self.server.downloads = Downloads(self.server.upstream, getattr(self.server, 'cache_dir', CACHE_DIR), DEFAULT_UA)
            return self.server.downloads

    def json(self, value, status=200):
        body=json.dumps(value,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            parsed=urlsplit(self.path)
            if parsed.path=='/api/local/state':
                return self.json(self.downloads.state())
            match=re.fullmatch(r'/(?:local-media|media)/([0-9a-f]{32})',parsed.path)
            if match:
                return self.local_video(match[1], 'download=' in parsed.query)
            if parsed.path.startswith('/api/'):
                return self.proxy()
            return super().do_GET()
        except (BrokenPipeError,ConnectionResetError):
            pass

    def do_POST(self):
        path=urlsplit(self.path).path
        match=re.fullmatch(r'/api/local/shots(?:/([0-9a-f]{32})/(save|delete))?',path)
        if match:
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=100000:
                    return self.json({'error':'请求大小无效'},400)
                data=json.loads(self.rfile.read(size))
                jid,action=match.groups()
                if action=='delete':
                    self.downloads.delete_shot(jid)
                    return self.json({'ok':True})
                job=self.downloads.edit_shot(jid,data) if action=='save' else self.downloads.create_shot(data)
                return self.json(job,200 if action else 201)
            except (ValueError,TypeError) as exc:
                return self.json({'error':str(exc)},400)
            except KeyError:
                return self.json({'error':'镜头不存在'},404)
        match=re.fullmatch(r'/api/local/jobs/([0-9a-f]{32})/retry',path)
        if match:
            size=int(self.headers.get('Content-Length','0'))
            if size>1000:
                return self.json({'error':'请求过大'},400)
            self.rfile.read(size)
            self.downloads.schedule(match[1],retry=True)
            return self.json({'ok':True})
        if path.startswith('/api/'):
            return self.proxy()
        self.send_error(404)

    def local_video(self,jid,download):
        manager=self.downloads
        path=manager.root/(jid+'.mp4')
        with manager.lock:
            ready=manager.jobs.get(jid,{}).get('download',{}).get('status')=='ready'
        if not ready or not path.is_file():
            manager.schedule(jid)
            return self.json({'error':'视频正在下载到电脑，请完成后再播放'},425)
        size=path.stat().st_size
        start,end,status=0,size-1,200
        value=self.headers.get('Range')
        if value:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',value)
            if not match or not any(match.groups()):
                return self.range_error(size)
            a,b=match.groups()
            if not a:
                start=max(0,size-int(b))
            else:
                start=int(a)
                end=min(size-1,int(b)) if b else size-1
            if start>end or start>=size:
                return self.range_error(size)
            status=206
        self.send_response(status)
        self.send_header('Content-Type','video/mp4')
        self.send_header('Content-Length',str(end-start+1))
        self.send_header('Accept-Ranges','bytes')
        self.send_header('Cache-Control','private, max-age=86400')
        if status==206:
            self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        if download:
            self.send_header('Content-Disposition',f'attachment; filename="pika-{jid}.mp4"')
        self.end_headers()
        with path.open('rb') as handle:
            handle.seek(start)
            left=end-start+1
            while left:
                block=handle.read(min(1024*1024,left))
                if not block: break
                self.wfile.write(block)
                left-=len(block)

    def range_error(self,size):
        self.send_response(416)
        self.send_header('Content-Range',f'bytes */{size}')
        self.send_header('Content-Length','0')
        self.end_headers()

    def upstream_connection(self):
        return open_connection(self.server.upstream, 120)

    def proxy(self):
        upstream=self.server.upstream
        try:
            conn=self.upstream_connection()
        except (OSError,http.client.HTTPException):
            return self.json({'error':'无法连接 Colab，请检查后端和隧道'},502)
        headers={k:v for k,v in self.headers.items() if k.lower() not in HOP|{'host','user-agent','accept-encoding'}}
        headers.update({'Host':upstream.netloc,'User-Agent':self.headers.get('User-Agent') or DEFAULT_UA,'Accept-Encoding':'identity','X-Forwarded-Proto':'http'})
        sent=False
        try:
            conn.putrequest(self.command,self.path,skip_host=True,skip_accept_encoding=True)
            for k,v in headers.items():conn.putheader(k,v)
            conn.endheaders()
            left=int(self.headers.get('Content-Length','0'))
            while left:
                block=self.rfile.read(min(1024*1024,left))
                if not block:raise ConnectionError('upload interrupted')
                conn.send(block);left-=len(block)
            response=conn.getresponse()
            capture=urlsplit(self.path).path=='/api/jobs' and response.status in {200,201}
            chunks=[]
            self.send_response(response.status)
            for k,v in response.getheaders():
                if k.lower() in HOP:continue
                if k.lower()=='set-cookie':v=v.replace('; Secure','').replace('; secure','')
                self.send_header(k,v)
            self.send_header('Connection','close')
            self.end_headers();sent=True;self.close_connection=True
            while True:
                block=response.read1(64*1024)
                if not block:break
                if capture:chunks.append(block)
                self.wfile.write(block)
            if capture:
                payload=json.loads(b''.join(chunks))
                for job in payload.get('jobs',[]) if 'jobs' in payload else [payload]:
                    self.downloads.upsert(job)
                self.downloads.wake.set()
        except (OSError,http.client.HTTPException,ValueError):
            if not sent:
                self.json({'error':'无法连接 Colab，请检查后端和隧道'},502)
        finally:
            conn.close()

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--upstream',default='https://mcp-mac.id96.vip')
    parser.add_argument('--port',type=int,default=3091)
    args=parser.parse_args()
    url=urlsplit(args.upstream)
    if url.scheme not in {'http','https'} or not url.hostname or url.path not in ('','/'):
        parser.error('upstream must be an http(s) origin')
    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    server.upstream=url;server.cache_dir=CACHE_DIR
    server.downloads=Downloads(url,CACHE_DIR,DEFAULT_UA)
    print(f'Local studio: http://127.0.0.1:{args.port}',flush=True)
    print(f'Automatic video folder: {CACHE_DIR}',flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.downloads.close()
        server.server_close()

if __name__=='__main__':main()
