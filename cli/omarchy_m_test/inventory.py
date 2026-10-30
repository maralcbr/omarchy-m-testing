"""The hardware inventory and gap map: what hardware the Mac has and what the kernel does with it.

Four reads, all read-only and none needing root:

  device tree   every node's compatible and status properties, and nothing
                else (grep over /sys/firmware/devicetree/base): the node's
                type is its first compatible string
  devices       which node each device came from and whether a driver is
                bound to it (the OF_FULLNAME and DRIVER lines of every uevent
                under /sys/devices)
  kernel log    this boot's firmware-load failures and driver probe errors;
                a "deferred probe pending" for a device a driver has bound
                since is only said, not failed: the kernel prints it when it
                stops waiting in the initramfs, and a device that waited for a
                module on the root filesystem (the M2 Max's display crossbar,
                mux_apple_display_crossbar, for its DCPs) binds once that loads
  kernel config the running kernel's build options (/proc/config.gz),
                compared with Asahi's pinned reference configuration
                (catalogue/asahi-kernel/)

Each hardware node (a device-tree node with a compatible string, outside
/cpus, /chosen and /reserved-memory, which the kernel core handles without
drivers) is bound (a driver claimed its device), unbound (it has a device no
driver claimed) or none (no device of its own: disabled, set up early by the
kernel core, or handled by its parent's driver). An enabled node whose device
no driver claimed is unclaimed, and is explained against the catalogue's
hardware map: the feature it is, failing, or unknown hardware. Bus and
register-block containers (simple-bus, simple-mfd, syscon: their children are
the hardware, e.g. the power manager's power domains) usually have no driver
of their own and are never unclaimed. Nor are the nodes no driver ever binds
by design: the CPU frequency clusters (apple-soc-cpufreq drives them through
the CPUs' performance domains; the CPU section checks it works), the
architected timer (the kernel core sets it up) and U-Boot's SMBIOS node
(the boot loader's, nothing for Linux). Nor is a node whose driver the
running kernel builds as a module that loads only on request: the SEP
(apple-sep has no module alias; Omarchy loads it when Touch ID is turned on,
and the Touch ID section checks it then).

The report's inventory block carries only node types, statuses, driver-bound
states and counts, never a property value or a node's path; kernel-log lines
are check evidence, scrubbed like all evidence.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from . import bundled
from .catalogue import Catalogue
from .host import Host
from .machine import Machine

DT_BASE = "/sys/firmware/devicetree/base"
NODE_PROPERTIES = ["grep", "-r", "-a", "-H", "", "--include=compatible", "--include=status", DT_BASE]
DEVICES = ["grep", "-r", "-H", "-E", "^(OF_FULLNAME|DRIVER)=", "--include=uevent", "/sys/devices"]
KERNEL_LOG = ["journalctl", "--dmesg", "--boot=0", "--no-pager"]
KERNEL_CONFIG = ["zcat", "/proc/config.gz"]

# Subtrees the kernel core handles without drivers: CPUs (the CPU subsystem),
# /chosen (what the boot loader hands over, e.g. the boot framebuffer) and
# memory carve-outs.
SKIPPED_SUBTREES = ("/cpus", "/chosen", "/reserved-memory")
# Containers: their children are the hardware; simple-pm-bus binds them only when this is their first compatible.
CONTAINERS = frozenset({"simple-bus", "simple-mfd", "simple-pm-bus", "syscon"})
# Nodes whose device no driver binds by design (seen unbound on the M1 Pro, 2026-09-27).
UNBOUND_BY_DESIGN = frozenset({"apple,cluster-cpufreq", "arm,armv8-timer", "u-boot,sysinfo-smbios"})
# Apple's CPU frequency clusters by SoC: newer device trees name only the SoC's own cluster compatible (the
# MacBook Neo's t8140) or an older SoC's, without apple,cluster-cpufreq.
UNBOUND_BY_DESIGN_PATTERN = re.compile(r"apple,t[0-9]{4}-cluster-cpufreq")
# Compatible -> the kernel option whose module drives it but loads only on request (no module alias).
OPT_IN_DRIVERS = {"apple,sep": "CONFIG_APPLE_SEP"}

LOG_LINES_PER_CHECK = 20
EVIDENCE_DIFFERENCES = 40
REPORT_DIFFERENCES = 200
REPORT_NODE_KINDS = 1000   # schema maxItems
REPORT_UNCLAIMED_KINDS = 500

_COMPATIBLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9,._+-]{0,79}")
_PROPERTY = re.compile(r"^" + re.escape(DT_BASE) + r"(/[^:]*)?/(compatible|status):(.*)$")
_UEVENT = re.compile(r"^(/sys/devices/\S+)/uevent:(OF_FULLNAME|DRIVER)=(.*)$")
_KERNEL_PREFIX = re.compile(r"^.*?\bkernel: ")
_FIRMWARE_FAILURE = re.compile(
    r"(?i)(direct firmware load for \S+ failed|failed to (?:load|request) (?:the )?(?:\S+ )?firmware"
    r"|firmware(?: file)? \S+ (?:not found|load failed)|request_firmware\S* failed)"
)
_PROBE_ERROR = re.compile(r"(probe with driver \S+ failed with error -?\d+|probe of \S+ failed with error -?\d+|deferred probe pending)")
_DEFERRED = re.compile(r"^(?:\S+ )?(\S+): deferred probe pending")
_CONFIG_SET = re.compile(r"^(CONFIG_[A-Za-z0-9_]{1,100})=(.*)$")
_CONFIG_UNSET = re.compile(r"^# (CONFIG_[A-Za-z0-9_]{1,100}) is not set$")
# Options the build toolchain decides rather than the kernel's packager.
_TOOLCHAIN = re.compile(r"^CONFIG_(CC|AS|LD|GCC|CLANG|LLD|RUSTC|RUST_IS|BINDGEN|PAHOLE|TOOLCHAIN|OBJTOOL)_")
_VALUE = re.compile(r"[ymn]|-?[0-9]{1,12}|0x[0-9A-Fa-f]{1,12}")


@dataclass(frozen=True)
class Node:
    compatibles: tuple[str, ...]
    status: str  # okay, disabled, reserved, fail or other
    driver: str  # bound, unbound or none
    opt_in: bool = False  # its driver is a module the running kernel loads only on request (OPT_IN_DRIVERS)

    @property
    def type(self) -> str:
        return self.compatibles[0]

    @property
    def container(self) -> bool:
        return bool(CONTAINERS.intersection(self.compatibles))

    @property
    def unclaimed(self) -> bool:
        return (self.status == "okay" and self.driver == "unbound" and not self.container and not self.unbound_by_design
                and not self.opt_in)

    @property
    def unbound_by_design(self) -> bool:
        return bool(UNBOUND_BY_DESIGN.intersection(self.compatibles)) or any(
            UNBOUND_BY_DESIGN_PATTERN.fullmatch(compatible) for compatible in self.compatibles)


@dataclass
class Inventory:
    nodes: list[Node] | None = None          # None: the device tree or devices couldn't be read
    missing: str = ""                        # why nodes is None
    log: list[str] | None = None             # this boot's kernel lines; None: unreadable
    config: dict[str, str] | None = None     # the running kernel's options; None: unreadable
    config_missing: str = ""
    reference: tuple[str, dict[str, str]] | None = None  # (label, options) of Asahi's config
    unclaimed: list[dict] = field(default_factory=list)  # filled by classify()
    bound_devices: set[str] = field(default_factory=set)  # device names (48dc00000.dcp, 0-003b) a driver is bound to now

    # -- the report's inventory block ------------------------------------------

    def report(self) -> dict:
        block: dict = {}
        if self.nodes is not None:
            counts = Counter((node.type, node.status, node.driver) for node in self.nodes)
            block["nodes"] = [
                {"compatible": compatible, "status": status, "driver": driver, "count": count}
                for (compatible, status, driver), count in sorted(counts.items())
            ][:REPORT_NODE_KINDS]
            block["unclaimed"] = self.unclaimed[:REPORT_UNCLAIMED_KINDS]
        differences = self.differences()
        if differences is not None:
            label, _ = self.reference or ("", {})
            block["kernel_config"] = {
                "reference": label,
                "differences": [{"option": o, "asahi": a, "kernel": k} for o, a, k in differences[:REPORT_DIFFERENCES]],
                "omitted": max(0, len(differences) - REPORT_DIFFERENCES),
            }
        return block

    def classify(self, catalogue: Catalogue, machine: Machine) -> None:
        by_type: dict[str, list[Node]] = {}
        for node in self.nodes or []:
            if node.unclaimed:
                by_type.setdefault(node.type, []).append(node)
        self.unclaimed = []
        for compatible, nodes in sorted(by_type.items()):
            compatibles = sorted({c for node in nodes for c in node.compatibles})
            entry = {"compatible": compatible, "count": len(nodes)}
            entry.update(catalogue.classify_unclaimed(compatibles, machine.soc, machine.board))
            self.unclaimed.append(entry)

    def differences(self) -> list[tuple[str, str, str]] | None:
        """(option, Asahi's value, this kernel's value) for every build option that differs."""
        if self.config is None or self.reference is None:
            return None
        _, asahi = self.reference
        found = []
        for option in sorted(set(asahi) | set(self.config)):
            ours, theirs = self.config.get(option, "n"), asahi.get(option, "n")
            if ours != theirs and not _TOOLCHAIN.match(option) and _VALUE.fullmatch(ours) and _VALUE.fullmatch(theirs):
                found.append((option, theirs, ours))
        return found

    # -- the checks ------------------------------------------------------------

    def results(self, catalogue: Catalogue) -> list[dict]:
        return [self._drivers(catalogue), self._log_check("hardware.firmware", _FIRMWARE_FAILURE, "firmware-load failures"),
                self._probe_errors(), self._kernel_config()]

    def _drivers(self, catalogue: Catalogue) -> dict:
        if self.nodes is None:
            return _result("hardware.drivers", "skip", [self.missing])
        states = Counter(_state(node) for node in self.nodes)
        unclaimed = sum(entry["count"] for entry in self.unclaimed)
        containers = f"{states['container']} bus or register containers with no driver of their own, " if states["container"] else ""
        if states["by-design"]:
            containers += f"{states['by-design']} that no driver binds by design (CPU frequency clusters, timer, SMBIOS), "
        if states["opt-in"]:
            containers += f"{states['opt-in']} whose driver loads only on request (the SEP, when Touch ID is turned on), "
        evidence = [
            f"{len(self.nodes)} hardware nodes: {states['bound']} claimed by a driver, "
            f"{states['none']} with no device of their own, {containers}{states['disabled']} disabled, {unclaimed} unclaimed"
        ]
        for entry in self.unclaimed:
            feature = catalogue.feature(entry.get("feature"))
            words = catalogue.words(entry["outcome"]) + (f" ({feature['name']})" if feature else "")
            nodes = "1 node" if entry["count"] == 1 else f"{entry['count']} nodes"
            evidence.append(f"unclaimed: {entry['compatible']} ({nodes}): {words}")
        return _result("hardware.drivers", "fail" if unclaimed else "pass", evidence)

    def _log_check(self, check_id: str, pattern: re.Pattern, what: str) -> dict:
        if self.log is None:
            return _result(check_id, "skip", ["this boot's kernel log is empty or can't be read (journalctl --dmesg)"])
        found = list(dict.fromkeys(line for line in self.log if pattern.search(line)))
        if not found:
            return _result(check_id, "pass", [f"no {what} in this boot's kernel log"])
        evidence = found[:LOG_LINES_PER_CHECK]
        if len(found) > LOG_LINES_PER_CHECK:
            evidence.append(f"[{len(found) - LOG_LINES_PER_CHECK} more {what}]")
        return _result(check_id, "fail", evidence)

    def _probe_errors(self) -> dict:
        result = self._log_check("hardware.probe-errors", _PROBE_ERROR, "driver probe errors")
        if result["status"] != "fail":
            return result
        found = list(dict.fromkeys(line for line in self.log or [] if _PROBE_ERROR.search(line)))
        since = [line for line in found if self._bound_since(line)]
        errors = [line for line in found if line not in since]
        evidence = errors[:LOG_LINES_PER_CHECK]
        if len(errors) > LOG_LINES_PER_CHECK:
            evidence.append(f"[{len(errors) - LOG_LINES_PER_CHECK} more driver probe errors]")
        if since:
            evidence.append(
                f"{len(since)} deferred probe(s) pending early in this boot, bound to their drivers since "
                "(waiting for a module the initramfs didn't carry):"
            )
            evidence += [f"since bound: {line}" for line in since[:LOG_LINES_PER_CHECK]]
            if len(since) > LOG_LINES_PER_CHECK:
                evidence.append(f"[{len(since) - LOG_LINES_PER_CHECK} more since bound]")
        return _result("hardware.probe-errors", "fail" if errors else "pass", evidence)

    def _bound_since(self, line: str) -> bool:
        """A "deferred probe pending" line for a device a driver is bound to now."""
        match = _DEFERRED.search(line)
        return bool(match) and match[1] in self.bound_devices

    def _kernel_config(self) -> dict:
        differences = self.differences()
        if differences is None:
            return _result("hardware.kernel-config", "skip", [self.config_missing])
        label, _ = self.reference or ("", {})
        built = lambda value: value in ("y", "m")  # noqa: E731
        here = sum(1 for _, asahi, ours in differences if built(ours) and not built(asahi))
        there = sum(1 for _, asahi, ours in differences if built(asahi) and not built(ours))
        evidence = [
            f"compared with Asahi's {label}",
            f"{len(differences)} options differ: {here} built here but not by Asahi, "
            f"{there} built by Asahi but not here, {len(differences) - here - there} set differently",
        ]
        evidence += [f"{option}: Asahi {asahi}, this kernel {ours}" for option, asahi, ours in differences[:EVIDENCE_DIFFERENCES]]
        if len(differences) > EVIDENCE_DIFFERENCES:
            evidence.append(f"[{len(differences) - EVIDENCE_DIFFERENCES} more differences]")
        return _result("hardware.kernel-config", "pass", evidence)


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}


def take(host: Host, catalogue: Catalogue, machine: Machine) -> Inventory:
    """Map this Mac's hardware."""
    found = Inventory()
    found.nodes, found.missing, found.bound_devices = _nodes(host)
    found.log = _kernel_log(host)
    found.config, found.config_missing = _kernel_config(host)
    found.nodes = _mark_opt_in(found.nodes, found.config)
    found.classify(catalogue, machine)
    if found.config is not None:
        found.reference, problem = _reference()
        if found.reference is None:
            found.config, found.config_missing = None, problem
    return found


# -- reading -------------------------------------------------------------------


def _nodes(host: Host) -> tuple[list[Node] | None, str, set[str]]:
    properties = host.run(NODE_PROPERTIES)
    compatible: dict[str, tuple[str, ...]] = {}
    status: dict[str, str] = {}
    for line in properties.stdout.split("\n"):
        match = _PROPERTY.match(line)
        if not match:
            continue
        path, name, values = match[1] or "/", match[2], [v for v in match[3].split("\0") if v]
        if name == "compatible":
            compatible[path] = tuple(v for v in values if _COMPATIBLE.fullmatch(v))
        else:
            status[path] = _status(values)
    if not compatible:
        return None, f"couldn't read the device tree ({DT_BASE})", set()

    devices = host.run(DEVICES)
    node_of: dict[str, str] = {}
    bound: set[str] = set()
    for line in devices.stdout.splitlines():
        match = _UEVENT.match(line)
        if match and match[2] == "OF_FULLNAME":
            node_of[match[1]] = match[3].strip()
        elif match:
            bound.add(match[1])
    names = {device.rsplit("/", 1)[-1] for device in bound}
    if not node_of:
        return None, "couldn't read which drivers are bound to the Mac's devices (/sys/devices)", names
    claimed: dict[str, bool] = {}
    for device, node in node_of.items():
        claimed[node] = claimed.get(node, False) or device in bound

    nodes = []
    for path, compatibles in compatible.items():
        if path == "/" or not compatibles or any(path == s or path.startswith(s + "/") for s in SKIPPED_SUBTREES):
            continue
        driver = "none" if path not in claimed else ("bound" if claimed[path] else "unbound")
        nodes.append(Node(compatibles, status.get(path, "okay"), driver))
    return nodes, "", names


def _status(values: list[str]) -> str:
    value = (values[0] if values else "okay").strip().lower()
    if value in ("okay", "ok"):
        return "okay"
    if value in ("disabled", "reserved"):
        return value
    return "fail" if value.startswith("fail") else "other"


def _kernel_log(host: Host) -> list[str] | None:
    journal = host.run(KERNEL_LOG)
    lines = [_KERNEL_PREFIX.sub("", line, count=1) for line in journal.stdout.splitlines() if _KERNEL_PREFIX.match(line)]
    return lines or None


def parse_config(text: str) -> dict[str, str]:
    """A kernel .config as option -> value ("n" for "is not set")."""
    options = {}
    for line in text.splitlines():
        if match := _CONFIG_SET.match(line):
            options[match[1]] = match[2]
        elif match := _CONFIG_UNSET.match(line):
            options[match[1]] = "n"
    return options


def _kernel_config(host: Host) -> tuple[dict[str, str] | None, str]:
    config = host.run(KERNEL_CONFIG)
    options = parse_config(config.stdout) if config.returncode == 0 else {}
    if not options:
        return None, "the running kernel's build options aren't readable (/proc/config.gz)"
    return options, ""


def _reference() -> tuple[tuple[str, dict[str, str]] | None, str]:
    try:
        source, config = bundled.asahi_kernel_config()
        about = json.loads(source)
        label = f"{about['package']} {about['version']} ({about['repository'].rsplit('/', 2)[-2]}/{about['repository'].rsplit('/', 1)[-1]}@{about['commit'][:7]})"
    except (OSError, ValueError, KeyError, AttributeError) as problem:
        return None, f"Asahi's reference kernel config can't be used ({problem})"
    return (label, parse_config(config.decode("utf-8", "replace"))), ""


def _state(node: Node) -> str:
    if node.status != "okay":
        return "disabled"
    if node.driver == "unbound" and node.container:
        return "container"
    if node.driver == "unbound" and node.unbound_by_design:
        return "by-design"
    if node.driver == "unbound" and node.opt_in:
        return "opt-in"
    return node.driver


def _mark_opt_in(nodes: list[Node] | None, config: dict[str, str] | None) -> list[Node] | None:
    """Mark the nodes whose driver the running kernel builds as a module that loads only on request."""
    if nodes is None or config is None:
        return nodes
    return [replace(node, opt_in=True)
            if any(config.get(OPT_IN_DRIVERS[c]) == "m" for c in node.compatibles if c in OPT_IN_DRIVERS) else node
            for node in nodes]
