"""Retro themes: a Textual Theme plus the glyphs, label palette, borders and banner that go with it."""

from __future__ import annotations

from dataclasses import dataclass, field

from textual.theme import Theme


@dataclass
class Retro:
    theme: Theme
    title: str                       # shown in the theme picker
    banner: str                      # left part of the top bar
    border: str = "round"            # Textual border type for panels and dialogs
    ticks: tuple[str, str, str] = ("[x]", "[~]", "[ ]")   # checked, partial, unchecked
    glyphs: tuple[str, str, str] = ("❄", "⏳", "✓")        # archived, restoring, restored
    upper: bool = False              # SHOUTY labels, 8-bit style
    # label palette: tick_on, tick_partial, tick_off, stats, archived, restoring, restored, include, exclude, dim
    palette: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.theme.name


def _p(tick_on, tick_partial, tick_off, stats, archived, restoring, restored, include, exclude, dim):
    return dict(tick_on=tick_on, tick_partial=tick_partial, tick_off=tick_off, stats=stats,
                archived=archived, restoring=restoring, restored=restored, include=include,
                exclude=exclude, dim=dim)


RETRO: dict[str, Retro] = {}


def _add(r: Retro) -> None:
    RETRO[r.name] = r


# Amber phosphor CRT — monochrome, everything in shades of amber.
_add(Retro(
    Theme("amber", primary="#ffb000", secondary="#ff8c00", accent="#ffd27f", warning="#ffcc00",
          error="#ff6a00", success="#ffd27f", foreground="#ffb000", background="#120b00",
          surface="#1c1100", panel="#2a1a00", dark=True),
    "Amber phosphor", "▓▒░ THAW ░▒▓  GLACIER RECOVERY TERMINAL", border="ascii",
    ticks=("[■]", "[▪]", "[ ]"), glyphs=("#", "~", "+"),
    palette=_p("bold #ffd27f", "bold #ffb000", "#7a5500", "#ffb000", "#ff8c00", "bold #ffcc00",
               "bold #ffe6b3", "#ffd27f", "#ff6a00", "#7a5500"),
))

# Green phosphor VT terminal.
_add(Retro(
    Theme("phosphor", primary="#33ff66", secondary="#1fbf4a", accent="#b3ffc6", warning="#ccff33",
          error="#ff5533", success="#b3ffc6", foreground="#33ff66", background="#020d04",
          surface="#04170a", panel="#08260f", dark=True),
    "Green phosphor", ">_ THAW :: DEEP ARCHIVE RETRIEVAL", border="solid",
    ticks=("[X]", "[/]", "[ ]"), glyphs=("*", "~", "√"),
    palette=_p("bold #b3ffc6", "bold #ccff33", "#1a6630", "#33ff66", "#1fbf4a", "bold #ccff33",
               "bold #e6ffec", "#b3ffc6", "#ff5533", "#1a6630"),
))

# Blue-panel DOS file manager.
_add(Retro(
    Theme("dos", primary="#55ffff", secondary="#ffff55", accent="#ffff55", warning="#ffff55",
          error="#ff5555", success="#55ff55", foreground="#ffffff", background="#0000aa",
          surface="#0000aa", panel="#00aaaa", boost="#000080", dark=True),
    "DOS commander", "═╡ THAW ╞═  Glacier Commander  F1=Help", border="double",
    ticks=("[√]", "[±]", "[ ]"), glyphs=("■", "▒", "√"),
    palette=_p("bold #ffff55", "bold #ffff55", "#aaaaaa", "#55ffff", "#ffffff", "bold #ffff55",
               "bold #55ff55", "#55ff55", "#ff5555", "#aaaaaa"),
))

# Synthwave / outrun neon.
_add(Retro(
    Theme("synthwave", primary="#ff2bd6", secondary="#00f0ff", accent="#ffd319", warning="#ffd319",
          error="#ff3864", success="#00ffa3", foreground="#f8e9ff", background="#1a0b2e",
          surface="#241040", panel="#3a1764", dark=True),
    "Synthwave", "▀▄▀▄ T H A W ▄▀▄▀  // NEON GLACIER 1986 //", border="heavy",
    ticks=("[◆]", "[◈]", "[◇]"), glyphs=("❄", "⚡", "★"),
    palette=_p("bold #ff2bd6", "bold #ffd319", "#6b4c8f", "#00f0ff", "#9d7bff", "bold #ffd319",
               "bold #00ffa3", "#00ffa3", "#ff3864", "#6b4c8f"),
))

# 8-bit home computer: light blue on blue, upper case.
_add(Retro(
    Theme("eightbit", primary="#a59fff", secondary="#6c5eb5", accent="#edf171", warning="#edf171",
          error="#c46c71", success="#9ad284", foreground="#a59fff", background="#352879",
          surface="#3d318a", panel="#6c5eb5", dark=True),
    "8-bit home computer", "**** THAW 64 GLACIER BASIC V2 ****  READY.", border="tall",
    ticks=("[*]", "[+]", "[ ]"), glyphs=("❄", "…", "✓"), upper=True,
    palette=_p("bold #edf171", "bold #edf171", "#6c5eb5", "#a59fff", "#ffffff", "bold #edf171",
               "bold #9ad284", "#9ad284", "#c46c71", "#6c5eb5"),
))

PLAIN = Retro(Theme("plain", primary="#0178D4"), "Plain (Textual default)", "thaw", border="round",
              palette=_p("bold green", "bold yellow", "dim", "cyan", "blue", "yellow", "green",
                         "green", "red", "dim"))


def get(name: str) -> Retro:
    return RETRO.get(name, PLAIN)
