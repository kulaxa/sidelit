"""Moving over from Claude Workspace, the name this project had before 0.5.0.

The first `sidelit start` on a machine that ran Claude Workspace takes over from it, once:
- what would start the old one again goes: the systemd user units, the autostart and desktop
  entries and the links in ~/.local/bin that its install.sh made. An old package's autostart
  entry is hidden (Sidelit's package replaces the old one: apt and pacman remove it). The old
  hook's link stays, pointed at Sidelit's hook, until the next login: the Claude Code sessions
  already running call it until they end. The old opener's link stays too, pointed at
  sidelit-open, for the $BROWSER and URL handlers that still name it;
- the old daemon and sidebar are stopped;
- its folders move into Sidelit's, entry by entry and never over something Sidelit already
  has: config (the layout, config.json, other sources, the usage cache), state (the journal)
  and runtime (the hook's session records, the turn checks);
- `claude-workspace open` in config.json and in the other sources' files becomes `sidelit open`;
- the old Claude Code hook's entries in ~/.claude/settings.json are swapped for Sidelit's (the
  file as it was before Sidelit first changed it is kept as settings.json.before-sidelit);
- the old Chrome native host is replaced by Sidelit's.
Then it says what is left for you: files that still name the old command (a shell rc's
$BROWSER, Terminator's URL handler, the default browser), the old checkout, the extension.

A marker in Sidelit's config folder makes it happen once. With SIDELIT_DRY_RUN=1 it only says
what it would do. With SIDELIT_HOME set (a test instance) `sidelit start` leaves it alone, and
`sidelit moveover` copies the old folders into SIDELIT_HOME and touches nothing else: the old
install keeps running as it was.

The old name is in this file and nowhere else in the code.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from sidelit import chrome, config, hooks, session
from sidelit.core import journal as journal_mod

OLD = "claude-workspace"
OLD_MODULE = "claude_workspace"              # its processes: python3 -m claude_workspace daemon|sidebar
OLD_UNITS = (f"{OLD}-daemon.service", f"{OLD}-sidebar.service")
OLD_BINS = (OLD, f"{OLD}-open", f"{OLD}-hook", f"{OLD}-chrome-host")
OLD_HOOK = f"{OLD}-hook"
OLD_OPENER = f"{OLD}-open"
OLD_CHROME_HOST = "io.github.kulaxa.claude_workspace"
# in the old runtime folder: the old processes' own, not anything they kept
RUNTIME_SKIP = frozenset({"daemon.sock", "daemon.pid", "sidebar.pid", "session.env"})
# the old command, as config.json and a source's "terminal" may name it -> ours
COMMANDS = ((f"{OLD}-open", "sidelit-open"), (f"{OLD} open", "sidelit open"))
# files where you may have named the old command yourself
SHELL_FILES = (".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile", ".zshenv")
MARKER = "moved-over.json"
PARTIAL = "moved-over.partial.json"  # a try that failed: what it did and left for you, for the next one
STOP_WAIT_S = 5.0


@dataclass
class Places:
    """Where the old install lived and where Sidelit lives (a test brings its own)."""
    home: Path
    config_home: Path         # $XDG_CONFIG_HOME
    state_home: Path
    data_home: Path
    cache_home: Path
    runtime: Path             # $XDG_RUNTIME_DIR
    new_config: Path
    new_state: Path
    new_runtime: Path
    system_autostart: Path = Path("/etc/xdg/autostart")
    system_bin: Path = Path("/usr/bin")
    proc: Path = Path("/proc")

    @classmethod
    def here(cls) -> "Places":
        home, env = Path.home(), os.environ.get
        return cls(home=home,
                   config_home=Path(env("XDG_CONFIG_HOME") or home / ".config"),
                   state_home=Path(env("XDG_STATE_HOME") or home / ".local/state"),
                   data_home=Path(env("XDG_DATA_HOME") or home / ".local/share"),
                   cache_home=Path(env("XDG_CACHE_HOME") or home / ".cache"),
                   runtime=Path(env("XDG_RUNTIME_DIR") or "/tmp"),
                   new_config=config.CONFIG_DIR, new_state=config.STATE_DIR, new_runtime=config.RUNTIME_DIR)

    @property
    def folders(self) -> list[tuple[Path, Path, frozenset]]:
        """(the old folder, Sidelit's, what stays behind)"""
        return [(self.config_home / OLD, self.new_config, frozenset()),
                (self.state_home / OLD, self.new_state, frozenset()),
                (self.runtime / OLD, self.new_runtime, RUNTIME_SKIP)]

    @property
    def logs(self) -> list[tuple[Path, Path]]:
        """The launcher's log, where it ran without systemd."""
        return [(self.cache_home / f"{OLD}.log{n}", self.cache_home / f"sidelit.log{n}") for n in ("", ".1")]

    @property
    def launchers(self) -> list[Path]:
        """What the old install.sh put in your home to start it."""
        return [*(self.config_home / "systemd" / "user" / u for u in OLD_UNITS),
                self.config_home / "autostart" / f"{OLD}.desktop",
                self.data_home / "applications" / f"{OLD}.desktop"]

    @property
    def links(self) -> list[Path]:
        return [self.home / ".local" / "bin" / name for name in OLD_BINS]

    @property
    def chrome_hosts(self) -> list[Path]:
        return [self.config_home / d / "NativeMessagingHosts" / f"{OLD_CHROME_HOST}.json" for d in chrome.BROWSERS]

    @property
    def package_autostart(self) -> Path:
        return self.system_autostart / f"{OLD}.desktop"

    def show(self, path: Path) -> str:
        s = str(path)
        return "~" + s[len(str(self.home)):] if s.startswith(str(self.home) + "/") else s


def old_processes(proc: Path = Path("/proc")) -> list[int]:
    """Your processes running the old package: its daemon and its sidebar."""
    uid, found = os.getuid(), []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return []
    for p in entries:
        if not p.name.isdigit():
            continue
        try:
            if p.stat().st_uid != uid:
                continue
            args = [a.decode(errors="replace") for a in (p / "cmdline").read_bytes().split(b"\0") if a]
        except OSError:
            continue
        if any(a == "-m" and (b == OLD_MODULE or b.startswith(OLD_MODULE + ".")) for a, b in zip(args, args[1:])):
            found.append(int(p.name))
    return sorted(found)


def _systemctl(*args: str) -> str:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _terminate(pids: list[int], kill=os.kill, wait_s: float = STOP_WAIT_S):
    """SIGTERM (the daemon saves its layout on it), then SIGKILL for whatever is still there."""
    def alive(pid):
        try:
            kill(pid, 0)
            return True
        except OSError:
            return False
    for pid in pids:
        try:
            kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + wait_s
    while any(alive(p) for p in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    for pid in pids:
        if alive(pid):
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass


def opener_command() -> str:
    """Sidelit's link opener: on PATH, else the one in this checkout."""
    return shutil.which("sidelit-open") or str(Path(__file__).resolve().parent.parent / "bin" / "sidelit-open")


def _renamed(text: str) -> str:
    for old, new in COMMANDS:
        text = text.replace(old, new)
    return text


@dataclass
class MoveOver:
    places: Places
    dry: bool = False          # only say what it would do
    test: bool = False         # a test instance (SIDELIT_HOME): copy the old folders, touch nothing else
    systemctl: object = _systemctl
    kill: object = os.kill
    did: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)   # for you to do
    failed: list[str] = field(default_factory=list)
    units_removed: bool = False
    bridge: str = ""           # the old hook's link, pointed at ours until the next login

    # ---- is there anything to take over? ------------------------------------------------------
    def done_before(self) -> dict | None:
        try:
            return json.loads((self.places.new_config / MARKER).read_text())
        except (OSError, ValueError):
            return None

    def traces(self) -> list[str]:
        """What is left of the old install."""
        p = self.places
        found = [p.show(old) for old, _, _ in p.folders if old.is_dir()]
        if self.test:  # (it only copies these)
            return found
        found += [p.show(f) for f in [*p.launchers, *p.chrome_hosts, p.package_autostart] if f.exists()]
        found += [p.show(link) for link in p.links if link.is_symlink()]
        if hooks.present(OLD_HOOK):
            found.append(f"its hook in {p.show(hooks.SETTINGS)}")
        if old_processes(p.proc):
            found.append("its daemon or sidebar, running")
        return found

    # ---- taking over --------------------------------------------------------------------------
    def run(self) -> bool:
        """Take over; False when something failed (it is tried again at the next start, and
        what this try did and left for you is kept for that one: some of it happens once)."""
        steps = ((self._move_folders, self._rename_commands) if self.test else
                 (self._remove_launchers, self._stop, self._move_folders, self._rename_commands,
                  self._swap_hook, self._swap_chrome_host, self._sweep, self._look_for_leftovers))
        for step in steps:
            try:
                step()
            except Exception as e:  # (a file of yours in a shape it did not expect, say: never in the way of starting)
                self.failed.append(f"{step.__name__.strip('_').replace('_', ' ')}: {e}")
        if self.dry:
            return not self.failed
        partial = self.places.new_config / PARTIAL
        earlier = self._earlier(partial)
        self.left += [line for line in earlier.get("left") or [] if line not in self.left]
        did = [*(earlier.get("did") or []), *self.did]
        if self.failed:
            try:
                _write(partial, json.dumps({"did": did, "left": self.left}, indent=2) + "\n")
            except OSError:
                pass
        else:
            self._mark(did)
            partial.unlink(missing_ok=True)
        return not self.failed

    @staticmethod
    def _earlier(partial: Path) -> dict:
        try:
            data = json.loads(partial.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _do(self, what: str, fn=None):
        """Say it, and do it unless this is a dry run."""
        self.did.append(what)
        if fn is not None and not self.dry:
            fn()

    def _remove_launchers(self):
        p = self.places
        for f in p.launchers:
            if f.exists() or f.is_symlink():
                self._do(f"removed {p.show(f)}", f.unlink)
                self.units_removed |= f.suffix == ".service"
        for link in p.links:
            if link.is_symlink():
                if link.name == OLD:
                    self._old_checkout(link)
                if link.name == OLD_HOOK and self._bridge_hook(link):
                    continue
                if link.name == OLD_OPENER:
                    self._bridge_opener(link)
                    continue
                self._do(f"removed {p.show(link)}", link.unlink)
            elif link.exists():
                self.left.append(f"{p.show(link)} is not a link the old install.sh made: delete it if it is the old command")
        if p.package_autostart.exists():
            hide = p.config_home / "autostart" / f"{OLD}.desktop"
            self._do(f"hid the old package's autostart entry ({p.show(hide)})", lambda: _write(
                hide, "[Desktop Entry]\nType=Application\nName=Claude Workspace\nHidden=true\n"))
            if (p.system_bin / OLD).exists():
                self.left.append("the old package is still installed: remove it (sudo apt remove claude-workspace, "
                                 "or sudo pacman -R claude-workspace); Sidelit's package replaces it by itself")

    def _old_checkout(self, link: Path):
        """The clone the old launcher ran from, which nothing uses any more."""
        root = Path(os.path.realpath(link)).parent.parent
        if root == Path(__file__).resolve().parent.parent or (root / "bin" / "sidelit").exists():
            return  # (that clone runs Sidelit now)
        if (root / ".git").exists():
            self.left.append(f"the old checkout {self.places.show(root)} is not used any more: delete it when you like")

    def _bridge_hook(self, link: Path) -> bool:
        """Claude Code sessions that are already running read their hooks when they started, and
        call the old hook's link until they end: point it at ours until the next login
        (end_bridge), so they keep reporting instead of failing."""
        try:
            ours = hooks.hook_command()
        except RuntimeError:
            return False

        def repoint():
            link.unlink()
            link.symlink_to(ours)
        self._do(f"pointed {self.places.show(link)} at {ours} until the next login, "
                 f"for the Claude Code sessions already running", repoint)
        self.bridge = str(link)
        return True

    def _bridge_opener(self, link: Path):
        """$BROWSER in the shells already running, Terminator's URL handler and the default browser
        may name the old opener's link: point it at ours, so a link clicked there still opens,
        until you have changed them (_look_for_leftovers says where)."""
        ours = opener_command()

        def repoint():
            link.unlink()
            link.symlink_to(ours)
        self._do(f"pointed {self.places.show(link)} at {ours}, for what still names it", repoint)
        self.left.append(f"{self.places.show(link)} now runs sidelit-open: delete it once nothing names it")

    def end_bridge(self, record: dict) -> str | None:
        """At the first start after a new login, the old hook's link goes: the sessions that
        called it ended with the login it was made in."""
        link = record.get("bridge")
        if not link or not os.path.islink(link):
            return None
        comp = session.compositor(str(self.places.proc))
        if comp is None or time.time() - comp.age <= float(record.get("at") or 0):
            return None
        if not self.dry:
            os.unlink(link)
        return link

    def _stop(self):
        self._stop_running()
        if self.units_removed and not self.dry:
            self.systemctl("daemon-reload")

    def _sweep(self):
        """Both autostart entries fire at a login: an old start that raced this one goes too."""
        if not self.dry:
            self._stop_running()

    def _stop_running(self):
        states = self.systemctl("is-active", *OLD_UNITS).split()
        running = [u for u, s in zip(OLD_UNITS, states) if s in ("active", "activating", "reloading")]
        if running:
            self._do(f"stopped {', '.join(running)}", lambda: self.systemctl("stop", *OLD_UNITS))
        pids = old_processes(self.places.proc)
        if pids:
            self._do(f"stopped the old daemon and sidebar (pid {', '.join(map(str, pids))})",
                     lambda: _terminate(pids, kill=self.kill))

    def _move_folders(self):
        p = self.places
        for old, new, skip in p.folders:
            if old.is_symlink() and old.is_dir() and not self.test:
                self._move_link(old, new)
                continue
            if not old.is_dir():
                continue
            moved, kept = self._merge(old.resolve() if self.test else old, new, skip)
            if moved:
                self.did.append(f"{'copied' if self.test else 'moved'} {p.show(old)} to {p.show(new)}")
            if self.test or self.dry:
                continue
            for name in skip:  # what the stopped processes left
                (old / name).unlink(missing_ok=True)
            if kept:
                names = ", ".join(sorted(k.name for k in kept))
                self.left.append(f"{p.show(old)} still has what Sidelit already had ({names}): "
                                 f"look through it, then delete it")
            else:
                _rmdir(old)
        if self.test:
            return
        for old, new in p.logs:
            if old.exists() and not new.exists():
                self._do(f"moved {p.show(old)} to {p.show(new)}", lambda o=old, n=new: shutil.move(str(o), str(n)))

    def _move_link(self, old: Path, new: Path):
        """An old folder that is a link (into a dotfiles repo, say): the link itself moves, so it
        keeps pointing where you keep it; unless Sidelit has a folder of its own there already."""
        p = self.places
        target = os.path.realpath(old)
        if new.is_symlink() or (new.exists() and not (new.is_dir() and not any(new.iterdir()))):
            self.left.append(f"{p.show(old)} is a link to {target}, and Sidelit has its own {p.show(new)}: "
                             f"move what you need from it by hand")
            return

        def relink():
            if new.is_dir():
                new.rmdir()
            new.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old, new)
        self._do(f"moved {p.show(old)} (a link to {target}) to {p.show(new)}", relink)

    def _merge(self, old: Path, new: Path, skip=frozenset()) -> tuple[int, list[Path]]:
        """Move (a test instance: copy) what is in `old` into `new`, entry by entry: (how many
        moved, what stayed)."""
        moved, kept = 0, []
        if not self.dry:
            new.mkdir(parents=True, exist_ok=True)
        for entry in sorted(old.iterdir()):
            if entry.name in skip:
                continue
            target = new / entry.name
            if target.exists() or target.is_symlink():
                if entry.is_dir() and target.is_dir() and not (entry.is_symlink() or target.is_symlink()):
                    m, k = self._merge(entry, target)
                    moved, kept = moved + m, kept + k
                    if not k and not self.dry and not self.test:
                        _rmdir(entry)
                else:
                    kept.append(entry)
                continue
            if not self.dry and not self.test:
                shutil.move(str(entry), str(target))
            # a test instance copies what a link points at: changing the copy must not change the old install
            elif not self.dry and entry.is_dir():
                shutil.copytree(entry, target, ignore_dangling_symlinks=True)
            elif not self.dry and entry.exists():
                shutil.copy2(entry, target)
            moved += 1
        return moved, kept

    def _rename_commands(self):
        """`claude-workspace open` -> `sidelit open` in config.json, the other sources' files and the
        layout (a terminal opened for a source's item keeps its command there, to run again)."""
        p = self.places
        old_config = p.config_home / OLD
        names = ["config.json", "layout.json"]
        for base in (p.new_config, old_config):  # (a dry run has moved nothing)
            names += [f"sources/{f.name}" for f in sorted((base / "sources").glob("*.json"))]
        for name in dict.fromkeys(names):
            path = p.new_config / name
            if self.dry and not path.exists():  # (only to say it; otherwise a file the move left is not ours to change)
                path = old_config / name
            try:
                text = path.read_text()
            except (OSError, ValueError):  # (not there; not text)
                continue
            new = _renamed(text)
            if new != text:
                self._do(f"{p.show(p.new_config / name)}: the old command is now sidelit",
                         lambda f=path, t=new: _write(f, t))

    def _swap_hook(self):
        p = self.places
        if not hooks.present(OLD_HOOK):
            return
        settings = hooks.SETTINGS
        if self.dry:
            self.did.append(f"{p.show(settings)}: swapped the old hook for Sidelit's")
            return
        try:
            removed, command = hooks.swap(OLD_HOOK)  # (the first save backs the file up: hooks.BACKUP)
        except (ValueError, RuntimeError) as e:
            self.failed.append(f"{p.show(settings)}: {e}")
            return
        if command:
            self.did.append(f"{p.show(settings)}: swapped the old hook for {command} "
                            f"(the file as it was: {hooks.BACKUP})")
        else:
            self.did.append(f"{p.show(settings)}: removed the old hook (the file as it was: {hooks.BACKUP})")
            self.left.append("Sidelit's hook is not installed: run `sidelit hooks install`")
        if not self.bridge:  # (the old hook was not a link of ours to point at Sidelit's: a package's)
            self.left.append("Claude Code sessions that are already running still call the old hook, which is "
                             "gone: restart them (until then their tabs follow their terminal titles)")

    def _swap_chrome_host(self):
        p = self.places
        found = [f for f in p.chrome_hosts if f.exists()]
        if not found:
            return
        host = chrome.host_command()  # (ours first: if that fails, the old one is still found next time)
        self._do(f"registered Sidelit's Chrome native host ({host})", lambda: chrome.install_host(host, p.config_home))
        for f in found:
            self._do(f"removed the old Chrome native host {p.show(f)}", f.unlink)
        self.left.append(f"Chrome: in chrome://extensions remove the old extension and load Sidelit's "
                         f"(Load unpacked: {chrome.extension_dir()}), in every profile that had it")

    def _look_for_leftovers(self):
        p = self.places
        files = [p.home / n for n in SHELL_FILES]
        files += [p.config_home / "terminator" / "config", p.config_home / "mimeapps.list"]
        files += sorted((p.config_home / "environment.d").glob("*.conf"))
        files += [f for f in sorted((p.data_home / "applications").glob("*.desktop")) if f not in p.launchers]
        for f in files:
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            if OLD in text:
                self.left.append(f"{p.show(f)} still names {OLD}: change it to sidelit "
                                 f"({OLD}-open is now sidelit-open)")

    def _mark(self, did: list[str]):
        p = self.places
        record = {"at": time.time(), "did": did, "left": self.left, "bridge": self.bridge}
        _write(p.new_config / MARKER, json.dumps(record, indent=2) + "\n")
        journal_mod.Journal(p.new_state / "journal.jsonl", echo=False)("moved_over", did=did, left=self.left)


def _write(path: Path, text: str):
    path = path.resolve()  # (a link into your dotfiles stays a link: what it points at changes)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    try:
        shutil.copymode(path, tmp)  # (a private file stays private)
    except OSError:  # (not there yet; a filesystem without modes)
        pass
    os.replace(tmp, path)


def _rmdir(path: Path):
    try:
        path.rmdir()
    except OSError:
        pass


def main(argv: list[str]) -> int:
    """`sidelit moveover [--if-needed]`; `sidelit start` passes --if-needed, to say nothing when
    there is nothing to do."""
    quiet = "--if-needed" in argv
    m = MoveOver(Places.here(), dry=config.DRY_RUN, test=bool(os.environ.get("SIDELIT_HOME")))
    before = m.done_before()
    if before is not None:
        gone = m.end_bridge(before)
        if gone:
            print(f"sidelit: removed {gone}, the old hook's link (a new login: no session calls it any more)")
        elif not quiet:
            print(f"sidelit: already took over from {OLD} on "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(before.get('at', 0)))}; nothing to do")
        return 0
    if not m.traces():
        if not quiet:
            print(f"sidelit: nothing of {OLD} here; nothing to do")
        return 0
    print(f"sidelit: taking over from {OLD}, this project's old name"
          + (" (a test instance: its folders are copied, nothing else changes)" if m.test else "")
          + (" (dry run: nothing is changed)" if m.dry else ""))
    ok = m.run()
    for line in m.did:
        print(f"  {line}")
    for line in m.failed:
        print(f"  ✗ {line}", file=sys.stderr)
    if m.left:
        print("left for you:")
        for line in m.left:
            print(f"  - {line}")
    if not ok:
        print("sidelit: the move is tried again at the next start", file=sys.stderr)
    return 0 if ok else 1
