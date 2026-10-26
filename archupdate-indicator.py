#!/usr/bin/env python

"""Creates a taskbar icon that indicates if updates are available and provides a
context menu to install them.

Dependencies:
 * pacman-contrib to check for updates
 * python-pystray for the icon and the context menu
"""

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pystray
from PIL import Image

HERE = Path(__file__).parent

UPDATE_PERIOD = int(os.getenv("UPDATE_PERIOD", "1800"))
TERMINAL = os.getenv("TERMINAL", "xfce4-terminal")
UPDATE_CMD = os.getenv("UPDATE_CMD", "sudo pacman -Syu")
ICONS_FOLDER = Path(os.getenv("ICONS_FOLDER", str(HERE / "icons")))


class Icons:
    NO_UPDATES = "no_updates.png"
    UPDATES_AVAILABLE = "updates_available.png"
    CHECK_UPDATES_FAILED = "updates_failed.png"


class ArchUpdateIndicator:
    log = logging.getLogger("ArchUpdateIndicator")
    title = "Arch Update Indicator"

    def __init__(self, period, terminal, update_cmd, icons_folder, notifications):
        self.period = period
        self.terminal = terminal
        self.update_cmd = update_cmd
        self.icons_folder = icons_folder
        self.notifications = notifications
        self.update_check_thread = None
        self.install_updates_thread = None
        self.periodic_check_thread = None
        self.updates = []
        menu = pystray.Menu(
            pystray.MenuItem("Check for updates", lambda _: self._check_updates(True)),
            pystray.MenuItem("Install updates", lambda _: self._install_updates()),
            pystray.MenuItem(
                "Notifications",
                lambda _: self._toggle_notifications(),
                lambda _: self.notifications,
            ),
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon(self.title, title=self.title, menu=menu)
        self._set_icon(Icons.NO_UPDATES)

    def mainloop(self):
        self.log.debug(
            "Arch Update Indicator initialized with period: %s seconds", self.period
        )
        self.log.debug(
            "Terminal: %s, Update command: %s", self.terminal, self.update_cmd
        )
        self.log.debug("Notifications enabled: %s", self.notifications)
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

    def _set_title(self, title):
        self.icon.title = title

    def _toggle_notifications(self):
        self.notifications = not self.notifications
        self.log.debug("Notifications enabled: %s", self.notifications)

    def _notify(self, message):
        self.log.debug(f"Notify: {message}")
        if self.notifications:
            self.icon.notify(message, self.title)

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

    def _check_updates_handler(self, check_notify=False):
        self.log.debug("Checking for updates...")
        proc = subprocess.run(
            ["checkupdates"], check=False, capture_output=True, text=True
        )
        self.log.debug("Process finished with return code: %s", proc.returncode)

        if proc.returncode == 0:
            # Updates available
            updates = proc.stdout.strip().splitlines()
            for s in updates:
                self.log.debug(s)
            if err := proc.stderr.strip():
                self.log.warning("checkupdates stderr: %s", err)
            self._set_icon(Icons.UPDATES_AVAILABLE)
            self._set_title(f"{len(updates)} updates available")
            if check_notify or len(updates) != len(self.updates):
                self._notify(f"{len(updates)} updates available!")
            self.updates = updates
        elif proc.returncode == 2:
            # No updates available
            self.updates = []
            self._set_icon(Icons.NO_UPDATES)
            self._set_title("No updates available")
            if check_notify:
                self._notify("No updates available.")
        else:
            # Check for updates failed
            self._set_icon(Icons.CHECK_UPDATES_FAILED)
            self._set_title("Failed to check for updates")
            if check_notify:
                self._notify("Failed to check for updates!")

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
        proc = subprocess.run([self.terminal, "-e", self.update_cmd], check=False)
        self.log.debug("Process finished with return code: %s", proc.returncode)
        self._check_updates()


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
    )
    indicator.mainloop()


if __name__ == "__main__":
    sys.exit(main())
