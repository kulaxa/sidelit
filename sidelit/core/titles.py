"""What window titles and app ids tell us, without asking the app.

Pure string work: which windows are terminals, which folder a file manager shows,
which directory a shell prompt is in, which URL a browser app window was opened for,
and how alike two titles are.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

TERMINAL_APP_IDS = {
    "terminator", "kitty", "Alacritty", "foot", "org.wezfurlong.wezterm", "com.mitchellh.ghostty",
    "cosmic-term", "com.system76.CosmicTerm",       # COSMIC's
    "org.gnome.Terminal", "org.gnome.Ptyxis", "org.gnome.Console",  # GNOME's (Ptyxis since Ubuntu 25.10, Fedora 41)
    "org.kde.konsole", "konsole", "org.kde.yakuake",  # KDE's
}
FILE_MANAGERS = {"com.system76.CosmicFiles", "org.gnome.Nautilus", "nautilus", "org.kde.dolphin",
                 "thunar", "nemo", "pcmanfm"}
BROWSERS = {"google-chrome", "chromium", "chromium-browser", "brave-browser", "microsoft-edge",
            "vivaldi", "vivaldi-stable", "firefox", "org.mozilla.firefox"}

# Claude Code puts a spinner glyph in the terminal title while working and '✳' when idle
BUSY_RANGE = (0x2800, 0x28FF)
BUSY_GLYPHS = "◐◑◒◓"
IDLE_GLYPH = "✳"
CLAUDE_TITLE_RE = re.compile(r"^[^\x00-\x7f]+\s+\S")

# "<folder> — COSMIC Files", "<folder> - Dolphin", Nautilus: just "<folder>"
FILES_TITLE_RE = re.compile(r"^(?P<name>.+?)(?:\s+[—–-]\s+(?:COSMIC Files|Dolphin|Thunar|Nemo|Files))?$")
# shell prompt titles: "user@host: ~/path" / "user@host:~/path"
PROMPT_TITLE_RE = re.compile(r"^[\w.-]+@[\w.-]+:\s*(?P<path>[~/].*)$")
# generic "document - App" suffixes stripped before comparing titles
APP_SUFFIX_RE = re.compile(r"\s+[—–-]\s+[^—–-]{2,40}$")
# A Chrome/Chromium `--app=URL` window reports app id "chrome-<host>__<path with / as _>-<profile>",
# e.g. chrome-discord.com__channels_123_456-Profile_1, so the URL and profile can be read back
CHROME_APP_RE = re.compile(r"^chrome-(?P<host>[^_/]+)__(?P<path>.*?)-(?P<profile>Default|Profile_\d+)$")


def title_state(text: str) -> str:
    """'busy' / 'done' from the glyph Claude puts in front of its title, else 'other'."""
    text = (text or "").lstrip()
    if not text:
        return "other"
    first = text[0]
    if BUSY_RANGE[0] <= ord(first) <= BUSY_RANGE[1] or first in BUSY_GLYPHS:
        return "busy"
    if first == IDLE_GLYPH:
        return "done"
    return "other"


def looks_like_claude(title: str) -> bool:
    return bool(CLAUDE_TITLE_RE.match(title or ""))


def bare_title(title: str | None) -> str | None:
    """'◐ Fix tests' -> 'Fix tests'"""
    if not title:
        return None
    return title.split(" ", 1)[1].strip() if title_state(title) != "other" and " " in title else title.strip()


def chrome_app_url(app_id: str):
    """(url, profile directory) for a browser app window's app id, else None."""
    m = CHROME_APP_RE.match(app_id or "")
    if not m:
        return None
    path = m.group("path").replace("_", "/")
    return f"https://{m.group('host')}/{path}", m.group("profile").replace("_", " ")


def folder_from_title(app_id: str, title: str) -> str | None:
    """The folder name a file-manager window shows, if the title looks like one."""
    if app_id not in FILE_MANAGERS or not title:
        return None
    m = FILES_TITLE_RE.match(title.strip())
    name = m.group("name").strip() if m else None
    if not name or name.lower().startswith(("search for", "trash", "recent", "starred")):
        return None
    return name


def cwd_from_prompt_title(title: str) -> str | None:
    """`user@host: ~/x` -> /home/user/x when that directory exists."""
    m = PROMPT_TITLE_RE.match((title or "").strip())
    if not m:
        return None
    path = os.path.expanduser(m.group("path").split(" ")[0])
    return path if os.path.isdir(path) else None


def resolve_folder(name: str, known_dirs: list[str]) -> str | None:
    """Find a directory called `name` near the directories we know (Claude cwds, home):
    the dirs themselves, their ancestors up to home, and their direct children."""
    home = Path.home()
    if name in ("~", "Home", home.name):
        return str(home)
    candidates: list[Path] = []
    seen = set()
    for d in [str(home)] + list(known_dirs):
        p = Path(d)
        while True:
            if p not in seen:
                seen.add(p)
                candidates.append(p)
            if p == home or p.parent == p or home not in p.parents:
                break
            p = p.parent
    for base in list(candidates):
        try:
            for child in os.scandir(base):
                if child.is_dir(follow_symlinks=False) and child.name == name:
                    return child.path
        except OSError:
            continue
    for p in candidates:
        if p.name == name:
            return str(p)
    return None


def _norm(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def _doc(title: str) -> str:
    return _norm(APP_SUFFIX_RE.sub("", (title or "").strip()))


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[\w#]+", _doc(title)) if len(w) > 2}


def match_score(title: str, remembered: str) -> float:
    """3 exact, 2 same document (app suffix ignored), 0..1.5 word overlap, 0 nothing."""
    if not title or not remembered:
        return 0.0
    if _norm(title) == _norm(remembered):
        return 3.0
    if _doc(title) and _doc(title) == _doc(remembered):
        return 2.0
    a, b = _words(title), _words(remembered)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / max(len(a), len(b))
    return 1.5 * overlap if overlap >= 0.5 else 0.0
