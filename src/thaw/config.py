"""Configuration (thaw.toml) and small persistent UI state (recent destinations)."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULTS: dict = {
    "aws": {"profile": "", "region": "ap-southeast-2"},
    "restore": {"tier": "Bulk", "days": 7, "workers": 16, "warn_objects": 50000},
    "copy": {"storage_class": "STANDARD"},
    "watch": {"interval_minutes": 60},
    "ui": {"theme": "eightbit"},
    "pricing": {
        "DEEP_ARCHIVE": {
            "Bulk": {"per_gb": 0.0025, "per_1000_requests": 0.025},
            "Standard": {"per_gb": 0.02, "per_1000_requests": 0.10},
        },
        "GLACIER": {
            "Bulk": {"per_gb": 0.0, "per_1000_requests": 0.0},
            "Standard": {"per_gb": 0.01, "per_1000_requests": 0.05},
        },
        "restored_copy": {"per_gb_month": 0.025},
    },
}


def home(package_file: str | Path | None = None) -> Path:
    """Where thaw.toml, jobs/ and the state file live.

    $THAW_HOME if set; else the source checkout when running from one (``uv run`` or an
    editable install); else ~/.local/share/thaw. A regular (non-editable) install must not
    use the package location, or jobs would end up inside site-packages and vanish on reinstall.
    """
    env = os.environ.get("THAW_HOME")
    if env:
        return Path(env).expanduser()
    repo = Path(package_file or __file__).resolve().parents[2]
    if (repo / "pyproject.toml").is_file() and (repo / "src" / "thaw").is_dir():
        return repo
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "thaw"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    data: dict = field(default_factory=lambda: dict(DEFAULTS))
    root: Path = field(default_factory=home)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        root = home()
        path = path or Path(os.environ.get("THAW_CONFIG", root / "thaw.toml"))
        data = DEFAULTS
        if path.exists():
            with open(path, "rb") as f:
                data = _merge(DEFAULTS, tomllib.load(f))
        return cls(data=data, root=root)

    @property
    def jobs_dir(self) -> Path:
        d = self.root / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def profile(self) -> str | None:
        return self.data["aws"]["profile"] or None

    @property
    def region(self) -> str:
        return self.data["aws"]["region"]

    @property
    def tier(self) -> str:
        return self.data["restore"]["tier"]

    @property
    def days(self) -> int:
        return int(self.data["restore"]["days"])

    @property
    def workers(self) -> int:
        return int(self.data["restore"]["workers"])

    @property
    def warn_objects(self) -> int:
        return int(self.data["restore"]["warn_objects"])

    @property
    def storage_class(self) -> str:
        return self.data["copy"]["storage_class"]

    @property
    def watch_interval(self) -> int:
        return int(self.data["watch"]["interval_minutes"])

    @property
    def ui_theme(self) -> str:
        return self.data["ui"]["theme"]

    @property
    def pricing(self) -> dict:
        return self.data["pricing"]

    # --- recent destinations (gitignored state file) -----------------------
    @property
    def _state_path(self) -> Path:
        return self.root / ".thaw_state.json"

    def recent_destinations(self) -> list[str]:
        try:
            return json.loads(self._state_path.read_text()).get("recent_destinations", [])
        except (OSError, ValueError):
            return []

    def remember_destination(self, dest: str, keep: int = 10) -> None:
        recents = [d for d in self.recent_destinations() if d != dest]
        recents.insert(0, dest)
        try:
            state = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            state = {}
        state["recent_destinations"] = recents[:keep]
        self.root.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, indent=2))
