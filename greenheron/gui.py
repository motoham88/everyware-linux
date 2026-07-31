"""
GTK4 panel for the Green Heron switch.

Same model as the curses TUI: columns are switches, rows are antenna ports, and
the grid only ever shows what the device has reported. Clicking a port sends a
command and then waits -- a relay that fails to move shows up as a button that
does not light, rather than as a UI that lies.

Threading: the client's reader thread must never touch widgets. Every callback
hops to the GTK main loop through GLib.idle_add.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, GLib, Gtk  # noqa: E402

from greenheron import client as gh_client  # noqa: E402

# Explicit colours rather than @accent_bg_color and friends: those are libadwaita
# names and are not defined under plain GTK4, where they would silently fall back
# to unstyled. These read correctly on both light and dark themes.
CSS = """
.port-selected  { background-image: none; background-color: #2e7d32; color: #ffffff;
                  font-weight: bold; }
.port-locked    { opacity: 0.5; font-style: italic; }
.switch-header  { font-weight: bold; }
.telemetry      { opacity: 0.65; font-size: 0.85em; }
.status-ok      { color: #2e7d32; font-weight: bold; }
.status-warn    { color: #c77800; font-weight: bold; }
.antenna-label  { font-family: monospace; }
"""


class PanelWindow(Gtk.ApplicationWindow):
    def __init__(self, app, client: gh_client.Client, title: str):
        super().__init__(application=app, title=f"Green Heron — {title}")
        self.client = client
        self.set_default_size(720, 460)

        self._buttons: dict[tuple[str, str], Gtk.Button] = {}
        self._built_for: tuple[str, ...] = ()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        header = Gtk.HeaderBar()
        self.status = Gtk.Label(label="connecting…")
        self.status.add_css_class("status-warn")
        header.pack_end(self.status)
        self.set_titlebar(header)

        if client.dry_run:
            banner = Gtk.Label(label="DRY RUN — nothing is transmitted")
            banner.add_css_class("status-warn")
            banner.set_margin_top(6)
            root.append(banner)

        self.grid = Gtk.Grid(
            row_spacing=4, column_spacing=8,
            margin_top=12, margin_bottom=6, margin_start=12, margin_end=12,
        )
        self.grid.set_column_homogeneous(False)
        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.grid)
        root.append(scroller)

        self.telemetry = Gtk.Label(label="")
        self.telemetry.add_css_class("telemetry")
        self.telemetry.set_margin_bottom(8)
        root.append(self.telemetry)

        self.message = Gtk.Label(label="")
        self.message.set_margin_bottom(8)
        root.append(self.message)

    # -- construction ------------------------------------------------------

    def _build_grid(self, panel) -> None:
        """(Re)build once the roster is known, or if it ever changes."""
        child = self.grid.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.grid.remove(child)
            child = nxt
        self._buttons.clear()

        names = panel.order
        ports = panel.switches[names[0]].ports

        corner = Gtk.Label(label="Antenna", xalign=0)
        corner.add_css_class("switch-header")
        self.grid.attach(corner, 0, 0, 1, 1)

        for col, name in enumerate(names, start=1):
            lbl = Gtk.Label(label=name)
            lbl.add_css_class("switch-header")
            self.grid.attach(lbl, col, 0, 1, 1)

        for row, port in enumerate(ports, start=1):
            lbl = Gtk.Label(label=port, xalign=0)
            lbl.add_css_class("antenna-label")
            self.grid.attach(lbl, 0, row, 1, 1)

            for col, name in enumerate(names, start=1):
                btn = Gtk.Button(label="—")
                btn.set_hexpand(True)
                btn.connect("clicked", self._on_click, name, port)
                self.grid.attach(btn, col, row, 1, 1)
                self._buttons[(name, port)] = btn

        self._built_for = tuple(names)

    # -- updates -----------------------------------------------------------

    def update(self, panel) -> bool:
        """Called on the GTK main loop only."""
        names = panel.order
        if not names or not panel.switches[names[0]].ports:
            return False

        if tuple(names) != self._built_for:
            self._build_grid(panel)

        for name in names:
            sw = panel.switches[name]
            held = panel.locks_by_switch(name)
            for port in sw.ports:
                btn = self._buttons.get((name, port))
                if btn is None:
                    continue
                owner = held.get(port)
                btn.remove_css_class("port-selected")
                btn.remove_css_class("port-locked")

                if sw.selected == port:
                    btn.set_label("ON")
                    btn.add_css_class("port-selected")
                    btn.set_sensitive(True)
                elif owner:
                    btn.set_label(f"in use · {owner}")
                    btn.add_css_class("port-locked")
                    # Locked elsewhere: the device would refuse anyway.
                    btn.set_sensitive(False)
                else:
                    btn.set_label("—")
                    btn.set_sensitive(True)

        self.grid.set_sensitive(not panel.stale)

        if panel.connected:
            self.status.set_label("connected")
            self.status.remove_css_class("status-warn")
            self.status.add_css_class("status-ok")
        else:
            self.status.set_label(
                "reconnecting — state is stale" if panel.stale else "connecting…"
            )
            self.status.remove_css_class("status-ok")
            self.status.add_css_class("status-warn")

        # Field 4 of SWITCHUPDATE. Meaning unknown, so it is shown raw and
        # deliberately not given a unit.
        self.telemetry.set_label(
            "unknown telemetry:   "
            + "    ".join(f"{n} = {panel.switches[n].telemetry or '?'}" for n in names)
        )
        return False

    # -- input -------------------------------------------------------------

    def _on_click(self, _button, switch: str, port: str) -> None:
        try:
            wire = self.client.select(switch, port)
        except (RuntimeError, ConnectionError) as exc:
            self._note(str(exc))
            return
        verb = "would send" if self.client.dry_run else "sent"
        self._note(f"{verb}  {switch} → {port}   ({len(wire)} bytes)")

    def _note(self, text: str) -> None:
        self.message.set_label(text)
        GLib.timeout_add_seconds(5, self._clear_note, text)

    def _clear_note(self, text: str) -> bool:
        if self.message.get_label() == text:
            self.message.set_label("")
        return False


def run(host: str, port: int, dry_run: bool, keepalive: bool = True) -> int:
    app = Gtk.Application(application_id="org.greenheron.SwitchPanel")
    client = gh_client.Client(host, port, dry_run=dry_run, keepalive=keepalive)
    state: dict = {}

    def on_activate(application):
        provider = Gtk.CssProvider()
        provider.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        win = PanelWindow(application, client, f"{host}:{port}")
        state["win"] = win
        win.present()

        # The reader thread must not touch widgets -- marshal to the main loop.
        client.on_change = lambda panel: GLib.idle_add(win.update, panel)
        client.start()

    app.connect("activate", on_activate)
    try:
        return app.run([])
    finally:
        client.stop()
