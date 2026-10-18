"""Sidelit: a workspace sidebar for COSMIC, KDE Plasma, GNOME and Hyprland, one tab per workspace."""
import os


def version() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.join(here, "VERSION"), os.path.join(here, "..", "VERSION")):
        try:
            with open(candidate) as handle:
                return handle.read().strip()
        except OSError:
            continue
    return "0.0.0"
