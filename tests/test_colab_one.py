"""Check the actual downloader payload without Colab, GPU or model downloads."""
import contextlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("colab_one", ROOT / "colab_one.py")
colab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(colab)


class ColabDownloadTests(unittest.TestCase):
    def test_startup_prepares_environment_before_model_download(self):
        order = []
        functions = ("mount_drive", "restore_env", "sync_code", "ensure_download_environment", "prefetch_models", "launch")
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(colab, "step"))
            for name in functions:
                stack.enter_context(patch.object(colab, name, side_effect=lambda name=name: order.append(name)))
            colab.main()
        self.assertEqual(order, list(functions))

    def test_restored_environment_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            python = Path(temp) / "python"
            python.touch()
            with patch.object(colab, "PY", str(python)), patch.object(colab, "SRC", Path(temp) / "missing"):
                colab.ensure_download_environment()

    def test_fresh_environment_is_prepared_before_downloading(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installer = root / "installer_runtime.py"
            marker = root / "prepared.txt"
            installer.write_text(
                "from pathlib import Path\nCONFIG = {}\n"
                "def prepare_environment():\n"
                f"    Path({str(marker)!r}).write_text(CONFIG['storage_root'])\n",
                encoding="utf-8")
            with patch.object(colab, "PY", str(root / "missing-python")), patch.object(colab, "SRC", root), patch.object(colab, "WORK", str(root / "Pika_H3_Flex_Official")), contextlib.redirect_stdout(io.StringIO()):
                colab.ensure_download_environment()
            self.assertEqual(marker.read_text(), str(root))

    def downloader_call(self):
        with patch.object(colab.subprocess, "run") as run, contextlib.redirect_stdout(io.StringIO()):
            colab.prefetch_models()
        run.assert_called_once()
        return run.call_args

    def test_both_tasks_use_pinned_versions_and_shared_components_once(self):
        call = self.downloader_call()
        command = call.args[0]
        downloads = []
        hf = types.ModuleType("huggingface_hub")
        hf.snapshot_download = lambda repo, **kw: downloads.append((repo, kw))
        with patch.dict(sys.modules, {"huggingface_hub": hf}), contextlib.redirect_stdout(io.StringIO()):
            exec(compile(command[2], "download-payload", "exec"), {})
        self.assertEqual(len(downloads), 2)
        adapter, base = downloads
        self.assertEqual(adapter[0], "Pika-Labs/Pika-H3-Flex")
        self.assertEqual(adapter[1]["revision"], colab.ADAPTER_REV)
        self.assertIn("ref2va/*", adapter[1]["allow_patterns"])
        self.assertIn("fl2va/*", adapter[1]["allow_patterns"])
        self.assertEqual(base[0], colab.BASE_REPO)
        self.assertEqual(base[1]["revision"], colab.BASE_REV)
        patterns = base[1]["allow_patterns"]
        for folder in ("transformer_ref", "transformer", "text_encoder", "tokenizer", "processor", "vae", "audio_vae"):
            self.assertEqual(patterns.count(folder + "/*"), 1)
        self.assertEqual(command[:2], [colab.PY, "-c"])
        self.assertTrue(call.kwargs["check"])

    def test_existing_or_partial_models_do_not_skip_completeness_check(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(colab, "WORK", temp):
            for folder in ("transformer", "transformer_ref"):
                shard = Path(temp) / "hf/hub" / ("models--" + colab.BASE_REPO.replace("/", "--")) / "snapshots" / colab.BASE_REV / folder / "one-shard.safetensors"
                shard.parent.mkdir(parents=True, exist_ok=True)
                shard.write_bytes(b"incomplete-model-fixture")
                call = self.downloader_call()
                self.assertEqual(call.kwargs["env"]["HF_HUB_CACHE"], f"{temp}/hf/hub")
                self.assertEqual(shard.read_bytes(), b"incomplete-model-fixture")


if __name__ == "__main__":
    unittest.main()
