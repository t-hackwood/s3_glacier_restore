"""Job store: one SQLite file per job in jobs/, plus a readable summary.json."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .s3util import FAILED, IN_PROGRESS, NOT_NEEDED, PENDING, RESTORED, ObjInfo, parse_when

# copy_state values
C_PENDING, C_COPIED, C_SKIPPED, C_CONFLICT, C_FAILED = "pending", "copied", "skipped", "conflict", "failed"
COPY_STATES = (C_PENDING, C_COPIED, C_SKIPPED, C_CONFLICT, C_FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS objects (
    key TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    storage_class TEXT NOT NULL,
    etag TEXT,
    restore_state TEXT NOT NULL,
    restore_expiry TEXT,
    copy_state TEXT NOT NULL DEFAULT 'pending',
    dest_key TEXT,
    error TEXT,           -- last restore / status-check error
    copy_error TEXT,      -- last copy error or note
    updated TEXT
);
CREATE INDEX IF NOT EXISTS ix_restore ON objects(restore_state);
CREATE INDEX IF NOT EXISTS ix_copy ON objects(copy_state);
"""


class NotAJobError(ValueError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:80] or "job"


class Job:
    def __init__(self, path: Path, create: bool = False):
        self.path = Path(path)
        self.name = self.path.stem
        if not create and not self.path.is_file():
            raise FileNotFoundError(f"No job file {self.path}")
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        if create:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
        else:
            self._check_and_migrate()

    def _check_and_migrate(self) -> None:
        """Refuse files that aren't thaw jobs (never add tables to someone else's database)."""
        try:
            tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as e:
            self._conn.close()
            raise NotAJobError(f"{self.path} is not a thaw job ({e})") from None
        if not {"meta", "objects"} <= tables:
            self._conn.close()
            raise NotAJobError(f"{self.path} is not a thaw job")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(objects)")}
        if "copy_error" not in cols:  # jobs created by v0.1
            with self._conn:
                self._conn.execute("ALTER TABLE objects ADD COLUMN copy_error TEXT")

    # --- lifecycle ---------------------------------------------------------
    @classmethod
    def create(cls, jobs_dir: Path, name: str, meta: dict, objects: Iterable[ObjInfo]) -> "Job":
        name = slug(name)
        path = jobs_dir / f"{name}.sqlite"
        if path.exists():
            raise FileExistsError(f"Job {name!r} already exists ({path})")
        job = cls(path, create=True)
        job.set_meta({**meta, "name": name, "created": now()})
        with job.tx() as c:
            c.executemany(
                "INSERT INTO objects(key,size,storage_class,etag,restore_state,restore_expiry,updated)"
                " VALUES (?,?,?,?,?,?,?)",
                [(o.key, o.size, o.storage_class, o.etag, o.restore_state or PENDING,
                  o.restore_expiry or None, now()) for o in objects],
            )
        job.write_summary()
        return job

    @classmethod
    def open(cls, jobs_dir: Path, name_or_path: str) -> "Job":
        """Open by job name (looked up in jobs_dir), or by an explicit path to a .sqlite file.
        A folder or file in the current directory that happens to share the job's name is ignored."""
        p = jobs_dir / f"{slug(name_or_path)}.sqlite"
        if not p.is_file() and name_or_path.endswith(".sqlite") and Path(name_or_path).is_file():
            p = Path(name_or_path)
        if not p.is_file():
            raise FileNotFoundError(f"No job {name_or_path!r} in {jobs_dir}")
        return cls(p)

    @staticmethod
    def list_all(jobs_dir: Path) -> list["Job"]:
        jobs = []
        for p in sorted(jobs_dir.glob("*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                jobs.append(Job(p))
            except (NotAJobError, sqlite3.Error):
                continue
        return jobs

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # --- meta --------------------------------------------------------------
    def set_meta(self, meta: dict) -> None:
        with self.tx() as c:
            c.executemany("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)",
                          [(k, json.dumps(v)) for k, v in meta.items()])

    @property
    def meta(self) -> dict:
        return {k: json.loads(v) for k, v in self._conn.execute("SELECT k,v FROM meta")}

    # --- objects -----------------------------------------------------------
    def objects(self, where: str = "1=1", params: tuple = ()) -> list[sqlite3.Row]:
        cur = self._conn.cursor()
        cur.row_factory = sqlite3.Row
        return cur.execute(f"SELECT * FROM objects WHERE {where} ORDER BY key", params).fetchall()

    def update(self, key: str, **fields) -> None:
        fields["updated"] = now()
        cols = ",".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE objects SET {cols} WHERE key=?", (*fields.values(), key))

    def problems(self) -> list[sqlite3.Row]:
        return self.objects("error IS NOT NULL OR copy_error IS NOT NULL")

    def counts(self) -> dict:
        c = self._conn
        restore = dict(c.execute("SELECT restore_state, COUNT(*) FROM objects GROUP BY restore_state").fetchall())
        copy = dict(c.execute("SELECT copy_state, COUNT(*) FROM objects GROUP BY copy_state").fetchall())
        total, size = c.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM objects").fetchone()
        archived = sum(restore.get(s, 0) for s in (PENDING, IN_PROGRESS, RESTORED, FAILED))
        return {
            "objects": total,
            "bytes": size,
            "archived": archived,
            "restore": {s: restore.get(s, 0) for s in (NOT_NEEDED, PENDING, IN_PROGRESS, RESTORED, FAILED)},
            "copy": {s: copy.get(s, 0) for s in COPY_STATES},
            "readable": restore.get(NOT_NEEDED, 0) + restore.get(RESTORED, 0),
        }

    def earliest_expiry(self) -> datetime | None:
        """Soonest expiry among restored copies that still have to be copied."""
        rows = self.objects("restore_state = ? AND copy_state NOT IN (?,?) AND restore_expiry IS NOT NULL",
                            (RESTORED, C_COPIED, C_SKIPPED))
        dates = [d for d in (parse_when(r["restore_expiry"]) for r in rows) if d]
        return min(dates) if dates else None

    @property
    def all_readable(self) -> bool:
        c = self.counts()
        return c["readable"] == c["objects"]

    @property
    def all_copied(self) -> bool:
        c = self.counts()["copy"]
        return c[C_PENDING] == 0 and c[C_FAILED] == 0 and c[C_CONFLICT] == 0

    def write_summary(self) -> Path:
        out = self.path.with_suffix(".summary.json")
        out.write_text(json.dumps({"meta": self.meta, "counts": self.counts(), "written": now()}, indent=2))
        return out
