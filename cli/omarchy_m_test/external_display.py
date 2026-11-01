"""External outputs at their own EDID preferred resolution, without changing a mode.

Refresh is evidence only: a lower refresh can be intentional or a link limit.
Hyprland reports unscaled, untransformed mode pixels; rotate both current and
preferred dimensions into the same orientation, never multiply by scale.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from . import ports
from .host import Host, TimedOut
from .session import Context

CHECK_ID = "display.external-native-mode"
MONITORS = ["hyprctl", "-j", "monitors", "all"]
LAYERS = ["hyprctl", "-j", "layers"]
# The one layer namespace a recording keeps, for the video check: Omarchy's screensaver.
SCREENSAVER_NAMESPACE = "org.omarchy.screensaver"
# Why Hyprland won't scan a monitor out directly (solitaryBlockedBy): a recording keeps these words and nothing else.
SOLITARY_REASONS = frozenset({
    "UNKNOWN", "NOTIFICATION", "LOCK", "WORKSPACE", "WINDOWED", "DND", "SPECIAL", "ALPHA", "OFFSET", "CANDIDATE",
    "OPAQUE", "TRANSFORM", "OVERLAYS", "FLOAT", "WORKSPACES", "SURFACES", "ERRORBAR", "FADEOUT",
})
SETTLE_SECONDS = 2
KERNEL_LOG = ["timeout", "3", "journalctl", "--dmesg", "--boot=0", "--output=cat", "--no-pager"]
DRM = "/sys/class/drm"
CONNECTOR = re.compile(r"(card\d+)-((?:USB|DP|HDMI-A|HDMI-B|DVI-D|DVI-I|DVI-A|VGA)-\d+)")


@dataclass(frozen=True)
class Mode:
    width: int
    height: int
    refresh: float

    def oriented(self, transform: int) -> tuple[int, int]:
        return (self.height, self.width) if transform % 2 else (self.width, self.height)

    def describe(self) -> str:
        return f"{self.width}x{self.height}@{self.refresh:.2f}Hz"


def preferred_mode(edid: bytes) -> Mode | None:
    """Only the base block's first detailed timing, never EDID identity fields."""
    if (len(edid) < 128 or edid[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00"
            or sum(edid[:128]) % 256 or edid[18] != 1 or not edid[24] & 2):
        return None
    timing = edid[54:72]
    clock = int.from_bytes(timing[:2], "little") * 10_000
    width = timing[2] | (timing[4] & 0xf0) << 4
    height = timing[5] | (timing[7] & 0xf0) << 4
    hblank = timing[3] | (timing[4] & 0x0f) << 8
    vblank = timing[6] | (timing[7] & 0x0f) << 8
    if not all((clock, width, height, hblank, vblank)) or timing[17] & 0x80:
        return None
    return Mode(width, height, clock / ((width + hblank) * (height + vblank)))


def anonymous_edid(edid: bytes) -> bytes | None:
    """A replayable timing block with no manufacturer, product, serial or strings."""
    if preferred_mode(edid) is None:
        return None
    block = bytearray(128)
    block[:8] = edid[:8]
    block[18:20] = edid[18:20]
    block[24] = 2
    block[54:62] = edid[54:62]
    block[127] = -sum(block) % 256
    return bytes(block)


def _result(status: str, evidence: list[str]) -> dict:
    return {"id": CHECK_ID, "kind": "automatic", "status": status, "evidence": evidence}


def _connected(host: Host) -> list[tuple[str, str]] | None:
    listed = host.run(ports.DISPLAYS_LIST)
    if listed.returncode != 0:
        return None
    found = []
    for line in listed.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0] == "connector" and fields[2] == "connected":
            match = CONNECTOR.fullmatch(fields[1])
            if match:
                found.append((match[1], match[2]))
    return found


def _monitors(host: Host) -> list[dict] | None:
    try:
        result = host.run(MONITORS)
    except TimedOut:
        return None
    if result.returncode != 0:
        return None
    try:
        monitors = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(monitors, list) or not all(isinstance(m, dict) and isinstance(m.get("name"), str) for m in monitors):
        return None
    return monitors


def _current(monitor: dict) -> tuple[Mode, int] | None:
    try:
        width, height = int(monitor["width"]), int(monitor["height"])
        refresh, transform = float(monitor.get("refreshRate", 0)), int(monitor.get("transform", 0))
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    if width <= 0 or height <= 0 or not math.isfinite(refresh) or not 0 <= transform <= 7:
        return None
    return Mode(width, height, refresh), transform


def _evaluate(host: Host, connectors: list[tuple[str, str]], monitors: list[dict]) -> dict:
    evidence = ["resolution compared in physical pixels; refresh reported only"]
    by_name = {m["name"]: m for m in monitors}
    intents = host.monitor_intent([{"name": name, "description": by_name.get(name, {}).get("description", "")}
                                   for _, name in connectors])
    mirrored = set()
    for monitor in monitors:
        target = str(monitor.get("mirrorOf", "none"))
        if target not in ("none", "None", "", "-1"):
            mirrored.add(monitor["name"])
            mirrored.update(m["name"] for m in monitors if str(m.get("id")) == target or m["name"] == target)
    statuses = []
    for card, name in connectors:
        monitor = by_name.get(name, {})
        intent = intents[name]
        reason = None
        if intent["disabled"] or monitor.get("disabled") or monitor.get("dpmsStatus") is False:
            reason = "output disabled on purpose"
        elif intent["mirrored"] or name in mirrored:
            reason = "output participates in mirroring"
        elif intent["unavailable"]:
            reason = "Hyprland monitor rules unavailable or computed; mode intent unknown"
        elif not intent["preferred"]:
            reason = "explicit monitor mode configured"
        elif not monitor:
            reason = "connected output unavailable in Hyprland"
        current = _current(monitor)
        if reason is None and current is None:
            reason = "current mode unavailable in Hyprland"
        preferred = None
        if reason is None:
            try:
                preferred = preferred_mode(host.read_file(f"{DRM}/{card}-{name}/edid"))
            except OSError:
                pass
            if preferred is None:
                reason = "EDID missing, unreadable or has no parseable preferred detailed timing"
        if reason:
            statuses.append("skip")
            evidence.append(f"{name}: skipped: {reason}")
            continue
        mode, transform = current
        now, wanted = mode.oriented(transform), preferred.oriented(transform)
        detail = f"{name}: current {mode.describe()}, EDID preferred {preferred.describe()}, transform {transform}"
        if now == wanted:
            statuses.append("pass")
            evidence.append(detail)
        elif any(actual < native for actual, native in zip(now, wanted)):
            statuses.append("fail")
            evidence.append(f"{detail}; below preferred resolution")
        else:
            statuses.append("skip")
            evidence.append(f"{detail}; skipped: current resolution exceeds the EDID preference")
    status = "fail" if "fail" in statuses else "skip" if "skip" in statuses else "pass"
    return _result(status, evidence)


def check(ctx: Context) -> dict:
    connectors = _connected(ctx.host)
    if connectors is None:
        return _result("skip", ["skipped: DRM connector state unavailable"])
    if not connectors:
        return _result("skip", ["skipped: no external display connected"])
    monitors = _monitors(ctx.host)
    if monitors is None:
        return _result("skip", ["skipped: Hyprland session or monitor list unavailable"])
    result = _evaluate(ctx.host, connectors, monitors)
    if result["status"] == "fail":
        ctx.host.sleep(SETTLE_SECONDS)
        connectors, monitors = _connected(ctx.host), _monitors(ctx.host)
        if connectors is None or monitors is None:
            result = _result("skip", ["skipped: display state unavailable after settling"])
        elif not connectors:
            result = _result("skip", ["skipped: no external display connected after settling"])
        else:
            result = _evaluate(ctx.host, connectors, monitors)
        result["evidence"].append(f"re-read after {SETTLE_SECONDS}s settling")
    result["evidence"].extend(diagnostics(ctx.host, connectors or []))
    return result


def _text(host: Host, path: str) -> str:
    try:
        return host.read_file(path).decode("utf-8", "replace")
    except OSError:
        return ""


def _debug_crtcs(state: str) -> dict[str, str]:
    crtcs = {name.strip(): ident for ident, name in re.findall(r"^crtc\[(\d+)\]:([^\n]+)", state, re.M)}
    found = {}
    for block in re.split(r"(?=^connector\[)", state, flags=re.M)[1:]:
        name = re.match(r"connector\[\d+\]:([^\n]+)", block)
        crtc = re.search(r"^\s+crtc=([^\n]+)", block, re.M)
        if name and crtc:
            value = crtc[1].strip()
            found[name[1].strip()] = crtcs.get(value, value)
    return found


def drm_info_argv(card: str) -> list[str]:
    return ["timeout", "3", "drm_info", "-j", f"/dev/dri/{card}"]


def diagnostics(host: Host, connectors: list[tuple[str, str]]) -> list[str]:
    """Optional reads never decide a result, including when a tool times out."""
    evidence = []
    for card in sorted({card for card, _ in connectors}):
        crtcs = _debug_crtcs(_text(host, f"/sys/kernel/debug/dri/{card[4:]}/state"))
        missing = [name for c, name in connectors if c == card and name not in crtcs]
        if missing:
            try:
                info = host.run(drm_info_argv(card))
                device = json.loads(info.stdout).get(f"/dev/dri/{card}", {}) if info.returncode == 0 else {}
                for name in missing:
                    ident = _text(host, f"{DRM}/{card}-{name}/connector_id").strip()
                    connector = next((c for c in device.get("connectors", []) if str(c.get("id")) == ident), {})
                    value = connector.get("properties", {}).get("CRTC_ID", {}).get("value")
                    if isinstance(value, int):
                        crtcs[name] = str(value)
            except (TimedOut, ValueError, AttributeError, TypeError):
                pass
        for c, name in connectors:
            if c == card:
                evidence.append(f"diagnostic: {card}-{name} CRTC {crtcs.get(name, 'unavailable')}")
    try:
        log = host.run(KERNEL_LOG)
        if log.returncode != 0:
            evidence.append("diagnostic: kernel log unavailable")
        else:
            evidence.extend(f"diagnostic: kernel: {line}" for line in kernel_findings(log.stdout))
    except TimedOut:
        evidence.append("diagnostic: kernel log unavailable (timed out)")
    return evidence


# -- apple-dcp kernel messages: fixed categories and validated numbers, never message text ----------

DCP = re.compile(r"apple[-_]dcp", re.I)
# (category, pattern): the first that matches a DCP line names it. Patterns follow the Asahi driver's messages.
DCP_CATEGORIES: tuple[tuple[str, re.Pattern], ...] = tuple((name, re.compile(pattern, re.I)) for name, pattern in (
    ("atomic-check-failed", r"atomic.*(?:fail|error|reject|invalid|no modeset)"),
    ("mode-set-timeout", r"set_digital_out_mode timed out|mode\w*.*(?:timed out|timeout)"),
    ("mode-set-failed", r"set_digital_out_mode finished:\s*-[1-9]"),  # positive: time left, not a failure
    ("mode-parse-failed", r"failed to parse modes|without valid modes|duplicate display mode"),
    ("mode-not-found", r"mode.*(?:not found|unknown|lookup failed|invalid|unsupported)"),
    ("swap-failed", r"swap(?:_clear)? failed"),
    ("link-failed", r"link complete failed"),
    ("edid-failed", r"copy_edid failed"),
    ("power-timeout", r"wait for power timed out|set(?:DCPPower|PowerState)\(0\) timeout"),
    ("mode-failed", r"mode.*(?:fail|error|unable|cannot|could not|reject)"),
))
CATEGORY_NAMES = {name for name, _ in DCP_CATEGORIES}
FIELDS: tuple[tuple[str, re.Pattern, int, int], ...] = (  # name, pattern, low, high
    ("errno", re.compile(r"(?:failed|error|ret|err|finished)\s*[:=]?\s*(-\d{1,4})(?![\w.-])", re.I), -4095, -1),
    ("status", re.compile(r"\bstatus\s*[:=]?\s*(\d{1,10})(?![\w.-])", re.I), 0, 2**32 - 1),
    ("port", re.compile(r"\bport\s*[:=]?\s*(\d{1,2})(?![\w.-])", re.I), 0, 99),
)
MODE = re.compile(r"(?<![\w.])(\d{2,5})x(\d{2,5})(?![\w.])")
CANONICAL = re.compile(r"apple-dcp: ([a-z-]+)((?: (?:errno|status|port)=-?\d+| mode=\d+x\d+)*)")
FINDINGS_LIMIT = 10


def dcp_finding(line: str) -> str | None:
    """A DCP kernel line as "apple-dcp: <category> [errno=N] [status=N] [port=N] [mode=WxH]"; None if not a finding.

    The canonical form reads back as itself, so a recording can keep it in place of the message."""
    found = DCP.search(line)
    if not found:
        return None
    message = line[found.start():]
    canonical = CANONICAL.fullmatch(message.strip())
    if canonical and canonical[1] in CATEGORY_NAMES:
        message = canonical[1] + canonical[2]  # parse the fields as written below, validated again
        category = canonical[1]
    else:
        category = next((name for name, pattern in DCP_CATEGORIES if pattern.search(message)), None)
    if category is None:
        return None
    fields = []
    for name, pattern, low, high in FIELDS:
        value = pattern.search(message) if not canonical else re.search(rf"\b{name}=(-?\d+)\b", message)
        if value and low <= int(value[1]) <= high:
            fields.append(f"{name}={int(value[1])}")
    mode = MODE.search(message) if not canonical else re.search(r"\bmode=(\d+)x(\d+)\b", message)
    if mode and all(1 <= int(n) <= 16384 for n in mode.groups()):
        fields.append(f"mode={int(mode[1])}x{int(mode[2])}")
    return " ".join(["apple-dcp:", category, *fields])


def kernel_findings(log: str) -> list[str]:
    """Distinct DCP findings, the last FINDINGS_LIMIT to appear, each with how often it appeared."""
    counts: dict[str, int] = {}
    for line in log.splitlines():
        finding = dcp_finding(line)
        if finding:
            counts[finding] = counts.pop(finding, 0) + 1  # re-inserted: ordered by last appearance
    return [f"{finding} (x{n})" if n > 1 else finding for finding, n in list(counts.items())[-FINDINGS_LIMIT:]]


# -- what a recording keeps: allowlisted projections, never monitor identity -------------------------

OUTPUT_NAME = re.compile(r"(?:eDP|DP|HDMI-[AB]|DVI-[DIA]|VGA|USB|DSI|DPI|LVDS|Virtual|Unknown|HEADLESS|WL|Writeback|SPI)-\d{1,3}(?:-\d{1,3}){0,3}")
MODE_TEXT = re.compile(r"\d{1,5}x\d{1,5}@\d{1,4}(?:\.\d{1,6})?Hz")


def _number(value, kind, low, high):
    if kind is float and isinstance(value, (int, float)) and not isinstance(value, bool):
        value = float(value)
    if type(value) is not kind or (kind is float and not math.isfinite(value)) or not low <= value <= high:
        return None
    return value


def _monitor_projection(monitor) -> dict | None:
    if not isinstance(monitor, dict) or not isinstance(monitor.get("name"), str) or not OUTPUT_NAME.fullmatch(monitor["name"]):
        return None
    kept = {"name": monitor["name"]}
    for key, kind, low, high in (("id", int, 0, 2**31), ("width", int, 1, 65535), ("height", int, 1, 65535),
                                 ("refreshRate", float, 0, 10000), ("transform", int, 0, 7), ("scale", float, 0, 100)):
        if key in monitor and (value := _number(monitor[key], kind, low, high)) is not None:
            kept[key] = value
    for key in ("disabled", "dpmsStatus"):
        if isinstance(monitor.get(key), bool):
            kept[key] = monitor[key]
    if isinstance(monitor.get("solitaryBlockedBy"), list):
        kept["solitaryBlockedBy"] = [reason for reason in monitor["solitaryBlockedBy"] if isinstance(reason, str) and reason in SOLITARY_REASONS]
    mirror = monitor.get("mirrorOf")
    if isinstance(mirror, str) and (mirror in ("none", "") or OUTPUT_NAME.fullmatch(mirror) or re.fullmatch(r"-?\d{1,10}", mirror)):
        kept["mirrorOf"] = mirror
    elif _number(mirror, int, -1, 2**31) is not None:
        kept["mirrorOf"] = mirror
    if isinstance(monitor.get("availableModes"), list):
        kept["availableModes"] = [m for m in monitor["availableModes"] if isinstance(m, str) and MODE_TEXT.fullmatch(m)]
    return kept


def _monitors_projection(text: str) -> str:
    try:
        monitors = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(monitors, list):
        return ""
    return json.dumps([kept for kept in map(_monitor_projection, monitors) if kept is not None])


def _layer_projection(surface: dict) -> dict:
    kept = {"w": surface.get("w"), "h": surface.get("h")}
    return {**kept, "namespace": SCREENSAVER_NAMESPACE} if surface.get("namespace") == SCREENSAVER_NAMESPACE else kept


def _layers_projection(text: str) -> str:
    """Only each layer surface's size and, for Omarchy's screensaver, its namespace: the video check's use for them (no other names, pids, addresses)."""
    try:
        outputs = json.loads(text)
        return json.dumps({str(name): {"levels": {str(level): [_layer_projection(surface) for surface in surfaces]
                                                  for level, surfaces in output["levels"].items()}}
                           for name, output in outputs.items()})
    except (ValueError, AttributeError, KeyError, TypeError):
        return ""


def _drm_info_projection(text: str) -> str:
    try:
        devices = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(devices, dict):
        return ""
    kept = {}
    for node, device in devices.items():
        if not re.fullmatch(r"/dev/dri/card\d{1,3}", str(node)) or not isinstance(device, dict):
            continue
        connectors = []
        for connector in device.get("connectors", []) if isinstance(device.get("connectors"), list) else []:
            if not isinstance(connector, dict) or _number(connector.get("id"), int, 0, 2**32) is None:
                continue
            entry = {"id": connector["id"]}
            props = connector.get("properties")
            crtc = props.get("CRTC_ID") if isinstance(props, dict) else None
            value = crtc.get("value") if isinstance(crtc, dict) else None
            if _number(value, int, 0, 2**32) is not None:
                entry["properties"] = {"CRTC_ID": {"value": value}}
            connectors.append(entry)
        kept[node] = {"connectors": connectors}
    return json.dumps(kept)


def debugfs_projection(state: str) -> str:
    """Only CRTC ids and which CRTC each connector is on, the lines _debug_crtcs reads."""
    kept = []
    for line in state.splitlines():
        if crtc := re.fullmatch(r"crtc\[(\d{1,6})\]:\s*(crtc-\d{1,3})\s*", line):
            kept.append(f"crtc[{crtc[1]}]: {crtc[2]}")
        elif connector := re.fullmatch(r"connector\[(\d{1,6})\]:\s*(\S+)\s*", line):
            kept.append(f"connector[{connector[1]}]: {connector[2]}" if OUTPUT_NAME.fullmatch(connector[2]) else f"connector[{connector[1]}]: <output>")
        elif target := re.fullmatch(r"\s+crtc=(crtc-\d{1,3}|\(null\))\s*", line):
            if kept and kept[-1].startswith("connector["):
                kept.append(f"\tcrtc={target[1]}")
    return "".join(line + "\n" for line in kept)


def is_debugfs_state(path: str) -> bool:
    return re.fullmatch(r"/sys/kernel/debug/dri/\d{1,3}/state", path) is not None


def recorded_output(argv: list[str], stdout: str, stderr: str) -> tuple[str, str] | None:
    """What a recording keeps of a command this check runs: (stdout, stderr), or None to keep it as it is
    (then only its DCP findings are rewritten, by recorded_text)."""
    if argv[:3] == ["hyprctl", "-j", "monitors"]:
        return _monitors_projection(stdout), ""
    if argv == LAYERS:
        return _layers_projection(stdout), ""
    if len(argv) >= 4 and argv[2] == "drm_info" and argv[:2] == ["timeout", "3"]:
        return _drm_info_projection(stdout), ""
    if argv == KERNEL_LOG:
        return "".join(f"{finding}\n" for line in stdout.splitlines() if (finding := dcp_finding(line))), ""
    return None


def recorded_text(text: str) -> str:
    """Any other recorded text (the full kernel log): DCP findings replaced by their canonical form in place."""
    if not DCP.search(text):
        return text
    lines = []
    for line in text.splitlines(keepends=True):
        finding = dcp_finding(line)
        if finding:
            end = "\n" if line.endswith("\n") else ""
            line = line[:DCP.search(line).start()] + finding + end
        lines.append(line)
    return "".join(lines)
