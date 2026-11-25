#!/usr/bin/env python

"""Creates a taskbar icon that indicates if updates are available and provides a
context menu to install them.

Dependencies:
 * pacman-contrib to check for updates
 * python-pystray for the icon and the context menu
 * optionally an AUR helper (paru or yay) to also check AUR updates
"""

import argparse
import logging
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image

HERE = Path(__file__).parent

UPDATE_PERIOD = int(os.getenv("UPDATE_PERIOD", "3600"))
TERMINAL = os.getenv("TERMINAL", "xfce4-terminal --disable-server")
UPDATE_CMD = os.getenv("UPDATE_CMD", "sudo pacman -Syu --noconfirm")
ICONS_FOLDER = Path(os.getenv("ICONS_FOLDER", str(HERE / "icons")))
AUR_HELPER = os.getenv("AUR_HELPER", "auto")

# Longest package list shown in the menu / notification before truncating.
MAX_MENU_PACKAGES = 40
MAX_NOTIFY_PACKAGES = 8


def detect_aur_helper(choice):
    """Return the AUR helper executable to use, or None."""
    if choice in ("", "none"):
        return None
    if choice != "auto":
        return choice
    return next((h for h in ("paru", "yay") if shutil.which(h)), None)


class Update:
    """One upgradable package, parsed from a "name old -> new" line."""

    def __init__(self, line, source):
        self.line = line
        self.source = source
        parts = line.split()
        self.name = parts[0]
        if len(parts) >= 4 and parts[2] == "->":
            self.old, self.new = parts[1], parts[3]
        else:
            self.old = self.new = None
        self.ignored = "[ignored]" in parts

    def label(self):
        text = self.name
        if self.old:
            text += f"  {self.old} \u2192 {self.new}"
        if self.source != "repo":
            text += f"  [{self.source}]"
        if self.ignored:
            text += "  (ignored)"
        return text

    def key(self):
        return (self.name, self.new)


class Icons:
    NO_UPDATES = "no_updates.png"
    UPDATES_AVAILABLE = "updates_available.png"
    CHECK_UPDATES_FAILED = "updates_failed.png"


class ArchUpdateIndicator:
    log = logging.getLogger("ArchUpdateIndicator")
    title = "Arch Update Indicator"

    def __init__(
        self, period, terminal, update_cmd, icons_folder, notifications, aur_helper
    ):
        self.period = period
        self.aur_helper = aur_helper
        self.terminal = terminal
        self.update_cmd = update_cmd
        self.icons_folder = icons_folder
        self.notifications = notifications
        self.update_check_thread = None
        self.install_updates_thread = None
        self.periodic_check_thread = None
        self.updates = []
        self.status = "Not checked yet"
        self.last_check = None
        self.icon = pystray.Icon(
            self.title, title=self.title, menu=pystray.Menu(self._menu_items)
        )
        self._set_icon(Icons.NO_UPDATES)

    def _menu_items(self):
        """Generate the context menu; re-evaluated by icon.update_menu()."""
        noop = lambda _: None  # noqa: E731
        status = self.status
        if self.last_check:
            status += f" (checked {time.strftime('%H:%M', self.last_check)})"
        yield pystray.MenuItem(status, noop, enabled=False)
        if self.updates:
            shown = self.updates[:MAX_MENU_PACKAGES]
            # Enabled (but no-op) so the list isn't rendered greyed out.
            items = [pystray.MenuItem(u.label(), noop) for u in shown]
            if hidden := len(self.updates) - len(shown):
                items.append(
                    pystray.MenuItem(f"\u2026 and {hidden} more", noop, enabled=False)
                )
            yield pystray.MenuItem("Upgradable packages", pystray.Menu(*items))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Check for updates", lambda _: self._check_updates(True))
        yield pystray.MenuItem(
            "Install updates",
            lambda _: self._install_updates(),
            enabled=bool(self.updates),
        )
        yield pystray.MenuItem(
            "Notifications",
            lambda _: self._toggle_notifications(),
            lambda _: self.notifications,
        )
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Quit", self._quit)

    def mainloop(self):
        self.log.debug(
            "Arch Update Indicator initialized with period: %s seconds", self.period
        )
        self.log.debug(
            "Terminal: %s, Update command: %s", self.terminal, self.update_cmd
        )
        self.log.debug("Notifications enabled: %s", self.notifications)
        self.log.debug("AUR helper: %s", self.aur_helper)
        self.log.debug("Icons folder: %s", self.icons_folder)
        self.icon.run(setup=self._setup)

    def _setup(self, icon):
        self.periodic_check_thread = threading.Thread(
            target=self._periodic_check_handler,
            name="PeriodicCheckThread",
            daemon=True,
        )
        self.periodic_check_thread.start()
        icon.visible = True
        self.log.debug("Setup complete.")

    def _quit(self):
        self.icon.stop()

    def _set_icon(self, icon_path):
        icon_path = self.icons_folder / icon_path
        if not icon_path.exists():
            self.log.error("Icon not found: %s", icon_path)
            return
        self.icon.icon = Image.open(icon_path)

    def _set_status(self, status):
        self.status = status
        self.icon.title = status
        self.icon.update_menu()

    def _toggle_notifications(self):
        self.notifications = not self.notifications
        self.log.debug("Notifications enabled: %s", self.notifications)

    def _notify(self, message, title=None):
        self.log.debug("Notify: %s", message)
        if self.notifications:
            self.icon.notify(message, title or self.title)

    def _periodic_check_handler(self):
        while True:
            self._check_updates()
            self.log.debug("Waiting for %s seconds before next check.", self.period)
            time.sleep(self.period)

    def _check_updates(self, check_notify=False):
        if self.update_check_thread and self.update_check_thread.is_alive():
            self.log.debug("Update check already in progress.")
            return
        if check_notify:
            self._notify("Checking for updates...")
        self.update_check_thread = threading.Thread(
            target=self._check_updates_handler,
            name="UpdateCheckThread",
            args=(check_notify,),
        )
        self.update_check_thread.start()

    def _run_check(self, cmd, source, no_updates_rc):
        """Run a check command. Returns a list of Update, or None on failure.

        no_updates_rc is the exit code meaning "nothing to upgrade"
        (checkupdates: 2, paru/yay -Qua: 1).
        """
        try:
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
        except OSError as e:
            self.log.error("%s failed: %s", cmd[0], e)
            return None
        self.log.debug("%s finished with return code: %s", cmd[0], proc.returncode)
        lines = proc.stdout.strip().splitlines()
        if proc.returncode == 0 or (proc.returncode == no_updates_rc and not lines):
            if err := proc.stderr.strip():
                self.log.warning("%s stderr: %s", cmd[0], err)
            return [Update(line, source) for line in lines if line.strip()]
        self.log.error("%s stderr: %s", cmd[0], proc.stderr.strip())
        return None

    def _check_updates_handler(self, check_notify=False):
        self.log.debug("Checking for updates...")
        updates = self._run_check(["checkupdates"], "repo", 2)
        if updates is not None and self.aur_helper:
            aur = self._run_check([self.aur_helper, "-Qua"], "AUR", 1)
            if aur is None:
                self.log.warning("AUR check failed; showing repo updates only.")
            else:
                updates += aur
        self.last_check = time.localtime()

        if updates is None:
            self._set_icon(Icons.CHECK_UPDATES_FAILED)
            self._set_status("Failed to check for updates")
            if check_notify:
                self._notify("Failed to check for updates!")
            return

        for u in updates:
            self.log.debug(u.line)
        changed = {u.key() for u in updates} != {u.key() for u in self.updates}
        self.updates = updates

        if updates:
            n = len(updates)
            summary = f"{n} update{'s' if n != 1 else ''} available"
            self._set_icon(Icons.UPDATES_AVAILABLE)
            self._set_status(summary)
            if check_notify or changed:
                names = [u.name for u in updates[:MAX_NOTIFY_PACKAGES]]
                if n > MAX_NOTIFY_PACKAGES:
                    names.append(f"\u2026 and {n - MAX_NOTIFY_PACKAGES} more")
                self._notify(", ".join(names), summary)
        else:
            self._set_icon(Icons.NO_UPDATES)
            self._set_status("System is up to date")
            if check_notify or changed:
                self._notify("No updates available.")

    def _install_updates(self):
        if self.install_updates_thread and self.install_updates_thread.is_alive():
            self.log.debug("Update installation already in progress.")
            return
        self.log.debug("Installing updates...")
        self.install_updates_thread = threading.Thread(
            target=self._install_updates_handler, name="InstallUpdatesThread"
        )
        self.install_updates_thread.start()

    def _install_updates_handler(self):
        # Close the terminal on success; keep it open on failure so the error
        # can be read.
        script = (
            f"{self.update_cmd} || {{ rc=$?; echo; "
            f"printf 'Update failed (exit code %s). Press Enter to close.' $rc; "
            f"read _; }}"
        )
        cmd = shlex.join(["sh", "-c", script])
        proc = subprocess.run(self.terminal.split() + ["-e", cmd], check=False)
        self.log.debug("Process finished with return code: %s", proc.returncode)
        self._check_updates(check_notify=True)


def main():
    parser = argparse.ArgumentParser(description="Arch Update Indicator")
    parser.add_argument(
        "-p",
        "--period",
        type=int,
        default=UPDATE_PERIOD,
        help="Set the update check period in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "-t",
        "--terminal",
        type=str,
        default=TERMINAL,
        help="Set the terminal emulator to use for installing updates (default: %(default)s)",
    )
    parser.add_argument(
        "-c",
        "--command",
        type=str,
        default=UPDATE_CMD,
        help="Set the command to run for installing updates (default: %(default)s)",
    )
    parser.add_argument(
        "-i",
        "--icons-folder",
        type=str,
        default=ICONS_FOLDER,
        help="Set the folder containing the icon images (default: %(default)s)",
    )
    parser.add_argument(
        "-a",
        "--aur-helper",
        type=str,
        default=AUR_HELPER,
        help="AUR helper used to check AUR updates: auto, none, paru, yay... "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "-n", "--no-notifications", action="store_true", help="Disable notifications"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose logging"
    )
    parser.add_argument(
        "-o", "--log-output", type=str, help="Set the log output file (default: stdout)"
    )
    args = parser.parse_args()

    if args.verbose or args.log_output:
        logging.getLogger("PIL").setLevel(logging.INFO)
        logging.basicConfig(
            level=logging.DEBUG,
            format="[%(levelname).4s] %(asctime)s (%(threadName)s): %(message)s",
            filename=args.log_output,
        )
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    icons_folder = Path(args.icons_folder)
    if not icons_folder.exists():
        sys.stderr.write(f"Error: Icons folder does not exist: {icons_folder}\n")
        return 1

    indicator = ArchUpdateIndicator(
        period=args.period,
        terminal=args.terminal,
        update_cmd=args.command,
        icons_folder=icons_folder,
        notifications=not args.no_notifications,
        aur_helper=detect_aur_helper(args.aur_helper),
    )
    indicator.mainloop()


if __name__ == "__main__":
    sys.exit(main())
