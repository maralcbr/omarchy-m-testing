"""The Storage section: the SSD's health, and a short benchmark on a temporary file.

  storage.health     the Mac's own SSD: model, firmware and the SMART health log.
                     Fails on media errors or a critical warning; other values
                     are evidence. The log needs root without a password and
                     smartmontools (installed for the section with consent):
                     without it the check skips, with why. No serial, EUI-64,
                     NGUID, namespace id or exact capacity ever leaves the host.
  benchmark.storage  fio: 1 GiB sequential write and read, then 4K random reads
                     at queue depths 1 and 32, with direct I/O, on one temporary
                     file in $XDG_CACHE_HOME/omarchy-m-test/. It never opens a
                     block device. A restorer that removes the file is registered
                     before it's made, so it goes on success, on an error and
                     when the run is stopped. Skipped under 4 GiB free.

The score is the sequential read in megabytes per second. Comparing it means
comparing the same capacity (machine.storage), and the evidence says whether the
root filesystem is encrypted, which lowers throughput, and what filesystem it is.
"""

from __future__ import annotations

import json
import re

from . import packages
from .host import CommandResult
from .session import Context
from .ui import Ui

HEALTH, BENCHMARK = "storage.health", "benchmark.storage"
CHECK_IDS = (HEALTH, BENCHMARK)
NOT_READ = "not read in this run (it predates this check)"
SMARTMONTOOLS, FIO = "smartmontools", "fio"
SUITE = 1  # bump when the jobs below change: scores of different suites aren't compared
FILE_NAME = "storage-benchmark.tmp"
FREE_BYTES_NEEDED = 4 * 2**30
RANDOM_SECONDS = 5
NOT_INSTALLED, TIMED_OUT = 127, 124
MB = 10**6

SMART_MISSING = {
    "not-installed": "smartmontools isn't installed",
    "needs-root": "reading the SSD's health log needs root without a password",
    "no-log": "smartctl gave no NVMe health log",
}
_FIO_VERSION = re.compile(r"fio-([0-9][0-9A-Za-z.+~-]{0,30})")
_FILESYSTEM = re.compile(r"[a-z0-9.+_-]{1,20}")


def run(ctx: Context) -> list[dict]:
    found = ctx.host.hardware_snapshot("storage")
    if not found.get("observed"):
        return [_result(check_id, "skip", [NOT_READ]) for check_id in CHECK_IDS]
    return [_health(ctx, found), benchmark(ctx)]


# -- health ------------------------------------------------------------------------


def _health(ctx: Context, found: dict) -> dict:
    if not found["controllers"]:
        other = f"{found['other_controllers']} other NVMe controllers, none of them Apple's" if found["other_controllers"] else "no NVMe controller at all"
        return _result(HEALTH, "fail", [f"the Mac's own SSD isn't visible: {other} (/sys/class/nvme)"])
    extra: list[str] = []
    if any(controller["smart_missing"] == "not-installed" for controller in found["controllers"]):
        ready = packages.temporary(ctx, [SMARTMONTOOLS], "The SSD health check")
        extra = ready.evidence()
        if ready.ready:
            found = ctx.host.hardware_snapshot("storage")
    return _judge(found["controllers"][0], found["other_controllers"], extra)


def _judge(controller: dict, others: int, extra: list[str]) -> dict:
    evidence = [f"{controller['model']}, firmware {controller['firmware'] or 'unknown'}, {controller['state'] or 'unknown state'}, {controller['namespaces']} namespaces"]
    if others:
        evidence.append(f"{others} other NVMe controllers aren't the Mac's own and aren't looked at")
    evidence += extra
    smart = controller["smart"]
    if smart is None:
        return _result(HEALTH, "skip", [*evidence, f"health log not read: {SMART_MISSING.get(controller['smart_missing'], 'unavailable')}"])
    evidence += _smart_lines(smart)
    problems = []
    if smart.get("media_errors"):
        problems.append(f"{smart['media_errors']} media errors")
    if smart.get("critical_warning"):
        problems.append(f"critical warning flags {smart['critical_warning']}")
    if problems:
        return _result(HEALTH, "fail", [*evidence, f"the SSD reports {' and '.join(problems)}"])
    return _result(HEALTH, "pass", evidence)


def _smart_lines(smart: dict) -> list[str]:
    shown = (("available_spare", "available spare", "%"), ("percentage_used", "percentage used", "%"), ("data_read_tb", "data read", " TB"),
             ("data_written_tb", "data written", " TB"), ("power_cycles", "power cycles", ""), ("power_on_hours", "power-on hours", ""),
             ("unsafe_shutdowns", "unsafe shutdowns", ""), ("media_errors", "media errors", ""), ("critical_warning", "critical warning flags", ""),
             ("temperature", "temperature", " °C"))
    return [f"{words}: {smart[key]}{unit}" for key, words, unit in shown if key in smart]


# -- benchmark ---------------------------------------------------------------------


def directory(ctx: Context) -> str | None:
    base = ctx.host.env("XDG_CACHE_HOME")
    if not base:
        home = ctx.host.env("HOME")
        base = f"{home}/.cache" if home else None
    return f"{base}/omarchy-m-test" if base else None


def fio_argv(path: str) -> list[str]:
    return ["fio", "--output-format=json", f"--filename={path}", "--size=1g", "--direct=1", "--ioengine=libaio", "--stonewall",
            "--name=write", "--rw=write", "--bs=1m", "--iodepth=8",
            "--name=read", "--rw=read", "--bs=1m", "--iodepth=8",
            "--name=random1", "--rw=randread", "--bs=4k", "--iodepth=1", f"--runtime={RANDOM_SECONDS}", "--time_based",
            "--name=random32", "--rw=randread", "--bs=4k", "--iodepth=32", f"--runtime={RANDOM_SECONDS}", "--time_based"]


def benchmark(ctx: Context) -> dict:
    where = directory(ctx)
    if where is None:
        return _skip("no home directory to put the temporary file in")
    if ctx.host.run(["mkdir", "-p", "--", where]).returncode != 0:
        return _skip("couldn't make a directory for the temporary file")
    free = _free_bytes(ctx.host.run(["df", "--output=avail", "-B1", "--", where]))
    if free is None:
        return _skip("couldn't tell how much disk space is free")
    if free < FREE_BYTES_NEEDED:
        return _skip(f"{FREE_BYTES_NEEDED // 2**30} GiB free is needed, and there's less")
    ready = packages.temporary(ctx, [FIO], "The storage benchmark")
    evidence = ready.evidence()
    if not ready.ready:
        return _skip(ready.skipped or "fio isn't available", evidence)
    evidence += _conditions(ctx, where)
    (ctx.ui or Ui(ctx.host)).text("A short storage benchmark runs now, about 20 s, on a temporary file. Leave the Mac alone until it finishes: anything else it does lowers the score.")
    path = f"{where}/{FILE_NAME}"
    ctx.changes.register("the storage benchmark's temporary file", ["rm", "-f", "--", path])
    done = ctx.host.run(fio_argv(path))
    if done.returncode == NOT_INSTALLED:
        return _skip("fio isn't installed", evidence)
    measured = _measured(done)
    if measured is None:
        return _result(BENCHMARK, "fail", [*evidence, f"fio gave no result (exit {done.returncode})"])
    version = _FIO_VERSION.search(ctx.host.run(["fio", "--version"]).stdout)
    evidence += [f"sequential write: {measured['write']} MB/s", f"sequential read: {measured['read']} MB/s",
                 f"4K random read, queue depth 1: {measured['random1']} IOPS", f"4K random read, queue depth 32: {measured['random32']} IOPS",
                 f"1 GiB file with direct I/O (suite {SUITE})"]
    score = {"value": measured["read"], "unit": "MBps", "tool": f"fio {version[1]}" if version else "fio", "suite": SUITE}
    return {**_result(BENCHMARK, "pass", evidence), "score": score}


def _free_bytes(done: CommandResult) -> int | None:
    lines = done.stdout.split()
    return int(lines[-1]) if done.returncode == 0 and lines and lines[-1].isdigit() else None


def _conditions(ctx: Context, where: str) -> list[str]:
    encryption = ctx.system.encryption if ctx.system else "unknown"
    filesystem = ctx.host.run(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", where]).stdout.strip()
    shown = filesystem if _FILESYSTEM.fullmatch(filesystem) else "unknown"
    return [f"root filesystem encryption: {encryption}", f"filesystem: {shown}"]


def _measured(done: CommandResult) -> dict | None:
    """Only fio's numbers; its JSON also names the file and the options."""
    start = done.stdout.find("{")
    try:
        jobs = {job["jobname"]: job for job in json.loads(done.stdout[start:])["jobs"]} if start >= 0 else {}
        found = {"write": round(jobs["write"]["write"]["bw_bytes"] / MB), "read": round(jobs["read"]["read"]["bw_bytes"] / MB),
                 "random1": round(jobs["random1"]["read"]["iops"]), "random32": round(jobs["random32"]["read"]["iops"])}
    except (ValueError, KeyError, TypeError):
        return None
    return found if found["read"] > 0 and found["write"] > 0 else None


def _skip(reason: str, evidence: list[str] = ()) -> dict:
    return _result(BENCHMARK, "skip", [*evidence, f"skipped: {reason}"])


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
