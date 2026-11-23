import asyncio
import threading

from rich.text import Text

from thaw import ops
from thaw.jobs import C_CONFLICT, Job
from thaw.s3util import IN_PROGRESS
from thaw.selection import PARTIAL, TreeSelection
from thaw.tui import app as tui
from thaw.tui.app import BrowseScreen, ConfirmRestore, CopyDialog, JobsScreen, SelectTree, ThawApp

from .conftest import BASE, REGION, SRC

EVIL = "report[old] [@click=app.quit]boom.txt"  # Textual markup if it were ever parsed


async def settle(app, pilot, timeout: float = 10.0):
    """Wait for Textual workers and the browse screen's own stats pool, without blocking the loop."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        await app.workers.wait_for_complete()
        await pilot.pause()
        stats_busy = any(getattr(s, "_futures", None) for s in app.screen_stack)
        if not stats_busy and all(w.is_finished for w in app.workers):
            await pilot.pause(0.3)  # let the debounced relabel run
            return
        assert loop.time() < end, "UI did not settle"
        await asyncio.sleep(0.02)


async def quick(app, pilot):
    """Wait for Textual workers only (folder listings, restore), not for folder stats."""
    await app.workers.wait_for_complete()
    await pilot.pause()


def child(node, rel):
    return next(c for c in node.children if c.data and c.data.path.endswith(rel))


def block_stats(monkeypatch, only_bucket: str | None = None) -> threading.Event:
    """Make the stats listings hang until the returned event is set."""
    gate = threading.Event()
    real = tui.list_objects

    def slow(s3, bucket, prefix):
        if only_bucket in (None, bucket):
            gate.wait(10)
        yield from real(s3, bucket, prefix)
    monkeypatch.setattr(tui, "list_objects", slow)
    return gate


async def test_browse_select_restore_and_copy(s3, cfg):
    app = ThawApp(cfg, f"s3://{SRC}/{BASE}", s3=s3, workers=4)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, BrowseScreen)
        tree = screen.query_one(SelectTree)
        assert {c.data.path[len(BASE):] for c in tree.root.children} >= {"ANALYSIS/", "DOCUMENTATION/", "readme.txt"}
        assert screen.stats[BASE + "DOCUMENTATION/"].objects == 4

        tree.move_cursor(tree.root)
        await pilot.press("space")
        tree.move_cursor(child(tree.root, "ANALYSIS/"))
        await pilot.press("space")
        assert screen.sel.state(BASE) == PARTIAL
        assert tui.ACTIVE.ticks[1] in str(tree.root.label)

        await pilot.press("r")
        await settle(app, pilot)
        assert isinstance(app.screen, ConfirmRestore)
        app.screen.query_one("#name").value = "tui"
        await pilot.click("#ok")
        await settle(app, pilot)
        assert isinstance(app.screen, JobsScreen)

        job = Job.open(cfg.jobs_dir, "tui")
        assert job.counts()["objects"] == 9
        assert job.counts()["restore"][IN_PROGRESS] == 6
        assert job.meta["base_prefix"] == BASE

        await pilot.press("u")
        await settle(app, pilot)
        assert job.all_readable

        s3.put_object(Bucket="dest-same", Key="t/readme.txt", Body=b"different size")
        await pilot.press("c")
        await settle(app, pilot)
        assert isinstance(app.screen, CopyDialog)
        app.screen.query_one("#dest").value = "s3://dest-same/t/"
        await pilot.click("#ok")
        await settle(app, pilot)
        assert job.counts()["copy"][C_CONFLICT] == 1  # existing object left alone
        assert s3.get_object(Bucket="dest-same", Key="t/readme.txt")["Body"].read() == b"different size"
        log = str(app.screen.query_one("#log").content)
        assert "conflicts 1, failed 0\nConflicts were left alone" in log  # real line breaks
        assert "\\x0a" not in log
        assert s3.list_objects_v2(Bucket="dest-same", Prefix="t/")["KeyCount"] == 9


async def test_browsing_and_restore_do_not_wait_for_folder_stats(s3, cfg, monkeypatch):
    # More stuck listings than Textual's shared thread pool has threads (≤ 32): the old
    # code ran stats there, so folder listings and the restore dialog queued behind them.
    for i in range(40):
        s3.put_object(Bucket=SRC, Key=f"{BASE}TILE_{i:02d}/a.txt", Body=b"1")
    gate = block_stats(monkeypatch)
    app = ThawApp(cfg, f"s3://{SRC}/{BASE}", s3=s3, workers=4)
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            await quick(app, pilot)
            screen = app.screen
            tree = screen.query_one(SelectTree)
            assert screen.stats_running and not screen.stats  # every stats listing is stuck

            doc = child(tree.root, "DOCUMENTATION/")
            doc.expand()
            await quick(app, pilot)
            assert {c.data.path[len(BASE):] for c in doc.children if c.data} >= {
                "DOCUMENTATION/sub/", "DOCUMENTATION/report.pdf"}

            tree.move_cursor(doc)
            await pilot.press("space")
            await pilot.press("r")
            await quick(app, pilot)
            assert isinstance(app.screen, ConfirmRestore)
            estimate = str(app.screen.query_one("#estimate").content)
            assert "Objects selected : 4" in estimate and "\nNeed restore" in estimate
            assert "\\x0a" not in estimate
            await pilot.press("escape")

            gate.set()
            await settle(app, pilot)
            assert screen.stats[BASE + "DOCUMENTATION/"].objects == 4
            assert not screen.stats_running
    finally:
        gate.set()


async def test_switching_location_drops_results_from_the_old_one(s3, cfg, monkeypatch):
    gate = block_stats(monkeypatch, only_bucket=SRC)
    s3.put_object(Bucket="dest-same", Key="other/a.txt", Body=b"1")
    app = ThawApp(cfg, f"s3://{SRC}/{BASE}", s3=s3, workers=4)
    try:
        async with app.run_test(size=(140, 45)) as pilot:
            await quick(app, pilot)
            screen = app.screen
            screen.load("s3://dest-same/other/")
            gate.set()  # the old location's listings now finish — too late to count
            await settle(app, pilot)
            assert set(screen.stats) == {"other/"}
            assert set(screen.cache) == {"other/a.txt"}
            tree = screen.query_one(SelectTree)
            assert [c.data.path for c in tree.root.children if c.data] == ["other/a.txt"]
    finally:
        gate.set()


async def test_big_folders_show_a_capped_number_of_files(s3, cfg, monkeypatch):
    monkeypatch.setattr(tui, "MAX_FILES", 3)
    for i in range(5):
        s3.put_object(Bucket=SRC, Key=f"{BASE}MANY/f{i}.txt", Body=b"1")
    app = ThawApp(cfg, f"s3://{SRC}/{BASE}MANY/", s3=s3, workers=4)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        tree = app.screen.query_one(SelectTree)
        assert len([c for c in tree.root.children if c.data]) == 3
        assert "2 more files" in str(tree.root.children[-1].label).lower()

        tree.move_cursor(tree.root)
        await pilot.press("space")
        await pilot.press("r")
        await settle(app, pilot)
        assert "Objects selected : 5" in str(app.screen.query_one("#estimate").content)


async def test_s3_names_and_errors_are_never_parsed_as_markup(s3, cfg):
    key = BASE + EVIL
    s3.put_object(Bucket=SRC, Key=key, Body=b"1")
    sel = TreeSelection(BASE, {key: True})
    job = ops.create_job(cfg, "evil", SRC, BASE, sel, ops.gather(s3, SRC, sel), "Bulk", 7, None, REGION)
    job.update(key, copy_error=f"AccessDenied: no access to {EVIL}\nFAKE LINE")
    job.close()

    app = ThawApp(cfg, f"s3://{SRC}/{BASE}", s3=s3, workers=4)
    async with app.run_test(size=(140, 45)) as pilot:
        await settle(app, pilot)
        tree = app.screen.query_one(SelectTree)
        label = child(tree.root, EVIL).label
        assert EVIL in label.plain  # markup would have eaten "[old]" and "[@click=…]"

        app.screen.load("s3://bad bucket [@click=app.quit]x/")  # invalid → error toast
        await pilot.pause()
        note = list(app._notifications)[-1]
        assert note.markup is False and "[@click=app.quit]" in note.message

        await pilot.press("j")
        await settle(app, pilot)
        await pilot.press("e")
        await pilot.pause()
        content = app.screen.query_one("#log").content
        assert isinstance(content, Text) and EVIL in content.plain and not content.spans
        assert "\\x0aFAKE LINE" in content.plain  # a line break inside S3 text can't fake a new line
