"""What is selected under a base prefix.

Two front-ends produce the same thing — a predicate over keys plus the
prefixes that need listing:

* ``TreeSelection``: tri-state tick tree used by the TUI. Rules are
  ``prefix -> True/False``; the longest matching rule decides a key.
* ``GlobSelection``: --include/--exclude globs used by the CLI, matched
  against the first path component under the base (or the whole relative
  key with ``deep=True``).
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

CHECKED, UNCHECKED, PARTIAL = "checked", "unchecked", "partial"


def is_folder(path: str) -> bool:
    return path == "" or path.endswith("/")


def covers(rule: str, path: str) -> bool:
    """A folder rule covers everything beneath it; a file rule only itself."""
    return rule == path or (is_folder(rule) and path.startswith(rule))


@dataclass
class TreeSelection:
    base: str = ""
    rules: dict[str, bool] = field(default_factory=dict)

    def _rule_for(self, path: str) -> bool:
        best, val = -1, False
        for p, v in self.rules.items():
            if covers(p, path) and len(p) > best:
                best, val = len(p), v
        return val

    def is_selected(self, key: str) -> bool:
        return self._rule_for(key)

    def state(self, path: str) -> str:
        """Tick state for a folder ('a/b/') or file key."""
        eff = self._rule_for(path)
        if is_folder(path):
            for p, v in self.rules.items():
                if p != path and covers(path, p) and v != eff:
                    return PARTIAL
        return CHECKED if eff else UNCHECKED

    def toggle(self, path: str) -> None:
        new = self.state(path) != CHECKED
        for p in [p for p in self.rules if covers(path, p)]:
            del self.rules[p]
        self.rules[path] = new
        self._simplify(path)

    def _simplify(self, path: str) -> None:
        # Drop the rule if the inherited value already gives the same answer.
        val = self.rules.pop(path)
        if self._rule_for(path) != val:
            self.rules[path] = val

    @property
    def roots(self) -> list[str]:
        """Minimal set of included prefixes to list."""
        inc = sorted(p for p, v in self.rules.items() if v)
        out: list[str] = []
        for p in inc:
            if not any(covers(r, p) for r in out):
                out.append(p)
        return out

    def describe(self) -> dict:
        return {"mode": "tree", "base": self.base, "rules": self.rules}

    @property
    def empty(self) -> bool:
        return not any(self.rules.values())


@dataclass
class GlobSelection:
    base: str = ""
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    deep: bool = False

    def _subject(self, key: str) -> str:
        rel = key[len(self.base):] if key.startswith(self.base) else key
        if self.deep:
            return rel
        return rel.split("/", 1)[0]

    def is_selected(self, key: str) -> bool:
        if not key.startswith(self.base):
            return False
        s = self._subject(key)
        if self.include and not any(fnmatch.fnmatchcase(s, g) for g in self.include):
            return False
        return not any(fnmatch.fnmatchcase(s, g) for g in self.exclude)

    @property
    def roots(self) -> list[str]:
        return [self.base]

    def describe(self) -> dict:
        return {"mode": "glob", "base": self.base, "include": self.include,
                "exclude": self.exclude, "deep": self.deep}
