"""cpu.dvfs-ladder: the frequency steps each CPU cluster advertises, against macOS's on the same board.

These are advertised steps, not clocks reached. Fewer steps or a lower top on Linux
is a description to look at and never a verdict: the check passes once the steps
can be read, and skips with no macOS reference for the board to compare them with.
Clusters pair up by top speed: the slower is the efficiency cluster, the faster
the performance one. An Ultra's repeated clusters count once.
"""

from __future__ import annotations

from . import macos_reference
from .session import Context

LADDER = "cpu.dvfs-ladder"
NOT_READ = "not read in this run (it predates this check)"
NAMES = {"e": "Efficiency", "p": "Performance", "only": "The only readable"}


def clusters(policies: list[dict]) -> dict[str, tuple[int, int, int]]:
    """(steps, lowest, highest) of the efficiency and performance clusters; a lone cluster can't be told which it is."""
    found = sorted({(len(policy["mhz"]), policy["mhz"][0], policy["mhz"][-1]) for policy in policies if policy["mhz"]}, key=lambda c: c[2])
    return {"e": found[0], "p": found[-1]} if len(found) > 1 else {"only": found[0]} if found else {}


def check(ctx: Context) -> dict:
    found = ctx.host.hardware_snapshot("clocks")
    if not found.get("observed"):
        return _result("skip", [NOT_READ])
    here = clusters(found["policies"])
    if not here:
        return _result("fail", ["no cluster lists its frequency steps (cpufreq scaling_available_frequencies)"])
    evidence = [f"{NAMES[kind]} cluster here: {macos_reference.ladder_words(*values)}" for kind, values in here.items()]
    reference = macos_reference.load(ctx.machine.board)
    ladders = (reference or {}).get("metrics", {}).get("cpu_ladders")
    if not ladders:
        return _result("skip", [*evidence, "no macOS reference for this Mac to compare them with"])
    for kind, values in here.items():
        there = ladders.get(kind)
        if there is None:
            continue
        words = macos_reference.ladder_words(there["states"], there["min_mhz"], there["max_mhz"])
        same = "the same" if values == (there["states"], there["min_mhz"], there["max_mhz"]) else "different"
        evidence.append(f"vs macOS: {NAMES[kind].lower()} cluster {macos_reference.ladder_words(*values)} here, {words} on macOS: {same} ({macos_reference.conditions(reference)}); advertised steps, not clocks reached")
    return _result("pass", evidence)


def _result(status: str, evidence: list[str]) -> dict:
    return {"id": LADDER, "kind": "automatic", "status": status, "evidence": evidence}
