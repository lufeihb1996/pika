"""Regression checks for Colab images without ensurepip and cloud env restoration."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "studio"))
import openh3_setup as setup

spec = importlib.util.spec_from_file_location("colab_launcher", ROOT / "colab_launcher.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class SetupTests(unittest.TestCase):
    def test_openh3_archive_restores_without_claiming_models_are_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "openh3"
            root.mkdir()
            (root/"ready.json").write_text("{}")
            archive = Path(directory)/"openh3_env.tar.gz"
            archive.touch()
            with patch.object(setup, "ENV_BACKUP", str(archive)), patch.object(setup, "run") as run:
                self.assertTrue(setup.restore_environment(root))
            self.assertEqual(run.call_args.args, ("tar", "-xzf", archive, "-C", root, "--strip-components=1"))
            self.assertFalse((root/"ready.json").exists())
            packages=root/"venv/lib/python3.12/site-packages"
            for name in ("torch", "comfy_kitchen"):
                (packages/name).mkdir(parents=True)
                (packages/name/"__init__.py").touch()
            with patch.object(setup, "run") as run:
                self.assertFalse(setup.restore_environment(root))
            run.assert_not_called()

    def test_cuda_129_is_upgraded_only_in_refiner_environment(self):
        python = Path("/refiner/venv/bin/python")
        with patch.object(setup, "probe_torch", side_effect=[{"torch":"2.13.0+cu129","cuda":"12.9"}, {"torch":"2.13.0+cu130","cuda":"13.0"}]), patch.object(setup, "run") as run:
            setup.ensure_cuda_torch(python)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], python)
        self.assertIn("https://download.pytorch.org/whl/cu130", run.call_args.args)
        for wheel in setup.CUDA_WHEELS:
            self.assertIn(wheel, run.call_args.args)

    def test_cuda_130_is_reused(self):
        with patch.object(setup, "probe_torch", return_value={"torch":"2.13.0+cu130","cuda":"13.0"}), patch.object(setup, "run") as run:
            setup.ensure_cuda_torch(Path("/refiner/python"))
        run.assert_not_called()

    def test_upgrade_cannot_silently_keep_a_fallback_build(self):
        with patch.object(setup, "probe_torch", return_value={"torch":"2.13.0+cu129","cuda":"12.9"}), patch.object(setup, "run"):
            with self.assertRaisesRegex(RuntimeError, "CUDA 13"):
                setup.ensure_cuda_torch(Path("/refiner/python"))

    def test_partial_venv_is_repaired_and_missing_ensurepip_installed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(setup.subprocess, "run", side_effect=[Mock(returncode=1), Mock(returncode=1)]), patch.object(setup, "run") as run, patch.object(setup.os, "geteuid", return_value=0, create=True), patch.object(setup.shutil, "which", return_value="/usr/bin/apt-get"):
                self.assertEqual(setup.ensure_venv(root), python)
            self.assertEqual(run.call_args_list[0].args, ("apt-get", "update", "-qq"))
            self.assertIn("-venv", run.call_args_list[1].args[-1])
            self.assertIn("venv", run.call_args_list[2].args)
            self.assertEqual(run.call_args_list[-1].args[1:], ("-m", "pip", "--version"))

    def test_working_venv_is_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            python = root / "venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(setup.subprocess, "run", return_value=Mock(returncode=0)), patch.object(setup, "run") as run:
                setup.ensure_venv(root)
            run.assert_not_called()

    def test_cloud_archive_is_only_used_for_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "env.tar.gz"
            backup.touch()
            work = root / "work"
            with patch.object(launcher, "ENV_BACKUP", str(backup)), patch.object(launcher.subprocess, "run") as run:
                launcher.restore_pika_environment(work)
            self.assertEqual(run.call_args.args[0], ["tar", "-xzf", str(backup), "-C", str(work)])
            (work / "environment-ready.json").touch()
            python = work / "venv-py312/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()
            with patch.object(launcher.subprocess, "run") as run:
                launcher.restore_pika_environment(work)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
