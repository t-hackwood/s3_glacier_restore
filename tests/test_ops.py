import pytest
from botocore.exceptions import ClientError

from thaw import ops
from thaw.cli import main
from thaw.jobs import C_CONFLICT, C_COPIED, C_PENDING, C_SKIPPED, Job
from thaw.s3util import (IN_PROGRESS, NOT_NEEDED, PENDING, RESTORED, restore_state_from_head)
from thaw.selection import GlobSelection

from .conftest import BASE, LAYOUT, REGION, SRC


def make_job(s3, cfg, exclude=("ANALYSIS",), name="t"):
    sel = GlobSelection(BASE, exclude=list(exclude))
    objs = ops.gather(s3, SRC, sel)
    return ops.create_job(cfg, name, SRC, BASE, sel, objs, "Bulk", 7, None, REGION), objs


def test_gather_excludes_analysis(s3):
    objs = ops.gather(s3, SRC, GlobSelection(BASE, exclude=["ANALYSIS"]))
    keys = {o.key[len(BASE):] for o in objs}
    assert keys == {k for k in LAYOUT if not k.startswith("ANALYSIS")}
    assert not any("OTHER" in o.key for o in objs)
    states = {o.key[len(BASE):]: o.restore_state for o in objs}
    assert states["IMAGES/photo.jpg"] == NOT_NEEDED
    assert states["PROPOSAL/proposal.docx"] == PENDING


def test_estimate_counts_and_cost(s3):
    objs = ops.gather(s3, SRC, GlobSelection(BASE, exclude=["ANALYSIS"]))
    pricing = {"DEEP_ARCHIVE": {"Bulk": {"per_gb": 0, "per_1000_requests": 1000}}}
    e = ops.estimate(objs, "Bulk", 7, pricing)
    assert e.to_restore == 6
    assert e.readable == 3
    assert e.cost == pytest.approx(6.0)


def test_head_parsing():
    assert restore_state_from_head({"StorageClass": "STANDARD"})[0] == NOT_NEEDED
    assert restore_state_from_head({"StorageClass": "DEEP_ARCHIVE"})[0] == PENDING
    assert restore_state_from_head({"StorageClass": "DEEP_ARCHIVE", "Restore": 'ongoing-request="true"'})[0] == IN_PROGRESS
    st, exp = restore_state_from_head({"StorageClass": "DEEP_ARCHIVE",
                                       "Restore": 'ongoing-request="false", expiry-date="Fri, 21 Dec 2026 00:00:00 GMT"'})
    assert st == RESTORED and "2026" in exp


def test_full_flow_restore_status_copy(s3, cfg):
    job, _ = make_job(s3, cfg)
    # copying before restore copies only readable objects
    r = ops.copy_objects(s3, job, "s3://dest-same/out/")
    assert r[C_COPIED] == 3

    res = ops.request_restores(s3, job)
    assert res == {"requested": 6, "already": 0, "failed": 0}
    assert job.counts()["restore"][IN_PROGRESS] == 6

    ops.refresh_status(s3, job)  # moto restores instantly
    c = job.counts()
    assert c["restore"][RESTORED] == 6 and job.all_readable

    r = ops.copy_objects(s3, job, "s3://dest-same/out", storage_class="STANDARD_IA")
    assert r[C_COPIED] == 6 and job.all_copied
    h = s3.head_object(Bucket="dest-same", Key="out/DOCUMENTATION/sub/deep.csv")
    assert h["ContentLength"] == 70 and h.get("StorageClass") == "STANDARD_IA"
    listed = s3.list_objects_v2(Bucket="dest-same", Prefix="out/")["KeyCount"]
    assert listed == 9

    # nothing left to do
    assert sum(ops.copy_objects(s3, job, "s3://dest-same/out/").values()) == 0


def test_copy_never_replaces_existing_objects_unless_overwrite(s3, cfg):
    job, _ = make_job(s3, cfg, name="a")
    s3.put_object(Bucket="dest-same", Key="d/IMAGES/photo.jpg", Body=b"y" * 120)  # same size
    s3.put_object(Bucket="dest-same", Key="d/readme.txt", Body=b"y" * 99)       # different size
    r = ops.copy_objects(s3, job, "s3://dest-same/d/")
    # readable now: DOCUMENTATION/ folder marker (copied), photo (already there), readme (conflict)
    assert (r[C_COPIED], r[C_SKIPPED], r[C_CONFLICT]) == (1, 1, 1)
    assert s3.get_object(Bucket="dest-same", Key="d/readme.txt")["Body"].read() == b"y" * 99
    [row] = job.objects("copy_state = ?", (C_CONFLICT,))
    assert row["key"] == BASE + "readme.txt" and "different object" in row["copy_error"]

    assert ops.copy_objects(s3, job, "s3://dest-same/d/")[C_CONFLICT] == 1  # still left alone
    r = ops.copy_objects(s3, job, "s3://dest-same/d/", overwrite=True)
    assert r[C_COPIED] == 1 and job.counts()["copy"][C_CONFLICT] == 0
    assert s3.get_object(Bucket="dest-same", Key="d/readme.txt")["Body"].read() == b"x" * 5


def test_already_in_progress_is_not_a_failure(s3, cfg):
    job, _ = make_job(s3, cfg)

    def boom(**kw):
        raise ClientError({"Error": {"Code": "RestoreAlreadyInProgress", "Message": "x"}}, "RestoreObject")
    s3.restore_object = boom
    res = ops.request_restores(s3, job)
    assert res["already"] == 6 and res["failed"] == 0
    assert job.counts()["restore"][IN_PROGRESS] == 6


def test_failed_requests_are_retried(s3, cfg):
    job, _ = make_job(s3, cfg)
    real = s3.restore_object

    def boom(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "RestoreObject")
    s3.restore_object = boom
    assert ops.request_restores(s3, job)["failed"] == 6
    s3.restore_object = real
    assert ops.request_restores(s3, job)["requested"] == 6


def test_watch_auto_copy(s3, cfg):
    job, _ = make_job(s3, cfg)
    ops.request_restores(s3, job)
    logs = []
    assert ops.watch(s3, job, 0, auto_copy="s3://dest-same/w/", log=logs.append, sleep=lambda s: None) == 0
    assert job.all_copied and logs[-1].endswith("done.")


def test_watch_stops_when_nothing_requested(s3, cfg):
    job, _ = make_job(s3, cfg)
    logs = []
    assert ops.watch(s3, job, 0, log=logs.append, sleep=lambda s: pytest.fail("should not sleep")) == 1
    assert any("no live restore" in m and "thaw restore --job t" in m for m in logs)


def test_regions(s3):
    assert ops.check_regions(s3, SRC, "dest-other") == (REGION, "us-west-2")


def test_cli_dry_run_creates_nothing(s3, cfg, capsys):
    rc = main(["restore", f"s3://{SRC}/{BASE}", "--exclude", "ANALYSIS", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "Need restore     : 6" in out and "dry run" in out
    assert not list(cfg.jobs_dir.glob("*.sqlite"))


def test_cli_restore_status_copy(s3, cfg, capsys):
    assert main(["restore", f"s3://{SRC}/{BASE}", "--exclude", "ANALYSIS", "--name", "cli", "-y"]) == 0
    assert main(["status", "cli"]) == 0
    assert "restored 6" in capsys.readouterr().out
    assert main(["copy", "cli", "s3://dest-same/c/"]) == 0
    job = Job.open(cfg.jobs_dir, "cli")
    assert job.counts()["copy"][C_PENDING] == 0
    assert cfg.recent_destinations() == ["s3://dest-same/c/"]
    assert main(["jobs"]) == 0
