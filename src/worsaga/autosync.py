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
locale-dependent scheduler output. The record is owner-only, records the
authenticated user id when one is known, and install refuses to overwrite
a record belonging to a different Moodle account (see
:mod:`worsaga.principal`). Every scheduler subprocess runs with the API
token stripped from its environment (see :func:`worsaga.secureio.child_env`).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import platformdirs

from worsaga.principal import (
    PrincipalMismatchError,
    bind_principal,
    known_principal,
)
from worsaga.secureio import child_env, write_private_file

_APP_NAME = "worsaga"

#: What to tell a user whose auto-sync record belongs to another account.
_RECORD_REMEDY = (
    "Run 'worsaga auto-sync remove' (or 'remove --force-local' if the "
    "scheduler cannot be queried) to clear it, then install again."
)

WINDOWS_TASK_NAME = "WorsagaAutoSync"
MACOS_LABEL = "com.worsaga.autosync"
LINUX_UNIT = "worsaga-autosync"

DEFAULT_INTERVAL_MINUTES = 30
#: schtasks /SC MINUTE accepts 1-1439; 15 is a politeness floor. A metadata
#: sync fans out over every enrolled course, so a shorter period buys no
#: freshness worth the load it puts on a shared Moodle instance.
MIN_INTERVAL_MINUTES = 15
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


def _stale_record_path() -> Path:
    """Path used to quarantine metadata from an earlier installation."""
    path = autosync_record_path()
    return path.with_name(f"{path.name}.stale")


def _read_record_with_error() -> tuple[dict[str, Any] | None, str | None]:
    """Return the local record and any read/validation error.

    A missing file is a known-absent record and returns ``(None, None)``.
    Every other read or validation failure remains distinguishable from
    absence so scheduler removal can fail closed instead of discarding the
    only evidence that a background job may still be registered.
    """
    try:
        with open(autosync_record_path(), encoding="utf-8") as f:
            record = json.load(f)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        return None, f"cannot read local auto-sync record: {exc}"
    except ValueError as exc:
        return None, f"invalid local auto-sync record: {exc}"
    if not isinstance(record, dict):
        return None, "invalid local auto-sync record: expected a JSON object"
    return record, None


def _write_record(record: dict[str, Any]) -> None:
    """Write the record atomically, owner-only (temp file + rename).

    A crash mid-write must never leave a truncated record: an
    unreadable record makes scheduler removal fail closed until the
    user reaches for ``--force-local``.
    """
    write_private_file(
        autosync_record_path(),
        json.dumps(record, indent=2, sort_keys=True) + "\n",
    )


def _stage_existing_record() -> tuple[bool, str | None]:
    """Quarantine the current record before changing the scheduler.

    Reinstalling replaces an existing scheduler entry. Moving its metadata
    aside first ensures a later record-write failure cannot leave the old
    interval and command looking current. The move is atomic and happens
    before any scheduler mutation; if it cannot be completed, installation
    aborts while the old scheduler and record are still untouched.
    """
    path = autosync_record_path()
    try:
        record_stat = path.lstat()
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return False, str(exc)
    if not stat.S_ISREG(record_stat.st_mode):
        return False, "the record path is not a regular file"
    try:
        os.replace(path, _stale_record_path())
    except FileNotFoundError:
        # Another process removed it after lstat; there is nothing stale.
        return False, None
    except OSError as exc:
        return False, str(exc)
    return True, None


def _local_client() -> Any | None:
    """Build the configured (or demo) client, or None if that fails.

    Configuration only — building a client makes no network request.
    Used for the two things this module wants to know about the local
    account without going online: which site it is, and whether an
    identity has already been verified in this process.
    """
    try:
        from worsaga.client import MoodleClient
        from worsaga.config import MoodleConfig
        from worsaga.demo import DemoMoodleClient, demo_mode_enabled

        if demo_mode_enabled():
            return DemoMoodleClient()
        return MoodleClient(MoodleConfig.load())
    except Exception:
        return None


def _local_binding() -> tuple[int | None, str]:
    """Return ``(known user id, site)`` for the local account, offline.

    Deliberately uses :func:`worsaga.principal.known_principal` rather
    than forcing verification: registering a scheduled job must keep
    working with no network and with credentials that are not set up
    yet, so the record is bound opportunistically. The record holds no
    account data of its own — the guards that carry weight are on the
    cache and the index, which do.
    """
    client = _local_client()
    if client is None:
        return None, ""
    try:
        return known_principal(client), str(client.base_url)
    except Exception:
        return None, ""


def _record_principal(record: dict[str, Any] | None) -> int | None:
    """Return the Moodle user id stamped on *record*, if any."""
    if not record:
        return None
    try:
        return int(record.get("principal_userid") or 0) or None
    except (TypeError, ValueError):
        return None


def _binding_fields(principal: int | None, site: str) -> dict[str, Any]:
    """Return the record fields describing an account binding.

    The site is recorded whenever it is known — it comes from
    configuration and costs no network access — so a later install can
    tell "same Moodle, other account" (a conflict) from "different
    Moodle entirely" (a stale binding to discard).
    """
    fields: dict[str, Any] = {}
    if site:
        fields["site"] = site
    if principal is not None:
        fields["principal_userid"] = int(principal)
    return fields


def _record_binding(principal: int | None, site: str) -> dict[str, Any]:
    """Return the binding fields a rewritten record should carry.

    Raises :class:`worsaga.principal.PrincipalMismatchError` when the
    existing record was installed by a different Moodle account on the
    same site. An unreadable record is treated as unstamped: install
    already replaces it, and failing closed here would leave the user
    with no way to reinstall short of deleting the file by hand.

    Install rewrites the record wholesale, so a binding that still
    applies is carried forward when this run could not verify an
    identity — otherwise an offline reinstall would quietly unbind the
    record and let the next account adopt it. A binding recorded for a
    *different* site no longer applies to anything and is dropped rather
    than carried onto the new one.
    """
    record, _ = _read_record_with_error()
    stored = _record_principal(record)
    stored_site = str(record.get("site") or "") if record else ""
    # A record written against another Moodle describes an account this
    # install has nothing to do with: stale, not a conflict. A record with
    # no site at all predates site-stamping and is treated as this site's.
    if stored_site and site and stored_site != site:
        stored = None
    if principal is None:
        return _binding_fields(stored, site or stored_site)
    bind_principal(
        stored=stored,
        principal=principal,
        site=site or "the configured site",
        store_label="auto-sync record",
        store_path=str(autosync_record_path()),
        remedy=_RECORD_REMEDY,
        holds_data=record is not None,
    )
    return _binding_fields(principal, site)


def _stale_record_state() -> tuple[bool, str | None]:
    """Return whether quarantined metadata exists, without mutating it."""
    try:
        _stale_record_path().stat()
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        # An uninspectable marker is still not proof of absence.
        return True, str(exc)
    return True, None


def _delete_record() -> None:
    autosync_record_path().unlink(missing_ok=True)
    _stale_record_path().unlink(missing_ok=True)


def _run(args: list[str]) -> subprocess.CompletedProcess:
    # env=: schedulers need the ordinary environment, but none of them
    # needs WORSAGA_TOKEN, so the child never gets a copy of it.
    return subprocess.run(
        args, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT,
        env=child_env(),
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
    principal: int | None = None,
    site: str = "",
) -> dict[str, Any]:
    """Register the periodic background sync with the platform scheduler.

    With ``dry_run=True`` nothing is executed or written; the returned
    ``actions`` list shows exactly what a real install would do.

    *principal* and *site* bind the record to a Moodle account; when
    neither is given they are resolved locally with no network access
    (see :func:`_local_binding`). The site is recorded whenever it is
    configured; the user id only when this process has already verified
    one. An existing record installed by a *different* account for the
    same site makes this a structured failure that changes nothing;
    a record for a different site is discarded as stale. Installing
    without a verifiable identity (offline, no credentials yet) is still
    allowed and simply leaves the record without a user id.
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

    if principal is None and not site:
        principal, site = _local_binding()
    # Checked before the scheduler is touched, in dry runs too: a dry run
    # that reported a clean plan for an install that cannot happen would
    # be worse than useless.
    try:
        binding = _record_binding(principal, site)
    except PrincipalMismatchError as exc:
        result["error"] = str(exc)
        return result

    record_staged = False

    def _prepare_record() -> bool:
        nonlocal record_staged
        record_staged, error = _stage_existing_record()
        if error:
            result["record_written"] = False
            result["record_stale"] = False
            result["error"] = (
                "cannot safely invalidate the existing local auto-sync "
                f"record before changing the scheduler: {error}. "
                "The scheduler was not changed."
            )
            return False
        return True

    def _mark_scheduler_failure(message: str) -> None:
        result["error"] = message
        stale_marker, _ = _stale_record_state()
        if record_staged or stale_marker:
            result["record_stale"] = True
            result["record_error"] = (
                "the previous local auto-sync record was invalidated before "
                "the reinstall; status will omit interval/command detail "
                "until a later successful install"
            )

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
            if not _prepare_record():
                return result
            try:
                proc = _run(create)
            except (OSError, subprocess.TimeoutExpired) as exc:
                _mark_scheduler_failure(str(exc))
                return result
            if proc.returncode != 0:
                _mark_scheduler_failure((proc.stderr or proc.stdout).strip())
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
            if not _prepare_record():
                return result
            try:
                plist_path.parent.mkdir(parents=True, exist_ok=True)
                plist_path.write_text(plist, encoding="utf-8")
                # A stale agent with the same label must be unloaded first;
                # failure here just means nothing was loaded.
                _run(["launchctl", "unload", str(plist_path)])
                proc = _run(["launchctl", "load", "-w", str(plist_path)])
            except (OSError, subprocess.TimeoutExpired) as exc:
                _mark_scheduler_failure(str(exc))
                return result
            if proc.returncode != 0:
                _mark_scheduler_failure((proc.stderr or proc.stdout).strip())
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
            if not _prepare_record():
                return result
            try:
                unit_dir.mkdir(parents=True, exist_ok=True)
                service_path.write_text(service, encoding="utf-8")
                timer_path.write_text(timer, encoding="utf-8")
                _run(["systemctl", "--user", "daemon-reload"])
                proc = _run(enable)
            except (OSError, subprocess.TimeoutExpired) as exc:
                _mark_scheduler_failure(str(exc))
                return result
            if proc.returncode != 0:
                _mark_scheduler_failure((proc.stderr or proc.stdout).strip())
                return result

    if not dry_run:
        # The scheduler entry is already registered at this point, so a
        # failed record write must never escape as an exception — the
        # caller would get no structured result while the job silently
        # stays active. A prior record was quarantined before the scheduler
        # mutation, so a failed rewrite cannot leave old metadata looking
        # current to subsequent status calls.
        result["installed"] = True
        record: dict[str, Any] = {
            "platform": platform,
            "method": result["method"],
            "interval_minutes": interval_minutes,
            "command": command,
            "installed_at": int(time.time()),
            **binding,
        }
        try:
            _write_record(record)
            result["record_written"] = True
            result["record_stale"] = False
            try:
                _stale_record_path().unlink(missing_ok=True)
            except OSError as exc:
                # The new record is authoritative, so a leftover quarantine
                # file is harmless but should still be visible to callers.
                result["record_cleanup_error"] = (
                    "the new local auto-sync record was written, but old "
                    f"quarantined metadata could not be deleted: {exc}"
                )
        except OSError as exc:
            result["record_written"] = False
            stale_marker, stale_error = _stale_record_state()
            result["record_stale"] = record_staged or stale_marker
            detail = (
                "the previous record was invalidated before registration"
                if result["record_stale"]
                else "no current local record is available"
            )
            result["record_error"] = (
                "the scheduler entry was registered, but the local "
                f"auto-sync record could not be written: {exc}. "
                f"Because {detail}, 'worsaga auto-sync status' will omit "
                "interval/command detail; scheduler state is still queried "
                "live."
            )
            if stale_error:
                result["record_error"] += (
                    " The invalidated record could not be inspected: "
                    f"{stale_error}."
                )
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


def _windows_state() -> tuple[str, str | None, dict[str, Any]]:
    """Classify the Windows task state from a full CSV task listing.

    ``schtasks /Query /TN`` exits nonzero both for "no such task" and
    for real failures (access denied), and its messages are localized —
    so absence is only ever concluded from a *successful* full listing
    that does not contain the task's exact quoted CSV path field.
    """
    try:
        proc = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", str(exc), {}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return "unknown", detail or f"schtasks exit {proc.returncode}", {}
    present = f'"\\{WINDOWS_TASK_NAME}"' in proc.stdout
    return ("installed" if present else "absent"), None, {}


def _macos_state() -> tuple[str, str | None, dict[str, Any]]:
    """Classify the launchd job state from the full job listing.

    Queried regardless of whether the plist file exists — a job can
    stay loaded after its plist is deleted. ``launchctl list`` prints
    the label as the last column, so membership is an exact token
    match, never exit-code guesswork.
    """
    try:
        proc = _run(["launchctl", "list"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", str(exc), {}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return "unknown", detail or f"launchctl exit {proc.returncode}", {}
    loaded = any(
        line.split()[-1] == MACOS_LABEL
        for line in proc.stdout.splitlines()
        if line.strip()
    )
    return ("installed" if loaded else "absent"), None, {}


def _linux_state() -> tuple[str, str | None, dict[str, Any]]:
    """Classify the systemd user timer via machine-readable properties.

    ``systemctl show`` reports fixed enum values: ``LoadState=loaded``
    (registered — including enabled-but-inactive or failed timers,
    which ``is-active`` would misclassify) vs ``not-found`` (proven
    absent). ``timer_active`` reports the live ActiveState separately.
    """
    try:
        proc = _run([
            "systemctl", "--user", "show", f"{LINUX_UNIT}.timer",
            "-p", "LoadState", "-p", "ActiveState",
        ])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "unknown", str(exc), {}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return "unknown", detail or f"systemctl exit {proc.returncode}", {}
    props: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key.strip()] = value.strip()
    load_state = props.get("LoadState", "")
    if load_state == "not-found":
        return "absent", None, {}
    if load_state in ("loaded", "masked"):
        return "installed", None, {
            "timer_active": props.get("ActiveState") == "active",
        }
    return "unknown", f"unexpected LoadState '{load_state or '?'}'", {}


def autosync_status() -> dict[str, Any]:
    """Report whether the background sync is registered (read-only).

    ``state`` is three-valued: ``"installed"`` (the scheduler
    affirmatively lists the job), ``"absent"`` (the scheduler answered
    and the job is not there), or ``"unknown"`` (the scheduler could
    not be queried — missing binary, permissions, timeouts). Absence is
    only ever concluded from machine-readable evidence — a successful
    full listing without the job, or ``LoadState=not-found`` — never
    from a bare nonzero exit code. ``installed`` is the boolean
    ``state == "installed"``.
    """
    platform = autosync_platform()
    record, record_error = _read_record_with_error()
    stale_record = False
    stale_error = None
    if record is None:
        stale_record, stale_error = _stale_record_state()
    result: dict[str, Any] = {
        "platform": platform,
        "installed": False,
        "state": "unknown",
        "record": record,
        "record_stale": stale_record,
    }
    if stale_record:
        stale_message = (
            "local auto-sync metadata was invalidated during an incomplete "
            "install; interval/command detail is unavailable"
        )
        record_error = (
            f"{record_error}; {stale_message}" if record_error
            else stale_message
        )
    if stale_error:
        record_error = f"{record_error}: {stale_error}"
    if record_error:
        result["record_error"] = record_error

    if platform == "windows":
        result["method"] = "schtasks"
        state, error, extra = _windows_state()
    elif platform == "macos":
        result["method"] = "launchd"
        plist_path = _macos_plist_path()
        result["plist"] = str(plist_path)
        result["plist_exists"] = plist_path.is_file()
        state, error, extra = _macos_state()
    else:
        if not shutil.which("systemctl"):
            result["method"] = "none"
            result["error"] = "systemctl not found; cannot inspect user systemd"
            _attach_last_sync(result)
            return result
        result["method"] = "systemd-user"
        state, error, extra = _linux_state()

    result["state"] = state
    result["installed"] = state == "installed"
    if error:
        result["error"] = error
    result.update(extra)
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

        client = _local_client()
        if client is None:
            return
        ts = read_last_sync_at(client.base_url)
        if ts is not None:
            result["last_sync_at"] = ts
    except Exception:
        pass


# ── Remove ───────────────────────────────────────────────────────


_MANUAL_REMOVE_HINTS = {
    "windows": f"schtasks /Delete /F /TN {WINDOWS_TASK_NAME}",
    "macos": f"launchctl remove {MACOS_LABEL}",
    "linux": f"systemctl --user disable --now {LINUX_UNIT}.timer",
}


def remove_autosync(
    *, dry_run: bool = False, force_local: bool = False,
) -> dict[str, Any]:
    """Unregister the background sync and delete its metadata record.

    Scheduler state is treated as three-valued: *installed*, *absent*
    (the scheduler answered and knows no such job), or *unknown* (the
    scheduler could not be queried at all). A real removal aborts
    without touching anything on *unknown* — deleting local state while
    a job may still be active would orphan it.

    ``force_local=True`` is the explicit escape hatch for machines
    where the scheduler cannot be queried but stale local state must
    go: it deletes only Worsaga's own files (metadata record, plist,
    unit files), never queries or changes the scheduler, and says so
    in the result.
    """
    platform = autosync_platform()
    result: dict[str, Any] = {
        "removed": False,
        "dry_run": dry_run,
        "platform": platform,
        "actions": [],
    }

    if force_local:
        result["method"] = "local-only"
        result["scheduler_untouched"] = True
        targets = [autosync_record_path(), _stale_record_path()]
        if platform == "macos":
            targets.insert(0, _macos_plist_path())
        elif platform == "linux":
            unit_dir = _linux_unit_dir()
            targets = [
                unit_dir / f"{LINUX_UNIT}.service",
                unit_dir / f"{LINUX_UNIT}.timer",
                autosync_record_path(),
                _stale_record_path(),
            ]
        for target in targets:
            result["actions"].append({"delete": str(target)})
        if not dry_run:
            # Deletion failures stay structured: report what could not
            # be removed instead of raising mid-cleanup.
            failures = []
            for target in targets:
                try:
                    Path(target).unlink(missing_ok=True)
                except OSError as exc:
                    failures.append(f"{target}: {exc}")
            if failures:
                result["error"] = (
                    "some local files could not be deleted: "
                    + "; ".join(failures)
                )
            else:
                result["removed"] = True
        result["warning"] = (
            "--force-local does not verify or change scheduler state. "
            "If the job is still registered, remove it manually: "
            f"{_MANUAL_REMOVE_HINTS[platform]}"
        )
        return result

    status = autosync_status()
    state = status.get("state", "unknown")
    # A real removal must know the scheduler's answer. "unknown" from a
    # real backend aborts before touching anything; the no-user-systemd
    # case (method "none") is decided in the Linux branch from local
    # unit-file evidence instead.
    if state == "unknown" and status.get("method") != "none" and not dry_run:
        result["method"] = status.get("method", "")
        result["error"] = (
            "cannot determine scheduler state, so nothing was removed"
            + (f": {status['error']}" if status.get("error") else "")
        )
        return result
    was_installed = state == "installed"

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
        # A job can stay loaded after its plist was deleted; stop it by
        # label in that case instead of skipping the unload entirely.
        stop_cmd = (
            ["launchctl", "unload", "-w", str(plist_path)]
            if plist_path.is_file()
            else ["launchctl", "remove", MACOS_LABEL]
        )
        result["method"] = "launchd"
        result["actions"].append({"run": stop_cmd})
        result["actions"].append({"delete": str(plist_path)})
        if not dry_run:
            if was_installed:
                # The agent is genuinely loaded: a failed stop means
                # the job is still active — report it and change
                # nothing, rather than deleting the plist and claiming
                # success while an orphaned job keeps running.
                proc = _run(stop_cmd)
                if proc.returncode != 0:
                    result["error"] = (proc.stderr or proc.stdout).strip()
                    return result
            plist_path.unlink(missing_ok=True)

    else:
        unit_dir = _linux_unit_dir()
        if not shutil.which("systemctl"):
            result["method"] = "none"
            result["actions"].append({"delete": str(autosync_record_path())})
            result["actions"].append({"delete": str(_stale_record_path())})
            units_exist = (
                (unit_dir / f"{LINUX_UNIT}.service").exists()
                or (unit_dir / f"{LINUX_UNIT}.timer").exists()
            )
            # Missing unit files do not prove the timer is gone: systemd
            # can keep a loaded timer until the manager reloads, and a
            # missing systemctl binary does not mean the user manager is
            # absent. If a systemd auto-sync was ever installed here
            # (per the local record) — or the record's provenance is
            # unreadable — fail closed. Record-only success is reserved
            # for the provably clean case: no unit files and no record
            # of a systemd install.
            record = status.get("record")
            record_error = status.get("record_error")
            record_method = (
                record.get("method") if record is not None else None
            )
            record_requires_abort = (
                record is not None
                and record_method not in ("schtasks", "launchd")
            )
            if (
                units_exist or record_requires_abort or record_error
            ) and not dry_run:
                if units_exist:
                    evidence = "worsaga-autosync unit files exist"
                elif status.get("record_stale"):
                    evidence = (
                        "local auto-sync metadata was invalidated during an "
                        "incomplete install"
                    )
                elif record_error:
                    evidence = "the local auto-sync record is unreadable"
                elif record_method == "systemd-user":
                    evidence = (
                        "the local record shows a systemd auto-sync was "
                        "installed"
                    )
                else:
                    evidence = (
                        "the local auto-sync record has unknown scheduler "
                        "provenance"
                    )
                result["error"] = (
                    f"systemctl not found but {evidence}"
                    + "; cannot prove the timer is stopped, so nothing was"
                    " removed. Use 'worsaga auto-sync remove --force-local'"
                    " to delete only Worsaga's local files."
                )
                return result
            if not dry_run:
                _delete_record()
                result["removed"] = True
                result["was_installed"] = was_installed
            return result
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
