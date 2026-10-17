"""Chrome, through the companion extension (chrome-extension/) and its native host.

The extension reports every window of its profile with its tabs, and opens windows with a
given set of tabs when asked. This module is the daemon's side of that:
- which compositor window is which Chrome window (`Pairer`), only when that is certain;
- which Chrome profile an extension instance lives in (`find_profile`), so Chrome can be
  started in that profile at login;
- registering the native host with the browsers (`install_host`).

A Chromium-family browser only; Firefox has no equivalent of --app windows or of this.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

HOST_NAME = "io.github.kulaxa.sidelit"
HOST_LAUNCHER = "sidelit-chrome-host"
EXTENSION_ID = "kgppgcbanlkliadhfmcfngihjjpfjafo"  # fixed by the key in chrome-extension/manifest.json

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
# user data dir (under ~/.config) -> executable
BROWSERS = {
    "google-chrome": "google-chrome",
    "google-chrome-beta": "google-chrome-beta",
    "chromium": "chromium",
    "BraveSoftware/Brave-Browser": "brave-browser",
    "microsoft-edge": "microsoft-edge",
}
TITLE_SUFFIXES = (" - Google Chrome", " - Chromium", " - Brave", " - Microsoft Edge")


def bare_title(title: str) -> str:
    for suffix in TITLE_SUFFIXES:
        if title.endswith(suffix):
            return title[: -len(suffix)]
    return title


def active_tab(win: dict) -> dict | None:
    return next((t for t in win.get("tabs") or [] if t.get("active")), None)


FOCUS_STABLE_S = 2.5   # a focus Chrome confirms in a report this long after it: the same window
MISMATCH_S = 5.0       # a pair whose titles have disagreed this long was wrong (or the window changed)
OPENED_S = 10.0        # a window the extension opened pairs with a new compositor window this soon


class Pairer:
    """Which compositor window is which Chrome window, only when that is certain.

    Chrome titles a window "<active tab title> - Google Chrome", and several windows can show
    tabs with the same title. A wrong guess records one window's tabs under another, so a
    window is paired only by something that cannot be wrong:
    - its title is unique among the unpaired windows on both sides;
    - the extension just opened it (the `opened` reply names the Chrome window, and the
      compositor window appearing then is it);
    - it has had the keyboard long enough for Chrome to report the same window focused.
    Pairs are kept until either window goes, or their titles disagree for MISMATCH_S."""

    def __init__(self):
        self.pairs: dict[str, tuple] = {}          # window id -> (instance, extension window id)
        self.opened: list[tuple] = []              # (instance, extension window id, when)
        self._mismatch: dict[str, float] = {}
        self._focus: tuple | None = None           # (window id, since)
        self._first_seen: dict[str, float] = {}

    def note_opened(self, instance: str, window: int, now: float):
        self.opened.append((instance, window, now))

    def wants_report(self, now: float) -> bool:
        """A Chrome window has had focus long enough to be confirmed, but is not paired yet:
        ask the extension for a fresh report instead of trusting an old one."""
        return bool(self._focus and self._focus[0] not in self.pairs and now - self._focus[1] >= FOCUS_STABLE_S)

    def update(self, tops: list, reported: list[tuple[str, dict]], now: float, report_t: dict | None = None) -> dict:
        report_t = report_t or {}
        normal = {(inst, w["id"]): w for inst, w in reported if w.get("type") == "normal"}
        by_id = {t.id: t for t in tops}
        for t in tops:
            self._first_seen.setdefault(t.id, now)
        for wid in [w for w in self._first_seen if w not in by_id]:
            self._first_seen.pop(wid, None)
        self.opened = [o for o in self.opened if now - o[2] < OPENED_S and (o[0], o[1]) in normal or now - o[2] < 2]

        def title_of(key):
            tab = active_tab(normal[key])
            return (tab.get("title") or tab.get("url") or "") if tab else ""

        # 1. keep what is still there and still agrees
        for wid, key in list(self.pairs.items()):
            if wid not in by_id or key not in normal:
                self._drop(wid)
                continue
            if bare_title(by_id[wid].title) != title_of(key):
                since = self._mismatch.setdefault(wid, now)
                if now - since >= MISMATCH_S:
                    self._drop(wid)
            else:
                self._mismatch.pop(wid, None)

        def free_tops():
            taken = set(self.pairs)
            return [t for t in tops if t.id not in taken]

        def free_keys():
            taken = set(self.pairs.values())
            return [k for k in normal if k not in taken]

        # 2. a window the extension opened: the compositor window that appeared with it
        for inst, ext, t_open in list(self.opened):
            key = (inst, ext)
            if key not in normal or key in self.pairs.values():
                continue
            new = [t for t in free_tops() if self._first_seen.get(t.id, 0) >= t_open - 1.5]
            same = [t for t in new if bare_title(t.title) == title_of(key)]
            pick = same if len(same) == 1 else (new if len(new) == 1 else [])
            if pick:
                self._pair(pick[0].id, key)
                self.opened.remove((inst, ext, t_open))

        # 3. focus Chrome has confirmed: the focused window, held steady, and a report since
        focused = next((t for t in tops if getattr(t, "focused", False)), None)
        if focused is None:
            self._focus = None
        elif self._focus is None or self._focus[0] != focused.id:
            self._focus = (focused.id, now)
        if self._focus and now - self._focus[1] >= FOCUS_STABLE_S:
            wid, since = self._focus
            confirmed = [k for k, w in normal.items() if w.get("focused")
                         and report_t.get(k[0], 0) >= since + FOCUS_STABLE_S]
            if len(confirmed) == 1 and bare_title(by_id[wid].title) == title_of(confirmed[0]):
                if self.pairs.get(wid) != confirmed[0]:
                    for other, key in list(self.pairs.items()):
                        if key == confirmed[0]:
                            self._drop(other)
                    self._pair(wid, confirmed[0])

        # 4. a title only one window on each side has
        tops_by_title: dict[str, list] = {}
        for t in free_tops():
            tops_by_title.setdefault(bare_title(t.title), []).append(t)
        keys_by_title: dict[str, list] = {}
        for k in free_keys():
            keys_by_title.setdefault(title_of(k), []).append(k)
        for title, ts in tops_by_title.items():
            ks = keys_by_title.get(title, [])
            if title and len(ts) == 1 and len(ks) == 1:
                self._pair(ts[0].id, ks[0])

        return {wid: (key[0], normal[key]) for wid, key in self.pairs.items() if key in normal}

    def _pair(self, wid: str, key: tuple):
        self.pairs[wid] = key
        self._mismatch.pop(wid, None)

    def _drop(self, wid: str):
        self.pairs.pop(wid, None)
        self._mismatch.pop(wid, None)


def urls(tabs: list[dict]) -> set[str]:
    return {t.get("url") for t in tabs or [] if t.get("url") and not t["url"].startswith("chrome://newtab")}


def overlap(a: list[dict], b: list[dict]) -> float:
    """How much two tab lists are the same window's (0..1, Jaccard on URLs)."""
    ua, ub = urls(a), urls(b)
    if not ua or not ub:
        return 0.0
    return len(ua & ub) / len(ua | ub)


def clean_tabs(tabs: list[dict]) -> list[dict]:
    """What is worth reopening: no blank tabs, no internal pages that cannot be opened."""
    out = []
    for t in tabs or []:
        url = str(t.get("url") or "")
        if not url or url == "about:blank" or url.startswith(("chrome://newtab", "chrome://new-tab-page",
                                                                "devtools://", "chrome-error://")):
            continue
        out.append({"url": url[:4000], "title": str(t.get("title") or "")[:200],
                    "active": bool(t.get("active")), "pinned": bool(t.get("pinned"))})
    return out


# ---- profiles ------------------------------------------------------------------------------------
def find_profile(instance: str, root: Path = CONFIG) -> tuple[str, str] | None:
    """(user data dir, profile dir) whose copy of the extension stored `instance`.

    The extension cannot know its profile's directory, but chrome.storage.local writes into
    <user data>/<profile>/Local Extension Settings/<extension id>/, a LevelDB whose log keeps
    the value as plain bytes."""
    needle = instance.encode()
    for data_dir in BROWSERS:
        base = root / data_dir
        if not base.is_dir():
            continue
        for store in base.glob(f"*/Local Extension Settings/{EXTENSION_ID}"):
            for f in store.iterdir():
                if f.suffix not in (".log", ".ldb"):
                    continue
                try:
                    if needle in f.read_bytes():
                        return data_dir, store.parent.parent.name
                except OSError:
                    continue
    return None


def last_exit(data_dir: str, profile: str, root: Path = CONFIG) -> str | None:
    """How the browser last ended in this profile, from its own record: "normal" (it quit, or the
    session ended), "crashed" (or it is running: Chrome writes "Crashed" as it starts, and
    "Normal" as it quits), None when there is no record."""
    try:
        prefs = json.loads((root / data_dir / profile / "Preferences").read_text())
    except (OSError, ValueError):
        return None
    how = (prefs.get("profile") or {}).get("exit_type") if isinstance(prefs, dict) else None
    return {"Normal": "normal", "SessionEnded": "normal", "Crashed": "crashed"}.get(how)


def launch_argv(data_dir: str, profile: str) -> list[str]:
    return [BROWSERS.get(data_dir, "google-chrome"), f"--profile-directory={profile}"]


# ---- the native host registration -------------------------------------------------------------------
def host_command() -> str:
    """The installed native host launcher: on PATH, else the one in this checkout."""
    here = Path(__file__).resolve().parent.parent / "bin" / HOST_LAUNCHER
    return shutil.which(HOST_LAUNCHER) or str(here)


def extension_dir() -> str:
    """The extension's folder, to load unpacked: in this checkout, or where the package put it."""
    here = Path(__file__).resolve().parent.parent / "chrome-extension"
    return str(here) if here.is_dir() else "/usr/share/sidelit/chrome-extension"


def host_manifest(host_path: str) -> dict:
    return {"name": HOST_NAME, "description": "Sidelit: Chrome windows and tabs",
            "path": host_path, "type": "stdio", "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"]}


def install_host(host_path: str, root: Path = CONFIG) -> list[Path]:
    """Register the native host with every Chromium-family browser that has a profile here."""
    done = []
    for data_dir in BROWSERS:
        base = root / data_dir
        if not base.is_dir():
            continue
        target = base / "NativeMessagingHosts" / f"{HOST_NAME}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(host_manifest(host_path), indent=2) + "\n")
        done.append(target)
    return done


def uninstall_host(root: Path = CONFIG) -> list[Path]:
    done = []
    for data_dir in BROWSERS:
        target = root / data_dir / "NativeMessagingHosts" / f"{HOST_NAME}.json"
        if target.exists():
            target.unlink()
            done.append(target)
    return done


def restores_itself(root: Path = CONFIG) -> list[str]:
    """Profiles with "Continue where you left off" on: with the daemon reopening windows too,
    they would come back twice."""
    out = []
    for data_dir in BROWSERS:
        for prefs in (root / data_dir).glob("*/Preferences"):
            try:
                if json.loads(prefs.read_text()).get("session", {}).get("restore_on_startup") == 1:
                    out.append(f"{data_dir}/{prefs.parent.name}")
            except (OSError, ValueError):
                continue
    return out
