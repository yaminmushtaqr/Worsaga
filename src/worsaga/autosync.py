"""Register, inspect, and remove a scheduled background sync.

``worsaga auto-sync install`` registers a periodic ``worsaga sync
--quiet`` with the platform's native scheduler; ``status`` inspects it
read-only; ``remove`` cleanly unregisters it. One mechanism per
platform:

- **Windows** — a Task Scheduler task (``schtasks``) named
  ``WorsagaAutoSync``.
- **macOS** — a launchd user LaunchAgent
  (``~/Library/LaunchAgents/com.worsaga.autosync.plist``).
- **Linux** — a systemd *user* service+timer pair
  (``worsaga-autosync.service`` / ``.timer``); systems without a user
  systemd get a structured "unsupported" result suggesting cron.

Every operation returns a structured dict and records an ``actions``
list of exactly what was (or, with ``dry_run=True``, would be) executed
or written, so agents and humans can review the change before and
after it happens. The scheduled command line contains **no
credentials** — the sync run loads configuration the way any other
invocation does — and all subprocess calls run without a shell and
with a timeout.

Alongside the scheduler entry, install writes a small local metadata
record (``autosync.json`` in the user data directory) so ``status``
can report the intended interval and command without parsing
locale-dependent scheduler output.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import platformdirs

_APP_NAME = "worsaga"

WINDOWS_TASK_NAME = "WorsagaAutoSync"
MACOS_LABEL = "com.worsaga.autosync"
LINUX_UNIT = "worsaga-autosync"

DEFAULT_INTERVAL_MINUTES = 30
#: schtasks /SC MINUTE accepts 1-1439; 5 is a politeness floor.
MIN_INTERVAL_MINUTES = 5
MAX_INTERVAL_MINUTES = 1439

_SUBPROCESS_TIMEOUT = 30

_MACOS_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{arguments}
    </array>
    <key>StartInterval</key>
    <integer>{interval_seconds}</integer>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
"""

_LINUX_SERVICE_TEMPLATE = """[Unit]
Description=Worsaga metadata sync

[Service]
Type=oneshot
ExecStart={exec_start}
"""

_LINUX_TIMER_TEMPLATE = """[Unit]
Description=Run the Worsaga metadata sync periodically

[Timer]
OnBootSec=5min
OnUnitActiveSec={interval_minutes}min

[Install]
WantedBy=timers.target
"""


def autosync_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def sync_command() -> list[str]:
    """Return the command the scheduler should run periodically.

    Prefers the installed ``worsaga`` entry point; falls back to the
    current interpreter with ``-m worsaga.cli``. Never contains
    credentials.
    """
    exe = shutil.which("worsaga")
    if exe:
        return [exe, "sync", "--quiet"]
    return [sys.executable, "-m", "worsaga.cli", "sync", "--quiet"]


def autosync_record_path() -> Path:
    """Path of the local install metadata record."""
    return Path(platformdirs.user_data_dir(_APP_NAME)) / "autosync.json"


def _read_record() -> dict[str, Any] | None:
    try:
        with open(autosync_record_path(), encoding="utf-8") as f:
            record = json.load(f)
        return record if isinstance(record, dict) else None
    except (OSError, ValueError):
        return None


def _write_record(record: dict[str, Any]) -> None:
    path = autosync_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )


def _delete_record() -> None:
    autosync_record_path().unlink(missing_ok=True)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
    )


def _clamp_interval(interval_minutes: int) -> int:
    return max(
        MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, int(interval_minutes))
    )


def _windows_task_command(command: list[str]) -> str:
    """Render the /TR command string, quoting arguments with spaces."""
    return " ".join(
        f'"{part}"' if " " in part else part for part in command
    )


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MACOS_LABEL}.plist"


def _linux_unit_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(config_home) if config_home else Path.home() / ".config"
    return base / "systemd" / "user"


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _systemd_quote(part: str) -> str:
    """Quote one ExecStart argument for a systemd unit file."""
    if any(ch in part for ch in ' \t"\\'):
        escaped = part.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return part


# ── Install ──────────────────────────────────────────────────────


def install_autosync(
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Register the periodic background sync with the platform scheduler.

    With ``dry_run=True`` nothing is executed or written; the returned
    ``actions`` list shows exactly what a real install would do.
    """
    interval_minutes = _clamp_interval(interval_minutes)
    platform = autosync_platform()
    command = sync_command()
    result: dict[str, Any] = {
        "installed": False,
        "dry_run": dry_run,
        "platform": platform,
        "interval_minutes": interval_minutes,
        "command": command,
        "actions": [],
    }

    if platform == "windows":
        create = [
            "schtasks", "/Create", "/F",
            "/SC", "MINUTE", "/MO", str(interval_minutes),
            "/TN", WINDOWS_TASK_NAME,
            "/TR", _windows_task_command(command),
        ]
        result["method"] = "schtasks"
        result["actions"].append({"run": create})
        if not dry_run:
            proc = _run(create)
            if proc.returncode != 0:
                result["error"] = (proc.stderr or proc.stdout).strip()
                return result

    elif platform == "macos":
        plist_path = _macos_plist_path()
        arguments = "\n".join(
            f"        <string>{_xml_escape(part)}</string>" for part in command
        )
        plist = _MACOS_PLIST_TEMPLATE.format(
            label=MACOS_LABEL,
            arguments=arguments,
            interval_seconds=interval_minutes * 60,
        )
        result["method"] = "launchd"
        result["actions"].append({"write": str(plist_path), "content": plist})
        result["actions"].append(
            {"run": ["launchctl", "load", "-w", str(plist_path)]}
        )
        if not dry_run:
            plist_path.parent.mkdir(parents=True, exist_ok=True)
            plist_path.write_text(plist, encoding="utf-8")
            # A stale agent with the same label must be unloaded first;
            # failure here just means nothing was loaded.
            _run(["launchctl", "unload", str(plist_path)])
            proc = _run(["launchctl", "load", "-w", str(plist_path)])
            if proc.returncode != 0:
                result["error"] = (proc.stderr or proc.stdout).strip()
                return result

    else:
        if not shutil.which("systemctl"):
            result["method"] = "none"
            result["error"] = (
                "no user systemd found; schedule "
                f"'{' '.join(command)}' with cron instead"
            )
            return result
        unit_dir = _linux_unit_dir()
        service_path = unit_dir / f"{LINUX_UNIT}.service"
        timer_path = unit_dir / f"{LINUX_UNIT}.timer"
        service = _LINUX_SERVICE_TEMPLATE.format(
            exec_start=" ".join(_systemd_quote(part) for part in command)
        )
        timer = _LINUX_TIMER_TEMPLATE.format(interval_minutes=interval_minutes)
        enable = [
            "systemctl", "--user", "enable", "--now", f"{LINUX_UNIT}.timer",
        ]
        result["method"] = "systemd-user"
        result["actions"].append(
            {"write": str(service_path), "content": service}
        )
        result["actions"].append({"write": str(timer_path), "content": timer})
        result["actions"].append({"run": ["systemctl", "--user", "daemon-reload"]})
        result["actions"].append({"run": enable})
        if not dry_run:
            unit_dir.mkdir(parents=True, exist_ok=True)
            service_path.write_text(service, encoding="utf-8")
            timer_path.write_text(timer, encoding="utf-8")
            _run(["systemctl", "--user", "daemon-reload"])
            proc = _run(enable)
            if proc.returncode != 0:
                result["error"] = (proc.stderr or proc.stdout).strip()
                return result

    if not dry_run:
        _write_record({
            "platform": platform,
            "method": result["method"],
            "interval_minutes": interval_minutes,
            "command": command,
            "installed_at": int(time.time()),
        })
        result["installed"] = True
        # Schedulers can accept a registration without guaranteeing the
        # job will run (schtasks documents this explicitly), so re-query
        # immediately instead of trusting the create call alone.
        result["verified"] = bool(autosync_status().get("installed"))
        if not result["verified"]:
            result["warning"] = (
                "the scheduler does not report the job as registered "
                "after install; check it manually"
            )
    return result


# ── Status ───────────────────────────────────────────────────────


def autosync_status() -> dict[str, Any]:
    """Report whether the background sync is registered (read-only)."""
    platform = autosync_platform()
    record = _read_record()
    result: dict[str, Any] = {
        "platform": platform,
        "installed": False,
        "record": record,
    }

    if platform == "windows":
        result["method"] = "schtasks"
        try:
            proc = _run(["schtasks", "/Query", "/TN", WINDOWS_TASK_NAME])
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["error"] = str(exc)
            return result
        result["installed"] = proc.returncode == 0

    elif platform == "macos":
        result["method"] = "launchd"
        plist_path = _macos_plist_path()
        result["plist"] = str(plist_path)
        if plist_path.is_file():
            try:
                proc = _run(["launchctl", "list", MACOS_LABEL])
            except (OSError, subprocess.TimeoutExpired) as exc:
                result["error"] = str(exc)
                return result
            result["installed"] = proc.returncode == 0

    else:
        if not shutil.which("systemctl"):
            result["method"] = "none"
            result["error"] = "no user systemd found on this system"
            return result
        result["method"] = "systemd-user"
        try:
            proc = _run(
                ["systemctl", "--user", "is-active", f"{LINUX_UNIT}.timer"]
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result["error"] = str(exc)
            return result
        result["installed"] = proc.returncode == 0

    _attach_last_sync(result)
    return result


def _attach_last_sync(result: dict[str, Any]) -> None:
    """Attach the site's most recent sync time, read-only.

    The timestamp covers **any** sync — manual or scheduled; the cache
    records no provenance — so it shows the data is moving, not that
    the scheduler specifically ran. Uses the read-only cache reader
    (status must never create the cache as a side effect) and is
    skipped silently when no site is configured or no cache exists.
    """
    try:
        from worsaga.cache import read_last_sync_at
        from worsaga.client import MoodleClient
        from worsaga.config import MoodleConfig
        from worsaga.demo import DemoMoodleClient, demo_mode_enabled

        client = (
            DemoMoodleClient() if demo_mode_enabled()
            else MoodleClient(MoodleConfig.load())
        )
        ts = read_last_sync_at(client.base_url)
        if ts is not None:
            result["last_sync_at"] = ts
    except Exception:
        pass


# ── Remove ───────────────────────────────────────────────────────


def remove_autosync(*, dry_run: bool = False) -> dict[str, Any]:
    """Unregister the background sync and delete its metadata record.

    Scheduler state is treated as three-valued: *installed*, *absent*
    (the scheduler answered and knows no such job), or *unknown* (the
    scheduler could not be queried at all). A real removal aborts
    without touching anything on *unknown* — deleting local state while
    a job may still be active would orphan it.
    """
    platform = autosync_platform()
    result: dict[str, Any] = {
        "removed": False,
        "dry_run": dry_run,
        "platform": platform,
        "actions": [],
    }
    status = autosync_status()
    # An error from a real scheduler backend means state is unknown; a
    # "none" method (no user systemd) is a *known absent* scheduler and
    # the record-only removal below is still safe.
    if (
        status.get("error")
        and status.get("method") != "none"
        and not dry_run
    ):
        result["method"] = status.get("method", "")
        result["error"] = (
            "cannot determine scheduler state, so nothing was removed: "
            f"{status['error']}"
        )
        return result
    was_installed = bool(status.get("installed"))

    if platform == "windows":
        delete = ["schtasks", "/Delete", "/F", "/TN", WINDOWS_TASK_NAME]
        result["method"] = "schtasks"
        result["actions"].append({"run": delete})
        if not dry_run and was_installed:
            proc = _run(delete)
            if proc.returncode != 0:
                result["error"] = (proc.stderr or proc.stdout).strip()
                return result

    elif platform == "macos":
        plist_path = _macos_plist_path()
        unload = ["launchctl", "unload", "-w", str(plist_path)]
        result["method"] = "launchd"
        result["actions"].append({"run": unload})
        result["actions"].append({"delete": str(plist_path)})
        if not dry_run and plist_path.is_file():
            if was_installed:
                # The agent is genuinely loaded: a failed unload means
                # the job is still active — report it and change
                # nothing, rather than deleting the plist and claiming
                # success while an orphaned job keeps running.
                proc = _run(unload)
                if proc.returncode != 0:
                    result["error"] = (proc.stderr or proc.stdout).strip()
                    return result
            plist_path.unlink(missing_ok=True)

    else:
        if not shutil.which("systemctl"):
            result["method"] = "none"
            result["actions"].append({"delete": str(autosync_record_path())})
            if not dry_run:
                _delete_record()
                result["removed"] = True
                result["was_installed"] = was_installed
            return result
        unit_dir = _linux_unit_dir()
        disable = [
            "systemctl", "--user", "disable", "--now", f"{LINUX_UNIT}.timer",
        ]
        result["method"] = "systemd-user"
        result["actions"].append({"run": disable})
        result["actions"].append(
            {"delete": str(unit_dir / f"{LINUX_UNIT}.service")}
        )
        result["actions"].append(
            {"delete": str(unit_dir / f"{LINUX_UNIT}.timer")}
        )
        result["actions"].append({"run": ["systemctl", "--user", "daemon-reload"]})
        if not dry_run:
            if was_installed:
                # Same contract as macOS: never delete unit files while
                # the timer is still active.
                proc = _run(disable)
                if proc.returncode != 0:
                    result["error"] = (proc.stderr or proc.stdout).strip()
                    return result
            (unit_dir / f"{LINUX_UNIT}.service").unlink(missing_ok=True)
            (unit_dir / f"{LINUX_UNIT}.timer").unlink(missing_ok=True)
            reload_proc = _run(["systemctl", "--user", "daemon-reload"])
            if reload_proc.returncode != 0:
                result["warning"] = (
                    "unit files removed but 'systemctl --user "
                    "daemon-reload' failed: "
                    + (reload_proc.stderr or reload_proc.stdout).strip()
                )

    if not dry_run:
        _delete_record()
        result["removed"] = True
        result["was_installed"] = was_installed
    return result


__all__ = [
    "DEFAULT_INTERVAL_MINUTES",
    "MIN_INTERVAL_MINUTES",
    "autosync_platform",
    "autosync_record_path",
    "autosync_status",
    "install_autosync",
    "remove_autosync",
    "sync_command",
]
