"""Whether the GPU can run MLX and whether the Neural Engine (ANE) is enabled.

Two read-only checks that collect what local machine-learning work needs.
The host reads one allowlisted snapshot (`collect`, run inside RealHost): sysfs
and device-tree names and states, module parameters, package versions, a few
kernel-log lines and the result of one tiny MLX matrix product. Nothing is
loaded, probed with root or changed. No file content other than the small
state files named below is read, and a node's unit address is dropped from its
path.

gpu.mlx   Vulkan device, driver and Mesa versions, GPU chip and generation,
          render node, GPU kernel-log lines, and (when `python3` can import
          mlx) a 128x128 matrix product on MLX's default device.
ane.enabled
          Passes when the ANE driver is loaded, the accelerator device exists
          and the driver is bound. Otherwise fails with what an enabling effort
          needs: the SoC and board, the ANE and DART device-tree nodes, the
          kernel's ANE and accelerator build options, the modules and firmware
          on disk, IOMMU state and the ANE kernel-log lines.
"""

from __future__ import annotations

import json
import re

from .host import Host
from .session import Context

GPU_MLX, ANE_ENABLED = "gpu.mlx", "ane.enabled"
CHECK_IDS = (GPU_MLX, ANE_ENABLED)

DT_BASE = "/sys/firmware/devicetree/base"
DRM = "/sys/class/drm"
PLATFORM = "/sys/bus/platform/devices"
MODULES = "/sys/module"
ACCEL_DEV, ACCEL_CLASS = "/dev/accel", "/sys/class/accel"
IOMMU_GROUPS, IOMMU_CLASS = "/sys/kernel/iommu_groups", "/sys/class/iommu"
FIRMWARE = "/usr/lib/firmware"
PACKAGES = ("mesa", "mesa-aurora", "vulkan-asahi", "vulkan-honeykrisp", "vulkan-tools", "linux-aurora", "linux-asahi",
            "m1n1", "m1n1-aurora", "uboot-asahi", "mlx", "mlx-omarchy")
MLX_INFO = ("device_name", "marketing_name", "driver", "driver_info", "gpu_cores")
VULKAN_FIELDS = ("deviceName", "deviceType", "driverID", "driverName", "driverInfo", "apiVersion", "conformanceVersion")
ANE_MODULE = re.compile(r"(?:apple_)?ane(?:_[a-z0-9]+)*")
ANE_NODE = ["(", "-iname", "ane", "-o", "-iname", "ane@*", "-o", "-iname", "*-ane", "-o", "-iname", "*-ane@*", "-o", "-iname", "ane-*", ")"]
ANE_DEVICE = re.compile(r"[0-9a-f]+\.([A-Za-z0-9_-]*ane[A-Za-z0-9_-]*)")
CONFIG_OPTION = re.compile(r"(?:# )?CONFIG_(?:DRM_ACCEL\w*|\w*_ANE\w*|APPLE_(?:DART|SART|RTKIT\w*|MBOX)|RUST_APPLE_RTKIT)\b")
# A driver's own line ("ane_t6021 284000000.ane: ..."), a line about its device ("platform 284000000.ane: ...") or the DRM registration.
LOG_ANE = re.compile(r"(?i)^(?:apple[-_])?ane\w*\s|\b[0-9a-f]+\.(?:dart-)?ane\b|\bInitialized ane\b|dart-ane")
LOG_GPU = re.compile(r"(?i)\b(?:asahi|agx|honeykrisp)\b")
# The GPU driver's page-table and register-map lines are noise for this check.
LOG_GPU_NOISE = re.compile(r"IO map|MMU:|page table")
DMESG_PREFIX = re.compile(r"^\[\s*[\d.]+\]\s*")
ADDRESS = re.compile(r"@[0-9a-fA-F,]+")
VULKAN_VERSION = re.compile(r"Vulkan Instance Version:\s*([0-9.]+)")
# A matrix product of ones: 128 ** 3, exact in float32.
MLX_EXPECTED = 128.0 ** 3
MLX_SCRIPT = """
import json
out = {"installed": False}
try:
    import mlx.core as mx
except ModuleNotFoundError as error:
    out["error"] = type(error).__name__
    out["absent"] = (error.name or "").split(".")[0] == "mlx"
    out["message"] = str(error).splitlines()[0][:120] if str(error) else ""
    print(json.dumps(out))
    raise SystemExit
except Exception as error:
    out["error"] = type(error).__name__
    out["absent"] = False
    out["message"] = str(error).splitlines()[0][:120] if str(error) else ""
    print(json.dumps(out))
    raise SystemExit
out["installed"] = True
out["version"] = getattr(mx, "__version__", None)
out["device"] = str(mx.default_device())
try:
    info = mx.device_info()
except Exception:
    info = {}
keys = %s
out["info"] = {k: info[k] for k in keys if k in info}
a = mx.ones((128, 128))
r = (a @ a).sum()
mx.eval(r)
out["result"] = float(r.item())
print(json.dumps(out))
""" % (MLX_INFO,)


def _read(host: Host, path: str, limit: int = 256) -> str | None:
    try:
        data = host.read_file(path)
        return data.decode("ascii").rstrip("\0\n").strip() if len(data) <= limit else None
    except (OSError, UnicodeError):
        return None


def _dirs(host: Host, path: str) -> list[str] | None:
    try:
        return host.list_dir(path)[:512]
    except OSError:
        return None


def _run(host: Host, argv: list[str], seconds: int = 3):
    return host.run(["timeout", "-k", "2", str(seconds), *argv])


def _uevent(host: Host, path: str) -> tuple[str | None, list[str]]:
    """A device's driver name and device-tree compatible strings, from its uevent file."""
    driver, compatible = None, []
    for line in (_read(host, path + "/uevent", 4096) or "").splitlines():
        key, _, value = line.partition("=")
        if key == "DRIVER" and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            driver = value
        elif re.fullmatch(r"OF_COMPATIBLE_[0-9]+", key) and re.fullmatch(r"[A-Za-z0-9,._+-]{1,64}", value):
            compatible.append(value)
    return driver, compatible


def _node_path(path: str) -> str:
    """A device-tree path without the unit addresses: /soc/ane@26bc04000 is /soc/ane."""
    return ADDRESS.sub("", path[len(DT_BASE):]) or "/"


def _kernel_lines(text: str, pattern: re.Pattern[str], limit: int, noise: re.Pattern[str] | None = None) -> list[str]:
    """Matching kernel-log lines without their time stamp; the first and the last ones when there are many.

    The text is journalctl's messages alone (-o cat), so no host name is in it.
    """
    found: list[str] = []
    for line in text.splitlines():
        message = DMESG_PREFIX.sub("", line.strip())
        if message and pattern.search(message) and not (noise and noise.search(message)) and message[:200] not in found:
            found.append(message[:200])
    return found if len(found) <= limit else found[: limit // 2] + found[-(limit - limit // 2):]


def _vulkan(host: Host) -> dict:
    result = _run(host, ["vulkaninfo", "--summary"], 10)
    if result.returncode == 127:
        return {"state": "missing", "devices": []}
    devices: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if re.match(r"^GPU\d+:\s*$", line.strip()):
            devices.append({})
        elif devices and "=" in line:
            key, _, value = line.partition("=")
            if key.strip() in VULKAN_FIELDS:
                devices[-1][key.strip()] = value.strip()[:120]
    instance = VULKAN_VERSION.search(result.stdout)
    state = "ok" if result.returncode == 0 and devices else "failed" if result.returncode not in (0, 124) else "timeout" if result.returncode == 124 else "none"
    return {"state": state, "devices": devices[:4], "instance": instance[1] if instance else None}


def _gpu(host: Host, log: str) -> dict:
    found: dict = {"drivers": [], "render_nodes": [], "cards": 0}
    for name in _dirs(host, DRM) or []:
        if not re.fullmatch(r"card[0-9]+", name):
            continue
        found["cards"] += 1
        driver, compatible = _uevent(host, f"{DRM}/{name}/device")
        if any(c.startswith("apple,agx") for c in compatible):
            found["drivers"].append({"driver": driver, "compatible": compatible})
    found["render_nodes"] = [n for n in _dirs(host, "/dev/dri") or [] if re.fullmatch(r"renderD[0-9]+", n)]
    found["vulkan"] = _vulkan(host)
    found["log"] = _kernel_lines(log, LOG_GPU, 10, LOG_GPU_NOISE)
    return found


def _mlx(host: Host) -> dict:
    # -I: no script directory or current directory on sys.path, no PYTHON* variables, no user site, so a json.py or an mlx
    # package in the directory the CLI was started from is never imported.
    result = _run(host, ["python3", "-I", "-c", MLX_SCRIPT], 20)
    if result.returncode == 127:
        return {"state": "no-python"}
    if result.returncode == 124:
        return {"state": "timeout"}
    last = (result.stdout.strip().splitlines() or [""])[-1]
    try:
        parsed = json.loads(last)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        message = (result.stderr.strip().splitlines() or ["no message"])[-1]
        return {"state": "error", "returncode": result.returncode, "error": message[:160]}
    if not parsed.get("installed"):
        if parsed.get("absent", True):
            return {"state": "missing", "error": str(parsed.get("error", ""))[:60]}
        return {"state": "broken", "error": f"{str(parsed.get('error', ''))[:60]}: {str(parsed.get('message', ''))[:120]}"}
    info = parsed.get("info")
    info = info if isinstance(info, dict) else {}
    return {
        "state": "ran", "version": str(parsed.get("version"))[:60], "device": str(parsed.get("device"))[:40],
        "info": {k: str(v)[:120] for k, v in info.items()}, "result": parsed.get("result"),
    }


def _ane_nodes(host: Host) -> list[dict]:
    found = _run(host, ["find", DT_BASE, "-maxdepth", "4", "-type", "d", *ANE_NODE])
    nodes = []
    for line in sorted(found.stdout.splitlines())[:12]:
        if not line.startswith(DT_BASE + "/"):
            continue
        names = _dirs(host, line) or []
        compatible = _read(host, line + "/compatible") or ""
        status = _read(host, line + "/status")
        nodes.append({
            "path": _node_path(line), "compatible": [c for c in compatible.split("\0") if c][:3],
            "status": status if status in ("okay", "ok", "disabled", "reserved", "fail") else (None if status is None else "other"),
            "properties": [p for p in ("iommus", "power-domains", "mboxes", "memory-region", "resets", "interrupts") if p in names],
        })
    return nodes


def _ane_modules(host: Host) -> list[dict]:
    loaded = []
    for name in _dirs(host, MODULES) or []:
        if not ANE_MODULE.fullmatch(name):
            continue
        params = {}
        for param in (_dirs(host, f"{MODULES}/{name}/parameters") or [])[:48]:
            value = _read(host, f"{MODULES}/{name}/parameters/{param}", 64)
            if value is not None and re.fullmatch(r"[A-Za-z0-9_.,:-]{0,40}", param) and len(value) <= 40:
                params[param] = value
        loaded.append({"name": name, "state": _read(host, f"{MODULES}/{name}/initstate", 16), "parameters": params})
    return loaded[:4]


def _ane_devices(host: Host) -> list[dict]:
    devices = []
    groups = {g: _dirs(host, f"{IOMMU_GROUPS}/{g}/devices") or [] for g in (_dirs(host, IOMMU_GROUPS) or [])[:128]}
    for name in _dirs(host, PLATFORM) or []:
        if not ANE_DEVICE.fullmatch(name):
            continue
        driver, _ = _uevent(host, f"{PLATFORM}/{name}")
        group = next((g for g, members in groups.items() if name in members), None)
        devices.append({"name": re.sub(r"^[0-9a-f]+\.", "", name), "driver": driver, "bound": _dirs(host, f"{PLATFORM}/{name}/driver") is not None,
                        "iommu_group": group if group is not None and re.fullmatch(r"[0-9]{1,4}", group) else None})
    return devices[:4]


def _ane(host: Host, release: str | None, log: str) -> dict:
    found: dict = {"nodes": _ane_nodes(host), "modules": _ane_modules(host), "devices": _ane_devices(host)}
    accel = _dirs(host, ACCEL_DEV)
    found["dev_accel"] = None if accel is None else [n for n in accel if re.fullmatch(r"accel[0-9]+", n)]
    classes = _dirs(host, ACCEL_CLASS)
    found["class_accel"] = None if classes is None else [n for n in classes if re.fullmatch(r"accel[0-9]+", n)]
    iommu = _dirs(host, IOMMU_CLASS)
    found["iommu"] = None if iommu is None else {"devices": len(iommu), "apple_dart": sum(1 for n in iommu if n.startswith("apple-dart."))}
    config = _run(host, ["zcat", "/proc/config.gz"])
    found["config"] = None if config.returncode != 0 else sorted({line.strip() for line in config.stdout.splitlines() if CONFIG_OPTION.match(line)})[:30]
    if release and re.fullmatch(r"[A-Za-z0-9._+-]{1,100}", release):
        disk = _run(host, ["find", f"/usr/lib/modules/{release}", "-type", "f", "(", "-iname", "ane*.ko*", "-o", "-iname", "*-ane*.ko*", "-o", "-iname", "apple-dart*.ko*", ")"])
        found["modules_on_disk"] = sorted({line.rsplit("/", 1)[-1] for line in disk.stdout.splitlines()})[:12] if disk.returncode == 0 else None
    else:
        found["modules_on_disk"] = None
    fw = _run(host, ["find", FIRMWARE, "-maxdepth", "4", "(", "-ipath", "*/ane/*", "-o", "-iname", "ane*", "-o", "-iname", "*_ane*", ")"])
    found["firmware"] = sorted({line[len(FIRMWARE) + 1:] for line in fw.stdout.splitlines() if line.startswith(FIRMWARE + "/")})[:12] if fw.returncode == 0 else None
    found["log"] = _kernel_lines(log, LOG_ANE, 14)
    return found


def collect(host: Host) -> dict:
    """Called inside RealHost; only this derived snapshot crosses the host boundary."""
    compatible = _read(host, "/proc/device-tree/compatible", 512)
    if compatible is None:
        return {"apple": None}
    board = [c for c in compatible.split("\0") if c]
    if not any(c.startswith("apple,") for c in board):
        return {"apple": False}
    release = _read(host, "/proc/sys/kernel/osrelease", 128)
    log = _run(host, ["journalctl", "-k", "-b", "--no-pager", "-o", "cat", "-n", "3000"])
    if log.returncode != 0 or not log.stdout.strip():
        log = _run(host, ["dmesg"])
    text = log.stdout if log.returncode == 0 else ""
    packages: dict[str, str] = {}
    for line in _run(host, ["pacman", "-Q", *PACKAGES]).stdout.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] in PACKAGES and re.fullmatch(r"[A-Za-z0-9._+:~-]{1,64}", words[1]):
            packages[words[0]] = words[1]
    return {
        "apple": True, "board": board[:4], "kernel": release, "packages": packages, "log_available": bool(text.strip()),
        "gpu": _gpu(host, text), "mlx": _mlx(host), "ane": _ane(host, release, text),
    }


MAX_EVIDENCE = 50
MAX_LINE = 480


def _automatic(check_id: str, status: str, evidence: list[str]) -> dict:
    """A result whose evidence fits the report schema: at most 50 lines of at most 500 characters."""
    lines = [line[:MAX_LINE] for line in evidence]
    if len(lines) > MAX_EVIDENCE:
        lines = [*lines[: MAX_EVIDENCE - 1], f"[{len(lines) - MAX_EVIDENCE + 1} more evidence lines left out]"]
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": lines}


def _yes(value: bool | None) -> str:
    return "unavailable" if value is None else "yes" if value else "no"


def gpu_evidence(found: dict) -> list[str]:
    gpu = found.get("gpu", {})
    vulkan = gpu.get("vulkan", {})
    lines = []
    for drm in gpu.get("drivers", []):
        chips = [c for c in drm["compatible"] if c.startswith("apple,agx-")]
        lines.append(f"GPU kernel driver: {drm['driver'] or 'none bound'}; device-tree compatible: {', '.join(chips) or ', '.join(drm['compatible'])}")
    if not gpu.get("drivers"):
        lines.append(f"no DRM device has an apple,agx compatible ({gpu.get('cards', 0)} DRM cards)")
    lines.append(f"render node: {', '.join(gpu.get('render_nodes') or []) or 'none in /dev/dri'}")
    state = vulkan.get("state")
    if state == "ok":
        lines.append(f"Vulkan instance {vulkan.get('instance') or '?'}; {len(vulkan['devices'])} device(s)")
        for device in vulkan["devices"]:
            lines.append(" ".join(f"{key}={device[key]}" for key in VULKAN_FIELDS if key in device))
    else:
        lines.append(f"vulkaninfo --summary: {state or 'unavailable'}")
    pkgs = found.get("packages", {})
    lines += [f"{name} {pkgs[name]}" for name in PACKAGES if name in pkgs]
    lines += [f"kernel: {line}" for line in gpu.get("log", [])]
    return lines


def gpu_result(found: dict) -> dict:
    """gpu.mlx: MLX ran a matrix product on the GPU, or why it did not."""
    mlx = found.get("mlx", {})
    details = gpu_evidence(found)
    state = mlx.get("state")
    if state == "ran":
        info = mlx.get("info", {})
        device = f"MLX {mlx.get('version')}: default device {mlx.get('device')}"
        named = ", ".join(f"{key}: {info[key]}" for key in MLX_INFO if key in info)
        if not str(mlx.get("device", "")).lower().startswith("device(gpu"):
            return _automatic(GPU_MLX, "fail", [f"{device}: MLX does not run on the GPU here", *([named] if named else []), *details])
        if mlx.get("result") != MLX_EXPECTED:
            return _automatic(GPU_MLX, "fail", [f"{device}: the 128x128 product summed to {mlx.get('result')}, expected {MLX_EXPECTED:.0f}", *([named] if named else []), *details])
        return _automatic(GPU_MLX, "pass", [f"{device}: a 128x128 matrix product ran on the GPU and summed to {mlx.get('result'):.0f}", *([named] if named else []), *details])
    if state == "error":
        return _automatic(GPU_MLX, "fail", [f"mlx imported but the test op failed (exit {mlx.get('returncode')}): {mlx.get('error')}", *details])
    if state == "timeout":
        return _automatic(GPU_MLX, "fail", ["the MLX test op did not finish in 20 s", *details])
    if state == "broken":
        return _automatic(GPU_MLX, "fail", [f"MLX is installed but import mlx.core failed: {mlx.get('error')}", *details])
    why = {"missing": "MLX isn't installed for python3 (import mlx.core failed): the MLX op was not run",
           "no-python": "python3 isn't available: the MLX op was not run"}.get(state, "the MLX op was not run")
    return _automatic(GPU_MLX, "skip", [why, *details])


def _wrapped(label: str, items: list[str], width: int = 360) -> list[str]:
    """Items as lines of at most `width` characters, each starting with the label, so a long list is never cut short."""
    lines, current = [], ""
    for item in items:
        if current and len(label) + len(current) + len(item) + 2 > width:
            lines.append(f"{label} {current}")
            current = ""
        current = f"{current}, {item}" if current else item
    return [*lines, f"{label} {current}"] if current else lines


def ane_evidence(found: dict) -> list[str]:
    ane = found.get("ane", {})
    lines = []
    for node in ane.get("nodes", []):
        lines.append(f"device-tree node {node['path']}: compatible {', '.join(node['compatible']) or 'none'}; status {node['status'] or 'not set'}; "
                     f"properties {', '.join(node['properties']) or 'none'}")
    if not ane.get("nodes"):
        lines.append("no ANE node in the device tree")
    for module in ane.get("modules", []):
        lines.append(f"module {module['name']} ({module['state'] or 'state unknown'}); {len(module['parameters'])} parameters" + ("" if module["parameters"] else ": none"))
        lines += _wrapped(f"module {module['name']} parameters:", [f"{k}={v}" for k, v in sorted(module["parameters"].items())])
    if not ane.get("modules"):
        lines.append("no ANE module is loaded")
    for device in ane.get("devices", []):
        lines.append(f"platform device {device['name']}: driver {device['driver'] or 'none'}, bound {_yes(device['bound'])}, IOMMU group {device['iommu_group'] if device['iommu_group'] is not None else 'none'}")
    lines.append(f"accelerator device /dev/accel: {', '.join(ane['dev_accel']) if ane.get('dev_accel') else 'none' if ane.get('dev_accel') is not None else 'directory absent'}; "
                 f"/sys/class/accel: {', '.join(ane['class_accel']) if ane.get('class_accel') else 'none' if ane.get('class_accel') is not None else 'absent'}")
    iommu = ane.get("iommu")
    lines.append("IOMMU: unavailable" if iommu is None else f"IOMMU: {iommu['devices']} devices, {iommu['apple_dart']} Apple DARTs")
    return lines


def ane_diagnostics(found: dict) -> list[str]:
    """What enabling the ANE needs to know, for a Mac that does not have it enabled."""
    ane = found.get("ane", {})
    lines = [f"board: {', '.join(found.get('board', [])) or 'unknown'}; kernel: {found.get('kernel') or 'unknown'}"]
    config = ane.get("config")
    lines.append("kernel config: /proc/config.gz unavailable" if config is None else "kernel config: " + ("; ".join(config) or "no ANE or accelerator option present"))
    disk = ane.get("modules_on_disk")
    lines.append("ANE and DART modules on disk: " + ("unknown" if disk is None else ", ".join(disk) or "none for this kernel"))
    firmware = ane.get("firmware")
    lines.append("ANE firmware files: " + ("unknown" if firmware is None else ", ".join(firmware) or "none under /usr/lib/firmware"))
    pkgs = found.get("packages", {})
    lines += [f"{name} {pkgs[name]}" for name in ("linux-aurora", "linux-asahi", "m1n1", "m1n1-aurora") if name in pkgs]
    return lines


def ane_result(found: dict) -> dict:
    """ane.enabled: the driver is loaded, the accelerator device exists and the driver is bound."""
    ane = found.get("ane", {})
    nodes, modules, devices = ane.get("nodes", []), ane.get("modules", []), ane.get("devices", [])
    enabled = bool(modules) and bool(ane.get("dev_accel") or ane.get("class_accel")) and any(d["bound"] for d in devices)
    log = [f"kernel: {line}" for line in ane.get("log", [])]
    if enabled:
        names = ", ".join(m["name"] for m in modules)
        return _automatic(ANE_ENABLED, "pass", [f"ANE enabled: module {names} loaded, accelerator device present, driver bound", *ane_evidence(found), *log])
    if not nodes:
        return _automatic(ANE_ENABLED, "skip", ["this Mac's device tree has no ANE node, so there is nothing to enable", *ane_evidence(found), *ane_diagnostics(found), *log])
    reasons = []
    if any(n["status"] == "disabled" for n in nodes):
        reasons.append("the ANE device-tree node is disabled")
    if not modules:
        reasons.append("no ANE module is loaded")
    if not (ane.get("dev_accel") or ane.get("class_accel")):
        reasons.append("no accelerator device exists")
    if devices and not any(d["bound"] for d in devices):
        reasons.append("the ANE platform device has no driver bound")
    return _automatic(ANE_ENABLED, "fail", [f"ANE not enabled: {'; '.join(reasons) or 'the driver state is incomplete'}", *ane_evidence(found), *ane_diagnostics(found), *log])


def run(ctx: Context) -> list[dict]:
    found = ctx.host.compute_snapshot()
    if found.get("apple") is not True:
        why = "not an Apple Mac" if found.get("apple") is False else "the Apple device tree couldn't be read, so nothing was collected"
        return [_automatic(GPU_MLX, "skip", [why]), _automatic(ANE_ENABLED, "skip", [why])]
    return [gpu_result(found), ane_result(found)]
