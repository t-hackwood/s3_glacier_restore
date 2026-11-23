"""Thin helpers around boto3 S3: URIs, listing, restore-state parsing, safe display."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Iterator

import boto3
from botocore.config import Config as BotoConfig

# Storage classes whose objects must be restored before they can be read/copied.
ARCHIVE_CLASSES = {"DEEP_ARCHIVE", "GLACIER"}

# restore_state values
NOT_NEEDED = "not_needed"     # object is readable as-is (STANDARD, IA, GLACIER_IR, ...)
PENDING = "pending"           # archived, no (live) restore request
IN_PROGRESS = "in_progress"   # restore requested, not finished
RESTORED = "restored"         # temporary copy readable until restore_expiry
FAILED = "failed"

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,253}[a-z0-9]$", re.IGNORECASE)


def parse_uri(uri: str) -> tuple[str, str]:
    """'s3://bucket/a/b/' -> ('bucket', 'a/b/'). A bare bucket name is accepted."""
    m = re.match(r"^(?:s3://)?([^/]+)/?(.*)$", uri.strip())
    if not m or not _BUCKET_RE.match(m.group(1)):
        raise ValueError(f"Not an S3 URI: {uri!r} (expected s3://bucket/prefix/)")
    return m.group(1), m.group(2)


def as_folder(prefix: str) -> str:
    return prefix if not prefix or prefix.endswith("/") else prefix + "/"


# --- display safety ------------------------------------------------------------
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe(text: object) -> str:
    """S3 keys may contain any character. Show control characters as visible \\xNN escapes so a
    crafted key can't send escape sequences to the terminal."""
    return _CTRL.sub(lambda m: f"\\x{ord(m.group()):02x}", str(text))


# --- dates -------------------------------------------------------------------------
def iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_when(text: str | None) -> datetime | None:
    """Parse a stored expiry: ISO 8601, or the HTTP date S3 uses ('Fri, 21 Dec 2012 00:00:00 GMT')."""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def describe_when(dt: datetime, now: datetime | None = None) -> str:
    """'2026-11-30 00:00 UTC (in 6.0 days)'."""
    now = now or datetime.now(timezone.utc)
    secs = (dt - now).total_seconds()
    when = dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if secs <= 0:
        return f"{when} (expired)"
    if secs < 86400:
        return f"{when} (in {secs / 3600:.0f} h)"
    return f"{when} (in {secs / 86400:.1f} days)"


# --- clients -------------------------------------------------------------------------
@lru_cache(maxsize=8)
def session(profile: str | None, region: str | None) -> boto3.session.Session:
    return boto3.session.Session(profile_name=profile or None, region_name=region or None)


def client(profile: str | None = None, region: str | None = None, workers: int = 16):
    cfg = BotoConfig(max_pool_connections=max(10, workers * 2), retries={"max_attempts": 10, "mode": "adaptive"})
    return session(profile, region).client("s3", config=cfg)


@dataclass
class ObjInfo:
    key: str
    size: int
    storage_class: str
    etag: str = ""
    restore_state: str = ""
    restore_expiry: str = ""

    @property
    def is_archived(self) -> bool:
        return self.storage_class in ARCHIVE_CLASSES


def restore_state_from_list(storage_class: str, rs: dict | None) -> tuple[str, str]:
    """Interpret ListObjectsV2 RestoreStatus (OptionalObjectAttributes=['RestoreStatus'])."""
    if storage_class not in ARCHIVE_CLASSES:
        return NOT_NEEDED, ""
    if not rs:
        return PENDING, ""
    if rs.get("IsRestoreInProgress"):
        return IN_PROGRESS, ""
    exp = rs.get("RestoreExpiryDate")
    if isinstance(exp, datetime):
        return RESTORED, iso_utc(exp)
    parsed = parse_when(str(exp)) if exp else None
    return RESTORED, iso_utc(parsed) if parsed else ""


_RESTORE_RE = re.compile(r'ongoing-request="(true|false)"(?:,\s*expiry-date="([^"]+)")?')


def restore_state_from_head(head: dict) -> tuple[str, str]:
    """Interpret a HeadObject response. A missing Restore header on an archived object means
    no restore was requested, or the restored copy has expired."""
    storage_class = head.get("StorageClass", "STANDARD")
    if storage_class not in ARCHIVE_CLASSES:
        return NOT_NEEDED, ""
    hdr = head.get("Restore")
    if not hdr:
        return PENDING, ""
    m = _RESTORE_RE.search(hdr)
    if not m:
        return PENDING, ""
    if m.group(1) == "true":
        return IN_PROGRESS, ""
    parsed = parse_when(m.group(2))
    return RESTORED, iso_utc(parsed) if parsed else ""


def _obj(o: dict) -> ObjInfo:
    sc = o.get("StorageClass", "STANDARD")
    state, exp = restore_state_from_list(sc, o.get("RestoreStatus"))
    return ObjInfo(o["Key"], o["Size"], sc, o.get("ETag", "").strip('"'), state, exp)


def list_objects(s3, bucket: str, prefix: str) -> Iterator[ObjInfo]:
    """All objects under prefix (recursive), with restore status where S3 provides it."""
    pager = s3.get_paginator("list_objects_v2")
    for page in pager.paginate(Bucket=bucket, Prefix=prefix, OptionalObjectAttributes=["RestoreStatus"]):
        for o in page.get("Contents", []):
            yield _obj(o)


def list_children(s3, bucket: str, prefix: str,
                  max_files: int | None = None) -> tuple[list[str], list[ObjInfo], int]:
    """One level: (sub-folder prefixes, objects directly in prefix, files not returned).

    Files beyond ``max_files`` are counted, not returned, so a folder holding 100k files
    doesn't flood the UI. All pages are still read so no sub-folder is missed.
    """
    folders: list[str] = []
    files: list[ObjInfo] = []
    more = 0
    pager = s3.get_paginator("list_objects_v2")
    for page in pager.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/",
                               OptionalObjectAttributes=["RestoreStatus"]):
        folders += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        for o in page.get("Contents", []):
            if o["Key"] == prefix:  # folder marker for the prefix itself
                continue
            if max_files is not None and len(files) >= max_files:
                more += 1
                continue
            files.append(_obj(o))
    return folders, files, more


def bucket_region(s3, bucket: str) -> str:
    loc = s3.get_bucket_location(Bucket=bucket).get("LocationConstraint")
    return {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"
