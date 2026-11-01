"""What macOS measures on the same Mac: reviewed files, one per board, compared descriptively.

catalogue/macos-reference/<board>.json comes from scripts/macos-reference, run on
macOS and reviewed before it's committed; a release ships the files. A delta here
is a description and never a verdict: the conditions differ (macOS's numbers are
taken on AC with the brightness noted), so no check passes or fails on one.

Storage numbers are keyed by capacity, because an SSD's speed depends on its size:
they compare only at the same capacity.
"""

from __future__ import annotations

import json
import re

from . import bundled

SCHEMA = "omarchy-m-test/macos-reference"
VERSION = 1
TIERS = ("256GB", "512GB", "1TB", "2TB", "4TB", "8TB")
METRICS = ("cpu_ladders", "h264_decode_fps", "hevc_decode_fps", "idle_power_w", "ane")
STORAGE_METRICS = ("read_mbps", "write_mbps", "random1_iops", "random32_iops")
_BOARD = re.compile(r"j[0-9]{3}[a-z]{0,2}")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_NOT_PRIVATE = re.compile(r"(?i)serial|hostname|/users/|/home/|uuid")


def problems(document: object) -> list[str]:
    """What's wrong with a reference file; empty when it's sound."""
    if not isinstance(document, dict):
        return ["not an object"]
    found = []
    if document.get("schema") != SCHEMA or document.get("version") != VERSION:
        found.append(f"schema must be {SCHEMA} version {VERSION}")
    if not (isinstance(document.get("board"), str) and _BOARD.fullmatch(document["board"])):
        found.append("board must be a board id")
    if not (isinstance(document.get("captured"), str) and _DATE.fullmatch(document["captured"])):
        found.append("captured must be a date")
    if not isinstance(document.get("source"), str) or not document.get("source"):
        found.append("source must say where the numbers came from")
    macos = document.get("macos")
    if not (isinstance(macos, dict) and isinstance(macos.get("version"), str) and isinstance(macos.get("build"), str)):
        found.append("macos must give its version and build")
    conditions = document.get("conditions")
    if not (isinstance(conditions, dict) and conditions.get("power") in ("ac", "battery") and isinstance(conditions.get("brightness"), str)
            and isinstance(conditions.get("displays"), int)):
        found.append("conditions must give power, brightness and displays")
    metrics = document.get("metrics", {})
    if not isinstance(metrics, dict) or set(metrics) - set(METRICS):
        found.append(f"metrics may only be {list(METRICS)}")
    else:
        for kind, ladder in metrics.get("cpu_ladders", {}).items():
            if kind not in ("e", "p") or not all(isinstance(ladder.get(key), int) for key in ("states", "min_mhz", "max_mhz")):
                found.append(f"cpu_ladders.{kind} needs states, min_mhz and max_mhz")
    storage = document.get("storage", {})
    if not isinstance(storage, dict):
        found.append("storage must be keyed by capacity")
    else:
        for capacity, values in storage.items():
            if capacity not in TIERS or not isinstance(values, dict) or set(values) - set(STORAGE_METRICS):
                found.append(f"storage.{capacity} must be a capacity tier with {list(STORAGE_METRICS)}")
    extra = set(document) - {"schema", "version", "board", "source", "captured", "macos", "conditions", "metrics", "storage"}
    if extra:
        found.append(f"unknown fields: {sorted(extra)}")
    if _NOT_PRIVATE.search(json.dumps({k: v for k, v in document.items() if k != "source"})):
        found.append("it mustn't carry a serial, a host name, a home path or a UUID")
    return found


def load(board: str) -> dict | None:
    """This board's reference, or None. A file that isn't sound is treated as absent: it's only ever a description."""
    try:
        document = json.loads(bundled.macos_reference_text(board))
    except (OSError, ValueError):
        return None
    return document if not problems(document) and document["board"] == board else None


def conditions(reference: dict) -> str:
    found = reference["conditions"]
    displays = found["displays"]
    power = "on AC" if found["power"] == "ac" else "on battery"
    screens = f"{displays} display{'' if displays == 1 else 's'}"
    return f"macOS {reference['macos']['version']}, {power}, brightness {found['brightness']}, {screens}, {reference['captured']}"


def line(reference: dict | None, what: str, here: float, unit: str, there: float | None) -> list[str]:
    """One "vs macOS" evidence line; nothing without a reference number."""
    if reference is None or there is None:
        return []
    return [f"vs macOS: {what} {here} {unit} here, {there} {unit} on macOS ({conditions(reference)}); a description, not a verdict"]


def storage_lines(reference: dict | None, tier: str | None, measured: dict) -> list[str]:
    """The storage benchmark against macOS at the same capacity, or why there's nothing to compare it with."""
    if reference is None:
        return []
    there = reference.get("storage", {}).get(tier) if tier else None
    if there is None:
        return [f"vs macOS: no macOS reference at this capacity ({tier or 'unknown'})"]
    pairs = (("read", "read_mbps", "read", "MB/s"), ("write", "write_mbps", "write", "MB/s"),
             ("random1", "random1_iops", "4K random read, queue depth 1", "IOPS"), ("random32", "random32_iops", "4K random read, queue depth 32", "IOPS"))
    return [text for key, field, words, unit in pairs for text in line(reference, f"{tier} SSD, {words}", measured[key], unit, there.get(field))]


def ladder_words(states: int, low: int, high: int) -> str:
    return f"{states} steps, {low}-{high} MHz"
