"""The Neural Engine and the hardware video encoder, in the Benchmarks section.

  engines.ane            the ANE's status, read only: driver, generation, firmware
                         and where that comes from, power state, wedged and error
                         counters. It never starts a workload, boots firmware or
                         resets the engine. No accelerator device on a Mac the
                         catalogue knows (every one has a Neural Engine), a wedged
                         engine or a power error fails; nonzero cumulative counters
                         are only said. An unknown board with no node skips.
  benchmark.ane          runs omarchy-ane-smoke, only if it's already installed: it
                         is never a temporary package while its packaging settles.
                         Its errors fail. The score is its runs a second.
  benchmark.h264-encode  ffmpeg encoding the decode benchmarks' test source through
                         the hardware encoder, when the kernel exposes one.

A generation name comes from an explicit attribute or a known SoC; an unknown chip
is never guessed. macOS's Neural Engine number is MobileNet, so it's put beside a
result only when that result says it ran the same model. Discovery follows
aurora-diag's ane.py (https://github.com/aurora-silicon/aurora-diag, MIT,
Copyright (c) 2026 Aurora Silicon).
"""

from __future__ import annotations

import json
import re

from . import macos_reference
from .benchmarks import CLIP_SECONDS, CLIP_SOURCE, NOT_INSTALLED, _last_line, _throughput
from .session import Context

ANE, ANE_BENCHMARK, ENCODE = "engines.ane", "benchmark.ane", "benchmark.h264-encode"
CHECK_IDS = (ANE, ANE_BENCHMARK, ENCODE)
NOT_READ = "not read in this run (it predates this check)"
SMOKE = ["omarchy-ane-smoke"]
SUITE = 1
ENOENT = -2
ENCODE_ARGV = ["ffmpeg", "-nostdin", "-hide_banner", "-benchmark", "-f", "lavfi", "-i", CLIP_SOURCE, "-t", str(CLIP_SECONDS),
               "-c:v", "h264_v4l2m2m", "-f", "null", "-"]
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,47}")
_NO_ENCODER = re.compile(r"Unknown encoder|Encoder .* not found", re.I)


def run(ctx: Context) -> list[dict]:
    found = ctx.host.hardware_snapshot("engines")
    if not found.get("observed"):
        return [_result(check_id, "skip", [NOT_READ]) for check_id in CHECK_IDS]
    return [_status(found, ctx.catalogue.knows_board(ctx.machine.board)), _benchmark(ctx, found), _encode(ctx, found)]


def _probe_reason(probe: dict | None) -> list[str]:
    if not probe:
        return []
    lines = [f"driver {probe['driver']} failed to probe, error {probe['error']}"] if probe.get("driver") else []
    if probe.get("firmware"):
        lines.append(f"its firmware isn't installed: {probe['firmware']}" if probe["firmware_error"] == ENOENT
                     else f"its firmware couldn't be loaded, error {probe['firmware_error']}: {probe['firmware']}")
    return lines


def _status(found: dict, known_board: bool = True) -> dict:
    devices = found["ane"]
    if not devices:
        if found["ane_node"]:
            return _result(ANE, "fail", ["the device tree has a Neural Engine node, but the kernel exposes no accelerator device for it (/sys/class/accel)", *_probe_reason(found.get("ane_probe"))])
        words = "the device tree has no Neural Engine node"
        return _result(ANE, "fail", [f"{words}, though this Mac has a Neural Engine"]) if known_board else _result(ANE, "skip", [words])
    evidence, status = [], "pass"
    for device in devices:
        firmware = f"{device['firmware']} ({device['firmware_source']})" if device["firmware"] else "not exposed"
        evidence.append(f"driver {device['driver'] or 'none'}, generation {device['generation'] or 'unknown'}, firmware {firmware}, "
                        f"power {device['power'] or 'unknown'}, device node {'present' if device['node_present'] else 'missing'}")
        if device["wedged"]:
            status = "fail"
            evidence.append("the engine says it's wedged")
        if device["power"] == "error":
            status = "fail"
            evidence.append("its runtime power state is an error")
        if not device["driver"] or not device["node_present"]:
            status = "fail"
            evidence.append("no driver bound, or no /dev/accel node")
        errors = {name: value for name, value in device["errors"].items() if value}
        if errors:
            evidence.append("cumulative error counters, which don't by themselves mean a new fault: " + ", ".join(f"{name} {value}" for name, value in sorted(errors.items())))
    return _result(ANE, status, evidence)


def _benchmark(ctx: Context, found: dict) -> dict:
    if not found["ane"]:
        return _skip(ANE_BENCHMARK, "no Neural Engine device")
    done = ctx.host.run(SMOKE)
    if done.returncode == NOT_INSTALLED:
        return _skip(ANE_BENCHMARK, "omarchy-ane-smoke isn't installed, and it isn't installed just for this run")
    try:
        smoke = json.loads(done.stdout)
    except ValueError:
        smoke = None
    if not isinstance(smoke, dict):
        return _result(ANE_BENCHMARK, "fail", [f"omarchy-ane-smoke gave no result (exit {done.returncode}: {_last_line(done.stderr)})"])
    if smoke.get("available") is False:
        return _skip(ANE_BENCHMARK, "omarchy-ane-smoke says it can't run here")
    name = smoke.get("name") or smoke.get("model")
    shown = name if isinstance(name, str) and _NAME.fullmatch(name) else "unnamed workload"
    median = smoke.get("median_ms", smoke.get("p50_ms"))
    errors = smoke.get("errors")
    if errors or done.returncode != 0:
        return _result(ANE_BENCHMARK, "fail", [f"{shown}: {errors if isinstance(errors, int) else 'some'} errors (exit {done.returncode})"])
    if not isinstance(median, (int, float)) or isinstance(median, bool) or median <= 0:
        return _result(ANE_BENCHMARK, "fail", [f"{shown}: no median time in its result"])
    evidence = [f"{shown}: median {median} ms a run, no errors", *_versus(ctx, smoke, median)]
    return {**_result(ANE_BENCHMARK, "pass", evidence), "score": {"value": round(1000 / median), "unit": "ops", "tool": "omarchy-ane-smoke", "suite": SUITE}}


def _versus(ctx: Context, smoke: dict, median: float) -> list[str]:
    reference = macos_reference.load(ctx.machine.board)
    there = (reference or {}).get("metrics", {}).get("ane")
    if not there:
        return []
    if smoke.get("model") != there.get("model") or smoke.get("batch") != there.get("batch"):
        return [f"vs macOS: macOS's number is {there.get('model')} at batch {there.get('batch')}, a different workload, so there's nothing to compare it with"]
    return macos_reference.line(reference, f"{there['model']} at batch {there['batch']}", median, "ms", there.get("p50_ms"))


def _encode(ctx: Context, found: dict) -> dict:
    encoder = found["encoder"]
    if not encoder:
        why = "the device tree has a video encoder node, but no V4L2 encoder device" if found["ave_node"] else "the device tree has no video encoder node"
        return _skip(ENCODE, why)
    done = ctx.host.run(ENCODE_ARGV)
    if done.returncode == NOT_INSTALLED:
        return _skip(ENCODE, "ffmpeg isn't installed")
    lines = [f"encoder device: {encoder}"]
    if _NO_ENCODER.search(done.stderr):
        return _skip(ENCODE, "this ffmpeg has no V4L2 H.264 encoder", lines)
    measured = _throughput(done)
    if done.returncode != 0 or measured is None:
        return _result(ENCODE, "fail", [*lines, f"hardware encode failed ({_last_line(done.stderr) or f'exit {done.returncode}'})"])
    frames, seconds, fps = measured
    lines.append(f"{frames} frames of 1080p H.264 in {seconds:.2f} s: {fps:.0f} fps")
    return {**_result(ENCODE, "pass", lines), "score": {"value": round(fps), "unit": "fps", "tool": "ffmpeg h264_v4l2m2m", "suite": SUITE}}


def _skip(check_id: str, reason: str, evidence: list[str] = ()) -> dict:
    return _result(check_id, "skip", [*evidence, f"skipped: {reason}"])


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
