"""Textual TUI: browse a bucket as a tick-tree, restore the selection, watch jobs, copy.

Anything that came from S3 (keys, prefixes, error messages) is shown through ``rich.text.Text``
after ``safe()``, never as markup: Textual markup can contain click actions, and a crafted
object name must not be able to create one.
"""

from __future__ import annotations

import bisect
import contextvars
import os
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.suggester import SuggestFromList
from textual.widget import Widget
from textual.widgets import (Button, Checkbox, DataTable, Footer, Input, Label,
                             ProgressBar, RadioButton, RadioSet, Select, Static, Tree)
from textual.widgets.tree import TreeNode

from .. import ops
from ..config import Config
from ..jobs import C_CONFLICT, Job
from ..s3util import (ARCHIVE_CLASSES, FAILED, IN_PROGRESS, NOT_NEEDED, PENDING, RESTORED, ObjInfo,
                      as_folder, client, describe_when, human_size, list_children, list_objects,
                      parse_uri, safe)
from ..selection import CHECKED, PARTIAL, TreeSelection, is_folder
from . import themes

STATS_LIMIT = 20_000    # auto-stats stop after this many objects per folder ('s' lists fully)
AUTO_STATS_MAX = 200    # auto-stats only for the first N sub-folders of an expanded folder
MAX_FILES = 1_000       # file leaves shown per folder; the rest are summarised in one line
CACHE_MAX = 200_000     # objects kept in memory from complete listings
STATS_THREADS = 4       # stats listings run on their own pool, never on Textual's shared one
STORAGE_CLASSES = ["STANDARD", "INTELLIGENT_TIERING", "STANDARD_IA", "ONEZONE_IA", "GLACIER_IR",
                   "GLACIER", "DEEP_ARCHIVE"]
AUTH_HINT = "AWS credentials expired or were rejected — log in again (e.g. `aws sso login`) and retry."

ACTIVE: themes.Retro = themes.PLAIN  # set by ThawApp.watch_theme


def pal(key: str) -> str:
    return ACTIVE.palette[key]


def tick(state: str) -> tuple[str, str]:
    i = {CHECKED: 0, PARTIAL: 1}.get(state, 2)
    return ACTIVE.ticks[i], pal(("tick_on", "tick_partial", "tick_off")[i])


def ui(text: str) -> str:
    """Chrome text (headings/help), upper-cased for themes that shout."""
    return text.upper() if ACTIVE.upper else text


def plain(text: object, style: str = "") -> Text:
    """Text that is never parsed as markup. Line breaks are kept and other control characters
    made visible, so untrusted parts that could hold a line break must go through safe() first."""
    return Text("\n".join(safe(line) for line in str(text).split("\n")), style=style)


def say(widget: Widget, message: object, **kw) -> None:
    """notify() without markup, for messages that may contain S3 names."""
    widget.notify(safe(message), markup=False, **kw)


def err_text(e: BaseException) -> str:
    return AUTH_HINT if isinstance(e, ops.AuthError) or ops.is_auth_error(e) else ops._err(e)


class Banner(Static):
    def render(self) -> Text:
        return plain(f" {ACTIVE.banner}   {safe(self.app.sub_title or '')}")


@dataclass
class NodeData:
    path: str
    folder: bool
    loaded: bool = False
    obj: ObjInfo | None = None


@dataclass
class Stats:
    objects: int = 0
    bytes: int = 0
    archived: int = 0
    in_progress: int = 0
    restored: int = 0
    complete: bool = True

    @classmethod
    def of(cls, objs, complete=True) -> "Stats":
        s = cls(complete=complete)
        for o in objs:
            s.objects += 1
            s.bytes += o.size
            if o.storage_class in ARCHIVE_CLASSES:
                s.archived += 1
                s.in_progress += o.restore_state == IN_PROGRESS
                s.restored += o.restore_state == RESTORED
        return s

    def text(self) -> Text:
        plus = "" if self.complete else "+"
        a, ip, ok = ACTIVE.glyphs
        t = Text(f"  {self.objects:,}{plus} {ui('obj')} · {human_size(self.bytes)}{plus}", style=pal("stats"))
        if self.archived:
            t.append(f" · {a} {self.archived:,}", style=pal("archived"))
        if self.in_progress:
            t.append(f" · {ip} {self.in_progress:,}", style=pal("restoring"))
        if self.restored:
            t.append(f" · {ok} {self.restored:,}", style=pal("restored"))
        return t


def common_folder(paths: list[str]) -> str:
    """Longest common folder prefix of the selected roots (the job's base prefix)."""
    if not paths:
        return ""
    if len(paths) == 1:
        p = paths[0]
        return p if is_folder(p) else p.rsplit("/", 1)[0] + "/" if "/" in p else ""
    pre = os.path.commonprefix(paths)
    return pre[: pre.rfind("/") + 1]


# --- messages from background threads (post_message is thread-safe and never blocks) ----
class ChildrenLoaded(Message):
    def __init__(self, gen: int, node: TreeNode, folders: list[str], files: list[ObjInfo], more: int,
                 error: BaseException | None = None):
        super().__init__()
        self.gen, self.node, self.folders, self.files, self.more, self.error = gen, node, folders, files, more, error


class StatsDone(Message):
    def __init__(self, gen: int, prefix: str, objs: list[ObjInfo], complete: bool,
                 error: BaseException | None = None):
        super().__init__()
        self.gen, self.prefix, self.objs, self.complete, self.error = gen, prefix, objs, complete, error


# --- modals ------------------------------------------------------------------
class ConfirmRestore(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, lines: list[str], base: str, tier: str, days: int, name: str):
        super().__init__()
        self.lines, self.base, self.tier, self.days, self.default_name = lines, base, tier, days, name

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Restore selection", classes="title")
            yield Static(plain(f"Base prefix (copy keeps paths relative to this): {safe(self.base) or '(bucket root)'}"))
            yield Static(plain("\n".join(safe(line) for line in self.lines)), id="estimate")
            yield Label("Tier")
            with RadioSet(id="tier"):
                yield RadioButton("Bulk  (≤48 h, cheapest)", value=self.tier == "Bulk", name="Bulk")
                yield RadioButton("Standard  (≤12 h)", value=self.tier == "Standard", name="Standard")
            yield Label("Keep restored copies for (days)")
            yield Input(str(self.days), id="days", type="integer", compact=True)
            yield Label("Job name")
            yield Input(self.default_name, id="name", compact=True)
            with Horizontal(classes="buttons"):
                yield Button("Request restores", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        rs = self.query_one("#tier", RadioSet)
        tier = rs.pressed_button.name if rs.pressed_button else self.tier
        try:
            days = max(1, int(self.query_one("#days", Input).value))
        except ValueError:
            self.notify("Days must be a number", severity="error")
            return
        name = self.query_one("#name", Input).value.strip() or self.default_name
        self.dismiss({"tier": tier, "days": days, "name": name})

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class CopyDialog(ModalScreen[dict | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, job_name: str, recents: list[str], storage_class: str, readable: str):
        super().__init__()
        self.job_name, self.recents, self.sc, self.readable = job_name, recents, storage_class, readable

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(plain(f"Copy job {self.job_name}"), classes="title")
            yield Static(plain(self.readable))
            yield Label("Destination (s3://bucket/prefix/)")
            yield Input(self.recents[0] if self.recents else "", id="dest",
                        placeholder="s3://dest-bucket/prefix/", suggester=SuggestFromList(self.recents))
            if self.recents:
                yield Select([(plain(r), r) for r in self.recents], prompt="Recent destinations", id="recent")
            yield Label("Destination storage class")
            yield Select([(s, s) for s in STORAGE_CLASSES], value=self.sc, allow_blank=False, id="sc")
            yield Checkbox("Replace objects that already exist at the destination "
                           "(otherwise they're left alone and reported as conflicts)", id="overwrite")
            with Horizontal(classes="buttons"):
                yield Button("Copy", variant="primary", id="ok")
                yield Button("Cancel", id="cancel")

    @on(Select.Changed, "#recent")
    def pick_recent(self, ev: Select.Changed) -> None:
        if ev.value and ev.value != Select.NULL:
            self.query_one("#dest", Input).value = str(ev.value)

    @on(Button.Pressed, "#ok")
    def ok(self) -> None:
        dest = self.query_one("#dest", Input).value.strip()
        try:
            parse_uri(dest)
            valid = dest.startswith("s3://")
        except ValueError:
            valid = False
        if not valid:
            self.notify("Destination must look like s3://bucket/prefix/", severity="error")
            return
        self.dismiss({"dest": dest, "storage_class": self.query_one("#sc", Select).value,
                      "overwrite": self.query_one("#overwrite", Checkbox).value})

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class YesNo(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Cancel")]

    def action_no(self) -> None:
        self.dismiss(False)

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(plain(self.question))
            with Horizontal(classes="buttons"):
                yield Button("Continue", variant="warning", id="yes")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")


# --- browse ------------------------------------------------------------------
class SelectTree(Tree[NodeData]):
    BINDINGS = [
        Binding("space", "tick", "Tick/untick"),
        Binding("right", "expand", "Expand", show=False),
        Binding("left", "collapse", "Collapse", show=False),
    ]

    def action_tick(self) -> None:
        self.screen.toggle_cursor()  # type: ignore[attr-defined]

    def action_expand(self) -> None:
        if self.cursor_node and self.cursor_node.allow_expand:
            self.cursor_node.expand()

    def action_collapse(self) -> None:
        n = self.cursor_node
        if n is None:
            return
        if n.is_expanded:
            n.collapse()
        elif n.parent is not None:
            self.move_cursor(n.parent)


class BrowseScreen(Screen):
    BINDINGS = [
        Binding("r", "restore", "Restore selected"),
        Binding("s", "full_stats", "Full stats"),
        Binding("c", "clear", "Clear selection"),
        Binding("j", "jobs", "Jobs"),
        Binding("g", "goto", "Go to URI"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, uri: str | None):
        super().__init__()
        self.uri = uri
        self.bucket = ""
        self.base = ""
        self.gen = 0                        # bumped on every load; stale background results are dropped
        self.sel = TreeSelection()
        self.cache: dict[str, ObjInfo] = {}
        self._keys: list[str] = []          # sorted cache keys, rebuilt lazily
        self._keys_dirty = False
        self.listed: set[str] = set()       # prefixes whose complete listing is in cache
        self.stats: dict[str, Stats] = {}
        self.stats_running: dict[str, int] = {}   # prefix -> listings in flight
        self.pool = ThreadPoolExecutor(max_workers=STATS_THREADS, thread_name_prefix="thaw-stats")
        self._futures: set[Future] = set()
        self._refresh_pending = False
        self._restoring = False             # one restore (list → confirm → send) at a time

    @property
    def s3(self):
        return self.app.s3  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        yield Input(self.uri or "", placeholder="s3://bucket/prefix/  — Enter to load", id="uri")
        with Horizontal():
            yield SelectTree(Text("(enter a bucket URI above)"), id="tree")
            with VerticalScroll(id="side"):
                yield Static("", id="summary")
                yield Static(self.help_text(), id="help")
        yield Footer()

    def help_text(self) -> Text:
        a, ip, ok = ACTIVE.glyphs
        return Text(ui("space  tick / untick\n→ / ←  expand / collapse\nenter  expand\n"
                       "s  full stats for folder\nr  restore selection\nj  jobs\ng  change URI\n"
                       "ctrl+t  next theme\n\n"
                       f"{a} archived  {ip} restoring  {ok} restored"))

    def restyle(self) -> None:
        self.query_one("#help", Static).update(self.help_text())
        self.query_one(SelectTree).border_title = ui(" Archive ")
        self.query_one("#side").border_title = ui(" Selection ")
        self.relabel_all()
        self.update_summary()

    def on_mount(self) -> None:
        self.restyle()
        if self.uri:
            self.load(self.uri)
        else:
            self.query_one("#uri", Input).focus()

    def on_unmount(self) -> None:
        self.gen += 1
        self.pool.shutdown(wait=False, cancel_futures=True)

    @on(Input.Submitted, "#uri")
    def uri_submitted(self, ev: Input.Submitted) -> None:
        self.load(ev.value)

    def action_goto(self) -> None:
        self.query_one("#uri", Input).focus()

    def load(self, uri: str) -> None:
        try:
            bucket, prefix = parse_uri(uri)
        except ValueError as e:
            say(self, e, severity="error")
            return
        self.gen += 1                       # drop results still coming from the previous location
        for f in list(self._futures):
            f.cancel()
        self.bucket, self.base = bucket, as_folder(prefix)
        self.sel = TreeSelection(self.base)
        self.cache.clear()
        self._keys, self._keys_dirty = [], False
        self.listed.clear()
        self.stats.clear()
        self.stats_running.clear()
        tree = self.query_one(SelectTree)
        tree.clear()
        tree.root.data = NodeData(self.base, True)
        tree.root.set_label(self.label_for(tree.root))
        tree.root.expand()
        tree.focus()
        self.app.sub_title = f"s3://{self.bucket}/{self.base}"
        self.update_summary()

    # --- labels --------------------------------------------------------
    def label_for(self, node: TreeNode[NodeData]) -> Text:
        d = node.data
        assert d is not None
        mark, style = tick(self.sel.state(d.path))
        if node.is_root:
            name = f"s3://{self.bucket}/{d.path}"
        else:
            name = d.path[len(node.parent.data.path):] if node.parent and node.parent.data else d.path
        t = Text(mark + " ", style=style)
        t.append(safe(name), style="bold" if d.folder else "")
        if d.folder:
            st = self.stats.get(d.path)
            if st:
                t.append_text(st.text())
            elif d.path in self.stats_running:
                t.append("  …", style=pal("dim"))
        elif d.obj:
            o = d.obj
            t.append(f"  {human_size(o.size)}  {o.storage_class}", style=pal("stats"))
            if o.restore_state == IN_PROGRESS:
                t.append(f"  {ACTIVE.glyphs[1]}", style=pal("restoring"))
            elif o.restore_state == RESTORED:
                t.append(f"  {ACTIVE.glyphs[2]}", style=pal("restored"))
        return t

    def relabel_all(self) -> None:
        tree = self.query_one(SelectTree)

        def walk(n: TreeNode[NodeData]):
            if n.data is not None:
                n.set_label(self.label_for(n))
            for c in n.children:
                walk(c)
        walk(tree.root)

    def schedule_refresh(self) -> None:
        """Coalesce relabel + summary work when many background results arrive at once."""
        if not self._refresh_pending:
            self._refresh_pending = True
            self.set_timer(0.2, self._flush)

    def _flush(self) -> None:
        self._refresh_pending = False
        self.relabel_all()
        self.update_summary()

    # --- cache -----------------------------------------------------------
    def _cache_put(self, o: ObjInfo) -> None:
        if o.key not in self.cache:
            self._keys_dirty = True
        self.cache[o.key] = o

    def _cached_under(self, root: str):
        if self._keys_dirty:
            self._keys, self._keys_dirty = sorted(self.cache), False
        i = bisect.bisect_left(self._keys, root)
        while i < len(self._keys) and self._keys[i].startswith(root):
            yield self.cache[self._keys[i]]
            i += 1

    def _is_listed(self, root: str) -> bool:
        return any(root.startswith(p) for p in self.listed)

    # --- loading (results arrive as messages, checked against the current generation) ----
    @on(Tree.NodeExpanded)
    def expanded(self, ev: Tree.NodeExpanded) -> None:
        d = ev.node.data
        if d and d.folder and not d.loaded:
            d.loaded = True
            ev.node.set_label(self.label_for(ev.node))
            self.load_children(ev.node, self.gen, self.bucket)

    @work(thread=True)
    def load_children(self, node: TreeNode[NodeData], gen: int, bucket: str) -> None:
        try:
            folders, files, more = list_children(self.s3, bucket, node.data.path, MAX_FILES)
        except Exception as e:  # noqa: BLE001
            self.post_message(ChildrenLoaded(gen, node, [], [], 0, e))
            return
        self.post_message(ChildrenLoaded(gen, node, folders, files, more))

    @on(ChildrenLoaded)
    def children_loaded(self, m: ChildrenLoaded) -> None:
        if m.gen != self.gen:
            return
        node = m.node
        if m.error is not None:
            node.data.loaded = False        # collapse + expand again to retry
            say(self, f"Listing failed: {err_text(m.error)}", severity="error")
            return
        for f in m.folders:
            n = node.add("", data=NodeData(f, True), allow_expand=True)
            n.set_label(self.label_for(n))
        for o in m.files:
            self._cache_put(o)
            n = node.add_leaf("", data=NodeData(o.key, False, True, o))
            n.set_label(self.label_for(n))
        if m.more:
            node.add_leaf(Text(ui(f"… {m.more:,} more files not shown (included when the folder is ticked)"),
                               style=pal("dim")))
        if not m.folders and not m.files and not m.more:
            node.add_leaf(Text("(empty)", style=pal("dim")))
        for f in m.folders[:AUTO_STATS_MAX]:
            self.queue_stats(f, STATS_LIMIT)
        if node.is_root:
            self.queue_stats(node.data.path, STATS_LIMIT)
        self.schedule_refresh()

    def queue_stats(self, prefix: str, limit: int | None) -> None:
        if limit is not None and prefix in self.stats_running:
            return
        self.stats_running[prefix] = self.stats_running.get(prefix, 0) + 1
        gen, bucket, s3 = self.gen, self.bucket, self.s3
        ctx = contextvars.copy_context()
        fut = self.pool.submit(ctx.run, self._stats_task, gen, s3, bucket, prefix, limit)
        self._futures.add(fut)
        fut.add_done_callback(self._futures.discard)

    def _stats_task(self, gen: int, s3, bucket: str, prefix: str, limit: int | None) -> None:
        """Runs on the stats pool. Gives up as soon as the user moves to another location."""
        if gen != self.gen:
            return
        objs: list[ObjInfo] = []
        complete = True
        error = None
        try:
            for o in list_objects(s3, bucket, prefix):
                if gen != self.gen:
                    return
                objs.append(o)
                if limit and len(objs) >= limit:
                    complete = False
                    break
        except Exception as e:  # noqa: BLE001
            error = e
        try:
            self.post_message(StatsDone(gen, prefix, objs, complete, error))
        except Exception:  # noqa: BLE001 — screen/app already gone
            pass

    @on(StatsDone)
    def stats_done(self, m: StatsDone) -> None:
        if m.gen != self.gen:
            return
        left = self.stats_running.pop(m.prefix, 0) - 1
        if left > 0:
            self.stats_running[m.prefix] = left
        if m.error is not None:
            say(self, f"Stats failed for {m.prefix}: {err_text(m.error)}", severity="error")
            return
        old = self.stats.get(m.prefix)
        if old and old.complete and not m.complete:
            return                          # a full listing already landed; keep it
        if m.complete and len(self.cache) + len(m.objs) <= CACHE_MAX:
            for o in m.objs:
                self._cache_put(o)
            self.listed.add(m.prefix)
        self.stats[m.prefix] = Stats.of(m.objs, m.complete)
        self.schedule_refresh()

    def action_full_stats(self) -> None:
        n = self.query_one(SelectTree).cursor_node
        if n and n.data and n.data.folder:
            self.stats.pop(n.data.path, None)
            self.queue_stats(n.data.path, None)
            n.set_label(self.label_for(n))

    # --- selection -----------------------------------------------------
    def toggle_cursor(self) -> None:
        n = self.query_one(SelectTree).cursor_node
        if not n or not n.data:
            return
        self.sel.toggle(n.data.path)
        self._flush()

    def action_clear(self) -> None:
        self.sel = TreeSelection(self.base)
        self._flush()

    def _known(self) -> tuple[list[ObjInfo], bool]:
        roots = self.sel.roots
        exact = all(self._is_listed(r) or r in self.cache for r in roots)
        found: dict[str, ObjInfo] = {}
        for r in roots:
            for o in self._cached_under(r):
                if self.sel.is_selected(o.key):
                    found[o.key] = o
        return list(found.values()), exact

    def update_summary(self) -> None:
        roots = self.sel.roots
        w = self.query_one("#summary", Static)
        if not roots:
            w.update(Text(ui("Nothing selected.\n\nTick folders with space."), style=pal("dim")))
            return
        objs, exact = self._known()
        s = Stats.of(objs, exact)
        need = sum(o.restore_state == PENDING for o in objs)
        t = Text()
        for r in roots[:12]:
            t.append(f"  + {safe(r[len(self.base):]) or ui('(all)')}\n", style=pal("include"))
        if len(roots) > 12:
            t.append(f"  … {len(roots) - 12} more\n")
        excl = [p for p, v in self.sel.rules.items() if not v]
        for p in excl[:12]:
            t.append(f"  − {safe(p[len(self.base):])}\n", style=pal("exclude"))
        t.append_text(s.text())
        t.append(f"\n  {ui('need restore')}: {need:,}{'' if exact else '+'}\n")
        if not exact:
            t.append(ui("  (totals partial — full listing happens on restore)") + "\n", style=pal("dim"))
        w.update(t)

    # --- restore -------------------------------------------------------
    def action_restore(self) -> None:
        if self.sel.empty:
            self.notify("Nothing selected", severity="warning")
            return
        if self._restoring:
            self.notify("A restore is still being prepared or sent — wait for it to finish",
                        severity="warning")
            return
        self._restoring = True
        self.notify("Listing selection…")
        # Snapshot on the UI thread: the worker must not read state the UI thread keeps changing.
        sel = TreeSelection(self.sel.base, dict(self.sel.rules))
        listed = set(self.listed)
        cached = {r: list(self._cached_under(r)) for r in sel.roots if self._is_listed(r)}
        self.prepare_restore(sel, listed, cached, self.bucket)

    def _restore_failed(self, msg: str) -> None:
        self._restoring = False
        say(self, msg, severity="error", timeout=15)

    @work(thread=True, group="restore")
    def prepare_restore(self, sel: TreeSelection, listed: set[str], cached: dict, bucket: str) -> None:
        try:
            objs = ops.gather(self.s3, bucket, sel, lambda r: cached.get(r, []), listed)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._restore_failed, f"Listing failed: {err_text(e)}")
            return
        self.app.call_from_thread(self._confirm_restore, objs, sel, bucket)

    def _confirm_restore(self, objs: list[ObjInfo], sel: TreeSelection, bucket: str) -> None:
        if not objs:
            self._restoring = False
            self.notify("Selection contains no objects", severity="warning")
            return
        cfg: Config = self.app.cfg  # type: ignore[attr-defined]
        base = common_folder(sel.roots)
        est = ops.estimate(objs, cfg.tier, cfg.days, cfg.pricing)
        lines = est.lines()
        if est.objects > cfg.warn_objects:
            lines.append(f"WARNING: {est.objects:,} objects — S3 Batch Operations may be better.")
        lines.append(f"(cost is for the {cfg.tier} tier and {cfg.days} days, using prices in thaw.toml)")
        leaf = base.rstrip("/").split("/")[-1] or bucket
        name = f"{leaf}_{datetime.now():%Y%m%d-%H%M}"

        def done(res: dict | None) -> None:
            if res:
                self.run_restore(objs, sel, bucket, base, res)
            else:
                self._restoring = False
        self.app.push_screen(ConfirmRestore(lines, base, cfg.tier, cfg.days, name), done)

    @work(thread=True, group="restore")
    def run_restore(self, objs: list[ObjInfo], sel: TreeSelection, bucket: str, base: str, res: dict) -> None:
        app = self.app
        cfg: Config = app.cfg  # type: ignore[attr-defined]
        try:
            job = ops.create_job(cfg, res["name"], bucket, base, sel, objs, res["tier"],
                                 res["days"], app.profile, app.region)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001 — e.g. a job with that name already exists
            app.call_from_thread(self._restore_failed, f"Job not created: {err_text(e)}")
            return
        name = job.name

        def prog(done: int, total: int) -> None:
            step = max(50, total // 20)  # at most ~20 progress toasts
            if total and (done == total or done % step == 0):
                app.call_from_thread(self.notify, f"Restore requests {done:,}/{total:,}", timeout=2)
        try:
            r = ops.request_restores(self.s3, job, app.n_workers, prog)  # type: ignore[attr-defined]
            msg = (f"Job {name}: restore requested for {r['requested']:,} objects "
                   f"({r['already']:,} already in progress, {r['failed']:,} failed)")
            app.call_from_thread(say, self, msg, severity="error" if r["failed"] else "information",
                                 timeout=10)
        except Exception as e:  # noqa: BLE001
            app.call_from_thread(say, self, f"Job {name}: {err_text(e)}", severity="error", timeout=15)
        finally:
            job.close()

        def show_jobs() -> None:
            self._restoring = False
            if isinstance(app.screen, JobsScreen):
                app.screen.select = name
                app.screen.reload()
            elif app.screen is self:
                app.push_screen(JobsScreen(select=name))
        app.call_from_thread(show_jobs)

    def action_jobs(self) -> None:
        self.app.push_screen(JobsScreen())


# --- jobs --------------------------------------------------------------------
class JobsScreen(Screen):
    BINDINGS = [
        Binding("u", "refresh", "Update status (S3)"),
        Binding("c", "copy", "Copy to bucket"),
        Binding("p", "resend", "Re-send pending restores"),
        Binding("e", "errors", "Show errors"),
        Binding("escape,b", "back", "Back"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, select: str | None = None):
        super().__init__()
        self.select = select
        self.busy: str | None = None  # one S3 operation at a time (thread workers can't be cancelled)

    @property
    def cfg(self) -> Config:
        return self.app.cfg  # type: ignore[attr-defined]

    def compose(self) -> ComposeResult:
        yield Banner(id="banner")
        yield DataTable(id="jobs", cursor_type="row", zebra_stripes=True)
        with Vertical(id="detail"):
            yield Static("", id="info")
            yield Label("Restored / archived")
            yield ProgressBar(id="pb_restore", show_eta=False)
            yield Label("Copied / total")
            yield ProgressBar(id="pb_copy", show_eta=False)
            yield Static("", id="log")
        yield Footer()

    def restyle(self) -> None:
        self.query_one(DataTable).border_title = ui(" Jobs ")
        self.query_one("#detail").border_title = ui(" Detail ")

    def on_mount(self) -> None:
        self.restyle()
        t = self.query_one(DataTable)
        t.add_columns("Job", "Created", "Objects", "Size", "Restored", "Restoring", "Pending",
                      "Failed", "Conflicts", "Copied", "Destination")
        self.reload()
        t.focus()

    def reload(self) -> None:
        t = self.query_one(DataTable)
        cur = self.current_name()
        t.clear()
        for job in Job.list_all(self.cfg.jobs_dir):
            c, m = job.counts(), job.meta
            r, cp = c["restore"], c["copy"]
            t.add_row(plain(job.name), m.get("created", "")[:16].replace("T", " "), f"{c['objects']:,}",
                      human_size(c["bytes"]), f"{r[RESTORED]:,}/{c['archived']:,}", f"{r[IN_PROGRESS]:,}",
                      f"{r[PENDING]:,}", f"{r[FAILED] + cp['failed']:,}", f"{cp[C_CONFLICT]:,}",
                      f"{cp['copied'] + cp['skipped']:,}", plain(m.get("destination", "")),
                      key=job.name)
            job.close()
        want = self.select or cur
        self.select = None
        if want:
            try:
                t.move_cursor(row=t.get_row_index(want))
            except Exception:  # noqa: BLE001
                pass
        self.show_detail()

    def current_name(self) -> str | None:
        t = self.query_one(DataTable)
        if not t.row_count:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None

    def current_job(self) -> Job | None:
        name = self.current_name()
        if not name:
            return None
        try:
            return Job.open(self.cfg.jobs_dir, name)
        except Exception as e:  # noqa: BLE001 — deleted or replaced while the screen was open
            say(self, f"Can't open job {name}: {e}", severity="error")
            return None

    @on(DataTable.RowHighlighted)
    def show_detail(self, *_):
        job = self.current_job()
        info = self.query_one("#info", Static)
        if not job:
            info.update("No jobs yet — tick folders in the browser and press r.")
            return
        c, m = job.counts(), job.meta
        r, cp = c["restore"], c["copy"]
        t = Text()
        t.append(safe(job.name), style="bold")
        t.append(f"   s3://{safe(m['bucket'])}/{safe(m['base_prefix'])}\n")
        t.append(f"tier {m['tier']} · {m['days']} days · {c['objects']:,} objects ({human_size(c['bytes'])})\n")
        t.append(f"restore: ✓ {r[RESTORED]:,}  ⏳ {r[IN_PROGRESS]:,}  pending {r[PENDING]:,}  "
                 f"failed {r[FAILED]:,}  not needed {r[NOT_NEEDED]:,}\n")
        t.append(f"copy: copied {cp['copied']:,}  already there {cp['skipped']:,}  "
                 f"conflicts {cp[C_CONFLICT]:,}  failed {cp['failed']:,}\n")
        t.append(f"requested {m.get('restore_requested', '-')} · status checked {m.get('status_checked', 'never')}")
        exp = job.earliest_expiry()
        if exp:
            t.append(f"\ncopy before {describe_when(exp)}", style="bold")
        if m.get("destination"):
            t.append(f"\ndestination {safe(m['destination'])} ({safe(m.get('dest_storage_class', ''))})")
        info.update(t)
        self.query_one("#pb_restore", ProgressBar).update(total=max(c["archived"], 1),
                                                          progress=r[RESTORED] if c["archived"] else 1)
        done = cp["copied"] + cp["skipped"]
        self.query_one("#pb_copy", ProgressBar).update(total=max(c["objects"], 1), progress=done)
        job.close()

    def _log(self, msg: object) -> None:
        self.query_one("#log", Static).update(plain(msg))

    def action_back(self) -> None:
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()
        else:
            self.app.switch_screen(BrowseScreen(None))

    def action_errors(self) -> None:
        job = self.current_job()
        if not job:
            return
        lines = [f"! {safe(row['key'])}: {safe(err)}" for row in job.problems()
                 for err in (row["error"], row["copy_error"]) if err]
        job.close()
        self._log("\n".join(lines[:30]) or "No errors.")

    # --- actions running against S3, one at a time ----------------------
    def _claim(self, what: str) -> bool:
        if self.busy:
            self.notify(f"Still busy {self.busy} — try again when it finishes", severity="warning")
            return False
        self.busy = what
        return True

    def _release(self, msg: str = "") -> None:
        self.busy = None
        if msg:
            self._log(msg)
        self.reload()

    def _job_task(self, name: str, failed: str, fn: Callable[[Job], str]) -> None:
        """Worker body: open the job, run fn, log what it returns; always frees the screen again."""
        msg = ""
        try:
            job = Job.open(self.cfg.jobs_dir, name)
            try:
                msg = fn(job)
            finally:
                job.close()
        except Exception as e:  # noqa: BLE001
            msg = f"{failed}: {safe(err_text(e))}"
        finally:
            self.app.call_from_thread(self._release, msg)

    def _progress(self, label: str):
        def cb(done: int, total: int) -> None:
            if total and (done == total or done % 25 == 0):
                self.app.call_from_thread(self._log, f"{label} {done:,}/{total:,}")
        return cb

    def action_refresh(self) -> None:
        name = self.current_name()
        if name and self._claim("updating status"):
            self.refresh_job(name)

    @work(thread=True, group="s3")
    def refresh_job(self, name: str) -> None:
        def run(job: Job) -> str:
            c = ops.refresh_status(self.app.s3, job, self.app.n_workers,  # type: ignore[attr-defined]
                                   self._progress("Checking"))
            msg = f"Status updated {datetime.now():%H:%M:%S}"
            if c["check_errors"]:
                msg += f" — {c['check_errors']:,} of {c['checked']:,} checks failed (press e)"
            return msg
        self._job_task(name, "Status update failed", run)

    def action_resend(self) -> None:
        name = self.current_name()
        if name and self._claim("sending restore requests"):
            self.resend(name)

    @work(thread=True, group="s3")
    def resend(self, name: str) -> None:
        def run(job: Job) -> str:
            r = ops.request_restores(self.app.s3, job, self.app.n_workers,  # type: ignore[attr-defined]
                                     self._progress("Requests"))
            return (f"Restore requests: sent {r['requested']:,}, already in progress {r['already']:,}, "
                    f"failed {r['failed']:,}")
        self._job_task(name, "Restore requests stopped", run)

    def action_copy(self) -> None:
        if self.busy:
            self.notify(f"Still busy {self.busy} — try again when it finishes", severity="warning")
            return
        job = self.current_job()
        if not job:
            return
        c = job.counts()
        readable = (f"{c['readable']:,} of {c['objects']:,} objects are readable now "
                    f"(as of last status update). Unreadable ones are left for a later copy.")
        name = job.name
        job.close()

        def done(res: dict | None) -> None:
            if res and self._claim("copying"):
                self.check_then_copy(name, res)
        self.app.push_screen(CopyDialog(name, self.cfg.recent_destinations(), self.cfg.storage_class,
                                        readable), done)

    @work(thread=True, group="s3")
    def check_then_copy(self, name: str, res: dict) -> None:
        app = self.app
        question = None
        try:
            job = Job.open(self.cfg.jobs_dir, name)
            src = job.meta["bucket"]
            job.close()
            try:
                a, b = ops.check_regions(app.s3, src, parse_uri(res["dest"])[0])  # type: ignore[attr-defined]
                if a != b:
                    question = (f"The destination bucket is in {safe(b)}; the source is in {safe(a)}.\n"
                                "Cross-region copies are charged for data transfer. Continue?")
            except Exception as e:  # noqa: BLE001
                if ops.is_auth_error(e):
                    raise
                question = f"Couldn't check the destination bucket's region ({safe(ops._err(e))}).\nCopy anyway?"
        except Exception as e:  # noqa: BLE001
            app.call_from_thread(self._release, f"Copy not started: {safe(err_text(e))}")
            return
        if question is None:
            app.call_from_thread(self.run_copy, name, res)
            return

        def answer(ok: bool) -> None:
            if ok:
                self.run_copy(name, res)
            else:
                self._release("Copy cancelled.")
        app.call_from_thread(lambda: app.push_screen(YesNo(question), answer))

    @work(thread=True, group="copy")
    def run_copy(self, name: str, res: dict) -> None:
        def run(job: Job) -> str:
            ops.refresh_status(self.app.s3, job, self.app.n_workers,  # type: ignore[attr-defined]
                               self._progress("Checking"))
            r = ops.copy_objects(self.app.s3, job, res["dest"], res["storage_class"], res["overwrite"],
                                 self.app.n_workers, self._progress("Copying"))  # type: ignore[attr-defined]
            self.cfg.remember_destination(res["dest"])
            msg = f"Copy finished: {ops.fmt_copy_result(r)}"
            if r[C_CONFLICT]:
                msg += ("\nConflicts were left alone — press e to see them; copy again with "
                        "'Replace' ticked to overwrite them.")
            if r["expired"]:
                msg += "\nSome restored copies had expired — press p to request them again."
            return msg
        self._job_task(name, "Copy stopped", run)


# --- app ---------------------------------------------------------------------
class ThawApp(App):
    TITLE = "thaw"
    CSS = """
    #banner { dock: top; height: 1; background: $primary; color: $background; text-style: bold; }
    #tree { width: 2fr; border: round $primary; }
    #side { width: 1fr; min-width: 34; border: round $primary; padding: 0 1; }
    #help { color: $text-muted; margin-top: 1; }
    #jobs { height: 1fr; border: round $primary; }
    #detail { height: auto; max-height: 50%; border: round $primary; padding: 0 1; }
    #log { color: $text-muted; margin-top: 1; }
    ModalScreen { align: center middle; }
    #dialog { width: 96; max-width: 95%; height: auto; max-height: 95%; border: thick $primary;
              background: $surface; padding: 0 2; overflow-y: auto; }
    #dialog RadioSet { border: none; padding: 0; background: $surface; }
    #dialog Label { margin-top: 1; color: $accent; }
    #dialog .title { text-style: bold; margin-bottom: 1; }
    #estimate { margin: 0; color: $text; }
    .buttons { height: auto; margin-top: 1; }
    .buttons Button { margin-right: 2; }
    """ + "".join(
        f".-b-{b} #tree, .-b-{b} #side, .-b-{b} #jobs, .-b-{b} #detail, .-b-{b} #dialog "
        f"{{ border: {b} $primary; }}\n"
        for b in sorted({r.border for r in themes.RETRO.values()}))

    BINDINGS = [Binding("ctrl+t", "next_theme", "Theme", show=False)]

    def __init__(self, cfg: Config, uri: str | None = None, profile: str | None = None,
                 region: str | None = None, workers: int = 16, s3=None, theme: str | None = None):
        super().__init__()
        for r in themes.RETRO.values():
            self.register_theme(r.theme)
        wanted = theme or cfg.ui_theme
        if wanted in themes.RETRO:
            self.theme = wanted
        self.cfg = cfg
        self.uri = uri
        self.profile = profile if profile is not None else cfg.profile
        self.region = region or cfg.region
        self.n_workers = workers
        self.s3 = s3 or client(self.profile, self.region, workers)

    def on_mount(self) -> None:
        self.push_screen(BrowseScreen(self.uri))

    def watch_theme(self, name: str) -> None:
        global ACTIVE
        ACTIVE = themes.get(name)
        for r in themes.RETRO.values():
            self.set_class(r.border == ACTIVE.border and ACTIVE is not themes.PLAIN, f"-b-{r.border}")
        for screen in self.screen_stack:
            if not screen.is_mounted:
                continue
            if hasattr(screen, "restyle"):
                screen.restyle()
            for b in screen.query(Banner):
                b.refresh()

    def action_next_theme(self) -> None:
        names = list(themes.RETRO) + ["textual-dark"]
        i = names.index(self.theme) + 1 if self.theme in names else 0
        self.theme = names[i % len(names)]
        self.notify(f"Theme: {themes.get(self.theme).title}", timeout=2)
