"""The on-screen video check: a known test card, hardware decoded, on the built-in screen.

For H.264 and HEVC in turn, the bundled video-card script (testcard/) makes
the test card with ffmpeg (six solid colour bars: red, green, yellow, blue,
magenta, cyan), plays it paused and full screen on the built-in screen with
mpv, and screenshots that screen with grim. Then:

  - the screenshot (PNG, decoded with png.py) is sampled in the middle of
    each bar; each bar's median colour must be within TOLERANCE of what the
    card holds on every channel. Green, blocky or garbled frames (the M2
    Max's zero-copy decode through gpu-next) fail here.
  - mpv's log says whether hardware decode was used ("Using hardware
    decoding (vaapi-copy)"), and with which video output and GPU context:
    the mode, recorded in the evidence. Software decode fails the check.

mpv runs with the machine's own configuration (/etc/mpv/mpv.conf, then the
user's mpv.conf), so the mode checked is the one videos play in. When that
configuration leaves hardware decode off, --hwdec=auto-safe is added: the
check is about hardware decode. Only the hwdec value is read from those
files, and only it reaches the evidence.

It claims nothing about tearing, frame pacing or VRR: one paused frame is
checked.

Needs a desktop at the Mac: a Wayland session (WAYLAND_DISPLAY), a local seat
and no SSH (presence.py), an unlocked screen, Hyprland (the built-in screen is
found with hyprctl) and mpv, grim and ffmpeg, installed for the run with
consent if missing (packages.py). Otherwise both checks are skipped, with the
reason.

It needs a screen, not a person: the tool takes the screenshot and reads the
colours itself. So an unattended run (--unattended) still runs it, where it
really finds an active local seat by asking logind (presence.detect, since
presence.of says nobody is at the Mac), and the test card briefly takes the
screen of whatever session is active there. It never asks for a password: test
packages it can't install with sudo's cached or passwordless credentials skip
the checks.

A locked screen shows the lock surface over the card, so whatever the
screenshot holds says nothing about video: logind's Active stays yes, and
Omarchy's lock doesn't set its LockedHint, so Hyprland is asked, the way Omarchy
asks (omarchy-hyprland-session-locked: LOCK among a monitor's
solitaryBlockedBy). Locked before the card: nothing is played or installed, and
both checks are skipped. A result that would fail is asked again, as the screen
can lock during the check. Where Hyprland can't say, an attended run carries on,
and an unattended one, where nobody would see why it failed, is skipped.

A full-screen overlay is the same: Omarchy's screensaver is a layer-shell
overlay above a full-screen window, and on an M3 Air it put wrong colours in the
screenshot of a screen that was not locked. So the built-in screen's overlay
layer is read too (`hyprctl -j layers`, level 3), the same two times. Any
surface there covering most of the screen counts, whoever drew it; a narrower
one (notifications) or a lower layer (the bar) doesn't. A screensaver that comes
and goes within one card can still reach a result.

The test cards, screenshots and logs go in a private temporary directory
that is removed when the section ends; none of it leaves the machine.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import external_display, human, packages, png, presence
from .host import Host
from .session import Context
from .ui import Ui

CODECS = (
    ("h264", "H.264", "video.h264-on-screen"),
    ("hevc", "HEVC", "video.hevc-on-screen"),
)
CHECK_IDS = tuple(check_id for _, _, check_id in CODECS)
PACKAGES = ("mpv", "grim", "ffmpeg")
SCRIPT = "video-card"
TEMP_PREFIX = "/tmp/omarchy-m-test-video."
MAKE_TEMP = ["mktemp", "-d", TEMP_PREFIX + "XXXXXXXX"]
SYSTEM_CONFIG = "/etc/mpv/mpv.conf"
FALLBACK_HWDEC = "auto-safe"

# The test card's bars, left to right (testcard/video-card.sh draws them).
BARS = (
    ("red", (255, 0, 0)),
    ("green", (0, 255, 0)),
    ("yellow", (255, 255, 0)),
    ("blue", (0, 0, 255)),
    ("magenta", (255, 0, 255)),
    ("cyan", (0, 255, 255)),
)
# Sampled: the middle 40% of each bar's width, 15-40% of the way down (clear of
# anything drawn over the screen's corners and edges).
SAMPLE_X = (0.3, 0.7)
SAMPLE_Y = (0.15, 0.40)
SAMPLES_PER_SIDE = 16
# How far a channel may be from the card: range, matrix and scaling
# differences stay well inside it; green or garbled frames don't.
TOLERANCE = 64

CARD_FAILED, NO_BUILT_IN, NO_HYPRLAND, NO_SCREENSHOT, NEVER_SHOWN = 3, 4, 5, 6, 7
NOT_INSTALLED = 127

LOCKED = "the screen is locked: a screenshot would show the lock screen, not the test card"
COVERED = "a full-screen overlay (a screensaver, say) covers the screen: a screenshot would show it, not the test card"
HIDDEN_UNKNOWN = "Hyprland doesn't say whether the screen is locked or covered, and either in the screenshot would look like a failure"
OVERLAY_LEVEL = "3"
# The part of the screen, each way, that makes an overlay surface full-screen: room for a reserved edge.
COVERS = 0.9
_BUILT_IN = re.compile(r"(eDP|LVDS|DSI)-")  # as testcard/video-card.sh finds it

_HWDEC_USED = re.compile(r"Using hardware decoding \(([\w-]+)\)")
_SOFTWARE = re.compile(r"Using software decoding|Falling back to software decoding")
_VO = re.compile(r"\bVO: \[([\w-]+)\]")
_CONTEXT = re.compile(r"Initializing GPU context '([\w-]+)'")
_ERROR = re.compile(r"^\[[^\]]*\]\[e\]\[([^\]]+)\]\s*(\S.*)$")
_CONFIG_HWDEC = re.compile(r"^(?:--)?hwdec\s*=\s*[\"']?([\w,.-]+)[\"']?\s*$")


@dataclass(frozen=True)
class Request:
    """How hardware decode was asked for: the configuration's own hwdec, or --hwdec=auto-safe."""

    args: tuple[str, ...]
    evidence: str


def run(ctx: Context) -> list[dict]:
    why = _blocked(ctx)
    if why:
        return [_skip(check_id, why) for check_id in CHECK_IDS]
    ready = packages.temporary(ctx, PACKAGES, "The on-screen video check", ask=not human.unattended(ctx))
    if not ready.ready:
        return [_skip(check_id, ready.skipped or "", ready.evidence()) for check_id in CHECK_IDS]
    made = ctx.host.run(MAKE_TEMP)
    directory = made.stdout.strip()
    if made.returncode != 0 or not re.fullmatch(re.escape(TEMP_PREFIX) + r"[A-Za-z0-9]+", directory):
        return [_skip(check_id, "couldn't make a temporary directory for the test card", ready.evidence()) for check_id in CHECK_IDS]
    ctx.changes.register("the video check's test cards and screenshots", ["rm", "-rf", "--", directory])
    request = hwdec_request(ctx.host)
    (ctx.ui or Ui(ctx.host)).text("The built-in screen shows colour bars for a few seconds, twice: leave the keyboard and trackpad alone until they go.")
    return [_check(ctx, directory, codec, label, check_id, request, ready.evidence()) for codec, label, check_id in CODECS]


def _blocked(ctx: Context) -> str | None:
    unattended = human.unattended(ctx)
    over_ssh = "running over SSH: the test card is played on this Mac's own screen" + ("" if unattended else ", for someone at it")
    if any(ctx.host.env(name) for name in presence.SSH_VARIABLES):
        return over_ssh
    if not ctx.host.env("WAYLAND_DISPLAY"):
        return "no Wayland desktop session to show the test card in"
    # presence.of is "nobody's at the Mac" all through an unattended run; this asks logind, and doesn't change that.
    found = presence.detect(ctx.host) if unattended else presence.of(ctx.host, ctx.cache)
    if found.ssh:
        return over_ssh
    if not found.seat:
        return f"no local desktop session ({found.why_no_seat})"
    hidden = _hidden(ctx.host)
    return None if hidden == HIDDEN_UNKNOWN and not unattended else hidden


def _hidden(host: Host) -> str | None:
    """Why the card wouldn't be what a screenshot shows: LOCKED, COVERED, HIDDEN_UNKNOWN; None when it would (see the module docstring)."""
    monitors = _ask(host, external_display.MONITORS)
    locked = _locked(monitors)
    if locked:
        return LOCKED
    if locked is None:
        return HIDDEN_UNKNOWN
    layers = _ask(host, external_display.LAYERS)
    if not isinstance(layers, dict):
        return HIDDEN_UNKNOWN
    return COVERED if _covered(monitors, layers) else None


def _ask(host: Host, argv: list[str]) -> object:
    answer = host.run(argv)
    try:
        return json.loads(answer.stdout) if answer.returncode == 0 else None
    except ValueError:
        return None


def _locked(monitors: object) -> bool | None:
    """Whether Hyprland holds a session lock; None when it can't say.

    Hyprland stops at the first reason on a monitor with no workspace yet, so
    there a missing LOCK means nothing.
    """
    if not isinstance(monitors, list):
        return None
    reasons = [monitor.get("solitaryBlockedBy") for monitor in monitors if isinstance(monitor, dict)]
    reasons = [reason for reason in reasons if isinstance(reason, list)]
    if any("LOCK" in reason for reason in reasons):
        return True
    return False if any("WORKSPACE" not in reason for reason in reasons) else None


def _covered(monitors: list, layers: dict) -> bool:
    """Whether an overlay surface covers most of the built-in screen. Layer sizes are logical pixels, monitors' physical."""
    try:
        for monitor in monitors:
            if not _BUILT_IN.match(monitor["name"]):
                continue
            width, height = monitor["width"] / monitor["scale"], monitor["height"] / monitor["scale"]
            if monitor.get("transform", 0) % 2:
                width, height = height, width
            for surface in layers.get(monitor["name"], {}).get("levels", {}).get(OVERLAY_LEVEL, []):
                if surface["w"] >= COVERS * width and surface["h"] >= COVERS * height:
                    return True
    except (AttributeError, KeyError, TypeError, ZeroDivisionError):
        pass
    return False


def hwdec_request(host: Host) -> Request:
    """mpv's configured hwdec (the user's mpv.conf over /etc/mpv/mpv.conf), or --hwdec=auto-safe when it leaves it off."""
    configured = None
    for path, name in ((SYSTEM_CONFIG, SYSTEM_CONFIG), (_user_config(host), "your mpv.conf")):
        if path is None:
            continue
        try:
            text = host.read_file(path).decode("utf-8", "replace")
        except OSError:
            continue
        value = _config_hwdec(text)
        if value is not None:
            configured = (value, name)
    if configured and configured[0] != "no":
        return Request((), f"hardware decode as mpv is configured: hwdec={configured[0]} ({configured[1]})")
    why = f"hwdec={configured[0]} in {configured[1]}" if configured else "mpv's configuration leaves hardware decode off"
    return Request((f"--hwdec={FALLBACK_HWDEC}",), f"hardware decode asked for with --hwdec={FALLBACK_HWDEC} ({why})")


def _user_config(host: Host) -> str | None:
    base = host.env("MPV_HOME")
    if base:
        return f"{base}/mpv.conf"
    config = host.env("XDG_CONFIG_HOME") or (f"{host.env('HOME')}/.config" if host.env("HOME") else None)
    return f"{config}/mpv/mpv.conf" if config else None


def _config_hwdec(text: str) -> str | None:
    """The last top-level hwdec= in an mpv.conf (profiles other than [default] don't apply)."""
    value = None
    top_level = True
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("["):
            top_level = line == "[default]"
            continue
        found = _CONFIG_HWDEC.match(line) if top_level else None
        if found:
            value = found.group(1)
    return value


def _check(ctx: Context, directory: str, codec: str, label: str, check_id: str, request: Request, evidence: list[str]) -> dict:
    result = _play(ctx, directory, codec, label, check_id, request, evidence)
    if result["status"] == "fail":
        hidden = _hidden(ctx.host)
        if hidden in (LOCKED, COVERED):
            return _skip(check_id, hidden, [*evidence, request.evidence])
    return result


def _play(ctx: Context, directory: str, codec: str, label: str, check_id: str, request: Request, evidence: list[str]) -> dict:
    played = ctx.host.run_bundled(SCRIPT, [directory, codec, *request.args])
    lines = [*evidence, request.evidence]
    if played.timed_out:
        return _skip(check_id, f"the {label} test card timed out after {played.timed_out}s", lines)
    if played.returncode == NOT_INSTALLED:
        return _skip(check_id, "the tool's video-card script isn't installed next to it", lines)
    if played.returncode == CARD_FAILED:
        return _skip(check_id, f"ffmpeg couldn't make the {label} test card ({_last_line(played.stderr) or 'no reason given'})", lines)
    if played.returncode == NO_BUILT_IN:
        return _skip(check_id, "no built-in screen (a desktop Mac, or the lid is closed)", lines)
    if played.returncode == NO_HYPRLAND:
        return _skip(check_id, "Hyprland didn't answer, so the built-in screen can't be found", lines)
    output = next((line[len("output: "):].strip() for line in played.stdout.splitlines() if line.startswith("output: ")), "?")
    lines.append(f"screen: {output} (built-in), {label} test card full screen, paused")

    log = _read_text(ctx.host, f"{directory}/{codec}.log")
    mode, hardware = _mode(log)
    lines.append(f"mode (from mpv's log): {mode}")
    if played.returncode == NO_SCREENSHOT:
        return _skip(check_id, f"grim couldn't take a screenshot ({_log_error(log) or 'no reason given'})", lines)
    if played.returncode == NEVER_SHOWN:
        return _result(check_id, "fail", [*lines, f"mpv never showed the test card ({_log_error(log) or 'no error in its log'})"])
    if played.returncode != 0:
        return _result(check_id, "fail", [*lines, f"mpv couldn't play the test card (exit {played.returncode}: {_log_error(log) or 'no error in its log'})"])

    try:
        image = png.decode(ctx.host.read_file(f"{directory}/{codec}.png"))
    except (OSError, png.PngError) as error:
        return _skip(check_id, f"the screenshot couldn't be read ({error})", lines)
    wrong, samples = _compare(image)
    lines.append(f"screenshot: {image.width}x{image.height}, each bar's median colour in its middle, {TOLERANCE} levels allowed per channel")
    lines += samples
    if wrong:
        lines.append(f"on screen: {len(wrong)} of {len(BARS)} bars in the wrong colour ({', '.join(wrong)}): green, blocky or garbled frames")
    else:
        lines.append("on screen: every bar in its colour")
    if not hardware:
        lines.append("hardware decode: not used, mpv decoded the test card in software")
    return _result(check_id, "fail" if wrong or not hardware else "pass", lines)


def _mode(log: str | None) -> tuple[str, bool]:
    """What mpv's log says it did: the decode (hwdec, zero-copy or copy-back), video output and GPU context."""
    if log is None:
        return "unknown (mpv wrote no log)", False
    # The last of each: mpv may try several GPU contexts (and decoders) before one works,
    # and falls back to software decoding when hardware decode fails after it started.
    hardware = list(_HWDEC_USED.finditer(log))
    software = list(_SOFTWARE.finditer(log))
    used = hardware[-1].group(1) if hardware and (not software or software[-1].start() < hardware[-1].start()) else None
    vo = (_VO.findall(log) or [None])[-1]
    context = (_CONTEXT.findall(log) or [None])[-1]
    if used:
        hwdec = used
        decode = f"hardware decode {hwdec} ({'copy-back to memory' if hwdec.endswith('-copy') else 'zero-copy'})"
    else:
        decode = "software decode"
    parts = [decode, f"video output {vo or 'unknown'}"]
    if context:
        parts.append(f"GPU context {context}")
    return ", ".join(parts), used is not None


def _compare(image: png.Image) -> tuple[list[str], list[str]]:
    wrong, lines = [], []
    y0, y1 = (int(image.height * f) for f in SAMPLE_Y)
    for index, (name, expected) in enumerate(BARS):
        x0, x1 = (int(image.width * (index + f) / len(BARS)) for f in SAMPLE_X)
        seen = _median(image, x0, x1, y0, y1)
        ok = all(abs(a - b) <= TOLERANCE for a, b in zip(seen, expected))
        if not ok:
            wrong.append(name)
        lines.append(f"{name} bar: expected {_hex(expected)}, saw {_hex(seen)}{'' if ok else ' (wrong)'}")
    return wrong, lines


def _median(image: png.Image, x0: int, x1: int, y0: int, y1: int) -> tuple[int, int, int]:
    xs = sorted({x0 + (x1 - x0) * i // SAMPLES_PER_SIDE for i in range(SAMPLES_PER_SIDE)})
    ys = sorted({y0 + (y1 - y0) * i // SAMPLES_PER_SIDE for i in range(SAMPLES_PER_SIDE)})
    pixels = [image.pixel(x, y) for y in ys for x in xs]
    middle = len(pixels) // 2
    r, g, b = (sorted(channel)[middle] for channel in zip(*pixels))
    return r, g, b


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{c:02x}" for c in rgb)


def _read_text(host: Host, path: str) -> str | None:
    try:
        return host.read_file(path).decode("utf-8", "replace")
    except OSError:
        return None


def _log_error(log: str | None) -> str:
    """mpv's last error line, e.g. "vo/gpu-next: Failed initializing any suitable GPU context!"."""
    errors = [f"{m.group(1)}: {m.group(2).strip()}" for m in map(_ERROR.match, (log or "").splitlines()) if m]
    return errors[-1][:200] if errors else ""


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}


def _skip(check_id: str, reason: str, evidence: list[str] = ()) -> dict:
    return _result(check_id, "skip", [*evidence, f"skipped: {reason}"])
