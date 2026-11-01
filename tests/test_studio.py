"""HTTP, subprocess queue, cancellation and bundle tests; no GPU/network downloads."""
import base64
import gzip
import http.cookiejar
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "studio"))
from server import Studio, Handler, ThreadingHTTPServer
from progress_runner import sampling_observer, build_refine_ladder, extract_pass2_args


class StudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cli = self.root / "cli"
        self.cli.touch()
        with patch.dict(os.environ, {"PIKA_STUDIO_PASSWORD": "test-password"}):
            self.app = Studio(self.root, self.cli)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.app = self.app
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        jar = http.cookiejar.CookieJar()
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.fake = self.root / "fake_cli.py"
        self.fake.write_text("import pathlib,sys,time\nprint('test subprocess started',flush=True)\ntime.sleep(float(sys.argv[2]))\npathlib.Path(sys.argv[1]).write_bytes(b'test-media-bytes')\n", encoding="utf-8")
        self.app.command = lambda j: [sys.executable, str(self.fake), str(self.app.output_path(j)), "0.15"]

    def tearDown(self):
        for job in list(self.app.jobs.values()):
            if job["status"] in {"running", "queued"}:
                self.app.cancel(job["id"])
        deadline = time.time()+5
        while self.app.active and time.time() < deadline:
            time.sleep(.05)
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None):
        if data is not None and not isinstance(data, bytes):
            data = json.dumps(data).encode()
        req = urllib.request.Request(self.base+path, data=data, headers=headers or {"Content-Type": "application/json"})
        return self.client.open(req, timeout=5)

    def login(self):
        self.assertTrue(json.load(self.request("/api/login", {"password": "test-password"}))["ok"])

    def wait(self, job, target):
        deadline = time.time()+8
        while self.app.jobs[job["id"]]["status"] not in target and time.time()<deadline:
            time.sleep(.03)
        self.assertIn(self.app.jobs[job["id"]]["status"], target)

    def test_auth_and_traversal(self):
        # 无状态执行器不再提供静态页面，只暴露 /api 与 /media。
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request("/api/jobs")
        self.assertEqual(exc.exception.code, 401)
        self.login()
        self.assertTrue(json.load(self.request("/api/health"))["authenticated"])
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request("/../server.py")
        self.assertEqual(exc.exception.code, 404)

    def test_job_media_range_and_restart(self):
        self.login()
        prompt = 'literal prompt $(nothing-executes); <script>alert(1)</script>'
        job = json.load(self.request("/api/jobs", {"prompt": prompt}))
        self.wait(job, {"done"})
        response = self.request("/media/"+job["id"], headers={"Range":"bytes=2-5"})
        self.assertEqual(response.status, 206)
        self.assertEqual(response.read(), b"st-m")
        self.assertEqual(self.app.jobs[job["id"]]["params"]["prompt"], prompt)
        self.assertIn("test subprocess started", json.load(self.request(f"/api/jobs/{job['id']}/log"))["log"])
        self.assertTrue(self.app.output_path(job).is_file())

    def test_upload_and_invalid_inputs(self):
        self.login()
        upload = json.load(self.request("/api/uploads?name=reference.png", b"test-image", {"Content-Type":"application/octet-stream"}))
        self.assertEqual(upload["kind"], "image")
        params = self.app.validate({"prompt":"test", "task":"i2va", "first_image":upload["id"]})
        self.assertEqual(params["task"], "i2va")
        for invalid in ({"task":[]}, {"duration":16}, {"seed": True}, {"first_image":"../../outside"}, {"task":"ref2va","references":[]}, {"start_sigma": 1.2}):
            with self.assertRaises(ValueError):
                self.app.validate({"prompt":"test", **invalid})
        vid = json.load(self.request("/api/uploads?name=pass1.mp4", b"test-video", {"Content-Type":"application/octet-stream"}))
        self.assertEqual(vid["kind"], "video")
        p2 = self.app.validate({"prompt":"<Picture 1> refine", "task":"ref2va", "references":[{"kind":"image","id":upload["id"]}], "source_video":vid["id"], "second_pass":True, "start_sigma":0.78})
        self.assertTrue(p2["second_pass"])
        self.assertEqual(p2["source_video"], vid["id"])
        cmd = Studio.command(self.app, {"id": "b"*32, "version": 2, "params": p2})
        self.assertIn("--second-pass", cmd)
        self.assertIn("--source-video", cmd)
        self.assertIn("--start-sigma", cmd)

    def test_cancel_running_and_queued(self):
        self.app.command = lambda j: [sys.executable, str(self.fake), str(self.app.output_path(j)), "30"]
        first = self.app.submit({"prompt":"first"})
        self.wait(first, {"running"})
        second = self.app.submit({"prompt":"second"})
        self.app.cancel(second["id"])
        self.assertEqual(second["status"], "cancelled")
        self.app.cancel(first["id"])
        self.wait(first, {"cancelled"})

    def test_restart_clears_queue_but_keeps_video_files(self):
        job_id = "a"*32
        output = self.root / "outputs" / f"{job_id}-v1.mp4"
        output.write_bytes(b"render")
        with patch.dict(os.environ, {"PIKA_STUDIO_PASSWORD":"test"}):
            app = Studio(self.root, self.cli)
        try:
            # 无状态：重启后内存队列清空，但已生成的文件还在，仍能通过 /media 取。
            self.assertEqual(app.jobs, {})
            self.assertTrue(app.output_path({"id": job_id, "version": 1}).is_file())
        finally:
            app.close()

    def test_local_frontend_proxy_login_upload_and_video_range(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('local_web', ROOT/'local_web.py')
        web = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web)
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
        proxy.upstream = urlsplit(self.base)
        proxy.cache_dir = self.root / 'proxy_cache'
        self.app.password = ''
        threading.Thread(target=proxy.serve_forever,daemon=True).start()
        original = self.base
        self.base = f'http://127.0.0.1:{proxy.server_port}'
        try:
            self.assertEqual(self.request('/').status, 200)
            upload=json.load(self.request('/api/uploads?name=test.png', b'test-image', {'Content-Type':'application/octet-stream'}))
            job=json.load(self.request('/api/jobs', {'task':'i2va', 'prompt':'test', 'first_image':upload['id']}))
            self.wait(job, {'done'})
            deadline=time.time()+6
            while time.time()<deadline:
                data=json.load(self.request('/api/local/state'))
                if any(j['id']==job['id'] and j.get('download',{}).get('status')=='ready' for j in data['jobs']): break
                time.sleep(.05)
            response=self.request('/media/'+job['id'],headers={'Range':'bytes=2-5'})
            self.assertEqual(response.status,206)
            self.assertEqual(response.read(),b'st-m')
            self.assertTrue((proxy.cache_dir / job['id'] / 'v1.mp4').is_file())
        finally:
            self.base=original
            if getattr(proxy,'downloads',None): proxy.downloads.close()
            proxy.shutdown()
            proxy.server_close()


class BundleTests(unittest.TestCase):
    def test_no_password_allows_direct_access(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"PIKA_STUDIO_PASSWORD": ""}):
            app = Studio(Path(tmp), Path(tmp)/'cli', preview=True)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            server.app = app
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url=f'http://127.0.0.1:{server.server_port}'
                health=json.load(urllib.request.urlopen(url+'/api/health'))
                self.assertTrue(health['authenticated'])
                self.assertFalse(health['auth_required'])
                self.assertEqual(json.load(urllib.request.urlopen(url+'/api/jobs'))['jobs'], [])
            finally:
                server.shutdown()
                server.server_close()
                app.close()

    def test_sampling_reports_real_steps_and_removes_hook(self):
        class Hook:
            def __init__(self, generator): self.generator=generator
            def remove(self): self.generator.hook=None
        class Generator:
            hook=None
            def register_forward_hook(self, hook): self.hook=hook; return Hook(self)
        g=Generator()
        def sample(generator, **kwargs):
            for _ in range(6): generator.hook(generator, (), None)
            return 'unchanged'
        with patch('progress_runner.emit') as emit, patch('progress_runner.time.monotonic',side_effect=[0,1,2,3,4,5,6]):
            sync=[]
            result=sampling_observer(sample,lambda:sync.append(True))(g,sigmas_v=list(range(7)))
        self.assertEqual(result,'unchanged')
        self.assertEqual(len(sync),6)
        self.assertEqual(emit.call_args.kwargs['completed'],6)
        self.assertEqual(emit.call_args.kwargs['total'],6)
        self.assertIsNone(g.hook)
        sigmas_v, sigmas_a = build_refine_ladder(0.78)
        self.assertEqual(len(sigmas_v), 4)
        self.assertAlmostEqual(sigmas_v[0], 0.78, places=4)
        self.assertEqual(sigmas_v[-1], 0.0)
        self.assertEqual(len(sigmas_a), 4)
        clean, sp, src, sigma, scale = extract_pass2_args(["--task", "ref2va", "--second-pass", "--source-video", "in.mp4", "--start-sigma", "0.78", "--upscale-factor", "2.25", "--output", "out.mp4"])
        self.assertTrue(sp)
        self.assertEqual(src, "in.mp4")
        self.assertAlmostEqual(sigma, 0.78)
        self.assertAlmostEqual(scale, 2.25)
        self.assertEqual(clean, ["--task", "ref2va", "--output", "out.mp4"])

    def test_bundle_matches_sources_and_does_not_auto_generate(self):
        target = next((ROOT / "dist").glob("*.py"))
        spec = importlib.util.spec_from_file_location("bundled_launcher", target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        files = json.loads(gzip.decompress(base64.b64decode(module.BUNDLE_GZ_B64)))
        self.assertEqual(set(files), {"installer_runtime.py", "server.py", "progress_runner.py",
                                      "openh3_cli.py", "openh3_setup.py"})
        for name in ("progress_runner.py", "openh3_cli.py", "openh3_setup.py"):
            self.assertEqual(files[name], (ROOT / "studio" / name).read_text(encoding="utf-8"))
        # 构建时会把 APP_VERSION 换成 git 版本号，比较前先抹掉。
        strip = lambda text: __import__("re").sub(r'APP_VERSION = "[^"]+"', 'APP_VERSION = "X"', text)
        self.assertEqual(strip(files["server.py"]),
                         strip((ROOT / "studio" / "server.py").read_text(encoding="utf-8")))
        import ast
        tree = ast.parse(files["installer_runtime.py"])
        prepare = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="prepare_environment")
        self.assertIsInstance(prepare.body[-1],ast.Return)
        self.assertNotIn("build_command(",ast.unparse(prepare))
        notebook = json.loads(next((ROOT / "dist").glob("*.ipynb")).read_text(encoding="utf-8"))
        self.assertEqual(''.join(notebook['cells'][1]['source']), target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
