"""Read-only host health collection for the Deploy Agent.

This module deliberately has no Flask or deployment dependencies.  The
legacy ``agent.py`` entry point supplies the MESFlow/QA status callbacks so
the public response remains byte-for-byte compatible at the field level.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

import psutil

CommandResult = dict[str, Any]
StatusProvider = Callable[[], dict[str, Any]]


def run_capture(cmd: Sequence[str] | str, timeout: int = 12, shell: bool = False) -> CommandResult:
    """Run a bounded command.

    Read-only health callers always pass argv and keep ``shell=False``.  The
    explicit, authenticated Terminal endpoint retains its historical
    ``shell=True`` contract; extracting this helper must not broaden or
    silently remove that separately guarded capability.
    """
    try:
        result = subprocess.run(
            cmd if isinstance(cmd, str) else list(cmd),
            shell=shell,
            executable="/bin/bash" if shell and os.name != "nt" else None,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout[-200000:],
            "stderr": result.stderr[-50000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "exit_code": 124,
            "stdout": str(exc.stdout or "")[-200000:],
            "stderr": "Command timed out",
        }
    except (OSError, ValueError) as exc:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(exc)}


def format_bytes(value: int | float | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def collect_summary(mes_status: StatusProvider, qa_status: StatusProvider) -> dict[str, Any]:
    """Collect the existing Operations summary without mutating host state."""
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    disk_root = "/" if os.name != "nt" else Path.cwd().anchor
    root = psutil.disk_usage(disk_root)
    loads = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    boot = datetime.fromtimestamp(psutil.boot_time())
    failed: list[str] = []
    if os.name != "nt":
        result = run_capture(["systemctl", "--failed", "--no-legend", "--plain"], 8)
        failed = [line for line in result.get("stdout", "").splitlines() if line.strip()]
    docker = run_capture(["docker", "ps", "-q"], 8) if shutil.which("docker") else {"ok": False, "stdout": ""}
    unhealthy = (
        run_capture(["docker", "ps", "--filter", "health=unhealthy", "-q"], 8)
        if shutil.which("docker")
        else {"ok": False, "stdout": ""}
    )
    score = 100
    alerts: list[str] = []
    cpu = psutil.cpu_percent(interval=0.15)
    if cpu >= 90:
        score -= 20
        alerts.append(f"CPU cao {cpu:.0f}%")
    elif cpu >= 75:
        score -= 8
        alerts.append(f"CPU {cpu:.0f}%")
    if vm.percent >= 90:
        score -= 20
        alerts.append(f"RAM cao {vm.percent:.0f}%")
    elif vm.percent >= 80:
        score -= 8
        alerts.append(f"RAM {vm.percent:.0f}%")
    if root.percent >= 95:
        score -= 30
        alerts.append(f"Disk gần đầy {root.percent:.0f}%")
    elif root.percent >= 85:
        score -= 12
        alerts.append(f"Disk {root.percent:.0f}%")
    if failed:
        score -= min(25, 5 * len(failed))
        alerts.append(f"{len(failed)} systemd service failed")
    unhealthy_count = len([line for line in unhealthy.get("stdout", "").splitlines() if line.strip()])
    if unhealthy_count:
        score -= min(25, 8 * unhealthy_count)
        alerts.append(f"{unhealthy_count} Docker container unhealthy")
    mes = mes_status()
    qa = qa_status()
    if not mes.get("online"):
        score -= 20
        alerts.append("MESFlow offline")
    return {
        "score": max(0, score), "alerts": alerts, "hostname": socket.gethostname(), "os": platform.platform(),
        "uptime_seconds": int(time.time() - psutil.boot_time()), "boot_time": boot.strftime("%Y-%m-%d %H:%M:%S"),
        "cpu_percent": cpu, "cpu_count": psutil.cpu_count(), "load": [round(value, 2) for value in loads],
        "ram": {"percent": vm.percent, "used": format_bytes(vm.used), "total": format_bytes(vm.total), "used_bytes": vm.used, "total_bytes": vm.total},
        "swap": {"percent": swap.percent, "used": format_bytes(swap.used), "total": format_bytes(swap.total)},
        "disk": {"percent": root.percent, "used": format_bytes(root.used), "total": format_bytes(root.total), "free": format_bytes(root.free), "used_bytes": root.used, "total_bytes": root.total, "free_bytes": root.free},
        "failed_services": len(failed),
        "docker_running": len([line for line in docker.get("stdout", "").splitlines() if line.strip()]),
        "docker_unhealthy": unhealthy_count, "mes": mes, "qa": qa,
    }
