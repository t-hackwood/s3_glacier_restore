"""Restore / status / copy operations shared by the CLI and the TUI."""

from __future__ import annotations

import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import botocore.exceptions as bexc
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from .config import Config
from .jobs import C_CONFLICT, C_COPIED, C_FAILED, C_PENDING, C_SKIPPED, Job, now
from .s3util import (FAILED, IN_PROGRESS, NOT_NEEDED, PENDING, RESTORED, ObjInfo,
                     bucket_region, list_objects, parse_uri, restore_state_from_head)

Progress = Callable[[int, int], None]  # (done, total)

# Objects at or above the threshold are copied in parts (UploadPartCopy; required above 5 GB).
# One shared config also caps per-object part concurrency (default is 10 threads per object).
TRANSFER = TransferConfig(multipart_threshold=8 * 1024 * 1024, max_concurrency=4)


def _noop(done: int, total: int) -> None:
    pass


# --- errors ------------------------------------------------------------------
class AuthError(RuntimeError):
    """AWS rejected the credentials (expired SSO session, missing keys, ...)."""


_AUTH_EXC = tuple(getattr(bexc, n) for n in (
    "NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError",
    "TokenRetrievalError", "UnauthorizedSSOTokenError", "SSOTokenLoadError") if hasattr(bexc, n))
_AUTH_CODES = {"ExpiredToken", "ExpiredTokenException", "InvalidToken", "InvalidAccessKeyId",
               "InvalidClientTokenId", "SignatureDoesNotMatch", "TokenRefreshRequired", "RequestExpired"}


def error_code(e: BaseException | None) -> str:
    if isinstance(e, ClientError):
        return str(e.response.get("Error", {}).get("Code", ""))
    return ""


def is_auth_error(e: BaseException | None) -> bool:
    return isinstance(e, _AUTH_EXC) or error_code(e) in _AUTH_CODES


def _err(e: BaseException) -> str:
    if isinstance(e, ClientError):
        return f"{e.response['Error'].get('Code')}: {e.response['Error'].get('Message')}"
    return f"{type(e).__name__}: {e}"


def _run_pool(items: Sequence, fn: Callable[[Any], Any], record: Callable[[Any], None],
              workers: int, progress: Progress) -> None:
    """Run ``fn`` over ``items`` in a thread pool and ``record`` each result on this thread.

    If ``record`` raises (e.g. AuthError) or the user presses Ctrl+C, queued calls are
    cancelled; calls already in flight finish and are recorded, then the exception propagates.
    So nothing that actually reached S3 goes unrecorded, and nothing more is sent.
    """
    total = len(items)
    progress(0, total)
    if not total:
        return
    ex = ThreadPoolExecutor(max_workers=workers)
    futs = [ex.submit(fn, it) for it in items]
    handled: set[Future] = set()
    try:
        for i, f in enumerate(as_completed(futs), 1):
            handled.add(f)
            record(f.result())
            progress(i, total)
    except BaseException:
        for f in futs:
            f.cancel()
        for f in futs:
            if f in handled or f.cancelled():
                continue
            try:
                record(f.result())  # waits only for the few calls already in flight
            except Exception:  # noqa: BLE001 — already unwinding; keep what we can
                pass
        raise
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# --- selection → objects ---------------------------------------------------
def gather(s3, bucket: str, selection,
           cached_under: Callable[[str], Iterable[ObjInfo]] | None = None,
           listed_prefixes: Iterable[str] = ()) -> list[ObjInfo]:
    """Objects matched by a selection. ``cached_under(root)`` supplies objects for roots that
    lie inside an already fully-listed prefix; everything else is listed from S3."""
    listed = list(listed_prefixes)
    out: dict[str, ObjInfo] = {}
    for root in selection.roots:
        if cached_under is not None and any(root.startswith(p) for p in listed):
            objs = cached_under(root)
        else:
            objs = list_objects(s3, bucket, root)
        for o in objs:
            if selection.is_selected(o.key):
                out[o.key] = o
    return sorted(out.values(), key=lambda o: o.key)


@dataclass
class Estimate:
    objects: int = 0
    bytes: int = 0
    to_restore: int = 0
    to_restore_bytes: int = 0
    in_progress: int = 0
    restored: int = 0
    readable: int = 0
    by_class: dict[str, list[int]] = field(default_factory=dict)  # class -> [count, bytes]
    cost: float = 0.0

    def lines(self) -> list[str]:
        from .s3util import human_size as hs
        out = [f"Objects selected : {self.objects:,} ({hs(self.bytes)})"]
        for c, (n, b) in sorted(self.by_class.items()):
            out.append(f"  {c:<20} {n:>8,}  {hs(b):>10}")
        out += [
            f"Need restore     : {self.to_restore:,} ({hs(self.to_restore_bytes)})",
            f"Already restoring: {self.in_progress:,}",
            f"Already restored : {self.restored:,}",
            f"Readable now     : {self.readable:,}",
            f"Estimated cost   : ~US${self.cost:,.{2 if self.cost >= 0.01 else 4}f} (retrieval + requests + temp storage)",
        ]
        return out


def estimate(objs: list[ObjInfo], tier: str, days: int, pricing: dict) -> Estimate:
    e = Estimate()
    gb = 1024 ** 3
    for o in objs:
        e.objects += 1
        e.bytes += o.size
        n_b = e.by_class.setdefault(o.storage_class, [0, 0])
        n_b[0] += 1
        n_b[1] += o.size
        if o.restore_state == PENDING:
            e.to_restore += 1
            e.to_restore_bytes += o.size
            p = pricing.get(o.storage_class, {}).get(tier, {})
            e.cost += o.size / gb * p.get("per_gb", 0) + p.get("per_1000_requests", 0) / 1000
        elif o.restore_state == IN_PROGRESS:
            e.in_progress += 1
        elif o.restore_state == RESTORED:
            e.restored += 1
        else:
            e.readable += 1
    e.cost += e.to_restore_bytes / gb * pricing.get("restored_copy", {}).get("per_gb_month", 0) * days / 30
    return e


# --- restore -----------------------------------------------------------------
def create_job(cfg: Config, name: str, bucket: str, base: str, selection, objs: list[ObjInfo],
               tier: str, days: int, profile: str | None, region: str) -> Job:
    meta = {"bucket": bucket, "base_prefix": base, "selection": selection.describe(),
            "tier": tier, "days": days, "profile": profile or "", "region": region}
    return Job.create(cfg.jobs_dir, name, meta, objs)


def request_restores(s3, job: Job, workers: int = 16, progress: Progress = _noop) -> dict:
    """Send RestoreObject for every archived object that has no live restore (pending/failed,
    including objects whose earlier restored copy has expired)."""
    m = job.meta
    bucket, tier, days = m["bucket"], m["tier"], int(m["days"])
    rows = job.objects("restore_state IN (?,?)", (PENDING, FAILED))
    result = {"requested": 0, "already": 0, "failed": 0}

    def one(key: str):
        try:
            s3.restore_object(Bucket=bucket, Key=key, RestoreRequest={
                "Days": days, "GlacierJobParameters": {"Tier": tier}})
            return key, IN_PROGRESS, None
        except Exception as e:  # noqa: BLE001
            if error_code(e) == "RestoreAlreadyInProgress":
                return key, "already", None
            return key, FAILED, e

    def record(res) -> None:
        key, state, exc = res
        if exc is not None and is_auth_error(exc):
            job.update(key, error=_err(exc))  # state unchanged: it never reached S3
            raise AuthError(_err(exc))
        if state == FAILED:
            result["failed"] += 1
            job.update(key, restore_state=FAILED, error=_err(exc))
        else:
            result["requested" if state == IN_PROGRESS else "already"] += 1
            job.update(key, restore_state=IN_PROGRESS, restore_expiry=None, error=None, copy_error=None)

    try:
        _run_pool([r["key"] for r in rows], one, record, workers, progress)
    finally:
        if rows:
            job.set_meta({"restore_requested": now()})
        job.write_summary()
    return result


# --- status ------------------------------------------------------------------
def refresh_status(s3, job: Job, workers: int = 16, progress: Progress = _noop) -> dict:
    """HEAD every archived object whose state can still change: not yet restored, or restored
    but not yet copied (so an expired restored copy is noticed and goes back to pending).

    Returns job.counts() plus ``checked`` and ``check_errors`` for this pass.
    """
    bucket = job.meta["bucket"]
    rows = job.objects("restore_state IN (?,?) OR (restore_state = ? AND copy_state NOT IN (?,?))",
                       (IN_PROGRESS, PENDING, RESTORED, C_COPIED, C_SKIPPED))
    errors = 0

    def one(key: str):
        try:
            return key, *restore_state_from_head(s3.head_object(Bucket=bucket, Key=key)), None
        except Exception as e:  # noqa: BLE001
            return key, None, "", e

    def record(res) -> None:
        nonlocal errors
        key, state, exp, exc = res
        if exc is not None:
            errors += 1
            job.update(key, error=_err(exc))
            if is_auth_error(exc):
                raise AuthError(_err(exc))
        else:
            job.update(key, restore_state=state, restore_expiry=exp or None, error=None)

    try:
        _run_pool([r["key"] for r in rows], one, record, workers, progress)
    finally:
        job.set_meta({"status_checked": now()})
        job.write_summary()
    counts = job.counts()
    counts["checked"] = len(rows)
    counts["check_errors"] = errors
    return counts


# --- copy --------------------------------------------------------------------
def dest_key_for(key: str, base: str, dest_prefix: str) -> str:
    rel = key[len(base):] if key.startswith(base) else key
    return dest_prefix + rel


def check_regions(s3, src_bucket: str, dest_bucket: str) -> tuple[str, str]:
    return bucket_region(s3, src_bucket), bucket_region(s3, dest_bucket)


def _head_or_none(s3, bucket: str, key: str) -> dict | None:
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if error_code(e) in ("404", "NoSuchKey", "NotFound"):
            return None
        raise  # 403 etc.: we can't tell whether something is there, so don't risk overwriting it


def fmt_copy_result(r: dict) -> str:
    parts = [f"copied {r.get(C_COPIED, 0):,}", f"already there {r.get(C_SKIPPED, 0):,}",
             f"conflicts {r.get(C_CONFLICT, 0):,}", f"failed {r.get(C_FAILED, 0):,}"]
    if r.get("expired"):
        parts.append(f"restore expired {r['expired']:,}")
    return ", ".join(parts)


def copy_objects(s3, job: Job, dest_uri: str, storage_class: str = "STANDARD",
                 overwrite: bool = False, workers: int = 16, progress: Progress = _noop,
                 retry_failed: bool = True) -> dict:
    """Server-side copy of every readable, not-yet-copied object to dest_uri.

    Without ``overwrite`` an existing destination object is never replaced: same size →
    'skipped' (already there), different size → 'conflict'. A source that has gone back
    to the archive (restored copy expired) is reset to pending so it can be restored again.
    """
    m = job.meta
    src_bucket, base = m["bucket"], m["base_prefix"]
    dest_bucket, dest_prefix = parse_uri(dest_uri)
    if dest_prefix and not dest_prefix.endswith("/"):
        dest_prefix += "/"
    states = (C_PENDING, C_FAILED, C_CONFLICT) if retry_failed else (C_PENDING,)
    rows = job.objects(
        f"restore_state IN (?,?) AND copy_state IN ({','.join('?' * len(states))})",
        (NOT_NEEDED, RESTORED, *states))
    result = {C_COPIED: 0, C_SKIPPED: 0, C_CONFLICT: 0, C_FAILED: 0, "expired": 0}

    def one(row):
        key, size = row["key"], row["size"]
        dkey = dest_key_for(key, base, dest_prefix)
        if not dkey:  # folder marker of the base prefix, copied to a bucket root: nothing to do
            return key, dkey, C_SKIPPED, None, None
        try:
            if not overwrite:
                existing = _head_or_none(s3, dest_bucket, dkey)
                if existing is not None:
                    if existing["ContentLength"] == size:
                        return key, dkey, C_SKIPPED, None, None
                    return key, dkey, C_CONFLICT, (
                        f"destination already holds a different object ({existing['ContentLength']:,} "
                        f"bytes vs {size:,}); left alone — use overwrite to replace it"), None
            tags: list = []
            note = None
            if size >= TRANSFER.multipart_threshold:
                # CopyObject keeps tags by itself; a multipart copy doesn't (and s3transfer ignores a
                # Tagging argument for it), so read them first and put them back afterwards.
                try:
                    tags = s3.get_object_tagging(Bucket=src_bucket, Key=key).get("TagSet", [])
                except ClientError as e:
                    if is_auth_error(e):
                        raise
                    note = f"copied without tags ({_err(e)})"
            s3.copy({"Bucket": src_bucket, "Key": key}, dest_bucket, dkey,
                    ExtraArgs={"StorageClass": storage_class}, Config=TRANSFER)
            if tags:
                try:
                    s3.put_object_tagging(Bucket=dest_bucket, Key=dkey, Tagging={"TagSet": tags})
                except ClientError as e:
                    if is_auth_error(e):
                        raise
                    note = f"copied without tags ({_err(e)})"
            return key, dkey, C_COPIED, note, None
        except Exception as e:  # noqa: BLE001
            return key, dkey, C_FAILED, _err(e), e

    def record(res) -> None:
        key, dkey, state, msg, exc = res
        if exc is not None and error_code(exc) == "InvalidObjectState":
            result["expired"] += 1
            job.update(key, restore_state=PENDING, restore_expiry=None, copy_state=C_PENDING,
                       dest_key=dkey, copy_error="source is archived again (restored copy expired?) "
                                                 "— request the restore again")
            return
        result[state] += 1
        job.update(key, copy_state=state, dest_key=dkey, copy_error=msg)
        if exc is not None and is_auth_error(exc):
            raise AuthError(_err(exc))

    try:
        _run_pool(rows, one, record, workers, progress)
    finally:
        job.set_meta({"destination": dest_uri, "dest_storage_class": storage_class, "last_copy": now()})
        job.write_summary()
    return result


# --- watch -------------------------------------------------------------------
def watch(s3, job: Job, interval_s: float, auto_copy: str | None = None, storage_class: str = "STANDARD",
          workers: int = 16, log: Callable[[str], None] = print, sleep=time.sleep) -> int:
    """Poll status until everything is readable; optionally copy as objects become ready.

    Returns 0 when finished, 1 when it stopped because something needs attention.
    AuthError propagates (credentials expired) so the caller can say so loudly.
    """
    while True:
        c = refresh_status(s3, job, workers)
        r = c["restore"]
        line = (f"[{job.name}] restored {r[RESTORED]}/{c['archived']}  in-progress {r[IN_PROGRESS]}  "
                f"pending {r[PENDING]}  failed {r[FAILED]}")
        if c["check_errors"]:
            line += f"  status-check errors {c['check_errors']}/{c['checked']}"
        log(line)
        if c["checked"] and c["check_errors"] == c["checked"]:
            log(f"[{job.name}] every status check failed — stopping. See `thaw status {job.name} --errors`.")
            return 1
        if auto_copy and c["readable"]:
            res = copy_objects(s3, job, auto_copy, storage_class, workers=workers)
            if any(res.values()):
                log(f"[{job.name}] copy: {fmt_copy_result(res)}")
        c = job.counts()
        r, cp = c["restore"], c["copy"]
        if c["readable"] == c["objects"] and (not auto_copy or job.all_copied):
            log(f"[{job.name}] done.")
            return 0
        if not r[IN_PROGRESS]:
            # Nothing is still thawing, so further polling won't change anything.
            if r[PENDING]:
                log(f"[{job.name}] {r[PENDING]} objects have no live restore (never requested, or the "
                    f"restored copy expired) — run `thaw restore --job {job.name}`.")
            if r[FAILED]:
                log(f"[{job.name}] {r[FAILED]} restore requests failed — see `thaw status {job.name} --errors`.")
            if auto_copy and cp[C_CONFLICT]:
                log(f"[{job.name}] {cp[C_CONFLICT]} objects already exist at the destination with a different "
                    f"size and were left alone — re-run `thaw copy {job.name} {auto_copy} --overwrite` to replace them.")
            if auto_copy and cp[C_FAILED]:
                log(f"[{job.name}] {cp[C_FAILED]} copies failed — see `thaw status {job.name} --errors`.")
            return 1
        sleep(interval_s)
