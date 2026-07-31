"""Curses panel for the Green Heron switch.

Columns are switches, rows are antenna ports. The grid only ever renders what the
device has reported -- selecting a port sends a command and then waits for the
device to confirm, so a relay that fails to move shows up as the cursor not
sticking rather than as a UI that lies.
"""

from __future__ import annotations

import curses
import time
from typing import Optional

from greenheron import client as gh_client
from greenheron import protocol as p

HELP = "↑↓ port   ←→ switch   ⏎ select   o OFF   r raw   q quit"
MAX_RAW = 400


class Panel:
    def __init__(self, stdscr, client: gh_client.Client, host: str):
        self.stdscr = stdscr
        self.client = client
        self.host = host
        self.row = 0
        self.col = 0
        self.show_raw = False
        self.raw: list[str] = []
        self.message = ""
        self.message_until = 0.0

    # -- helpers -----------------------------------------------------------

    def note(self, text: str, seconds: float = 4.0) -> None:
        self.message = text
        self.message_until = time.monotonic() + seconds

    def put(self, y: int, x: int, text: str, attr: int = 0) -> None:
        """addstr that silently clips instead of raising at the screen edge."""
        h, w = self.stdscr.getmaxyx()
        if not (0 <= y < h) or x >= w:
            return
        try:
            self.stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass

    def record(self, rec: p.Record) -> None:
        self.raw.append(rec.raw.decode("utf-8", "replace"))
        del self.raw[:-MAX_RAW]

    # -- drawing -----------------------------------------------------------

    def draw(self) -> None:
        panel = self.client.snapshot()
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()

        names = panel.order
        if not names:
            self.put(0, 0, f"Green Heron panel — {self.host}", curses.A_BOLD)
            msg = "connected, waiting for roster…" if panel.connected \
                else f"connecting… {panel.last_error}"
            self.put(2, 2, msg)
            self.stdscr.refresh()
            return

        self.col = min(self.col, len(names) - 1)
        ports = panel.switches[names[self.col]].ports
        if ports:
            self.row = min(self.row, len(ports) - 1)

        self._header(panel, w)
        body_bottom = self._grid(panel, names, ports, 3, h, w)
        self._footer(panel, names, body_bottom, h, w)
        self.stdscr.refresh()

    def _header(self, panel, w: int) -> None:
        self.put(0, 0, f"Green Heron switch panel — {self.host}", curses.A_BOLD)
        if panel.connected:
            up = time.time() - panel.connected_since
            state, attr = f"connected {int(up)//60}m{int(up)%60:02d}s", self.c(2)
        elif panel.stale:
            state, attr = "RECONNECTING — state is stale", self.c(3) | curses.A_BOLD
        else:
            state, attr = "connecting…", self.c(3)
        self.put(0, max(0, w - len(state) - 1), state, attr)

        warn = []
        if gh_client.SELECT is None:
            warn.append("select command UNCONFIRMED (see NOTES.md)")
        if self.client.dry_run:
            warn.append("DRY RUN — nothing is transmitted")
        if warn:
            self.put(1, 0, "  ·  ".join(warn), self.c(3) | curses.A_BOLD)

    def _grid(self, panel, names, ports, top: int, h: int, w: int) -> int:
        label_w = max((len(x) for x in ports), default=10) + 2
        cell_w = max(max(len(n) for n in names) + 2, 10)

        self.put(top, 0, "Antenna".ljust(label_w), curses.A_BOLD | curses.A_UNDERLINE)
        for i, name in enumerate(names):
            attr = curses.A_BOLD | curses.A_UNDERLINE
            if i == self.col:
                attr |= self.c(4)
            self.put(top, label_w + i * cell_w, name.center(cell_w), attr)

        held = {n: panel.locks_by_switch(n) for n in names}

        y = top + 1
        for r, port in enumerate(ports):
            if y >= h - 4:
                self.put(y, 0, "… terminal too short to show every port")
                return y + 1
            self.put(y, 0, port.ljust(label_w),
                     curses.A_BOLD if r == self.row else 0)
            for i, name in enumerate(names):
                sw = panel.switches[name]
                cursor = (i == self.col and r == self.row)
                owner = held[name].get(port)

                if sw.selected == port:
                    text, attr = "● ON", self.c(2) | curses.A_BOLD
                elif owner:
                    text, attr = f"◦ {owner[-1]}", self.c(3)
                else:
                    text, attr = "·", curses.A_DIM
                if panel.stale:
                    attr |= curses.A_DIM
                if cursor:
                    attr |= curses.A_REVERSE
                self.put(y, label_w + i * cell_w, text.center(cell_w), attr)
            y += 1
        return y

    def _footer(self, panel, names, y: int, h: int, w: int) -> None:
        y = min(y + 1, h - 4)
        # Field 4 of SWITCHUPDATE. Meaning unknown -- shown raw, deliberately
        # not labelled with a unit we would only be guessing at.
        tele = "  ".join(
            f"{n}={panel.switches[n].telemetry or '?'}" for n in names
        )
        self.put(y, 0, f"unknown telemetry: {tele}", curses.A_DIM)

        if self.show_raw:
            self.put(y + 1, 0, "raw:", curses.A_DIM)
            lines = self.raw[-(h - y - 4):] if h - y - 4 > 0 else []
            for i, line in enumerate(lines):
                self.put(y + 2 + i, 2, line.replace("\x1f", "|").replace("\x1d", "·"),
                         curses.A_DIM)

        if self.message and time.monotonic() < self.message_until:
            self.put(h - 2, 0, self.message, self.c(3) | curses.A_BOLD)
        self.put(h - 1, 0, HELP.ljust(max(0, w - 1)), curses.A_REVERSE)

    def c(self, n: int) -> int:
        return curses.color_pair(n) if curses.has_colors() else 0

    # -- input -------------------------------------------------------------

    def select_here(self, port: Optional[str] = None) -> None:
        panel = self.client.snapshot()
        names = panel.order
        if not names:
            return
        switch = names[self.col]
        ports = panel.switches[switch].ports
        if not ports:
            return
        target = port if port is not None else ports[self.row]

        owner = panel.locks_by_switch(switch).get(target)
        if owner:
            self.note(f"{target} is held by {owner}")
            return
        try:
            wire = self.client.select(switch, target)
        except RuntimeError as exc:
            self.note(str(exc), 8.0)
            return
        except ConnectionError as exc:
            self.note(f"not sent: {exc}")
            return
        verb = "would send" if self.client.dry_run else "sent"
        self.note(f"{verb} {switch} → {target}  ({len(wire)}B)")

    def handle(self, key: int) -> bool:
        panel = self.client.snapshot()
        names = panel.order
        nports = len(panel.switches[names[self.col]].ports) if names else 0

        if key in (ord("q"), 27):
            return False
        if key in (curses.KEY_UP, ord("k")) and nports:
            self.row = (self.row - 1) % nports
        elif key in (curses.KEY_DOWN, ord("j")) and nports:
            self.row = (self.row + 1) % nports
        elif key in (curses.KEY_LEFT, ord("h")) and names:
            self.col = (self.col - 1) % len(names)
        elif key in (curses.KEY_RIGHT, ord("l")) and names:
            self.col = (self.col + 1) % len(names)
        elif key in (curses.KEY_ENTER, 10, 13, ord(" ")):
            self.select_here()
        elif key == ord("o"):
            self.select_here("OFF")
        elif key == ord("r"):
            self.show_raw = not self.show_raw
        return True


def run(stdscr, host: str, port: int, dry_run: bool, keepalive: bool = True) -> None:
    curses.curs_set(0)
    stdscr.timeout(200)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_YELLOW, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)

    client = gh_client.Client(host, port, dry_run=dry_run, keepalive=keepalive)
    ui = Panel(stdscr, client, f"{host}:{port}")
    client.on_record = ui.record
    client.start()
    try:
        while True:
            ui.draw()
            key = stdscr.getch()
            if key == -1:
                continue
            if key == curses.KEY_RESIZE:
                continue
            if not ui.handle(key):
                return
    finally:
        client.stop()
