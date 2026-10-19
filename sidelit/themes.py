"""The sidebar's themes: CSS drawn over the sidebar's own, with pictures of their own.

config.json's "theme" picks one: "default" (the sidebar as it is), "auto" (the season's theme
while it is on, Halloween's in the two weeks up to it, else the default) or a theme's name; a
CSS file at ~/.config/sidelit/themes/<name>.css is a theme too, drawn over the default. The
environment's SIDELIT_THEME wins over the config, to try one out.

Needs no GTK: the sidebars load what this returns.
"""
from __future__ import annotations

import datetime
import os
from dataclasses import dataclass
from pathlib import Path

from sidelit import config

ENV = "SIDELIT_THEME"
USER_THEMES = config.CONFIG_DIR / "themes"


@dataclass(frozen=True)
class Theme:
    name: str
    css: str = ""        # drawn over the sidebar's own CSS
    images: tuple = ()   # (file name, SVG) the CSS reaches as {images}/<file name>
    # its picture in the settings: the panel, a card, the card where you are and its ring, a
    # session working and one waiting; `art` is SVG drawn over it, in the right quarter
    colors: tuple = ("#26262b", "#3e3e44", "#285c74", "#64d2ff", "#ff9f0a", "#ffd60a")
    art: str = ""
    about: str = ""      # a line under its name in the settings


# A cobweb in the panel's top right corner, a spider down its thread
HALLOWEEN_WEB = b"""<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120">
 <g fill="none" stroke="#ffffff" stroke-opacity="0.30" stroke-width="1" stroke-linecap="round">
  <path d="M120 0 L20 0 M120 0 L35 45 M120 0 L75 85 M120 0 L120 100"/>
  <path d="M100 0 Q101 8 105 9 Q110 13 111 18 Q114 22 120 21"/>
  <path d="M78 0 Q80 14 87 18 Q96 25 97 34 Q104 41 120 41"/>
  <path d="M56 0 Q59 20 70 27 Q82 36 84 50 Q96 60 120 61"/>
  <path d="M36 0 Q39 26 54 36 Q68 47 71 66 Q90 79 120 81"/>
 </g>
 <g transform="translate(-20 -20)">
 <path d="M58 0 L58 52" stroke="#ffffff" stroke-opacity="0.35" stroke-width="0.8"/>
 <g fill="#16101c" stroke="#ff9f0a" stroke-opacity="0.45" stroke-width="0.8">
  <ellipse cx="58" cy="56" rx="3.2" ry="4"/>
  <circle cx="58" cy="51" r="2.2"/>
 </g>
 <g fill="none" stroke="#ff9f0a" stroke-opacity="0.45" stroke-width="0.8" stroke-linecap="round">
  <path d="M55 54 L50 51 M55 56 L49 57 M55 58 L50 62 M61 54 L66 51 M61 56 L67 57 M61 58 L66 62"/>
 </g>
 </g>
</svg>"""

# A night behind the cards, where the list leaves room: a pale moon, bats across it
HALLOWEEN_NIGHT = b"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="240" height="180" viewBox="0 0 240 180">
 <defs>
  <radialGradient id="halo" cx="0.5" cy="0.5" r="0.5">
   <stop offset="0.55" stop-color="#ffe9a8" stop-opacity="0.16"/>
   <stop offset="1" stop-color="#ffe9a8" stop-opacity="0"/>
  </radialGradient>
  <path id="bat" d="M0 0 C-3 -5 -9 -7 -16 -5 C-12 -3 -11 0 -12 3 C-9 1 -6 2 -5 5 C-3 2 -1 2 0 3
    C1 2 3 2 5 5 C6 2 9 1 12 3 C11 0 12 -3 16 -5 C9 -7 3 -5 0 0 Z M-2 -1 L-2.5 -4 L-0.8 -2 L0.8 -2 L2.5 -4 L2 -1 Z"/>
 </defs>
 <circle cx="168" cy="52" r="46" fill="url(#halo)"/>
 <circle cx="168" cy="52" r="26" fill="#f6e7b4" fill-opacity="0.30"/>
 <g fill="#c9b47a" fill-opacity="0.22">
  <circle cx="159" cy="45" r="5"/>
  <circle cx="176" cy="60" r="3.5"/>
  <circle cx="174" cy="40" r="2.5"/>
 </g>
 <g fill="#0d0811" fill-opacity="0.8" stroke="#ff9f0a" stroke-opacity="0.25" stroke-width="0.6">
  <use xlink:href="#bat" transform="translate(162 54) rotate(-8) scale(1.25)"/>
  <use xlink:href="#bat" transform="translate(110 30) rotate(10) scale(0.9)"/>
  <use xlink:href="#bat" transform="translate(205 92) rotate(-14) scale(0.7)"/>
 </g>
</svg>"""

# A jack-o'-lantern lit from inside, on the usage card
HALLOWEEN_PUMPKIN = b"""<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48">
 <defs>
  <radialGradient id="skin" cx="0.4" cy="0.35" r="0.75">
   <stop offset="0" stop-color="#ffb347"/>
   <stop offset="1" stop-color="#e2580b"/>
  </radialGradient>
 </defs>
 <path d="M24 12 C25 8 27 7 29 6.5" fill="none" stroke="#5d7a2a" stroke-width="2.6" stroke-linecap="round"/>
 <ellipse cx="14.5" cy="28" rx="9" ry="12" fill="url(#skin)"/>
 <ellipse cx="33.5" cy="28" rx="9" ry="12" fill="url(#skin)"/>
 <ellipse cx="24" cy="28" rx="10" ry="13.5" fill="url(#skin)"/>
 <path d="M19.5 16 C18.5 22 18.5 34 20 40 M28.5 16 C29.5 22 29.5 34 28 40" fill="none" stroke="#b8460a" stroke-opacity="0.5" stroke-width="1"/>
 <g fill="#ffe066">
  <path d="M12 25 L17.5 20 L19.5 26.5 Z M36 25 L30.5 20 L28.5 26.5 Z M22.3 29 L24 26 L25.7 29 Z"/>
  <path d="M11.5 31 Q24 40 36.5 31 L33.5 34 L31 32 L28.5 35.5 L26 33 L24 36 L22 33 L19.5 35.5 L17 32 L14.5 34 Z"/>
 </g>
</svg>"""

# A small ghost beside "New workspace"
HALLOWEEN_GHOST = b"""<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24">
 <path d="M3 10 C3 4.5 7 1.5 12 1.5 C17 1.5 21 4.5 21 10 L21 22 L18 19.5 L15 22.5 L12 19.5 L9 22.5 L6 19.5 L3 22 Z"
   fill="#ffffff" fill-opacity="0.85"/>
 <ellipse cx="9" cy="9.5" rx="1.5" ry="2.1" fill="#241a2c"/>
 <ellipse cx="15" cy="9.5" rx="1.5" ry="2.1" fill="#241a2c"/>
 <ellipse cx="12" cy="14.5" rx="1.6" ry="1.3" fill="#241a2c" fill-opacity="0.8"/>
</svg>"""

# The usage bars' fill: blood, a 6 px band with drips running down from it. A drip is a thin
# neck swelling to a round tip, with a fillet where it leaves the band, a dark shadow under it
# and a wet glint on its tip. Two tiles of different widths lie over each other (the band and
# its drips, and more drips alone), so the pattern does not show its repeat.
BLOOD_BAND = 6
BLOOD_HEIGHT = 16
# (centre x, how far below the band its tip hangs, the tip's radius)
BLOOD_DRIPS = [(8, 3.2, 1.3), (23, 7.8, 1.7), (41, 1.8, 1.0), (57, 5.4, 1.5), (76, 8.0, 1.8),
               (88, 2.6, 1.1)]
BLOOD_MORE_DRIPS = [(15, 4.4, 1.3), (44, 6.6, 1.5)]


def _drip(x: float, down: float, r: float) -> str:
    """Wide where it leaves the band, narrowing down to the neck, then the round tip."""
    top, wide, neck = BLOOD_BAND - 0.5, r * 0.9, r * 0.42
    tip = BLOOD_BAND + down - r  # the tip's centre
    mid = (top + 1.2 + tip) / 2
    return (f"M{x - wide - 1.6:.2f} {top:.2f} Q{x - wide:.2f} {top:.2f} {x - wide:.2f} {top + 1.2:.2f} "
            f"Q{x - neck:.2f} {mid:.2f} {x - neck:.2f} {tip:.2f} A{r} {r} 0 1 0 {x + neck:.2f} {tip:.2f} "
            f"Q{x + neck:.2f} {mid:.2f} {x + wide:.2f} {top + 1.2:.2f} Q{x + wide:.2f} {top:.2f} {x + wide + 1.6:.2f} {top:.2f} Z")


def _blood_svg(width: int, drips: list, band: bool) -> bytes:
    shape = " ".join(_drip(*d) for d in drips)
    if band:
        shape = f"M0 0 H{width} V{BLOOD_BAND} H0 Z " + shape
    glints = "".join(f'<ellipse cx="{x - r * 0.35:.2f}" cy="{BLOOD_BAND + down - r * 1.25:.2f}" rx="{r * 0.28:.2f}" '
                     f'ry="{r * 0.45:.2f}" fill="#ffffff" fill-opacity="0.35"/>' for x, down, r in drips if r >= 1.2)
    sheen = f'<rect x="0" y="1" width="{width}" height="0.8" fill="#ffffff" fill-opacity="0.18"/>' if band else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{BLOOD_HEIGHT}" '
            f'viewBox="0 0 {width} {BLOOD_HEIGHT}"><defs><linearGradient id="blood" gradientUnits="userSpaceOnUse" '
            f'x1="0" y1="0" x2="0" y2="{BLOOD_HEIGHT}"><stop offset="0" stop-color="#d4142e"/>'
            f'<stop offset="0.35" stop-color="#a3001b"/><stop offset="1" stop-color="#5e000d"/></linearGradient></defs>'
            f'<path d="{shape}" fill="#2a0006" fill-opacity="0.55" transform="translate(0.5 0.6)"/>'
            f'<path d="{shape}" fill="url(#blood)"/>{sheen}{glints}</svg>').encode()


HALLOWEEN_BLOOD = _blood_svg(97, BLOOD_DRIPS, band=True)
HALLOWEEN_MORE_DRIPS = _blood_svg(61, BLOOD_MORE_DRIPS, band=False)

# Pumpkin orange for a session at work (its stripe flickers like a candle rather than breathes),
# a potion green for one that wants you, purple for where you are, blood red for failed.
HALLOWEEN_CSS = """
.cw-panel { background-color: rgba(22,12,30,0.55); border-color: rgba(255,122,26,0.22);
  background-image: url("{images}/web.svg"), url("{images}/night.svg"); background-repeat: no-repeat;
  background-position: right top, center 74%; background-size: 130px 130px, 240px 180px; }
.cw-header-title { color: #ffb347; text-shadow: 0 0 10px rgba(255,122,26,0.55), 0 1px 3px rgba(0,0,0,0.6); }

.cw-tab { background-color: rgba(46,30,56,0.86); border-color: rgba(255,255,255,0.08); }
.cw-tab:hover { background-color: rgba(60,40,74,0.92); }
.cw-tab.active { background-color: rgba(76,38,110,0.90); border-color: rgba(191,90,242,0.55);
  box-shadow: 0 0 16px -4px rgba(191,90,242,0.45); }
.cw-tab.active:hover { background-color: rgba(88,46,126,0.94); }
.cw-tab.drop-before { box-shadow: inset 0 3px #bf5af2; }
.cw-tab.drop-after { box-shadow: inset 0 -3px #bf5af2; }
.cw-well { background-color: rgba(14,8,20,0.55); }

@keyframes cw-candle {
  0%   { background-color: #ff7a1a; }
  12%  { background-color: rgba(255,122,26,0.55); }
  20%  { background-color: #ffa040; }
  38%  { background-color: rgba(255,122,26,0.70); }
  45%  { background-color: #ff7a1a; }
  63%  { background-color: rgba(255,122,26,0.45); }
  70%  { background-color: #ffb347; }
  86%  { background-color: rgba(255,122,26,0.65); }
  100% { background-color: #ff7a1a; }
}
@keyframes cw-potion {
  0%   { background-color: rgba(166,255,77,0.40); }
  50%  { background-color: #a6ff4d; }
  100% { background-color: rgba(166,255,77,0.40); }
}
.cw-tab.claude.working .cw-pill, .cw-box.working .cw-pill { background-color: #ff7a1a; animation: cw-candle 1.6s linear infinite; }
.cw-tab.claude.shell .cw-pill, .cw-box.shell .cw-pill { background-color: rgba(255,122,26,0.55); }
.cw-tab.claude.waiting .cw-pill, .cw-box.waiting .cw-pill { background-color: #a6ff4d; }
.cw-tab.claude.attention .cw-pill { background-color: #a6ff4d; animation: cw-potion 1.4s ease-in-out infinite; }
.cw-box.attention .cw-pill { background-color: #a6ff4d; box-shadow: 0 0 8px rgba(166,255,77,0.6);
  animation: cw-potion 1.4s ease-in-out infinite; }
.cw-tab.claude.failed .cw-pill { background-color: #ff2d2d; }
.cw-box.working { background-image: linear-gradient(90deg, rgba(255,122,26,0.14), transparent 50%); }
.cw-box.attention { background-image: linear-gradient(90deg, rgba(166,255,77,0.12), transparent 45%); }
.cw-box.working .cw-dot { background-color: rgba(255,122,26,0.85); box-shadow: 0 0 4px rgba(255,122,26,0.6); }
.cw-tab.attention .cw-name, .cw-box.attention .cw-box-name, .cw-src.waiting .cw-src-name { color: #a6ff4d; }
.cw-tab.claude.attention, .cw-tab.card.attention { border-color: rgba(166,255,77,0.40);
  box-shadow: 0 0 14px -2px rgba(166,255,77,0.25); }

.cw-usage { background-color: rgba(46,30,56,0.86); }
tooltip.background { background-color: rgba(34,20,44,0.97); border-color: rgba(191,90,242,0.35); }
/* the jack-o'-lantern heads the usage, the ghost is the new workspace's icon (one line each:
   without the images, css() drops them whole) */
.usage-agent { padding-left: 19px; background-image: url("{images}/pumpkin.svg"); background-repeat: no-repeat; background-position: left center; background-size: 14px 14px; }
button.cw-new image { color: transparent; background-image: url("{images}/ghost.svg"); background-repeat: no-repeat; background-position: center; background-size: 15px 15px; }
/* the usage is blood: the bar is 16 px, its trough drawn only in its top 6, so the drips hang
   below the fill; running low it glows, nearly out it throbs */
@keyframes cw-throb {
  0%   { box-shadow: 0 0 3px rgba(255,30,50,0.35); }
  50%  { box-shadow: 0 0 10px rgba(255,30,50,0.85); }
  100% { box-shadow: 0 0 3px rgba(255,30,50,0.35); }
}
progressbar.usage-bar > trough { min-height: 16px; background-color: transparent;
  background-image: linear-gradient(to bottom, rgba(255,255,255,0.10) 6px, transparent 6px); }
progressbar.usage-bar > trough > progress { min-height: 16px; border-radius: 3px 3px 0 0; background-color: #9c0019; }
progressbar.usage-bar > trough > progress { background-color: transparent; background-image: url("{images}/drips.svg"), url("{images}/blood.svg"); background-repeat: repeat-x; background-position: left top; background-size: 61px 16px, 97px 16px; }
progressbar.usage-bar.warn > trough > progress { box-shadow: 0 0 6px rgba(220,20,40,0.55); }
progressbar.usage-bar.high > trough > progress { animation: cw-throb 1.2s ease-in-out infinite; }
.usage-row.warn .usage-value { color: #ff4d5e; }
.usage-row.high .usage-value { color: #ff2d2d; text-shadow: 0 0 6px rgba(255,30,50,0.6); }
button.cw-new { background-color: rgba(60,36,72,0.86); }
button.cw-new:hover { background-color: rgba(255,122,26,0.30); }
button.cw-update { color: #ffb347; background: rgba(255,122,26,0.16); }
button.cw-update:hover { background: rgba(255,122,26,0.26); }
.cw-history-day, .cw-restore-title { color: #ffb347; }
.cw-restore { background-color: rgba(60,30,80,0.88); }
popover.cw-pop > contents, popover.cw-pop > arrow { background-color: #241a2c; }
.cw-history-view list > row.cw-history-row, button.cw-setting { background-color: rgba(46,30,56,0.86); }
.cw-history-view list > row.cw-history-row:hover, button.cw-setting:hover { background-color: rgba(60,40,74,0.92); }
button.cw-setting.picked { background-color: rgba(76,38,110,0.90); border-color: rgba(191,90,242,0.55); }
button.cw-setting.picked:hover { background-color: rgba(88,46,126,0.94); }
.cw-setting-check { color: #bf5af2; }
"""

HALLOWEEN_ART = (
    '<path d="M96 0 L74 0 M96 0 L80 15 M96 0 L96 22 M86 0 Q88 6 96 7 M78 0 Q81 12 96 15" fill="none" '
    'stroke="#ffffff" stroke-opacity="0.35" stroke-width="0.6"/>'
    + HALLOWEEN_PUMPKIN.decode().replace('width="48" height="48"', 'x="73" y="31" width="20" height="20"', 1)
)

THEMES = {
    "default": Theme("default", about="Frosted glass, the colours of iOS"),
    "halloween": Theme("halloween", HALLOWEEN_CSS, colors=("#1a0f22", "#2e1e38", "#4c266e", "#bf5af2", "#ff7a1a", "#a6ff4d"),
                       art=HALLOWEEN_ART, about="Cobwebs, bats, candlelight", images=(("web.svg", HALLOWEEN_WEB), ("night.svg", HALLOWEEN_NIGHT),
                                                  ("pumpkin.svg", HALLOWEEN_PUMPKIN), ("ghost.svg", HALLOWEEN_GHOST),
                                                  ("blood.svg", HALLOWEEN_BLOOD), ("drips.svg", HALLOWEEN_MORE_DRIPS))),
}


def seasonal(today: datetime.date) -> str:
    """The season's theme on `today`: Halloween's in the two weeks up to it."""
    if today.month == 10 and today.day >= 18:
        return "halloween"
    return "default"


def wanted(cfg: dict | None = None, today: datetime.date | None = None) -> str:
    """The theme's name: the environment's, else the config's, with "auto" made the season's."""
    name = os.environ.get(ENV) or str((cfg if cfg is not None else config.load()).get("theme") or "auto")
    name = name.strip().lower()
    if name == "auto":
        return seasonal(today or datetime.date.today())
    return name


def choices(today: datetime.date | None = None) -> list[tuple[str, str, str]]:
    """(name, title, a line about it) for every theme there is: "auto" first, then the built-in
    ones, then the user's CSS files."""
    season = seasonal(today or datetime.date.today())
    out = [("auto", "Seasonal", f"{season.capitalize()} now" if season != "default"
            else "Halloween in late October, else the default")]
    out += [(name, name.capitalize(), theme.about) for name, theme in THEMES.items()]
    try:
        mine = sorted(f.stem for f in USER_THEMES.glob("*.css"))
    except OSError:
        mine = []
    return out + [(name, name, f"{USER_THEMES.name}/{name}.css") for name in mine if name not in THEMES and name != "auto"]


def preview(name: str, today: datetime.date | None = None) -> bytes:
    """A small picture of the theme for the settings, 96×56: three cards, the middle one where
    you are, their stripes working, waiting and done ("auto": the season's theme)."""
    if name == "auto":
        name = seasonal(today or datetime.date.today())
    theme = THEMES.get(name, THEMES["default"])
    panel, card, active, ring, working, waiting = theme.colors
    width = 62 if theme.art else 80
    cards = []
    for i, (stripe, here) in enumerate(((working, False), (waiting, True), ("#aeaeb2", False))):
        y = 7 + i * 15
        edge = f' stroke="{ring}" stroke-width="1"' if here else ""
        cards.append(f'<rect x="8" y="{y}" width="{width}" height="12" rx="3" fill="{active if here else card}"{edge}/>'
                     f'<rect x="11" y="{y + 3}" width="2" height="6" rx="1" fill="{stripe}"/>'
                     f'<rect x="16" y="{y + 4}" width="{width - 22 - i * 8}" height="4" rx="2" fill="#ffffff" fill-opacity="0.7"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="96" height="56" viewBox="0 0 96 56">'
            f'<clipPath id="panel"><rect width="96" height="56" rx="8"/></clipPath><g clip-path="url(#panel)">'
            f'<rect width="96" height="56" fill="{panel}"/>{"".join(cards)}{theme.art}</g></svg>').encode()


def load(name: str) -> Theme:
    """The theme by that name: a built-in one, else the user's CSS file, else the default."""
    if name in THEMES:
        return THEMES[name]
    path = USER_THEMES / f"{name}.css"
    try:
        return Theme(name, path.read_text())
    except (OSError, UnicodeDecodeError):
        return THEMES["default"]


def css(theme: Theme) -> str:
    """The theme's CSS, its images written out where the CSS can reach them."""
    if not theme.images:
        return theme.css
    folder = config.RUNTIME_DIR / "theme" / theme.name
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for file, data in theme.images:
            (folder / file).write_bytes(data)
    except OSError:  # no images: the theme's colours alone
        return "\n".join(line for line in theme.css.splitlines() if "{images}" not in line)
    return theme.css.replace("{images}", Path(folder).as_uri())
