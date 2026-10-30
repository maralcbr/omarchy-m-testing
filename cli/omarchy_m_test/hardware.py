"""Automatic checks omarchy-mac's scripts don't cover: first-boot hardware setup,
the hardware packages, disk encryption, GPU driver and APIs, battery and CPU
frequency scaling. All read-only and none needs root.
"""

from __future__ import annotations

import re

from .host import Host
from .system import HARDWARE_REQUIREMENTS, System

FIRST_BOOT_JOURNAL = ["journalctl", "--unit=omarchy-provision-hardware.service", "--output=short-iso", "--no-pager"]
# Converged images: omarchy-provision-hardware.service is disabled by design and omarchy-mac-first-boot runs
# the deferred hardware setup itself; this drop-in (omarchy-mac) orders the unit after it.
MAC_FIRST_BOOT_DROPIN = "/usr/lib/systemd/system/omarchy-provision-hardware.service.d/20-mac-first-boot.conf"
MAC_FIRST_BOOT_JOURNAL = ["journalctl", "--unit=omarchy-mac-first-boot.service", "--output=short-iso", "--no-pager"]
MAC_FIRST_BOOT_PENDING = "/var/lib/omarchy/mac-first-boot/pending"
# The hardware steps still queued (omarchy-provision-hardware drains it; its unit runs only while it exists).
IMAGE_QUEUE = "/var/lib/omarchy/image/deferred-steps"
MAC_FIRST_BOOT_INCOMPLETE = "Some hardware setup could not finish yet"
_MAC_FIRST_BOOT_LINE = re.compile(r"(systemd\[1\]: .*(Omarchy first boot|omarchy-mac-first-boot)|omarchy-mac-first-boot\[\d+\]: )")
MAC_FIRST_BOOT_DONE = ("Finished Omarchy first boot", "omarchy-mac-first-boot.service: Deactivated successfully")
ASAHI_GPU_DRIVER = "/sys/bus/platform/drivers/asahi"
VULKAN_ICDS = "/usr/share/vulkan/icd.d"
VULKANINFO = ["vulkaninfo", "--summary"]
EGLINFO = ["eglinfo", "-B"]
EGLINFO_PACKAGE = "mesa-utils"
NO_EGLINFO = "eglinfo (mesa-utils) isn't installed"
POWER_SUPPLY = "/sys/class/power_supply"
CPUFREQ = "/sys/devices/system/cpu/cpufreq"

FIRST_BOOT_FAILED = ("Deferred hardware step failed", "Failed with result")
FIRST_BOOT_DONE = "Deferred hardware setup is complete"
JOURNAL_DENIED = ("insufficient permissions", "not seeing messages from other users")


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}


def _read(host: Host, path: str) -> str | None:
    try:
        return host.read_file(path).decode("utf-8", "replace").strip()
    except OSError:
        return None


def _list(host: Host, path: str) -> list[str] | None:
    try:
        return host.list_dir(path)
    except OSError:
        return None


# -- boot, setup and packages -------------------------------------------------


def encryption(system: System) -> dict:
    chain = " < ".join(system.root_chain)
    if system.encryption == "on":
        return _result("boot.encryption", "pass", [f"the root filesystem is on an unlocked encrypted volume: {chain}"])
    if system.encryption == "off":
        return _result("boot.encryption", "skip", [f"the root filesystem isn't encrypted: {chain}"])
    return _result("boot.encryption", "skip", ["couldn't tell which block devices hold the root filesystem"])


def first_boot_setup(host: Host) -> dict:
    """omarchy-mac's deferred first-boot hardware setup, from its journal across boots."""
    journal = host.run(FIRST_BOOT_JOURNAL)
    lines = [line for line in journal.stdout.splitlines() if "omarchy-provision-hardware" in line]
    if not lines:
        if any(hint in journal.stdout + journal.stderr for hint in JOURNAL_DENIED):
            return _result("setup.first-boot-hardware", "skip", ["can't read the system journal as this user"])
        converged = _mac_first_boot(host)
        if converged is not None:
            return converged
        return _result("setup.first-boot-hardware", "skip", ["omarchy-provision-hardware.service never ran: this install has no deferred first-boot hardware setup"])
    failed = [i for i, line in enumerate(lines) if any(mark in line for mark in FIRST_BOOT_FAILED)]
    done = [i for i, line in enumerate(lines) if FIRST_BOOT_DONE in line]
    if failed:
        evidence = [lines[i] for i in failed[:6]]
        later = [i for i in done if i > failed[-1]]
        # The journal line on its own: the scrubber finds a hostname only where a journal line starts.
        evidence += ["a later run completed:", lines[later[-1]]] if later else ["no later run completed"]
        return _result("setup.first-boot-hardware", "fail", evidence)
    if done:
        return _result("setup.first-boot-hardware", "pass", [lines[done[-1]]])
    return _result("setup.first-boot-hardware", "skip", ["the first-boot hardware setup hasn't finished yet"] + lines[-3:])


def _exists(host: Host, path: str) -> bool:
    try:
        host.read_file(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _mac_first_boot(host: Host) -> dict | None:
    """A converged image's first boot (omarchy-mac-first-boot), which runs the deferred hardware setup; None if not one."""
    if not _exists(host, MAC_FIRST_BOOT_DROPIN):
        return None
    check = "setup.first-boot-hardware"
    lines = [line for line in host.run(MAC_FIRST_BOOT_JOURNAL).stdout.splitlines() if _MAC_FIRST_BOOT_LINE.search(line)]
    evidence = ["omarchy-provision-hardware.service is disabled on this image by design: "
                "omarchy-mac-first-boot runs the deferred hardware setup (20-mac-first-boot.conf)"]
    queued = [step.strip() for step in (_read(host, IMAGE_QUEUE) or "").splitlines() if step.strip()]
    failed = [line for line in lines if any(mark in line for mark in FIRST_BOOT_FAILED) or "Failed to start Omarchy first boot" in line]
    done = [line for line in lines if any(mark in line for mark in MAC_FIRST_BOOT_DONE)]
    if failed:
        later = [line for line in done if lines.index(line) > lines.index(failed[-1])]
        return _result(check, "fail", [*evidence, *failed[:6], *(["a later run completed:", later[-1]] if later else ["no later run completed"])])
    if _exists(host, MAC_FIRST_BOOT_PENDING):
        return _result(check, "skip", [*evidence, f"first boot hasn't finished yet ({MAC_FIRST_BOOT_PENDING} is still there)", *lines[-3:]])
    if not done:
        return _result(check, "skip", [*evidence, "first boot is done, but its journal no longer shows how it went"])
    said = [line for line in lines if "omarchy-mac-first-boot[" in line][-2:]
    incomplete = [line for line in lines if MAC_FIRST_BOOT_INCOMPLETE in line]
    if not queued and _read(host, IMAGE_QUEUE) is None and _exists(host, IMAGE_QUEUE):
        return _result(check, "skip", [*evidence, done[-1], f"{IMAGE_QUEUE} is there but can't be read, so what's still queued isn't known"])
    if queued:  # first boot finishes with steps that need the network still queued, for the unit to retry
        return _result(check, "fail", [*evidence, *incomplete[-1:], done[-1], f"still queued in {IMAGE_QUEUE}: {', '.join(queued)}"])
    return _result(check, "pass", [*evidence, *said, done[-1], f"nothing left queued ({IMAGE_QUEUE} is gone)"])


def hardware_packages(system: System) -> dict:
    if not system.has_pacman:
        return _result("packages.hardware", "skip", ["pacman isn't available"])
    missing = [" or ".join(names) for names in HARDWARE_REQUIREMENTS if not any(name in system.packages for name in names)]
    installed = ", ".join(f"{name} {system.packages[name]}" for names in HARDWARE_REQUIREMENTS for name in names
                          if name in system.packages)
    if missing:
        return _result("packages.hardware", "fail", [f"not installed: {', '.join(missing)}"] + ([f"installed: {installed}"] if installed else []))
    return _result("packages.hardware", "pass", [f"installed: {installed}"])


# -- GPU ---------------------------------------------------------------------


def _bound_gpus(names: list[str]) -> list[str]:
    return [name for name in names if re.fullmatch(r"[0-9a-f]+\.gpu", name)]


def _gpu_bound(host: Host) -> bool:
    return bool(_bound_gpus(_list(host, ASAHI_GPU_DRIVER) or []))


def gpu_driver(host: Host) -> dict:
    names = _list(host, ASAHI_GPU_DRIVER)
    if names is None:
        return _result("gpu.driver", "fail", ["the asahi GPU driver isn't loaded"])
    bound = _bound_gpus(names)
    if not bound:
        return _result("gpu.driver", "fail", ["the asahi GPU driver is loaded but bound to no GPU"])
    return _result("gpu.driver", "pass", [f"asahi driver bound to {', '.join(bound)}"])


def _vulkan_devices(text: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    for line in text.splitlines():
        if re.match(r"^GPU\d+:\s*$", line.strip()):
            devices.append({})
        elif devices and "=" in line:
            key, _, value = line.partition("=")
            devices[-1][key.strip()] = value.strip()
    return devices


def gpu_vulkan(host: Host, system: System) -> dict:
    icds = _list(host, VULKAN_ICDS) or []
    asahi = [name for name in icds if name.startswith("asahi_icd")]
    if not asahi:
        evidence = ["no Asahi Vulkan driver in /usr/share/vulkan/icd.d" + (f" (only {', '.join(icds)})" if icds else "")]
        if system.has_pacman and not any(name in system.packages for name in ("vulkan-asahi", "vulkan-aurora")):
            evidence.append("neither vulkan-asahi nor vulkan-aurora is installed")
        return _result("gpu.vulkan", "fail", evidence)
    info = host.run(VULKANINFO)
    if info.returncode == 127:
        installed = [f"Asahi Vulkan driver installed ({asahi[0]})", "vulkaninfo (vulkan-tools) isn't installed, so the Vulkan version wasn't read"]
        # The driver file alone isn't a GPU: on a Mac whose GPU the asahi driver doesn't bind (the M3), it has nothing to drive.
        if not _gpu_bound(host):
            return _result("gpu.vulkan", "fail", [*installed, "no GPU is bound to the asahi GPU driver, so it has no Apple GPU to use"])
        return _result("gpu.vulkan", "pass", installed)
    devices = _vulkan_devices(info.stdout)
    apple = [d for d in devices if "HONEYKRISP" in d.get("driverID", "").upper()]
    if not apple:
        names = ", ".join(d.get("deviceName", "?") for d in devices) or "none"
        evidence = [f"vulkaninfo lists no Asahi (Honeykrisp) device; devices: {names}"]
        if info.returncode != 0:
            evidence.append(f"vulkaninfo exited {info.returncode}: " + ((info.stderr.strip().splitlines() or ["no message"])[-1]))
        return _result("gpu.vulkan", "fail", evidence)
    return _result("gpu.vulkan", "pass", [
        f"{d.get('deviceName', '?')}: {d.get('driverName', '?')}, {d.get('driverInfo', '?')}, Vulkan {d.get('apiVersion', '?')}" for d in apple
    ])


def gpu_opengl(host: Host) -> dict:
    info = host.run(EGLINFO)
    if info.returncode == 127:
        return _result("gpu.opengl", "skip", [NO_EGLINFO])
    fields: dict[str, list[str]] = {}
    for line in info.stdout.splitlines():
        key, sep, value = line.strip().partition(": ")
        if sep and key.startswith("OpenGL"):
            fields.setdefault(key, []).append(value.strip())
    renderers = fields.get("OpenGL core profile renderer", []) + fields.get("OpenGL ES profile renderer", [])
    apple = next((r for r in renderers if r.startswith("Apple")), None)
    if apple is None:
        return _result("gpu.opengl", "fail", [f"no Apple GPU renderer; renderers: {', '.join(dict.fromkeys(renderers)) or 'none'}"])
    evidence = [f"renderer: {apple}"]
    for key in ("OpenGL core profile version", "OpenGL ES profile version"):
        if fields.get(key):
            evidence.append(f"{key}: {fields[key][0]}")
    return _result("gpu.opengl", "pass", evidence)


# -- battery and CPU ----------------------------------------------------------


SMC_BATTERY_NAME = "macsmc-battery"


def system_battery(host: Host) -> str | None:
    """The Mac's own battery: a power supply of type Battery whose scope isn't Device.

    A wireless mouse or keyboard's battery (hidpp_battery_0 for a Logitech
    receiver, scope Device) is a power supply of type Battery too, and sorts
    before macsmc-battery. The Apple SMC's battery is preferred, then one with
    scope System, then one without a scope file (older drivers leave it out).
    """
    supplies = _list(host, POWER_SUPPLY) or []
    if SMC_BATTERY_NAME in supplies and _read(host, f"{POWER_SUPPLY}/{SMC_BATTERY_NAME}/type") == "Battery":
        return SMC_BATTERY_NAME  # the SMC's is always the system's (scope System)
    batteries = [n for n in supplies if _read(host, f"{POWER_SUPPLY}/{n}/type") == "Battery"]
    scopes = {n: _read(host, f"{POWER_SUPPLY}/{n}/scope") for n in batteries}
    system = [n for n in batteries if scopes[n] != "Device"]
    return next((n for n in system if scopes[n] == "System"), system[0] if system else None)


def battery(host: Host) -> dict:
    name = system_battery(host)
    if name is None:
        return _result("power.battery", "fail", ["no system battery in /sys/class/power_supply (a device's own battery, scope Device, doesn't count)"])
    base = f"{POWER_SUPPLY}/{name}"
    status, capacity = _read(host, f"{base}/status"), _read(host, f"{base}/capacity")
    if not status or not capacity or not capacity.isdigit() or int(capacity) > 100:
        return _result("power.battery", "fail", [f"{name}: status {status or 'unreadable'}, capacity {capacity or 'unreadable'}"])
    line = f"{name}: {status}, {capacity}% charged"
    limit = _read(host, f"{base}/charge_control_end_threshold")
    if limit and limit.isdigit():
        line += f", charge limit {limit}%"
    return _result("power.battery", "pass", [line])


def _cpus(text: str) -> str:
    cpus = [int(c) for c in text.split() if c.isdigit()]
    if cpus and cpus == list(range(cpus[0], cpus[-1] + 1)):
        return f"{cpus[0]}-{cpus[-1]}" if len(cpus) > 1 else str(cpus[0])
    return ",".join(map(str, cpus)) or "?"


def cpu_scaling(host: Host) -> dict:
    """Frequency scaling on every cluster: efficiency and performance cores scale separately."""
    policies = sorted((n for n in _list(host, CPUFREQ) or [] if re.fullmatch(r"policy\d+", n)), key=lambda n: int(n[6:]))
    if not policies:
        return _result("cpu.frequency-scaling", "fail", ["no cpufreq policies: CPU frequency scaling isn't active"])
    evidence, ranges, broken = [], set(), []
    for policy in policies:
        base = f"{CPUFREQ}/{policy}"
        driver = _read(host, f"{base}/scaling_driver") or ""
        low, high = _read(host, f"{base}/cpuinfo_min_freq") or "", _read(host, f"{base}/cpuinfo_max_freq") or ""
        governor = _read(host, f"{base}/scaling_governor") or "?"
        cpus = _cpus(_read(host, f"{base}/related_cpus") or "")
        if not driver or not low.isdigit() or not high.isdigit() or int(high) <= int(low):
            broken.append(policy)
            evidence.append(f"{policy}: CPUs {cpus}, driver {driver or 'none'}, range {low or '?'}-{high or '?'} kHz")
            continue
        ranges.add(int(high))
        evidence.append(f"{policy}: CPUs {cpus}, {driver}, {int(low) // 1000}-{int(high) // 1000} MHz, {governor}")
    if broken:
        evidence.append(f"not scaling: {', '.join(broken)}")
    elif len(ranges) < 2:
        evidence.append("only one kind of core scales: efficiency and performance clusters should have different ranges")
    return _result("cpu.frequency-scaling", "pass" if not broken and len(ranges) >= 2 else "fail", evidence)
