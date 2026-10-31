import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from local_media import Downloads


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.jobs=[]
        self.requests=[]
        self.truncate=False
        self.seen=0
        owner=self
        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/api/jobs':
                    content=json.dumps({'jobs':owner.jobs}).encode()
                    self.send_response(200);self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)
                    return
                owner.requests.append(self.headers.get('Range'))
                offset=int(self.headers.get('Range','bytes=0-')[6:].split('-')[0])
                payload=b'0123456789'*1000
                remaining=payload[offset:]
                self.send_response(206 if offset else 200)
                self.send_header('Content-Type','video/mp4')
                self.send_header('Content-Length',str(len(remaining)))
                if offset:self.send_header('Content-Range',f'bytes {offset}-{len(payload)-1}/{len(payload)}')
                self.end_headers()
                if owner.truncate and len(owner.requests)==1:
                    self.wfile.write(remaining[:1000]);self.close_connection=True
                else:self.wfile.write(remaining)
            def log_message(self,*args):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.url=urlsplit(f'http://127.0.0.1:{self.server.server_port}')
        self.manager=None

    def tearDown(self):
        if self.manager:self.manager.close()
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()

    def wait(self,jid,status):
        deadline=time.time()+5
        while time.time()<deadline:
            job=self.manager.jobs.get(jid,{})
            if job.get('download',{}).get('status')==status:return
            time.sleep(.025)
        self.fail(str(self.manager.state()))

    def test_background_download_without_browser_and_no_duplicates(self):
        jid='1'*32
        self.manager=Downloads(self.url,self.root,interval=.1,retry_delays=(0,))
        self.manager.jobs[jid]=dict(id=jid,version=1,status='done',created=1,params={},
                                    download={'status':'pending'})
        self.manager.schedule(jid)
        self.wait(jid,'ready')
        self.assertEqual((self.root/jid/'v1.mp4').stat().st_size,10000)
        # 远端重复报同一条状态时，不应再下第二遍。
        self.manager.merge_remote([dict(id=jid,version=1,status='done')])
        time.sleep(.15)
        self.assertEqual(len(self.requests),1)
        self.manager.close();self.manager=None
        # 重启后本地仍然记得已经下载过。
        self.manager=Downloads(self.url,self.root,interval=.1,retry_delays=(0,))
        self.assertEqual(self.manager.jobs[jid]['download']['status'],'ready')

    def test_truncated_download_is_not_promoted_and_can_resume(self):
        jid='2'*32
        self.truncate=True
        self.manager=Downloads(self.url,self.root,interval=.1,retry_delays=(0,))
        self.manager.jobs[jid]=dict(id=jid,version=1,status='done',created=1,params={},
                                    download={'status':'pending'})
        self.manager.schedule(jid)
        self.wait(jid,'failed')
        self.assertFalse((self.root/jid/'v1.mp4').exists())
        self.assertEqual((self.root/jid/'v1.part').stat().st_size,1000)
        self.manager.schedule(jid,retry=True)
        self.wait(jid,'ready')
        self.assertEqual(self.requests,[None,'bytes=1000-'])
        self.assertEqual((self.root/jid/'v1.mp4').read_bytes(),b'0123456789'*1000)

    def test_local_media_missing_returns_immediately(self):
        import local_web,urllib.request,urllib.error
        self.manager=Downloads(self.url,self.root,interval=.1,retry_delays=(0,))
        server=ThreadingHTTPServer(('127.0.0.1',0),local_web.Handler)
        server.upstream=self.url;server.downloads=self.manager
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            started=time.monotonic()
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/local-media/'+('3'*32))
            self.assertEqual(error.exception.code,425)
            self.assertLess(time.monotonic()-started,1)
            self.assertEqual(self.requests,[])
        finally:server.shutdown();server.server_close()

    def test_shot_create_edit_delete_and_persistence(self):
        # 这个用例只测增删改，不需要轮询；interval 放大避免 monitor 把 running 标成中断。
        self.manager=Downloads(self.url,self.root,interval=100,retry_delays=(0,))
        draft=self.manager.create_shot({})
        self.assertEqual(draft['status'],'draft')
        self.assertEqual(draft['params']['prompt'],'')
        self.manager.edit_shot(draft['id'],{'prompt':'a saved shot','seed':42})
        self.assertEqual(self.manager.jobs[draft['id']]['params']['seed'],42)
        self.manager.close();self.manager=None
        self.manager=Downloads(self.url,self.root,interval=100,retry_delays=(0,))
        self.assertEqual(self.manager.jobs[draft['id']]['params']['prompt'],'a saved shot')
        self.manager.delete_shot(draft['id'])
        self.assertFalse(any(j['id']==draft['id'] for j in self.manager.state()['jobs']))
        # 远端出现本地没记录的任务时，不能凭空创建镜头。
        remote=dict(id='4'*32,version=1,status='cancelled')
        self.manager.merge_remote([remote])
        self.assertFalse(any(j['id']==remote['id'] for j in self.manager.state()['jobs']))
        active=self.manager.create_shot({'prompt':'busy'})
        # 先让远端也报同一个任务在跑，再改本地状态，避免 monitor 在中间把它标成中断。
        self.jobs=[dict(id=active['id'],version=1,status='running')]
        self.manager.jobs[active['id']]['status']='running'
        try:
            self.manager.delete_shot(active['id'])
        except ValueError:
            pass
        else:
            self.fail(f"删除没有拦截；status={self.manager.jobs[active['id']]['status']} "
                      f"remote={self.jobs}")
        with self.assertRaises(ValueError):self.manager.edit_shot(active['id'],{})


if __name__=='__main__':unittest.main()
