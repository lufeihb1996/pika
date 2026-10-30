"""本地视频库：shots / takes / shot_assets。

只有电脑这边存状态。Colab 是无状态执行器，跑完只留 MP4，不建库。
- shots       镜头：提示词、模式、分辨率、时长、画幅、种子、显存策略
- takes      这个镜头的第 N 次生成：状态、进度、视频路径、下载结果
- shot_assets 首帧 / 尾帧 / 参考素材
"""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS shots (
    id                TEXT PRIMARY KEY,
    created           REAL NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT '',
    stage             TEXT NOT NULL DEFAULT '',
    task              TEXT,
    prompt            TEXT,
    resolution        TEXT,
    duration          INTEGER,
    aspect            TEXT,
    seed              INTEGER,
    cpu_offload       INTEGER NOT NULL DEFAULT 0,
    first_image       TEXT,
    last_image        TEXT,
    hidden            INTEGER NOT NULL DEFAULT 0,
    local_draft       INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    editor_task       TEXT,
    editor_prompt     TEXT,
    editor_resolution TEXT,
    editor_duration   INTEGER,
    editor_aspect     TEXT,
    editor_seed       INTEGER,
    editor_cpu_offload INTEGER,
    editor_first_image TEXT,
    editor_last_image TEXT
);
CREATE INDEX IF NOT EXISTS shots_created ON shots(created);
CREATE INDEX IF NOT EXISTS shots_status ON shots(status);

CREATE TABLE IF NOT EXISTS takes (
    shot_id           TEXT NOT NULL,
    version           INTEGER NOT NULL,
    created           REAL NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT '',
    stage             TEXT NOT NULL DEFAULT '',
    started           REAL,
    finished          REAL,
    error             TEXT,
    prompt            TEXT,
    resolution        TEXT,
    duration          INTEGER,
    aspect            TEXT,
    seed              INTEGER,
    cpu_offload       INTEGER,
    progress_phase    TEXT,
    progress_message  TEXT,
    progress_completed INTEGER,
    progress_total    INTEGER,
    sampling_seconds  REAL,
    sampling_eta_seconds REAL,
    video_path        TEXT,
    video_bytes       INTEGER,
    download_status   TEXT,
    download_error    TEXT,
    downloaded_bytes  INTEGER,
    total_bytes       INTEGER,
    PRIMARY KEY (shot_id, version)
);

CREATE TABLE IF NOT EXISTS shot_assets (
    shot_id   TEXT NOT NULL,
    role      TEXT NOT NULL,
    position  INTEGER NOT NULL DEFAULT 0,
    kind      TEXT NOT NULL,
    upload_id TEXT NOT NULL,
    PRIMARY KEY (shot_id, role, position)
);
"""

LEGACY_TABLES = ("jobs", "job_references", "job_progress", "job_downloads", "downloads", "uploads")


class JobStore:
    def __init__(self, path, migrate_dir=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self._drop_legacy_tables()
        self.db.executescript(SCHEMA)
        self.db.commit()
        if migrate_dir:
            self.migrate(migrate_dir)

    def _drop_legacy_tables(self):
        existing = {row[0] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        legacy = set(existing) & set(LEGACY_TABLES)
        # 旧版 takes 用 (shot_id, take_no) 做主键，主键不可改，整表重建后从 .json.migrated 重导。
        if "takes" in existing:
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(takes)")}
            if "take_no" in columns:
                legacy.update({"shots", "takes", "shot_assets"})
        if legacy:
            for table in legacy:
                self.db.execute(f"DROP TABLE IF EXISTS {table}")
            self.db.commit()

    # ---------- 写入 ----------

    def next_version(self, shot_id):
        row = self.db.execute("SELECT MAX(version) FROM takes WHERE shot_id=?",
                              (str(shot_id),)).fetchone()
        return (row[0] or 0) + 1

    def put(self, job):
        job_id = str(job["id"])
        params = job.get("params") or {}
        editor = job.get("editor_params") or {}
        progress = job.get("progress") or {}
        download = job.get("download") or {}
        version = job.get("version")
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO shots (id, created, status, stage, task, prompt, resolution, duration,"
                " aspect, seed, cpu_offload, first_image, last_image, hidden, local_draft, error,"
                " editor_task, editor_prompt, editor_resolution, editor_duration, editor_aspect,"
                " editor_seed, editor_cpu_offload, editor_first_image, editor_last_image)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET created=excluded.created, status=excluded.status,"
                " stage=excluded.stage, task=excluded.task, prompt=excluded.prompt,"
                " resolution=excluded.resolution, duration=excluded.duration, aspect=excluded.aspect,"
                " seed=excluded.seed, cpu_offload=excluded.cpu_offload,"
                " first_image=excluded.first_image, last_image=excluded.last_image,"
                " hidden=excluded.hidden, local_draft=excluded.local_draft, error=excluded.error,"
                " editor_task=excluded.editor_task, editor_prompt=excluded.editor_prompt,"
                " editor_resolution=excluded.editor_resolution, editor_duration=excluded.editor_duration,"
                " editor_aspect=excluded.editor_aspect, editor_seed=excluded.editor_seed,"
                " editor_cpu_offload=excluded.editor_cpu_offload,"
                " editor_first_image=excluded.editor_first_image,"
                " editor_last_image=excluded.editor_last_image",
                (job_id, float(job.get("created") or 0), str(job.get("status") or ""),
                 str(job.get("stage") or ""),
                 params.get("task"), params.get("prompt"), params.get("resolution"),
                 params.get("duration"), params.get("aspect"), params.get("seed"),
                 1 if params.get("cpu_offload") else 0,
                 params.get("first_image"), params.get("last_image"),
                 1 if job.get("hidden") else 0,
                 1 if job.get("local_draft") else 0,
                 job.get("error"),
                 editor.get("task"), editor.get("prompt"), editor.get("resolution"),
                 editor.get("duration"), editor.get("aspect"), editor.get("seed"),
                 (1 if editor.get("cpu_offload") else 0) if editor else None,
                 editor.get("first_image"), editor.get("last_image")))
            if version is None:
                version = self.next_version(job_id)
            self.db.execute(
                "INSERT INTO takes (shot_id, version, created, status, stage, started, finished,"
                " error, prompt, resolution, duration, aspect, seed, cpu_offload,"
                " progress_phase, progress_message, progress_completed, progress_total,"
                " sampling_seconds, sampling_eta_seconds, video_path, video_bytes,"
                " download_status, download_error, downloaded_bytes, total_bytes)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(shot_id, version) DO UPDATE SET status=excluded.status,"
                " stage=excluded.stage, started=excluded.started, finished=excluded.finished,"
                " error=excluded.error, prompt=excluded.prompt, resolution=excluded.resolution,"
                " duration=excluded.duration, aspect=excluded.aspect, seed=excluded.seed,"
                " cpu_offload=excluded.cpu_offload,"
                " progress_phase=excluded.progress_phase,"
                " progress_message=excluded.progress_message,"
                " progress_completed=excluded.progress_completed,"
                " progress_total=excluded.progress_total,"
                " sampling_seconds=excluded.sampling_seconds,"
                " sampling_eta_seconds=excluded.sampling_eta_seconds,"
                " video_path=excluded.video_path, video_bytes=excluded.video_bytes,"
                " download_status=excluded.download_status,"
                " download_error=excluded.download_error,"
                " downloaded_bytes=excluded.downloaded_bytes, total_bytes=excluded.total_bytes",
                (job_id, version, float(job.get("created") or 0), str(job.get("status") or ""),
                 str(job.get("stage") or ""), job.get("started"), job.get("finished"),
                 job.get("error") or progress.get("error"),
                 params.get("prompt"), params.get("resolution"), params.get("duration"),
                 params.get("aspect"), params.get("seed"),
                 1 if params.get("cpu_offload") else 0,
                 progress.get("phase"), progress.get("message"),
                 progress.get("completed"), progress.get("total"),
                 progress.get("sampling_seconds"), progress.get("sampling_eta_seconds"),
                 job.get("video_path"), job.get("video_bytes"),
                 download.get("status"), download.get("error"),
                 download.get("downloaded_bytes"), download.get("total_bytes")))
            self._put_assets(job_id, editor, params)
        job["version"] = version

    def _put_assets(self, job_id, editor, params):
        self.db.execute("DELETE FROM shot_assets WHERE shot_id=?", (job_id,))
        rows = []
        for role, value in (("first", params.get("first_image")), ("last", params.get("last_image")),
                            ("editor_first", editor.get("first_image")),
                            ("editor_last", editor.get("last_image"))):
            if value:
                rows.append((job_id, role, 0, "image", value))
        for role, items in (("reference", params.get("references") or []),
                            ("editor_reference", editor.get("references") or [])):
            for position, ref in enumerate(items):
                if isinstance(ref, dict) and ref.get("kind") and ref.get("id"):
                    rows.append((job_id, role, position, ref["kind"], ref["id"]))
        self.db.executemany(
            "INSERT OR REPLACE INTO shot_assets (shot_id, role, position, kind, upload_id)"
            " VALUES (?,?,?,?,?)", rows)

    def delete(self, job_id):
        job_id = str(job_id)
        with self.lock, self.db:
            self.db.execute("DELETE FROM takes WHERE shot_id=?", (job_id,))
            self.db.execute("DELETE FROM shot_assets WHERE shot_id=?", (job_id,))
            self.db.execute("DELETE FROM shots WHERE id=?", (job_id,))

    # ---------- 读取 ----------

    def all(self):
        with self.lock:
            shots = self.db.execute("SELECT * FROM shots ORDER BY created DESC").fetchall()
            takes = self._latest_takes()
            assets = self._assets()
        return [self._assemble(shot, takes.get(shot["id"]), assets.get(shot["id"]))
                for shot in shots]

    def get(self, job_id):
        job_id = str(job_id)
        with self.lock:
            shot = self.db.execute("SELECT * FROM shots WHERE id=?", (job_id,)).fetchone()
            if shot is None:
                return None
            take = self._latest_takes(job_id).get(job_id)
            assets = self._assets(job_id).get(job_id)
        return self._assemble(shot, take, assets)

    def takes(self, job_id):
        with self.lock:
            rows = self.db.execute("SELECT * FROM takes WHERE shot_id=? ORDER BY version",
                                   (str(job_id),)).fetchall()
        return [dict(row) for row in rows]

    def _latest_takes(self, job_id=None):
        sql = ("SELECT t.* FROM takes t JOIN (SELECT shot_id, MAX(version) AS version FROM takes"
               " GROUP BY shot_id) m ON m.shot_id = t.shot_id AND m.version = t.version")
        args = ()
        if job_id:
            sql += " WHERE t.shot_id=?"
            args = (job_id,)
        return {row["shot_id"]: dict(row) for row in self.db.execute(sql, args).fetchall()}

    def _assets(self, job_id=None):
        sql = "SELECT shot_id, role, position, kind, upload_id FROM shot_assets"
        args = ()
        if job_id:
            sql += " WHERE shot_id=?"
            args = (job_id,)
        sql += " ORDER BY shot_id, role, position"
        grouped = {}
        for row in self.db.execute(sql, args).fetchall():
            grouped.setdefault(row["shot_id"], {}).setdefault(row["role"], []).append(
                {"kind": row["kind"], "id": row["upload_id"]})
        return grouped

    @staticmethod
    def _assemble(shot, take, assets):
        assets = assets or {}
        job = {
            "id": shot["id"],
            "created": shot["created"],
            "status": shot["status"],
            "stage": shot["stage"],
            "hidden": bool(shot["hidden"]),
            "params": {
                "task": shot["task"], "prompt": shot["prompt"],
                "resolution": shot["resolution"], "duration": shot["duration"],
                "aspect": shot["aspect"], "seed": shot["seed"],
                "cpu_offload": bool(shot["cpu_offload"]),
                "first_image": shot["first_image"] or "",
                "last_image": shot["last_image"] or "",
                "references": assets.get("reference", []),
            },
        }
        if take:
            job["version"] = take["version"]
            if take["started"] is not None:
                job["started"] = take["started"]
            if take["finished"] is not None:
                job["finished"] = take["finished"]
            if take["error"]:
                job["error"] = take["error"]
            progress = {k: take[v] for k, v in
                        (("phase", "progress_phase"), ("message", "progress_message"),
                         ("completed", "progress_completed"), ("total", "progress_total"),
                         ("sampling_seconds", "sampling_seconds"),
                         ("sampling_eta_seconds", "sampling_eta_seconds"))
                        if take[v] is not None}
            if progress:
                job["progress"] = progress
            if take["video_path"]:
                job["video_path"] = take["video_path"]
            if take["video_bytes"]:
                job["video_bytes"] = take["video_bytes"]
            download = {}
            if take["download_status"]:
                download["status"] = take["download_status"]
            for key in ("download_error", "downloaded_bytes", "total_bytes"):
                if take[key] is not None:
                    download[key.split("_", 1)[1] if key == "download_error" else key] = take[key]
            if download:
                job["download"] = download
        if shot["local_draft"]:
            job["local_draft"] = True
        if shot["error"]:
            job["error"] = shot["error"]
        if shot["editor_task"] is not None:
            job["editor_params"] = {
                "task": shot["editor_task"], "prompt": shot["editor_prompt"],
                "resolution": shot["editor_resolution"], "duration": shot["editor_duration"],
                "aspect": shot["editor_aspect"], "seed": shot["editor_seed"],
                "cpu_offload": bool(shot["editor_cpu_offload"]),
                "first_image": shot["editor_first_image"] or "",
                "last_image": shot["editor_last_image"] or "",
                "references": assets.get("editor_reference", []),
            }
        return job

    # ---------- 迁移 ----------

    def migrate(self, folder):
        """首启时把旧的 <id>.json 导成 shot + take。"""
        folder = Path(folder)
        with self.lock:
            empty = self.db.execute("SELECT COUNT(*) FROM shots").fetchone()[0] == 0
        if not empty:
            return
        for path in sorted(folder.glob("*.json")) + sorted(folder.glob("*.json.migrated")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not isinstance(job, dict) or not job.get("id"):
                continue
            job.setdefault("version", 1)
            legacy_video = folder / (str(job["id"]) + ".mp4")
            if legacy_video.is_file() and not job.get("video_path"):
                job["video_path"] = str(legacy_video)
                job.setdefault("download", {})["status"] = "ready"
                job["download"]["total_bytes"] = legacy_video.stat().st_size
                job["download"]["downloaded_bytes"] = legacy_video.stat().st_size
            self.put(job)
            if not path.name.endswith(".migrated"):
                try:
                    path.rename(path.with_suffix(".json.migrated"))
                except OSError:
                    pass

    def close(self):
        with self.lock:
            self.db.close()
