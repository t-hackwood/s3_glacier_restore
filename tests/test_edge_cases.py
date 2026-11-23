"""Regression tests for the review fixes: expiry, credentials, interrupts, job files, CLI input."""

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
from botocore.exceptions import ClientError

from thaw import ops
from thaw.cli import main
from thaw.config import home
from thaw.jobs import C_COPIED, C_PENDING, Job, NotAJobError
from thaw.s3util import FAILED, IN_PROGRESS, PENDING, RESTORED, describe_when, parse_uri, parse_when, safe
from thaw.selection import GlobSelection

from .conftest import BASE, REGION, SRC
from .test_ops import make_job

PROPOSAL = BASE + "PROPOSAL/proposal.docx"


def client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} message"}}, op)


def raises(exc: Exception):
    def call(*a, **kw):
        raise exc
    return call


def restored_job(s3, cfg, name="t"):
    job, _ = make_job(s3, cfg, name=name)
    ops.request_restores(s3, job)
    ops.refresh_status(s3, job)  # moto restores instantly
    assert job.counts()["restore"][RESTORED] == 6
    return job


# --- restored copies that expire -------------------------------------------------
def test_status_refresh_notices_an_expired_restore(s3, cfg):
    job = restored_job(s3, cfg)
    real = s3.head_object

    def head(**kw):
        h = real(**kw)
        if kw["Key"] == PROPOSAL:
            h.pop("Restore", None)  # S3 drops the header once the restored copy expires
        return h
    s3.head_object = head
    ops.refresh_status(s3, job)
    assert job.objects("key = ?", (PROPOSAL,))[0]["restore_state"] == PENDING
    assert job.counts()["restore"][RESTORED] == 5


def test_copy_of_an_expired_restore_resets_it_for_a_new_request(s3, cfg):
    job = restored_job(s3, cfg)
    real = s3.copy

    def copy(src, *a, **kw):
        if src["Key"] == PROPOSAL:
            raise client_error("InvalidObjectState", "CopyObject")
        return real(src, *a, **kw)
    s3.copy = copy
    r = ops.copy_objects(s3, job, "s3://dest-same/e/")
    assert r["expired"] == 1 and r[C_COPIED] == 8 and r["failed"] == 0
    row = job.objects("key = ?", (PROPOSAL,))[0]
    assert (row["restore_state"], row["copy_state"]) == (PENDING, C_PENDING)
    assert "request the restore again" in row["copy_error"]

    s3.copy = real
    assert ops.request_restores(s3, job)["requested"] == 1  # only the expired one
    assert job.objects("key = ?", (PROPOSAL,))[0]["copy_error"] is None


def test_dates_parse_both_formats_and_compare_as_dates():
    rfc, iso = parse_when("Mon, 07 Dec 2026 00:00:00 GMT"), parse_when("2026-12-05T10:00:00+00:00")
    assert rfc.tzinfo and iso.tzinfo and iso < rfc
    assert parse_when("") is None and parse_when("not a date") is None
    now = datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert describe_when(iso, now) == "2026-12-05 10:00 UTC (in 4.4 days)"
    assert describe_when(now + timedelta(hours=6), now).endswith("(in 6 h)")
    assert describe_when(now - timedelta(seconds=1), now).endswith("(expired)")


def test_earliest_expiry_is_the_soonest_date_not_the_smallest_string(s3, cfg):
    job, _ = make_job(s3, cfg)
    keys = [r["key"] for r in job.objects("restore_state = ?", (PENDING,))]
    dec = lambda d: datetime(2026, 12, d, tzinfo=timezone.utc)  # noqa: E731
    # As strings, "2026-12-09…" and "Fri, 11 Dec…" both sort before "Mon, 07 Dec…".
    job.update(keys[0], restore_state=RESTORED, restore_expiry=format_datetime(dec(11), usegmt=True))
    job.update(keys[1], restore_state=RESTORED, restore_expiry=format_datetime(dec(7), usegmt=True))
    job.update(keys[2], restore_state=RESTORED, restore_expiry=dec(9).isoformat())
    job.update(keys[3], restore_state=RESTORED, restore_expiry=dec(2).isoformat(), copy_state=C_COPIED)
    assert job.earliest_expiry() == dec(7)  # the Dec 2 one is already copied, so it doesn't count


# --- credentials -------------------------------------------------------------------
def test_expired_credentials_stop_restore_requests_early(s3, cfg):
    job, _ = make_job(s3, cfg)
    calls = []

    def expired(**kw):
        calls.append(kw["Key"])
        time.sleep(0.05)
        raise client_error("ExpiredToken", "RestoreObject")
    s3.restore_object = expired
    with pytest.raises(ops.AuthError):
        ops.request_restores(s3, job, workers=1)
    assert len(calls) <= 2  # the rest were never sent
    c = job.counts()["restore"]
    assert c[PENDING] == 6 and c[FAILED] == 0  # not marked failed: nothing reached S3
    assert all("ExpiredToken" in r["error"] for r in job.problems())


def test_watch_stops_on_expired_credentials(s3, cfg, monkeypatch, capsys):
    job, _ = make_job(s3, cfg)
    ops.request_restores(s3, job)
    s3.head_object = raises(client_error("ExpiredToken", "HeadObject"))
    with pytest.raises(ops.AuthError):
        ops.watch(s3, job, 0, log=lambda m: None, sleep=lambda s: pytest.fail("should not sleep"))

    monkeypatch.setattr("thaw.cli.client", lambda *a, **kw: s3)
    assert main(["watch", "t"]) == 1
    assert "log in again" in capsys.readouterr().out.lower()


def test_watch_stops_when_every_status_check_fails(s3, cfg):
    job, _ = make_job(s3, cfg)
    ops.request_restores(s3, job)
    s3.head_object = raises(client_error("AccessDenied", "HeadObject"))
    logs = []
    assert ops.watch(s3, job, 0, log=logs.append, sleep=lambda s: pytest.fail("should not sleep")) == 1
    assert "status-check errors 6/6" in logs[0]
    assert "every status check failed" in logs[1]


def test_cli_explains_aws_errors_instead_of_a_traceback(s3, capsys):
    assert main(["restore", "s3://no-such-bucket-here/x/", "--dry-run"]) == 1
    err = capsys.readouterr().err
    assert "Bucket not found" in err and "Traceback" not in err


# --- interrupts --------------------------------------------------------------------
def test_ctrl_c_records_requests_already_sent_and_sends_no_more(s3, cfg):
    job, _ = make_job(s3, cfg)
    real = s3.restore_object
    sent = []

    def slow(**kw):
        time.sleep(0.05)  # so the interrupt lands while later requests are still queued
        r = real(**kw)
        sent.append(kw["Key"])
        return r
    s3.restore_object = slow

    def progress(done, total):
        if done == 1:
            raise KeyboardInterrupt  # what Ctrl+C looks like on the main thread
    with pytest.raises(KeyboardInterrupt):
        ops.request_restores(s3, job, workers=1, progress=progress)
    in_progress = {r["key"] for r in job.objects("restore_state = ?", (IN_PROGRESS,))}
    n = len(sent)
    assert set(sent) == in_progress and 1 <= n < 6
    assert ops.request_restores(s3, job)["requested"] == 6 - n  # a re-run carries on


# --- multipart copies keep tags ----------------------------------------------------
def test_large_copies_keep_object_tags(s3, cfg):
    big, small = BASE + "BIG/raster.tif", BASE + "BIG/small.txt"
    s3.put_object(Bucket=SRC, Key=big, Body=b"z" * (10 * 1024 * 1024), Tagging="project=demo&keep=yes")
    s3.put_object(Bucket=SRC, Key=small, Body=b"z", Tagging="project=demo")
    sel = GlobSelection(BASE, include=["BIG"])
    job = ops.create_job(cfg, "big", SRC, BASE, sel, ops.gather(s3, SRC, sel), "Bulk", 7, None, REGION)
    assert ops.copy_objects(s3, job, "s3://dest-same/big/")[C_COPIED] == 2
    tags = lambda k: {t["Key"]: t["Value"] for t in s3.get_object_tagging(Bucket="dest-same", Key=k)["TagSet"]}  # noqa: E731
    assert tags("big/BIG/raster.tif") == {"project": "demo", "keep": "yes"}
    assert tags("big/BIG/small.txt") == {"project": "demo"}


# --- job files and paths -------------------------------------------------------------
def test_job_open_ignores_same_named_paths_and_refuses_other_databases(s3, cfg, tmp_path, monkeypatch):
    make_job(s3, cfg, name="demo")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "demo").mkdir()  # a folder in the current directory with the job's name
    assert Job.open(cfg.jobs_dir, "demo").meta["bucket"] == SRC
    with pytest.raises(FileNotFoundError):
        Job.open(cfg.jobs_dir, "nope")

    other = cfg.jobs_dir / "someone_else.sqlite"
    con = sqlite3.connect(other)
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    (cfg.jobs_dir / "garbage.sqlite").write_text("not a database")
    for bad in (str(other), "garbage"):
        with pytest.raises(NotAJobError):
            Job.open(cfg.jobs_dir, bad)
    tables = {r[0] for r in sqlite3.connect(other).execute("SELECT name FROM sqlite_master")}
    assert tables == {"t"}  # untouched: no thaw tables added to someone else's database
    assert [j.name for j in Job.list_all(cfg.jobs_dir)] == ["demo"]


def test_jobs_live_outside_site_packages_for_a_regular_install(tmp_path, monkeypatch):
    monkeypatch.delenv("THAW_HOME")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    installed = tmp_path / "site-packages" / "thaw" / "config.py"
    assert home(installed) == tmp_path / "xdg" / "thaw"
    repo = tmp_path / "repo"
    (repo / "src" / "thaw").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("")
    assert home(repo / "src" / "thaw" / "config.py") == repo
    monkeypatch.setenv("THAW_HOME", str(tmp_path / "chosen"))
    assert home(installed) == tmp_path / "chosen"


# --- input validation ----------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    ["restore", f"s3://{SRC}/{BASE}", "--days", "-1"],
    ["restore", f"s3://{SRC}/{BASE}", "--days", "0"],
    ["restore", f"s3://{SRC}/{BASE}", "--workers", "0"],
    ["watch", "t", "--interval", "0"],
])
def test_cli_rejects_non_positive_numbers(argv):
    with pytest.raises(SystemExit) as e:
        main(argv)
    assert e.value.code == 2


def test_bucket_names_are_validated():
    assert parse_uri("s3://my-bucket.data/a/b/") == ("my-bucket.data", "a/b/")
    for bad in ("s3://b/x", "s3://bad bucket/x", "s3://-dash/x", "s3://"):
        with pytest.raises(ValueError):
            parse_uri(bad)


def test_control_characters_are_made_visible():
    assert safe("a\x1b[31mb\nc\x7f") == "a\\x1b[31mb\\x0ac\\x7f"
    assert safe("ok/é/✓ [x]") == "ok/é/✓ [x]"
