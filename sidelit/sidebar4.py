#!/usr/bin/env python3
"""Sidelit's sidebar, GTK 4: drawn on the GPU, with real transforms for the hover.

The same sidebar as sidebar.py (GTK 3), which stays as the fallback: what it does is shared
(sidebar_common.py), only the drawing is here. GTK 4 renders through OpenGL or Vulkan and its
CSS can scale and move a widget without laying anything out again, so pointing at a tab
swells its card and slides the cards below it out of the way smoothly, text and all.

It needs gtk4-layer-shell (the GTK 4 build of gtk-layer-shell), which has to be loaded before
libwayland-client: `main()` restarts the process once with it in LD_PRELOAD. Without it, or
when the compositor has no layer shell, `sidelit sidebar` runs the GTK 3 sidebar.

- click a tab .......... switch to that workspace (a closed one is reopened)
- double-click ......... rename it; drag it to reorder (COSMIC)
- right-click .......... its menu; on the header, the sidebar's menu
- '+' at the bottom .... new pinned workspace from a template
- fork icon ............ new workspace running `claude --resume <id> --fork-session`
- x .................... close every window on the workspace and stop keeping it
"""
from __future__ import annotations

import os
import sys
import sysconfig
from pathlib import Path

from sidelit import config

LIB = "libgtk4-layer-shell.so.0"
TYPELIB = "Gtk4LayerShell-1.0.typelib"
CHOICE_ENV = "SIDELIT_SIDEBAR"  # gtk4 | gtk3 | auto, over the config's "sidebar"


def layer_shell_paths(prefixes=None) -> tuple[str, str] | None:
    """(library, typelib directory) of an installed gtk4-layer-shell: one built into ~/.local
    or /usr/local, or the distribution's (or in a directory $SIDELIT_GTK4_LAYER_SHELL
    names)."""
    arch = sysconfig.get_config_var("MULTIARCH") or ""
    dirs = [Path(d) for d in os.environ.get("SIDELIT_GTK4_LAYER_SHELL", "").split(os.pathsep) if d]
    for prefix in (Path.home() / ".local", Path("/usr/local"), Path("/usr")) if prefixes is None else prefixes:
        dirs += [prefix / "lib" / arch, prefix / "lib64", prefix / "lib"] if arch else [prefix / "lib64", prefix / "lib"]
    for d in dirs:
        if (d / LIB).exists() and (d / "girepository-1.0" / TYPELIB).exists():
            return str(d / LIB), str(d / "girepository-1.0")
    return None


def choice() -> str:
    """Which sidebar to run: the environment, else the config's "sidebar", else auto."""
    wanted = os.environ.get(CHOICE_ENV) or str(config.load().get("sidebar") or "auto")
    return wanted if wanted in ("gtk4", "gtk3", "auto") else "auto"


def should_run() -> bool:
    """Run this sidebar rather than the GTK 3 one: not turned off, and gtk4-layer-shell found."""
    return choice() != "gtk3" and layer_shell_paths() is not None


def _exec_sidebar(env: dict):
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, "-m", "sidelit", "sidebar"], env)


def _fall_back(why: str):
    """Start over as the GTK 3 sidebar (this process may already hold GTK 4)."""
    print(f"sidebar: GTK 4 sidebar unavailable ({why}); running the GTK 3 one", file=sys.stderr, flush=True)
    env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
    env[CHOICE_ENV] = "gtk3"
    _exec_sidebar(env)


def _ensure_preloaded():
    """gtk4-layer-shell has to be linked before libwayland-client, which GTK pulls in: restart
    once with it preloaded (and its typelib on the search path)."""
    paths = layer_shell_paths()
    if paths is None:
        _fall_back("gtk4-layer-shell not found")
    lib, typelib_dir = paths
    if lib in os.environ.get("LD_PRELOAD", "").split(":"):
        return
    env = dict(os.environ)
    env["LD_PRELOAD"] = ":".join(filter(None, [lib, env.get("LD_PRELOAD")]))
    env["GI_TYPELIB_PATH"] = os.pathsep.join(filter(None, [typelib_dir, env.get("GI_TYPELIB_PATH")]))
    _exec_sidebar(env)


def main():
    _ensure_preloaded()
    # GTK 4.14's default renderer (ngl) handed frames to COSMIC unevenly, and the hover felt
    # choppy; the older GL renderer paced them evenly and felt smooth (cairo glitched under
    # the transforms). Where "gl" is gone (GTK 4.18+) GTK falls back to its default.
    os.environ.setdefault("GSK_RENDERER", "gl")
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Gdk", "4.0")
        gi.require_version("Gtk4LayerShell", "1.0")
    except (ImportError, ValueError) as e:
        _fall_back(str(e))
    from sidelit import sidebar4_ui
    return sidebar4_ui.main(_fall_back)


if __name__ == "__main__":
    main()
