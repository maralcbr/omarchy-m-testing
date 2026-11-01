"""What the kernel exposes about the hardware, read inside the host.

One snapshot per area, built by RealHost.hardware_snapshot through the host's
own reads: only these allowlisted, validated facts cross the host boundary, so
recordings and reports can't carry anything else (Touch ID's snapshot works
the same way). Nothing here writes. A file that can't be read is just missing.

A recording made before an area existed answers {"observed": false}: not read,
which a check skips, never a reconstructed reading.

The areas' discovery follows aurora-diag's kernel-class walkers
(https://github.com/aurora-silicon/aurora-diag, MIT, Copyright (c) 2026 Aurora Silicon).
"""

from __future__ import annotations

import json
import math
import re

from . import gpu_spec
from .host import Host

HWMON = "/sys/class/hwmon"
THERMAL = "/sys/class/thermal"
IIO = "/sys/bus/iio/devices"
DEVICE_TREE = "/sys/firmware/devicetree/base"
CPUFREQ = "/sys/devices/system/cpu/cpufreq"
ACCEL = "/sys/class/accel"
VIDEO = "/sys/class/video4linux"
NVME = "/sys/class/nvme"
BLOCK = "/sys/block"

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


def _sensors(host: Host) -> dict:
    return {"iio": [_iio_device(host, device) for device in names(host, IIO) or [] if re.fullmatch(r"iio:device[0-9]{1,3}", device)],
            "aop_node": any(entry.startswith("aop@") for entry in (names(host, f"{DEVICE_TREE}/soc") or []) + (names(host, DEVICE_TREE) or []))}


def _iio_device(host: Host, device: str) -> dict:
    path = f"{IIO}/{device}"
    entries = set(names(host, path) or [])
    found: dict = {"name": label(text(host, f"{path}/name")) or "unnamed"}
    for key, channel in (("accel", "accel"), ("gyro", "anglvel")):
        axes, unreadable = {}, 0
        for axis in "xyz":
            stem = f"in_{channel}_{axis}"
            if not {f"{stem}_raw", f"{stem}_input"} & entries:
                continue
            value = _channel(host, path, entries, stem, f"in_{channel}")
            if value is None:
                unreadable += 1
            else:
                axes[axis] = round(value, 4)
        if axes or unreadable:
            found[key] = axes
            found[f"{key}_unreadable"] = unreadable
    for entry in sorted(entries):
        if match := re.fullmatch(r"(in_angl[0-9]?)_(raw|input)", entry):
            value = _channel(host, path, entries, match[1], "in_angl")
            # IIO angles are radians once scaled.
            scaled = match[2] == "raw" and {f"{match[1]}_scale", "in_angl_scale"} & entries
            found["angle"] = None if value is None else round(math.degrees(value) if scaled else value, 1)
            break
    for key, channel in (("lux", "in_illuminance"), ("colour_temperature", "in_colortemp")):
        if {f"{channel}_input", f"{channel}_raw"} & entries:
            value = _channel(host, path, entries, channel, channel)
            found[key] = None if value is None else round(value, 1)
    return found


def _channel(host: Host, path: str, entries: set[str], stem: str, kind: str) -> float | None:
    """A channel's value: its _input, or _raw times its scale when there is one."""
    if f"{stem}_input" in entries:
        return number(text(host, f"{path}/{stem}_input"))
    raw = number(text(host, f"{path}/{stem}_raw"))
    if raw is None:
        return None
    scale = next((number(text(host, f"{path}/{name}_scale")) for name in (stem, kind) if f"{name}_scale" in entries), None)
    return raw * scale if scale is not None else raw


# -- storage: the Mac's own SSD. Never its serial, EUI-64, NGUID or namespace ids, and only a capacity tier. --

_CONTROLLER = re.compile(r"nvme[0-9]{1,3}")
_NAMESPACE = re.compile(r"nvme[0-9]{1,3}n[0-9]{1,3}")
_APPLE_SSD = re.compile(r"APPLE SSD [A-Za-z0-9 ._-]{1,32}")
_FIRMWARE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_SECTOR = 512
_DATA_UNIT = 512_000  # NVMe counts data in units of 1000 512-byte sectors
_TERABYTE = 10**12
TIERS = (("256GB", 256 * 10**9), ("512GB", 512 * 10**9), ("1TB", 10**12), ("2TB", 2 * 10**12), ("4TB", 4 * 10**12), ("8TB", 8 * 10**12))
TIER_TOLERANCE = 0.15
SMART_INTEGERS = ("critical_warning", "available_spare", "available_spare_threshold", "percentage_used", "power_cycles",
                  "power_on_hours", "unsafe_shutdowns", "media_errors", "temperature")


def smart_argv(controller: str) -> list[str]:
    return ["sudo", "-n", "smartctl", "-j", "-a", f"/dev/{controller}"]


SMART_VERSION = ["smartctl", "--version"]


def tier(size_bytes: int) -> str | None:
    """Apple's sales size nearest a namespace's size in bytes, when one is close enough."""
    name, wanted = min(TIERS, key=lambda t: abs(math.log(size_bytes / t[1]))) if size_bytes > 0 else (None, 0)
    return name if name and abs(size_bytes - wanted) <= wanted * TIER_TOLERANCE else None


def _apple_controllers(host: Host) -> tuple[list[tuple[str, str]], int]:
    """(controller, model) of each of the Mac's own SSD controllers, and how many other NVMe controllers there are."""
    found, others = [], 0
    for controller in names(host, NVME) or []:
        if not _CONTROLLER.fullmatch(controller):
            continue
        model = text(host, f"{NVME}/{controller}/model") or ""
        if _APPLE_SSD.fullmatch(model):
            found.append((controller, model))
        else:
            others += 1
    return found, others


def _largest_namespace(host: Host, controller: str) -> tuple[int, int]:
    """(namespaces, the largest one's size in bytes)."""
    sizes = [whole(text(host, f"{BLOCK}/{entry}/size")) or 0 for entry in names(host, f"{NVME}/{controller}") or [] if _NAMESPACE.fullmatch(entry)]
    return len(sizes), max(sizes, default=0) * _SECTOR


def _capacity(host: Host) -> dict:
    controllers, _ = _apple_controllers(host)
    return {"tier": tier(_largest_namespace(host, controllers[0][0])[1]) if controllers else None}


def _smart(host: Host, controller: str) -> tuple[dict | None, str]:
    """The allowlisted SMART health log, or why there is none. smartctl's serial, WWN and namespace ids stay here."""
    if host.run(SMART_VERSION).returncode == 127:
        return None, "not-installed"
    done = host.run(smart_argv(controller))
    try:
        log = json.loads(done.stdout).get("nvme_smart_health_information_log")
    except (ValueError, AttributeError):
        log = None
    if not isinstance(log, dict):
        return None, "needs-root" if done.returncode == 1 else "no-log"
    smart = {key: log[key] for key in SMART_INTEGERS if isinstance(log.get(key), int) and not isinstance(log.get(key), bool)}
    for key, field in (("data_read_tb", "data_units_read"), ("data_written_tb", "data_units_written")):
        if isinstance(log.get(field), int):
            smart[key] = round(log[field] * _DATA_UNIT / _TERABYTE)
    return smart, ""


def _storage(host: Host) -> dict:
    controllers, others = _apple_controllers(host)
    found = []
    for controller, model in controllers:
        path = f"{NVME}/{controller}"
        firmware = text(host, f"{path}/firmware_rev")
        smart, missing = _smart(host, controller)
        found.append({"model": model, "firmware": firmware if firmware and _FIRMWARE.fullmatch(firmware) else None,
                      "state": label(text(host, f"{path}/state")), "namespaces": _largest_namespace(host, controller)[0],
                      "smart": smart, "smart_missing": missing})
    return {"controllers": found, "other_controllers": others, "any_nvme": bool(controllers) or others > 0, **_capacity(host)}


# -- GPU spec checks: vulkaninfo's device profile and glprobe's limits, cut to what the checks compare. ------

VULKAN_TEMP = "/tmp/omarchy-m-test-vulkan.XXXXXXXX"
VULKAN_PROFILE = 'cd "$1" && exec vulkaninfo --json="$2"'
MAX_VULKAN_DEVICES = 4
_API_VERSION = re.compile(r"[0-9]{1,3}\.[0-9]{1,4}(\.[0-9]{1,6})?")
_CONFORMANCE = re.compile(r"[0-9]{1,3}(\.[0-9]{1,4}){2,3}")
_GL_VERSION = re.compile(r"[A-Za-z0-9 ._()-]{1,80}")
_TEMP_DIR = re.compile(r"/tmp/omarchy-m-test-vulkan\.[A-Za-z0-9]+")


def _api_version(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):  # VK_MAKE_API_VERSION
        return f"{(value >> 22) & 0x7f}.{(value >> 12) & 0x3ff}.{value & 0xfff}"
    return value if isinstance(value, str) and _API_VERSION.fullmatch(value) else None


def _conformance(value: object) -> str | None:
    if isinstance(value, dict):
        value = ".".join(str(value.get(part)) for part in ("major", "minor", "subminor", "patch"))
    return value if isinstance(value, str) and _CONFORMANCE.fullmatch(value) else None


def _vulkan_device(profile: dict) -> dict | None:
    """One GPU's facts from vulkaninfo's profile JSON, or None for a software device. Never the UUIDs or the driver's paths."""
    capabilities = profile.get("capabilities", {}).get("device", {})
    properties = capabilities.get("properties", {})
    core = properties.get("VkPhysicalDeviceProperties", {})
    device_type = core.get("deviceType")
    if device_type == 4 or (isinstance(device_type, str) and "CPU" in device_type):
        return None
    api = _api_version(core.get("apiVersion"))
    enabled: set[str] = set()
    for struct in capabilities.get("features", {}).values():
        if isinstance(struct, dict):
            enabled |= {name for name, value in struct.items() if value is True}
    wanted = gpu_spec.required_for(gpu_spec.version_of(api))
    return {"name": label(core.get("deviceName") if isinstance(core.get("deviceName"), str) else None) or "unnamed", "api": api,
            "conformance": _conformance(properties.get("VkPhysicalDeviceVulkan12Properties", {}).get("conformanceVersion")),
            "checked": len(wanted), "missing": [name for name in wanted if name not in enabled]}


def _vulkan(host: Host) -> dict:
    made = host.run(["mktemp", "-d", VULKAN_TEMP])
    directory = made.stdout.strip()
    if made.returncode != 0 or not _TEMP_DIR.fullmatch(directory):
        return {"missing": "couldn't make a temporary directory for vulkaninfo's profile"}
    try:
        devices: list[dict] = []
        for index in range(MAX_VULKAN_DEVICES):
            done = host.run(["sh", "-c", VULKAN_PROFILE, "sh", directory, str(index)])
            if done.returncode == 127:
                return {"missing": "vulkaninfo (vulkan-tools) isn't installed"}
            if done.returncode != 0:
                break
            profiles = [name for name in names(host, directory) or [] if name.endswith(".json")]
            for name in profiles:
                try:
                    device = _vulkan_device(json.loads(host.read_file(f"{directory}/{name}")))
                except (OSError, ValueError, AttributeError):
                    device = None
                if device:
                    devices.append(device)
                host.run(["rm", "-f", "--", f"{directory}/{name}"])
        return {"devices": devices}
    finally:
        host.run(["rm", "-rf", "--", directory])


def _gl(host: Host) -> dict:
    done = host.run_bundled("gl-spec")
    try:
        found = json.loads(done.stdout)
    except ValueError:
        return {"missing": f"the GL probe gave no answer (exit {done.returncode})"}
    contexts = {}
    for key in ("gl_core", "gl_compat", "gles"):
        context = found.get(key)
        if not isinstance(context, dict):
            continue
        version = context.get("version")
        limits = context.get("limits", {})
        contexts[key] = {"version": version if isinstance(version, str) and _GL_VERSION.fullmatch(version) else None,
                         "renderer": label(context.get("renderer") if isinstance(context.get("renderer"), str) else None),
                         "path": context.get("path") if context.get("path") in ("native", "zink", "llvmpipe") else "unknown",
                         "limits": {name: value for name, value in limits.items() if name in gpu_spec.LIMIT_NAMES and isinstance(value, (int, float))}}
    return {"contexts": contexts} if contexts else {"missing": "no GL context could be made through EGL"}


def _clocks(host: Host) -> dict:
    """Each CPU cluster's advertised frequency steps, in MHz, lowest first: what the kernel offers, not what it reaches."""
    found = []
    for policy in names(host, CPUFREQ) or []:
        if not re.fullmatch(r"policy[0-9]{1,3}", policy):
            continue
        path = f"{CPUFREQ}/{policy}"
        steps = set()
        for name in ("scaling_available_frequencies", "scaling_boost_frequencies"):
            steps |= {int(word) // 1000 for word in (text(host, f"{path}/{name}") or "").split() if word.isdigit()}
        low, high = whole(text(host, f"{path}/cpuinfo_min_freq")), whole(text(host, f"{path}/cpuinfo_max_freq"))
        found.append({"driver": label(text(host, f"{path}/scaling_driver")), "mhz": sorted(steps),
                      "min_mhz": None if low is None else low // 1000, "max_mhz": None if high is None else high // 1000})
    return {"policies": found}


# -- the Neural Engine and the video encoder -----------------------------------------------------------

ANE_GENERATIONS = {"t8103": "H13", "t6000": "H13", "t6001": "H13", "t6002": "H13", "t8112": "H14", "t6020": "H14", "t6021": "H14", "t6022": "H14",
                   "t8122": "H15", "t6030": "H15", "t6031": "H15", "t6034": "H15", "t8132": "H16", "t6040": "H16", "t6041": "H16"}
ANE_ERRORS = ("errors", "error_count", "timeouts", "faults", "resets", "crashes")
_ANE_COUNTER = re.compile(r"([a-z][a-z0-9_]{0,40})\s*(?:=|:|\s)\s*(\d{1,18})")
_ANE_ERROR = re.compile(r"error|fault|timeout|reset|crash")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_ENCODER = re.compile(r"apple-ave|\bave\b")


def _driver_of(host: Host, device: str) -> str | None:
    """The driver a device is bound to, from its uevent: the host doesn't read links."""
    for line in (text(host, f"{device}/uevent") or "").splitlines():
        if line.startswith("DRIVER="):
            return label(line.partition("=")[2])
    return None


def _ane_generation(explicit: str | None, compatibles: list[str]) -> str | None:
    if explicit and (match := re.fullmatch(r"[Hh]?(1[3-7])", explicit)):
        return f"H{match[1]}"
    for compatible in compatibles:
        if match := re.fullmatch(r"apple,h(1[3-7])-ane", compatible):
            return f"H{match[1]}"
        chip = compatible.removeprefix("apple,").removesuffix("-ane")
        if chip in ANE_GENERATIONS:
            return ANE_GENERATIONS[chip]
    return None


def _ane_device(host: Host, name: str, nodes: list[str]) -> dict | None:
    path = f"{ACCEL}/{name}/device"
    compatibles = [c for c in (text(host, f"{path}/of_node/compatible") or "").split("\0") if c]
    driver = _driver_of(host, path)
    if not ("ane" in (driver or "").lower() or any(c.endswith("-ane") or c == "apple,ane" for c in compatibles)):
        return None  # other accelerator drivers use this class too
    attribute = next((value for value in (text(host, f"{path}/{a}") for a in ("firmware_version", "fw_version")) if value), None)
    firmware, source = attribute, "driver" if attribute else None
    if firmware is None and (firmware := text(host, f"{path}/of_node/apple,firmware-version")):
        source = "device tree, which doesn't confirm what's running"
    errors = {}
    for line in (text(host, f"{path}/ane_stats") or "").splitlines():
        if (match := _ANE_COUNTER.fullmatch(line.strip())) and _ANE_ERROR.search(match[1]):
            errors[match[1]] = int(match[2])
    for counter in ANE_ERRORS:
        if (value := whole(text(host, f"{path}/{counter}"))) is not None:
            errors[counter] = value
    explicit = next((value for value in (text(host, f"{path}/{a}") for a in ("generation", "ane_generation", "chip_family")) if value), None)
    return {"driver": driver, "generation": _ane_generation(explicit, compatibles), "firmware": firmware if firmware and _VERSION.fullmatch(firmware) else None,
            "firmware_source": source, "power": label(text(host, f"{path}/power/runtime_status")), "wedged": whole(text(host, f"{path}/wedged")),
            "errors": errors, "node_present": name in nodes}


def _engines(host: Host) -> dict:
    nodes = names(host, "/dev/accel") or []
    devices = [_ane_device(host, name, nodes) for name in names(host, ACCEL) or [] if re.fullmatch(r"accel[0-9]{1,3}", name)]
    tree = (names(host, f"{DEVICE_TREE}/soc") or []) + (names(host, DEVICE_TREE) or [])
    encoder = None
    for video in names(host, VIDEO) or []:
        name = label(text(host, f"{VIDEO}/{video}/name"))
        if name and _ENCODER.search(name) and re.fullmatch(r"video[0-9]{1,3}", video):
            encoder = name
            break
    return {"ane": [device for device in devices if device], "ane_node": any(entry.startswith("ane@") for entry in tree),
            "encoder": encoder, "ave_node": any(entry.startswith(("ave@", "video-codec@")) for entry in tree)}


def _gpu_spec(host: Host) -> dict:
    return {"vulkan": _vulkan(host), "gl": _gl(host)}


_AREAS = {"thermal": _thermal, "sensors": _sensors, "storage": _storage, "capacity": _capacity, "gpu-spec": _gpu_spec, "clocks": _clocks, "engines": _engines}
AREAS = tuple(_AREAS)
