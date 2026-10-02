"""Passive SEP diagnostics and a human's existing Touch ID unlock.

The host collects a small allowlisted snapshot: cached sysfs state, firmware
provenance and fixed kernel-event categories. Raw enclave logs, fingerprint
names, calibration bytes and identity files never enter a recording.
The system firmware is a sepOS proxy; neither the stub nor the selected
driver protocol establishes the enclave's actual build.
"""

from __future__ import annotations

import re

from . import human, presence
from .host import Host
from .session import Context

ATTACH, READY, UNLOCK = "sep.attach", "touch-id.ready", "touch-id.unlock"
CHECK_IDS = (ATTACH, READY, UNLOCK)
PLATFORM = "/sys/bus/platform/devices"
SPI = "/sys/bus/spi/devices"
CHOSEN = "/proc/device-tree/chosen"
PARAMETERS = "/sys/module/apple_sep/parameters"
PACKAGES = ("linux-aurora", "m1n1", "m1n1-aurora", "libfprint", "aurora-touchid", "fprintd")
VERSIONS = {
    "asahi,system-fw-version": "system firmware (sepOS proxy)",
    "asahi,iboot1-version": "system iBoot build",
    "asahi,os-fw-version": "stub firmware (not running sepOS)",
    "asahi,iboot2-version": "stub iBoot build",
    "asahi,m1n1-stage2-version": "m1n1 stage 2",
}
ENUMS = {
    "boot": {"cold", "warm"}, "protocol": {"sepos13", "variant5"}, "xart": {"enabled", "disabled"},
    "attach": {"pending", "attached", "failed"}, "keystore": {"unknown", "open", "closed"},
    "keybag": {"unknown", "present", "missing", "failed"}, "sensor": {"unbound", "bound", "online", "failed"},
    "touchid": {"unknown", "ready", "not-ready", "failed"},
}
SERVICE_STATES = {"active", "inactive", "failed", "activating", "deactivating", "reloading"}
UNLOCK_QUESTION = (
    "With a fingerprint you already enrolled, did your normal Touch ID unlock "
    "(lock screen or sudo) work this boot? Skip if you haven't tried it or have no enrollment."
)
_VERSION = re.compile(r"(?:v?[0-9]|(?:mBoot|iBoot|m1n1)[- v])[A-Za-z0-9._+:/() -]{0,127}")
_PROFILE = re.compile(r"T[0-9]{4}/J[0-9]{3}[A-Za-z]{0,2}")
_SEP_LINE = re.compile(r"(?i)\b(?:apple[-_]sep|apple-mesa)\b|\b[0-9a-f]+\.sep:|\bSEP platform profile:|\bTouch ID:")
_ERROR = re.compile(r"\b(?:error|status|errno)[ :=]+(-?[0-9]{1,4})\b", re.I)
LOG_EVENTS = (
    ("reloading the driver is not supported", "driver reload refused"),
    ("no endpoint advertised", "no endpoints advertised"),
    ("endpoints advertised", "endpoints advertised"),
    ("SEP platform profile:", "platform profile selected"),
    ("TZ0 accepted", "cold boot TZ0 accepted"),
    ("IMG4 acknowledged", "cold boot IMG4 acknowledged"),
    ("no persisted identity keybag", "identity keybag missing"),
    ("CREATE_KEYBAG", "identity keybag creation outcome"),
    ("sequence counter", "sensor sequence-counter event"),
    ("sensor gated", "sensor gated"),
    ("did not come back online", "sensor did not come back online"),
    ("sensor patch:", "sensor patch outcome"),
    ("sensor:", "sensor setup outcome"),
    ("bringup:", "bring-up outcome"),
    ("xART:", "anti-replay store event"),
    ("apple-mesa", "sensor driver event"),
)


def log_event(line: str) -> str | None:
    """A fixed category and numeric error only; never copy driver message text."""
    if not _SEP_LINE.search(line):
        return None
    event = next((name for needle, name in LOG_EVENTS if needle.lower() in line.lower()), "other driver event")
    error = _ERROR.search(line)
    return "SEP: " + event + (f" (error {error[1]})" if error else "")


def redact_log(text: str) -> str:
    """Record mode's extra kernel-log capture must omit private SEP messages too."""
    return "".join((log_event(line) + ("\n" if line.endswith("\n") else "")) if log_event(line) else line
                   for line in text.splitlines(keepends=True))


def _read(host: Host, path: str) -> str | None:
    try:
        data = host.read_file(path)
        return data.decode("ascii").rstrip("\0\n").strip() if len(data) <= 256 else None
    except (OSError, UnicodeError):
        return None


def _dirs(host: Host, path: str) -> list[str] | None:
    try:
        return host.list_dir(path)[:512]
    except OSError:
        return None


def _run(host: Host, argv: list[str]):
    return host.run(["timeout", "3", *argv])


def _boolean(value: str | None) -> bool | None:
    return True if value in ("1", "Y") else False if value in ("0", "N") else None


def collect(host: Host) -> dict:
    """Called inside RealHost; only this derived snapshot crosses the host boundary."""
    compatible = _read(host, "/proc/device-tree/compatible")
    if compatible is None:
        return {"apple": None}
    if not any(c.startswith("apple,") for c in compatible.split("\0")):
        return {"apple": False}
    found: dict = {"apple": True, "firmware": {}, "diag": {}, "packages": {}, "services": {}}
    for prop in VERSIONS:
        value = _read(host, f"{CHOSEN}/{prop}")
        found["firmware"][prop] = value if value and _VERSION.fullmatch(value) else None
    devices = _dirs(host, PLATFORM)
    found["sep"] = None if devices is None else False
    for name in devices or []:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            continue
        node = f"{PLATFORM}/{name}"
        compatible = _read(host, node + "/of_node/compatible")
        if compatible is None:
            found["sep"] = None if found["sep"] is False else found["sep"]
            continue
        if "apple,sep" not in compatible.split("\0"):
            continue
        found["sep"] = True
        found["node_status"] = _read(host, node + "/of_node/status") == "disabled"
        found["driver"] = _dirs(host, node + "/driver") is not None
        diag = found["diag"]
        abi = _read(host, node + "/diag/abi")
        diag["abi"] = abi if abi and re.fullmatch(r"[0-9]{1,3}", abi) else None
        if abi == "1":
            for attr, values in ENUMS.items():
                value = _read(host, f"{node}/diag/{attr}")
                diag[attr] = value if value in values else None
            profile = _read(host, node + "/diag/profile")
            diag["profile"] = profile if profile and _PROFILE.fullmatch(profile) else None
            endpoints = _read(host, node + "/diag/endpoints")
            diag["endpoints"] = int(endpoints) if endpoints and re.fullmatch(r"[0-9]{1,3}", endpoints) else None
        break
    found["xart_writes"] = _boolean(_read(host, PARAMETERS + "/xart_writes"))
    found["provision_keybag"] = _boolean(_read(host, PARAMETERS + "/provision_keybag"))
    device = _run(host, ["sh", "-c", "test -c /dev/sep-bio"])
    found["device"] = device.returncode == 0 if device.returncode in (0, 1) else None
    for service in ("apple-sep.service", "aurora-sep.service", "fprintd.service"):
        result = _run(host, ["systemctl", "show", service, "--property=ActiveState", "--value"])
        value = result.stdout.strip()
        found["services"][service] = value if result.returncode == 0 and value in SERVICE_STATES else None
    result = _run(host, ["pacman", "-Q", *PACKAGES])
    for line in result.stdout.splitlines():
        words = line.split()
        if len(words) == 2 and words[0] in PACKAGES and _VERSION.fullmatch(words[1]):
            found["packages"][words[0]] = words[1]
    sensor = False
    for name in _dirs(host, SPI) or []:
        if not re.fullmatch(r"spi[0-9]+\.[0-9]+", name):
            continue
        node = f"{SPI}/{name}/of_node"
        compatible = _read(host, node + "/compatible") or ""
        if "apple,mesa-fingerprint" not in compatible.split("\0"):
            continue
        sensor = True
        value = _read(host, node + "/status")
        found["sensor_enabled"] = value in (None, "okay", "ok")
        break
    found["sensor_node"] = sensor
    # Never open calibration or identity files. Only stat the fixed calibration path.
    size = _run(host, ["stat", "-c", "%s", "--", "/usr/lib/firmware/apple/mesa_calibration.bin"])
    value = size.stdout.strip()
    found["calibration_bytes"] = int(value) if size.returncode == 0 and re.fullmatch(r"[0-9]{1,8}", value) else None
    log = _run(host, ["journalctl", "-k", "-b", "--no-pager", "-n", "2000"])
    if log.returncode != 0 or not log.stdout.strip():
        log = _run(host, ["dmesg"])
    found["log_available"] = log.returncode == 0 and bool(log.stdout.strip())
    events = list(dict.fromkeys(event for line in log.stdout.splitlines() if (event := log_event(line))))
    found["log"] = events[-20:] if found["log_available"] else []
    return found


def _automatic(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}


def firmware_details(found: dict) -> list[str]:
    values = found.get("firmware", {})
    return [f"{label}: {values.get(prop) or 'unavailable'}" for prop, label in VERSIONS.items()] + [
        "actual sepOS version/build: unavailable (not exposed by the kernel)",
    ]


def attachment(found: dict) -> dict:
    """Firmware provenance alone never passes a SEP functionality check."""
    diag = found.get("diag", {})
    status, reason = "skip", "passive SEP attachment state unavailable or unsupported"
    if found.get("sep") is False:
        reason = "no SEP platform device"
    elif found.get("node_status"):
        reason = "SEP node is disabled"
    elif found.get("sep") is True and diag.get("abi") == "1":
        if diag.get("attach") == "failed":
            status, reason = "fail", "SEP advertised no endpoints: attachment failed"
        elif diag.get("attach") == "pending":
            reason = "SEP attachment is still pending"
        elif diag.get("attach") == "attached" and found.get("driver") is True and (diag.get("endpoints") or 0) > 0:
            status, reason = "pass", "SEP attached and advertised endpoints; fingerprint matching is checked separately"
    return _automatic(ATTACH, status, [reason, *firmware_details(found), *details(found)])


def details(found: dict) -> list[str]:
    diag = found.get("diag", {})
    lines = [f"SEP diagnostics ABI: {diag.get('abi') or 'unavailable'}"]
    for attr in ("profile", "boot", "protocol", "attach", "endpoints", "keystore", "keybag", "sensor", "touchid", "xart"):
        value = diag.get(attr)
        lines.append(f"SEP {attr}: {value if value is not None else 'unknown'}")
    lines += ["profile and protocol describe the driver, not the running sepOS version"]
    for field, label in (("driver", "SEP driver bound"), ("device", "/dev/sep-bio present"),
                         ("xart_writes", "xART writes parameter"), ("provision_keybag", "keybag provisioning parameter")):
        value = found.get(field)
        lines.append(f"{label}: {'yes' if value is True else 'no' if value is False else 'unavailable'}")
    size = found.get("calibration_bytes")
    lines.append(f"default sensor calibration file: {str(size) + ' bytes (contents not read)' if size is not None else 'unavailable'}")
    lines.append(f"sensor device-tree node: {'present' if found.get('sensor_node') else 'unavailable'}")
    enabled = found.get("sensor_enabled")
    lines.append(f"sensor node enabled: {'yes' if enabled is True else 'no' if enabled is False else 'unavailable'}")
    lines += [f"{name}: {version}" for name, version in found.get("packages", {}).items()]
    lines += [f"{name}: {state or 'unavailable'}" for name, state in found.get("services", {}).items()]
    lines += found.get("log", [])
    if not found.get("log_available"):
        lines.append("SEP kernel events: unavailable")
    return lines


def ready(found: dict) -> dict:
    evidence = details(found)
    diag = found.get("diag", {})
    reason, status = "", "skip"
    if found.get("sep") is not True:
        reason = "no SEP platform device" if found.get("sep") is False else "SEP platform device unavailable"
    elif found.get("node_status"):
        reason = "SEP node is disabled"
    elif diag.get("abi") != "1":
        reason = "passive SEP diagnostics unavailable or unsupported; legacy evidence does not prove readiness"
    elif diag.get("xart") == "disabled":
        reason = "read-only SEP installation: fingerprint operations are disabled"
    elif diag.get("keybag") == "missing":
        reason = "no existing identity keybag; setup is required (nothing was provisioned)"
    elif diag.get("touchid") == "failed":
        status, reason = "fail", "Touch ID activation or biometric device publication failed for this boot"
    elif diag.get("attach") == "failed" or diag.get("keystore") == "closed" or diag.get("keybag") == "failed" or diag.get("sensor") == "failed":
        status, reason = "fail", "SEP or sensor initialization failed"
    elif (diag.get("attach"), diag.get("keystore"), diag.get("keybag"), diag.get("sensor"), diag.get("touchid"), diag.get("xart")) == (
            "attached", "open", "present", "online", "ready", "enabled") and found.get("device") is True:
        status, reason = "pass", "SEP and sensor report ready; a fingerprint match was not tested"
    elif diag.get("sensor") == "unbound":
        reason = "no bound Touch ID sensor; this board or kernel may not describe one"
    elif diag.get("attach") == "pending" or diag.get("touchid") == "unknown" or diag.get("sensor") == "bound":
        reason = "SEP or Touch ID initialization is still pending"
    else:
        reason = "incomplete or unknown passive state; fingerprint readiness is unconfirmed"
    version = found.get("firmware", {}).get("asahi,system-fw-version") or ""
    older = re.fullmatch(r"([0-9]{1,3})\.([0-9]{1,3})(?:\.[0-9]+)?", version)
    if diag.get("keybag") == "failed" and diag.get("profile") in ("T6020/J414s", "T8112/J415") and older:
        if (int(older[1]), int(older[2])) < (26, 6):
            evidence.append("system firmware predates the kernel team's tested M2 SEP baseline (26.6.x); firmware compatibility may explain the keybag failure")
    return _automatic(READY, status, [reason, *evidence])


def unlock(ctx: Context, found: dict) -> dict:
    """Observe an existing normal unlock; do not initiate verification or enrollment."""
    if human.absent(ctx, UNLOCK):
        return human.skip(UNLOCK, "this Mac has no built-in Touch ID sensor")
    if found.get("sep") is not True or found.get("device") is not True:
        return human.skip(UNLOCK, "SEP biometric device unavailable")
    if any(ctx.host.env(name) for name in presence.SSH_VARIABLES) or ctx.host.terminal() is None:
        return human.skip(UNLOCK, "requires a person at this Mac's local terminal; unattended and SSH runs collect diagnostics only")
    where = presence.of(ctx.host, ctx.cache)
    if where.ssh or not where.seat:
        return human.skip(UNLOCK, "no active local desktop seat")
    return human.check(ctx, UNLOCK, UNLOCK_QUESTION, [
        "observational check of an existing unlock this boot; no verification command was run",
        "an enrollment listing or a readiness flag alone is not proof of matching",
    ])


def run(ctx: Context) -> list[dict]:
    found = ctx.host.touchid_snapshot()
    if found.get("apple") is not True:
        why = "not an Apple Mac" if found.get("apple") is False else "Apple device tree unavailable"
        return [_automatic(ATTACH, "skip", [why]), _automatic(READY, "skip", [why]), human.skip(UNLOCK, why)]
    if human.absent(ctx, READY):
        why = "this Mac has no built-in Touch ID sensor"
        return [attachment(found), _automatic(READY, "skip", [why]), human.skip(UNLOCK, why)]
    state = ready(found)
    if state["status"] == "skip" and state["evidence"][0] == "SEP or Touch ID initialization is still pending":
        ctx.host.sleep(2)
        found = ctx.host.touchid_snapshot()
        state = ready(found)
    observed = unlock(ctx, found)
    if observed["status"] in ("pass", "fail") and not observed.get("answered_by_default"):
        found = ctx.host.touchid_snapshot()
        state = ready(found)
    if observed["status"] == "pass" and state["status"] != "pass":
        human.not_backed(observed, "passive SEP diagnostics did not confirm readiness")
    return [attachment(found), state, observed]
