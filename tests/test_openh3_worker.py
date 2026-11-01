"""Resident lifecycle, reuse, failures and cancellation, without GPU imports."""
from collections import OrderedDict
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "studio"))
import openh3_cli
import openh3_worker as worker


def job(root):
    return {"op":"refine", "args":dict(source=str(root/"source.mp4"), output=str(root/"output.mp4"),
        prompt_file=str(root/"prompt.txt"), resolution="1080p", start_sigma=.78, seed=42)}


class WorkerTests(unittest.TestCase):
    def test_real_main_model_factory_loads_and_patches_once(self):
        nodes, regen = Mock(), Mock()
        nodes.UNETLoader.return_value.load_unet.return_value = [object()]
        model = object()
        regen.OpenH3RegenLora.return_value.apply.return_value = [model]
        with patch.object(openh3_cli, "_MODELS", OrderedDict()):
            self.assertIs(openh3_cli.main_model(nodes, regen), model)
            self.assertIs(openh3_cli.main_model(nodes, regen), model)
        nodes.UNETLoader.return_value.load_unet.assert_called_once()
        regen.OpenH3RegenLora.return_value.apply.assert_called_once()

    def start(self, root, refine):
        ready = threading.Event()
        thread = threading.Thread(target=worker.serve, args=(root, root, refine, ready), daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        return thread

    def shutdown(self, root):
        connection, reader, _ = worker.connect(root)
        worker.send(connection, {"op":"shutdown"})
        self.assertEqual(json.loads(reader.readline())["kind"], "bye")
        reader.close()
        connection.close()

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Resident runtime is Linux; run socket tests on Colab")
    def test_two_jobs_reuse_process_and_model_object(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            models = OrderedDict()
            factories, results = [], []
            def refine(args):
                model = openh3_cli.cached(models, "main", lambda: factories.append(1) or object(), "main", 6)
                results.append(model)
                print("job-finished", flush=True)
            thread = self.start(root, refine)
            try:
                first, second = io.StringIO(), io.StringIO()
                a = worker.request(root, job(root), first)
                b = worker.request(root, job(root), second)
                self.assertEqual(a["pid"], b["pid"])
                self.assertEqual(len(factories), 1)
                self.assertIs(results[0], results[1])
                self.assertIn('"cache_hit": false', first.getvalue())
                self.assertIn('"cache_hit": true', second.getvalue())
                self.assertIn("job-finished", second.getvalue())
                # The next CLI sees the same already-running process.
                with patch.object(worker.subprocess, "Popen") as spawn:
                    worker.ensure_running(sys.executable, root, root)
                spawn.assert_not_called()
            finally:
                self.shutdown(root)
                thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse((root/"worker.sock").exists())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Resident runtime is Linux; run socket tests on Colab")
    def test_failure_reports_error_then_discards_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def fail(args):
                raise RuntimeError("simulated inference failure")
            thread = self.start(root, fail)
            with self.assertRaisesRegex(RuntimeError, "simulated inference failure"):
                worker.request(root, job(root), io.StringIO())
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse((root/"worker.sock").exists())

    def test_condition_cache_is_bounded_and_refreshes_recent_entries(self):
        cache = OrderedDict()
        a = openh3_cli.cached(cache, "a", object, "test", 2)
        openh3_cli.cached(cache, "b", object, "test", 2)
        self.assertIs(openh3_cli.cached(cache, "a", object, "test", 2), a)
        openh3_cli.cached(cache, "c", object, "test", 2)
        self.assertEqual(list(cache), ["a", "c"])

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Resident runtime is Linux; run socket tests on Colab")
    def test_proxy_disconnect_terminates_active_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = """
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from openh3_worker import serve
def fake(args):
    print('job-started',flush=True)
    time.sleep(60)
serve(Path(sys.argv[2]),Path(sys.argv[2]),fake)
"""
            process = subprocess.Popen([sys.executable,"-c",script,str(ROOT/"studio"),str(root)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name=="posix")
            try:
                deadline = time.monotonic()+10
                while not (root/"worker.sock").exists():
                    self.assertIsNone(process.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.05)
                connection, reader, _ = worker.connect(root)
                worker.send(connection, job(root))
                self.assertIn("job-started", reader.readline())
                reader.close()
                connection.close()
                process.wait(timeout=5)
                self.assertNotEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    unittest.main()
