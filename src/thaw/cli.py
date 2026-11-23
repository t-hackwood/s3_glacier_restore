"""thaw — restore S3 objects from Glacier Deep Archive, track them, copy them.

    thaw browse [s3://bucket/prefix/]          pick folders visually (TUI)
    thaw restore s3://bucket/prefix/ --exclude ANALYSIS [--dry-run]
    thaw restore --job NAME                    re-send pending/failed/expired requests
    thaw jobs                                  list jobs
    thaw status NAME [--errors]                refresh + show progress
    thaw copy NAME s3://dest/prefix/           server-side copy of readable objects
    thaw watch NAME [--auto-copy s3://dest/]   poll until restored (and copied)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

import botocore.exceptions as bexc

from . import ops
from .config import Config
from .jobs import C_CONFLICT, Job
from .s3util import (FAILED, IN_PROGRESS, NOT_NEEDED, PENDING, RESTORED, as_folder, client,
                     describe_when, human_size, parse_uri, safe)
from .selection import GlobSelection


def _progress(label: str):
    def cb(done: int, total: int) -> None:
        if total:
            sys.stderr.write(f"\r{label}: {done:,}/{total:,}")
            if done == total:
                sys.stderr.write("\n")
            sys.stderr.flush()
    return cb


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, OSError):  # no usable terminal (e.g. under nohup): treat as "no"
        return False


def _positive_int(text: str) -> int:
    v = int(text)
    if v < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return v


def _positive_float(text: str) -> float:
    v = float(text)
    if v <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return v


def _s3(args, cfg: Config, job: Job | None = None):
    profile = args.profile if args.profile is not None else (job.meta.get("profile") if job else None) or cfg.profile
    region = args.region or (job.meta.get("region") if job else None) or cfg.region
    return client(profile, region, args.workers or cfg.workers), profile, region


def _workers(args, cfg):
    return args.workers or cfg.workers


def _region_ok(s3, src_bucket: str, dest_uri: str, assume_yes: bool) -> bool:
    """Warn (and ask) before a cross-region copy, or when the destination's region can't be read."""
    try:
        src_r, dst_r = ops.check_regions(s3, src_bucket, parse_uri(dest_uri)[0])
    except Exception as e:  # noqa: BLE001
        if ops.is_auth_error(e):
            raise
        print(f"WARNING: couldn't check the destination bucket's region ({safe(ops._err(e))}).")
        return _confirm("Copy anyway?", assume_yes)
    if src_r != dst_r:
        print(f"WARNING: cross-region copy {src_r} → {dst_r} incurs data-transfer charges.")
        return _confirm("Continue?", assume_yes)
    return True


def print_counts(job: Job) -> None:
    c = job.counts()
    r, cp = c["restore"], c["copy"]
    m = job.meta
    print(f"Job {safe(job.name)}: s3://{safe(m['bucket'])}/{safe(m['base_prefix'])}  tier={m['tier']} days={m['days']}")
    print(f"  objects   {c['objects']:,} ({human_size(c['bytes'])}), archived {c['archived']:,}")
    print(f"  restore   restored {r[RESTORED]:,} | in-progress {r[IN_PROGRESS]:,} | "
          f"pending {r[PENDING]:,} | failed {r[FAILED]:,} | not needed {r[NOT_NEEDED]:,}")
    print(f"  copy      copied {cp['copied']:,} | already there {cp['skipped']:,} | conflicts {cp[C_CONFLICT]:,} | "
          f"pending {cp['pending']:,} | failed {cp['failed']:,}"
          + (f"  → {safe(m['destination'])}" if m.get("destination") else ""))
    exp = job.earliest_expiry()
    if exp:
        print(f"  copy before: {describe_when(exp)} (earliest restored-copy expiry)")
    if m.get("status_checked"):
        print(f"  status checked {m['status_checked']}")


# --- commands ----------------------------------------------------------------
def cmd_restore(args, cfg: Config) -> int:
    if args.job:
        job = Job.open(cfg.jobs_dir, args.job)
        s3, *_ = _s3(args, cfg, job)
        res = ops.request_restores(s3, job, _workers(args, cfg), _progress("Restore requests"))
        print(res)
        print_counts(job)
        return 0 if not res["failed"] else 1

    if not args.uri:
        print("restore needs an s3:// URI or --job NAME", file=sys.stderr)
        return 2
    bucket, prefix = parse_uri(args.uri)
    prefix = as_folder(prefix)
    s3, profile, region = _s3(args, cfg)
    tier, days = args.tier or cfg.tier, args.days or cfg.days
    sel = GlobSelection(prefix, args.include or [], args.exclude or [], args.deep)
    print(f"Listing s3://{safe(bucket)}/{safe(prefix)} ...", file=sys.stderr)
    objs = ops.gather(s3, bucket, sel)
    if not objs:
        print("Nothing selected.")
        return 1
    est = ops.estimate(objs, tier, days, cfg.pricing)
    print(f"Selection: s3://{safe(bucket)}/{safe(prefix)}  include={sel.include or ['*']} exclude={sel.exclude}"
          f"{' (deep)' if sel.deep else ''}")
    folders: dict[str, list[int]] = {}
    for o in objs:
        top = o.key[len(prefix):].split("/", 1)
        f = top[0] + "/" if len(top) > 1 else "(files)"
        n = folders.setdefault(f, [0, 0, 0])
        n[0] += 1
        n[1] += o.size
        n[2] += o.restore_state == PENDING
    for f, (n, b, need) in sorted(folders.items()):
        print(f"  {safe(f):<40} {n:>8,} obj {human_size(b):>10}  need restore {need:,}")
    print(f"Tier {tier}, keep restored copies {days} days")
    print("\n".join(est.lines()))
    if est.objects > cfg.warn_objects:
        print(f"WARNING: {est.objects:,} objects — consider S3 Batch Operations for very large jobs.")
    if args.dry_run:
        print("(dry run — nothing requested, no job created)")
        return 0
    if not _confirm("Create job and request restores?", args.yes):
        return 1
    name = args.name or f"{prefix.rstrip('/').split('/')[-1] or bucket}_{datetime.now():%Y%m%d-%H%M}"
    job = ops.create_job(cfg, name, bucket, prefix, sel, objs, tier, days, profile, region)
    print(f"Job {job.name} created: {job.path}")
    res = ops.request_restores(s3, job, _workers(args, cfg), _progress("Restore requests"))
    print(res)
    print_counts(job)
    return 0 if not res["failed"] else 1


def cmd_jobs(args, cfg: Config) -> int:
    jobs = Job.list_all(cfg.jobs_dir)
    if not jobs:
        print(f"No jobs in {cfg.jobs_dir}")
    for job in jobs:
        c, m = job.counts(), job.meta
        print(f"{safe(job.name):<45} {m.get('created', ''):<26} {c['objects']:>7,} obj  "
              f"restored {c['restore'][RESTORED]:,}/{c['archived']:,}  copied {c['copy']['copied'] + c['copy']['skipped']:,}")
        job.close()
    return 0


def cmd_status(args, cfg: Config) -> int:
    if not args.job:
        return cmd_jobs(args, cfg)
    job = Job.open(cfg.jobs_dir, args.job)
    if not args.no_refresh:
        s3, *_ = _s3(args, cfg, job)
        c = ops.refresh_status(s3, job, _workers(args, cfg), _progress("Checking"))
        if c["check_errors"]:
            print(f"WARNING: {c['check_errors']:,} of {c['checked']:,} status checks failed (see --errors).")
    print_counts(job)
    if args.errors:
        for row in job.problems():
            for err in (row["error"], row["copy_error"]):
                if err:
                    print(f"  ! {safe(row['key'])}: {safe(err)}")
    return 0


def cmd_copy(args, cfg: Config) -> int:
    job = Job.open(cfg.jobs_dir, args.job)
    s3, *_ = _s3(args, cfg, job)
    if not _region_ok(s3, job.meta["bucket"], args.dest, args.yes):
        return 1
    ops.refresh_status(s3, job, _workers(args, cfg), _progress("Checking"))
    c = job.counts()
    if c["readable"] < c["objects"]:
        print(f"Note: only {c['readable']:,}/{c['objects']:,} objects are readable; the rest will be "
              "copied on a later run (or use `thaw watch --auto-copy`).")
    sc = args.storage_class or cfg.storage_class
    res = ops.copy_objects(s3, job, args.dest, sc, args.overwrite, _workers(args, cfg), _progress("Copying"))
    cfg.remember_destination(args.dest)
    print(ops.fmt_copy_result(res))
    if res[C_CONFLICT]:
        print(f"{res[C_CONFLICT]:,} destination objects already exist with a different size and were left alone. "
              "Check them (`thaw status --errors`), then re-run with --overwrite to replace them.")
    if res["expired"]:
        print(f"{res['expired']:,} objects are archived again (their restored copy expired). "
              f"Run `thaw restore --job {job.name}` to restore them again.")
    print_counts(job)
    return 0 if not (res["failed"] or res[C_CONFLICT] or res["expired"]) else 1


def cmd_watch(args, cfg: Config) -> int:
    job = Job.open(cfg.jobs_dir, args.job)
    s3, *_ = _s3(args, cfg, job)

    def log(msg: str) -> None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {safe(msg)}", flush=True)

    if args.auto_copy:
        if not _region_ok(s3, job.meta["bucket"], args.auto_copy, args.yes):
            log("not starting: confirm the destination with --yes (needed when running unattended).")
            return 1
        cfg.remember_destination(args.auto_copy)
    try:
        return ops.watch(s3, job, (args.interval or cfg.watch_interval) * 60, args.auto_copy,
                         args.storage_class or cfg.storage_class, _workers(args, cfg), log)
    except ops.AuthError as e:
        log(f"[{job.name}] AWS credentials expired or were rejected ({e}). Log in again "
            f"(e.g. `aws sso login`) and re-run the watch; progress so far is saved.")
        return 1


def cmd_browse(args, cfg: Config) -> int:
    from .tui.app import ThawApp
    ThawApp(cfg, args.uri, profile=args.profile, region=args.region,
            workers=_workers(args, cfg), theme=args.theme).run()
    return 0


# --- errors ------------------------------------------------------------------
def friendly_error(e: BaseException) -> str | None:
    """One-line explanation for the AWS errors people actually hit; None = unexpected (re-raise)."""
    if isinstance(e, ops.AuthError) or ops.is_auth_error(e):
        return (f"AWS credentials are missing, expired or invalid ({safe(e)}). "
                "Log in again (e.g. `aws sso login --profile NAME`) and re-run.")
    if isinstance(e, bexc.ProfileNotFound):
        return f"{safe(e)}. Check --profile or [aws] profile in thaw.toml."
    if isinstance(e, bexc.NoRegionError):
        return "No AWS region configured. Set [aws] region in thaw.toml or pass --region."
    if isinstance(e, (bexc.EndpointConnectionError, bexc.ConnectTimeoutError)):
        return f"Can't reach AWS ({safe(e)}). Check the network connection."
    if isinstance(e, bexc.ClientError):
        code = ops.error_code(e)
        msg = safe(e.response.get("Error", {}).get("Message", ""))
        if code == "NoSuchBucket":
            return f"Bucket not found ({msg})."
        if code in ("AccessDenied", "403", "Forbidden", "AllAccessDisabled"):
            return f"Access denied ({msg}). See the Permissions section of the README."
        return f"AWS error {code}: {msg}"
    return None


# --- parser ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", help="AWS profile (default: thaw.toml / boto3 chain)")
    common.add_argument("--region", help="AWS region (default: thaw.toml)")
    common.add_argument("--workers", type=_positive_int, help="concurrent API calls")

    p = argparse.ArgumentParser(prog="thaw", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("browse", parents=[common], help="pick folders visually")
    b.add_argument("uri", nargs="?", help="s3://bucket[/prefix/] to start at")
    b.add_argument("--theme", help="amber | phosphor | dos | synthwave | eightbit (ctrl+t cycles)")
    b.set_defaults(fn=cmd_browse)

    r = sub.add_parser("restore", parents=[common], help="request restores for a prefix")
    r.add_argument("uri", nargs="?", help="s3://bucket/prefix/")
    r.add_argument("--include", action="append", metavar="GLOB", help="first-level folder/file glob (repeatable)")
    r.add_argument("--exclude", action="append", metavar="GLOB", help="first-level folder/file glob (repeatable)")
    r.add_argument("--deep", action="store_true", help="match globs against the full relative key")
    r.add_argument("--tier", choices=["Bulk", "Standard"])
    r.add_argument("--days", type=_positive_int, help="days to keep restored copies")
    r.add_argument("--name", help="job name (default: <folder>_<timestamp>)")
    r.add_argument("--job", help="re-send pending/failed/expired restore requests for an existing job")
    r.add_argument("--dry-run", action="store_true", help="list + estimate only")
    r.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    r.set_defaults(fn=cmd_restore)

    j = sub.add_parser("jobs", parents=[common], help="list jobs")
    j.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("status", parents=[common], help="refresh and show job progress")
    s.add_argument("job", nargs="?")
    s.add_argument("--no-refresh", action="store_true", help="show stored state without calling S3")
    s.add_argument("--errors", action="store_true", help="list objects with errors, conflicts or notes")
    s.set_defaults(fn=cmd_status)

    c = sub.add_parser("copy", parents=[common], help="copy readable objects to another bucket")
    c.add_argument("job")
    c.add_argument("dest", help="s3://dest-bucket/prefix/ (structure kept relative to the job's base prefix)")
    c.add_argument("--storage-class", help="default from thaw.toml (STANDARD)")
    c.add_argument("--overwrite", action="store_true",
                   help="replace objects that already exist at the destination (default: never replace)")
    c.add_argument("-y", "--yes", action="store_true", help="don't ask before cross-region copies")
    c.set_defaults(fn=cmd_copy)

    w = sub.add_parser("watch", parents=[common], help="poll until restored; optionally auto-copy")
    w.add_argument("job")
    w.add_argument("--auto-copy", metavar="S3_URI", help="copy objects here as they become readable")
    w.add_argument("--storage-class")
    w.add_argument("--interval", type=_positive_float, help="minutes between checks (default thaw.toml)")
    w.add_argument("-y", "--yes", action="store_true",
                   help="accept a cross-region (or unverifiable) destination without asking")
    w.set_defaults(fn=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load()
    try:
        return args.fn(args, cfg) or 0
    except (FileNotFoundError, FileExistsError, ValueError) as e:
        print(f"error: {safe(e)}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Requests that reached S3 are recorded in the job; "
              "re-run the same command to continue.", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        msg = friendly_error(e)
        if msg is None:
            raise
        print(f"error: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
