"""Routing, process isolation and geometry checks; no GPU or model downloads."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "studio"))
import openh3_cli
import progress_runner
from server import Studio


class OpenH3Tests(unittest.TestCase):
    def test_output_budget_and_aspect(self):
        with self.assertRaises(ValueError):
            openh3_cli.target_size(0, 1080, "1080p")
        self.assertEqual(openh3_cli.target_size(1920, 1080, "1080p"), (1920, 1080))
        self.assertEqual(openh3_cli.target_size(1080, 1920, "1080p"), (1080, 1920))
        for width, height in ((1344, 768), (768, 1344), (1024, 1024), (2048, 896)):
            w, h = openh3_cli.target_size(width, height, "1080p")
            self.assertLess(abs(w * h / (1920 * 1080) - 1), .004)
            self.assertLess(abs((w / h) / (width / height) - 1), .004)
            self.assertEqual((w % 2, h % 2), (0, 0))

    def test_routes_before_gpu_imports(self):
        argv = ["runner", "--second-pass", "--source-video", "draft.mp4", "--output", "new.mp4"]
        with patch.object(sys, "argv", argv), patch.dict(os.environ, {"PIKA_REFINER_BACKEND": "openh3"}), patch.object(openh3_cli, "run_job") as job:
            progress_runner.main()
        clean, source, sigma, scale = job.call_args.args
        self.assertEqual(source, "draft.mp4")
        self.assertEqual(clean, ["--output", "new.mp4"])
        self.assertEqual((sigma, scale), (.78, 2.0))

    def test_reference_flags_are_not_consumed_as_backend_abbreviations(self):
        refs = ["--ref", "image:/uploads/person.png", "--ref", "video:/uploads/scene.mp4"]
        for backend_args in ([], ["--refiner-backend", "openh3"]):
            with self.subTest(backend_args=backend_args):
                argv = ["runner", "--task", "ref2va", *refs, *backend_args,
                        "--second-pass", "--source-video", "draft.mp4", "--output", "new.mp4"]
                with patch.object(sys, "argv", argv), patch.dict(os.environ, {"PIKA_REFINER_BACKEND": "openh3"}), patch.object(openh3_cli, "run_job") as job:
                    progress_runner.main()
                self.assertEqual(job.call_args.args[0], ["--task", "ref2va", *refs, "--output", "new.mp4"])

    def test_fused_stages_use_separate_processes_and_keep_old_output(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "old.mp4"
            output.write_bytes(b"previous successful artifact")
            calls = []
            def run(cmd, **kwargs):
                calls.append((cmd, kwargs))
                if len(calls) == 1:
                    self.assertIn("progress_runner.py", cmd[1])
                    self.assertNotIn("--second-pass", cmd)
                    Path(cmd[cmd.index("--output") + 1]).write_bytes(b"draft")
                else:
                    self.assertEqual(cmd[0], "refiner-python")
                    self.assertEqual(cmd[cmd.index("--resolution") + 1], "1080p")
                    raise RuntimeError("simulated refiner failure")
            with patch("openh3_setup.prepare", return_value=("refiner-python", Path(directory))), patch.object(openh3_cli.subprocess, "run", side_effect=run):
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    openh3_cli.run_job(["--output", str(output), "--prompt", "test"], "", .78, 2)
            self.assertEqual(len(calls), 2)
            self.assertEqual(output.read_bytes(), b"previous successful artifact")
            self.assertEqual(calls[0][1]["env"]["PIKA_REFINER_BACKEND"], "pika")

    def test_existing_source_skips_pika(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "draft.mp4"
            source.touch()
            with patch("openh3_setup.prepare", return_value=("refiner-python", Path(directory))), patch.object(openh3_cli.subprocess, "run") as run:
                openh3_cli.run_job(["--output", str(Path(directory) / "new.mp4")], str(source), .78, 2.5)
            self.assertEqual(run.call_count, 1)
            self.assertIn("2k", run.call_args.args[0])

    def test_missing_source_fails_before_setup(self):
        with patch("openh3_setup.prepare") as setup:
            with self.assertRaises(FileNotFoundError):
                openh3_cli.run_job(["--output", "new.mp4"], "nonexistent-source.mp4", .78, 2)
            setup.assert_not_called()

    def test_queue_freezes_backend_and_legacy_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Studio(Path(directory), Path(directory) / "cli", preview=True)
            try:
                with patch.dict(os.environ, {"PIKA_REFINER_BACKEND": "openh3"}):
                    params = app.validate({"prompt": "test", "second_pass": True})
                self.assertEqual(params["refiner_backend"], "openh3")
                job = {"id": "a" * 32, "version": 1, "params": params}
                cmd = app.command(job)
                self.assertEqual(cmd[cmd.index("--refiner-backend") + 1], "openh3")
                del params["refiner_backend"]
                cmd = app.command(job)
                self.assertEqual(cmd[cmd.index("--refiner-backend") + 1], "pika")
                with self.assertRaises(ValueError):
                    app.validate({"prompt": "test", "refiner_backend": "invalid"})
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
