"""Exercise the download manifest and incomplete-file handling without model downloads."""
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
spec = importlib.util.spec_from_file_location("colab_moss", ROOT / "colab_moss.py")
moss = importlib.util.module_from_spec(spec)
spec.loader.exec_module(moss)


class MossDownloadTests(unittest.TestCase):
    def run_download(self, root, downloader):
        hf = types.ModuleType("huggingface_hub")
        hf.constants = types.SimpleNamespace(HF_HUB_DISABLE_XET=False)
        hf.hf_hub_download = downloader
        # Small fixture sizes exercise the actual download loop without large files.
        manifest = tuple((name, i + 2) for i, (name, _) in enumerate(moss.MOSS_FILES))
        with patch.dict(sys.modules, {"huggingface_hub": hf}), patch.object(moss.importlib.util, "find_spec", return_value=True), patch.object(moss, "MOSS_FILES", manifest), patch.dict(moss.os.environ, {"HF_TOKEN": "fixture-token"}), contextlib.redirect_stdout(io.StringIO()):
            result = moss.download_moss(root)
        return result, hf.constants

    def test_exact_three_pinned_files_and_comfy_directories_without_torch_installs(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def download(**kwargs):
                self.assertEqual(moss.os.environ["HF_HUB_DISABLE_XET"], "1")
                calls.append(kwargs)
                path = root / kwargs["filename"]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x" * (len(calls) + 1))
                return str(path)
            with patch.object(moss.subprocess, "run") as run:
                result, constants = self.run_download(root, download)
            run.assert_not_called()
            self.assertEqual(result, root)
            self.assertTrue(constants.HF_HUB_DISABLE_XET)
            self.assertEqual([call["filename"] for call in calls], [name for name, _ in moss.MOSS_FILES])
            for call in calls:
                self.assertEqual(call["repo_id"], "Comfy-Org/MOSS-Image")
                self.assertEqual(call["revision"], moss.MOSS_REV)
                self.assertEqual(call["local_dir"], str(root))
                self.assertNotIn("force_download", call)

    def test_partial_file_is_retried_instead_of_reported_ready(self):
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            names = [name for name, _ in moss.MOSS_FILES]
            def download(**kwargs):
                calls.append(kwargs)
                path = root / kwargs["filename"]
                path.parent.mkdir(parents=True, exist_ok=True)
                size = names.index(kwargs["filename"]) + 2
                path.write_bytes(b"x" * (1 if len(calls) == 1 else size))
                return path
            self.run_download(root, download)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[0]["filename"], calls[1]["filename"])
        self.assertTrue(calls[1]["force_download"])

    def test_still_incomplete_download_fails_before_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "incomplete"
            path.write_bytes(b"x")
            with self.assertRaisesRegex(RuntimeError, "文件不完整"):
                self.run_download(Path(temp), lambda **kwargs: path)


if __name__ == "__main__":
    unittest.main()
