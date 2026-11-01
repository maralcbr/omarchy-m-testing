"""What the kernel exposes about the hardware, read inside the host.

One snapshot per area, built by RealHost.hardware_snapshot through the host's
own reads: only these allowlisted, validated facts cross the host boundary, so
recordings and reports can't carry anything else (Touch ID's snapshot works
the same way). Nothing here writes. A file that can't be read is just missing.

A recording made before an area existed answers {"observed": false}: not read,
which a check skips, never a reconstructed reading.

The areas' discovery follows aurora-diag's kernel-class walkers
(https://github.com/aurora-silicon/aurora-diag, MIT, Copyright (c) 2026 Ryan Murray).
"""

from __future__ import annotations

import re

from .host import Host

HWMON = "/sys/class/hwmon"
THERMAL = "/sys/class/thermal"

MAX_READINGS = 400
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+/()-]{0,47}")
_TEMP_INPUT = re.compile(r"temp([0-9]{1,4})_input")
_FAN_INPUT = re.compile(r"fan([0-9]{1,3})_input")
_ZONE = re.compile(r"thermal_zone[0-9]{1,4}")
_COOLING = re.compile(r"cooling_device[0-9]{1,4}")
_TRIP = re.compile(r"trip_point_([0-9]{1,3})_temp")
_MILLIDEGREES = 1000.0


def collect(host: Host, area: str) -> dict:
    return {"observed": True, **_AREAS[area](host)}


def text(host: Host, path: str) -> str | None:
    try:
        return host.read_file(path).decode("utf-8", "replace").strip("\0\n ")
    except OSError:
        return None


def names(host: Host, path: str) -> list[str] | None:
    try:
        return sorted(host.list_dir(path))
    except OSError:
        return None


def number(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def whole(value: str | None) -> int | None:
    found = number(value)
    return None if found is None else int(found)


def label(value: str | None) -> str | None:
    return value if value and _LABEL.fullmatch(value) else None


def _thermal(host: Host) -> dict:
    return {"hwmon": _hwmon(host), "zones": _zones(host), "cooling": _cooling(host)}


def _hwmon(host: Host) -> list[dict]:
    found = []
    readings = 0
    for device in names(host, HWMON) or []:
        if not re.fullmatch(r"hwmon[0-9]{1,4}", device):
            continue
        path = f"{HWMON}/{device}"
        entries = names(host, path) or []
        temps, fans, unreadable = [], [], 0
        for entry in entries:
            if readings >= MAX_READINGS:
                break
            if match := _TEMP_INPUT.fullmatch(entry):
                value = number(text(host, f"{path}/{entry}"))
                if value is None:
                    unreadable += 1
                    continue
                temps.append({"label": label(text(host, f"{path}/temp{match[1]}_label")) or f"temp{match[1]}",
                              "c": round(value / _MILLIDEGREES, 1)})
                readings += 1
            elif match := _FAN_INPUT.fullmatch(entry):
                fan = f"{path}/fan{match[1]}"
                fans.append({"label": label(text(host, f"{fan}_label")) or f"fan{match[1]}", "rpm": whole(text(host, f"{fan}_input")),
                             "target": whole(text(host, f"{fan}_target")), "min": whole(text(host, f"{fan}_min")),
                             "max": whole(text(host, f"{fan}_max"))})
                readings += 1
        found.append({"name": label(text(host, f"{path}/name")) or "unnamed", "temps": temps, "fans": fans, "unreadable": unreadable})
    return found


def _zones(host: Host) -> list[dict]:
    found = []
    for zone in names(host, THERMAL) or []:
        if not _ZONE.fullmatch(zone):
            continue
        path = f"{THERMAL}/{zone}"
        value = number(text(host, f"{path}/temp"))
        trips = []
        for entry in names(host, path) or []:
            if match := _TRIP.fullmatch(entry):
                trip = number(text(host, f"{path}/{entry}"))
                if trip is not None:
                    trips.append({"type": label(text(host, f"{path}/trip_point_{match[1]}_type")) or "unknown", "c": round(trip / _MILLIDEGREES, 1)})
        found.append({"type": label(text(host, f"{path}/type")) or zone, "c": None if value is None else round(value / _MILLIDEGREES, 1),
                      "policy": label(text(host, f"{path}/policy")), "trips": trips})
    return found


def _cooling(host: Host) -> list[dict]:
    found = []
    for device in names(host, THERMAL) or []:
        if _COOLING.fullmatch(device):
            path = f"{THERMAL}/{device}"
            found.append({"type": label(text(host, f"{path}/type")) or device, "state": whole(text(host, f"{path}/cur_state")),
                          "max": whole(text(host, f"{path}/max_state"))})
    return found


_AREAS = {"thermal": _thermal}
AREAS = tuple(_AREAS)
