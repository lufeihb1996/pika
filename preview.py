"""Local UI preview only; no models or GPU required."""
import os
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
os.environ.pop("PIKA_STUDIO_PASSWORD", None)
sys.path.insert(0, str(root / "studio"))
sys.argv = ["server.py", "--preview", "--host", "127.0.0.1", "--port", "3090", "--root", str(root / ".preview-data")]
print("Local preview: http://127.0.0.1:3090  |  No password", flush=True)
from server import main
main()
