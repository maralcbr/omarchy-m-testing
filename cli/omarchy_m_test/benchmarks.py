"""Short GPU benchmarks and video decode throughput: about a minute, off-screen.

  benchmark.opengl        glmark2 (glmark2-wayland --off-screen): a fixed set of
                          scenes, rendered into an off-screen 1920x1080 buffer
                          through the desktop's Wayland compositor
  benchmark.vulkan        vkmark with its headless window system: a fixed set of
                          scenes at 1920x1080, nothing drawn on screen
  benchmark.h264-decode   ffmpeg decoding a generated 1080p clip with VA-API
  benchmark.hevc-decode   hardware decode, as fast as it goes; software decode of
                          the same clip is measured too, for the evidence

A passed benchmark carries its score in the check (schema: checks[].score):
the value, a whole number, in its unit (glmark2's and vkmark's points, decode
frames per second), the tool and its version, and SUITE, the version of this fixed set
of scenes, clip and settings. The site compares only scores of the same
check, suite and unit. A score is the Mac's own measurement at the time, so
other work on the Mac lowers it: the human is told to leave it alone.

Pass, fail, skip:
  - a score from the Apple GPU (or hardware decode) passes;
  - rendering on a software rasteriser (llvmpipe, lavapipe), or a benchmark
    that ran and gave no score (a crash, a lost device), fails;
  - no Wayland session for glmark2, a declined or impossible package install,
    ffmpeg unable to set up VA-API, or a clip it can't encode: skipped, with why.

glmark2 needs a Wayland session: WAYLAND_DISPLAY, or over SSH the user's own
compositor socket in XDG_RUNTIME_DIR (it renders off-screen, so nothing is
shown; a window may be mapped for a moment). vkmark's headless window system
and ffmpeg need none, so they run over SSH too. Nothing here is disruptive.

glmark2, vkmark and ffmpeg are installed for the run with consent when
missing (packages.py): from the repositories as they are synced now, never
with a sync or upgrade; a package that would need a system upgrade first is
skipped, with why. The clips go in a private temporary directory that is
removed when the section ends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import macos_reference, packages
from .host import CommandResult
from .session import Context
from .ui import Ui

OPENGL, VULKAN = "benchmark.opengl", "benchmark.vulkan"
CODECS = (
    ("h264", "H.264", "benchmark.h264-decode", ("-c:v", "libx264", "-preset", "veryfast")),
    ("hevc", "HEVC", "benchmark.hevc-decode", ("-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=error")),
)
CHECK_IDS = (OPENGL, VULKAN, *(check_id for _, _, check_id, _ in CODECS))

# Bump when the scenes, their duration, the size, the clip or the decode settings change: scores of
# different suites aren't compared.
SUITE = 1
SIZE = "1920x1080"
SCENE_SECONDS = 3
GLMARK2 = "glmark2-wayland"
GLMARK2_SCENES = (
    "build:use-vbo=true",
    "texture:texture-filter=linear",
    "shading:shading=phong",
    "bump:bump-render=high-poly",
    "effect2d:kernel=0,1,0;1,-4,1;0,1,0;",
    "desktop:blur-radius=5:effect=blur:passes=1:separable=true:windows=4",
    "jellyfish",
    "terrain",
)
VKMARK_SCENES = (
    "vertex:device-local=true",
    "texture:anisotropy=16",
    "shading:shading=phong",
    "shading:shading=cel",
    "effect2d:kernel=blur",
    "desktop",
    "cube",
)
CLIP_SECONDS = 5
CLIP_SOURCE = f"testsrc2=size={SIZE}:rate=30"
CLIP_FRAMES = CLIP_SECONDS * 30
LOOPS = 3  # the clip is decoded 1 + LOOPS times, so a fast decoder runs long enough to time
TEMP_PREFIX = "/tmp/omarchy-m-test-bench."
MAKE_TEMP = ["mktemp", "-d", TEMP_PREFIX + "XXXXXXXX"]
NOT_INSTALLED, TIMED_OUT = 127, 124

_SOCKET = re.compile(r"wayland-[0-9]+")
_TOOL_VERSION = re.compile(r"^\s*(glmark2|vkmark) (\d[\w.+~-]*)\s*$", re.M)
_SCORE = re.compile(r"^\s*(?:glmark2|vkmark) Score: (\d+)\s*$", re.M)
_SCENE = re.compile(r"^\[([\w-]+)\] (.*?): FPS: (\d+) FrameTime: ([\d.]+) ms", re.M)
_FAILED_SCENE = re.compile(r"^\[([\w-]+)\] (.*?): (?:Failed with exception: |Unsupported|Set up failed)(.*)$", re.M)
_GL = re.compile(r"^\s*GL_(RENDERER|VERSION):\s*(\S.*?)\s*$", re.M)
_VK_DEVICE = re.compile(r"^\s*Device Name:\s*(\S.*?)\s*$", re.M)
_SOFTWARE_RENDERER = re.compile(r"llvmpipe|softpipe|lavapipe|swrast|software", re.I)
_NO_WAYLAND = re.compile(r"Failed to connect to (?:the )?Wayland display|wl_display_connect|Couldn't connect to Wayland", re.I)
_CONTEXT = re.compile(r"^\[[^\]]* @ 0x[0-9a-fA-F]+\]\s*")
_POINTER = re.compile(r"\b0x[0-9a-fA-F]{8,}\b")
_FFMPEG_VERSION = re.compile(r"^ffmpeg version (\S+)", re.M)
_FRAMES = re.compile(r"frame=\s*(\d+)")
_RTIME = re.compile(r"\brtime=([\d.]+)s")
_NO_VAAPI = re.compile(
    r"Failed setup for format vaapi|hwaccel initialisation returned error|No VA display found|"
    r"Device creation failed|Failed to initialise VAAPI|Error creating a VAAPI device|vaInitialize failed", re.I)


@dataclass(frozen=True)
class Wayland:
    """How glmark2 reaches a Wayland compositor: the argv prefix to run it with, or why it can't."""

    prefix: tuple[str, ...] = ()
    missing: str | None = None


def run(ctx: Context) -> list[dict]:
    wayland = find_wayland(ctx)
    wanted = [*(["glmark2"] if wayland.missing is None else []), "vkmark", "ffmpeg"]
    ready = packages.temporary(ctx, wanted, "The Benchmarks section")
    evidence = ready.evidence()
    if not ready.ready:
        return [_skip(check_id, ready.skipped or "", evidence) for check_id in CHECK_IDS]
    (ctx.ui or Ui(ctx.host)).text(
        "Short benchmarks run now, about a minute in all: OpenGL (glmark2, off-screen), Vulkan (vkmark, headless) "
        "and video decode (ffmpeg). Leave the Mac alone until they finish: anything else it does lowers the scores."
    )
    results = [opengl(ctx, wayland, evidence), vulkan(ctx, evidence)]
    return [*results, *decode(ctx, evidence)]


def find_wayland(ctx: Context) -> Wayland:
    if ctx.host.env("WAYLAND_DISPLAY"):
        return Wayland()
    runtime = ctx.host.env("XDG_RUNTIME_DIR")
    if runtime:
        try:
            sockets = sorted((name for name in ctx.host.list_dir(runtime) if _SOCKET.fullmatch(name)), key=lambda n: int(n.split("-")[1]))
        except OSError:
            sockets = []
        if sockets:
            return Wayland(("env", f"WAYLAND_DISPLAY={sockets[0]}"))
    return Wayland(missing="no Wayland session to render in (glmark2 renders off-screen, but through the desktop's compositor)")


def glmark2_argv(wayland: Wayland) -> list[str]:
    return [*wayland.prefix, GLMARK2, "--off-screen", "-s", SIZE, "-b", f":duration={SCENE_SECONDS}",
            *(arg for scene in GLMARK2_SCENES for arg in ("-b", scene))]


def vkmark_argv() -> list[str]:
    return ["vkmark", "--winsys", "headless", "-s", SIZE, "-b", f":duration={SCENE_SECONDS}",
            *(arg for scene in VKMARK_SCENES for arg in ("-b", scene))]


def clip_argv(directory: str, codec: str, encoder: tuple[str, ...]) -> list[str]:
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", CLIP_SOURCE,
            "-t", str(CLIP_SECONDS), *encoder, "-pix_fmt", "yuv420p", f"{directory}/clip-{codec}.mp4"]


def hardware_decode_argv(directory: str, codec: str) -> list[str]:
    return ["ffmpeg", "-nostdin", "-benchmark", "-stream_loop", str(LOOPS), "-hwaccel", "vaapi", "-hwaccel_output_format", "vaapi",
            "-i", f"{directory}/clip-{codec}.mp4", "-f", "null", "-"]


def software_decode_argv(directory: str, codec: str) -> list[str]:
    return ["ffmpeg", "-nostdin", "-hide_banner", "-benchmark", "-stream_loop", str(LOOPS),
            "-i", f"{directory}/clip-{codec}.mp4", "-f", "null", "-"]


# -- OpenGL and Vulkan -------------------------------------------------------------------

def opengl(ctx: Context, wayland: Wayland, evidence: list[str]) -> dict:
    if wayland.missing:
        return _skip(OPENGL, wayland.missing, evidence)
    done = ctx.host.run(glmark2_argv(wayland))
    lines = [*evidence, f"{GLMARK2} --off-screen at {SIZE}, {len(GLMARK2_SCENES)} scenes of {SCENE_SECONDS} s (suite {SUITE})"]
    if done.returncode == NOT_INSTALLED:
        return _skip(OPENGL, f"{GLMARK2} isn't installed", lines)
    if _NO_WAYLAND.search(done.stdout + done.stderr):
        return _skip(OPENGL, "glmark2 couldn't connect to the Wayland session", lines)  # its message names the socket's path
    info = {key: value for key, value in _GL.findall(done.stdout)}
    renderer = info.get("RENDERER")
    if renderer:
        lines.append(f"renderer: {renderer}" + (f", OpenGL {info['VERSION']}" if info.get("VERSION") else ""))
    return _scored(OPENGL, "glmark2", done, renderer, lines)


def vulkan(ctx: Context, evidence: list[str]) -> dict:
    done = ctx.host.run(vkmark_argv())
    lines = [*evidence, f"vkmark --winsys headless at {SIZE}, {len(VKMARK_SCENES)} scenes of {SCENE_SECONDS} s (suite {SUITE})"]
    if done.returncode == NOT_INSTALLED:
        return _skip(VULKAN, "vkmark isn't installed", lines)
    found = _VK_DEVICE.search(done.stdout)
    device = found.group(1) if found else None
    if device:
        lines.append(f"device: {device}")
    return _scored(VULKAN, "vkmark", done, device, lines)


def _scored(check_id: str, name: str, done: CommandResult, renderer: str | None, lines: list[str]) -> dict:
    """A glmark2 or vkmark run as a check: its scenes, its score, and whether the Apple GPU gave it."""
    version = next((v for tool, v in _TOOL_VERSION.findall(done.stdout) if tool == name), None)
    for scene, options, fps, frame_time in _SCENE.findall(done.stdout):
        lines.append(f"[{scene}] {options}: {fps} fps ({frame_time} ms a frame)")
    failed = _FAILED_SCENE.findall(done.stdout + "\n" + done.stderr)
    for scene, options, why in failed:
        why = _clean(why)
        lines.append(f"[{scene}] {options}: failed{f' ({why[:200]})' if why else ''}")
    score = _SCORE.search(done.stdout)
    if done.returncode == TIMED_OUT:
        return _result(check_id, "fail", [*lines, f"{name} didn't finish in time"])
    if score is None:
        why = _last_line(done.stderr) or _last_line(done.stdout) or f"exit {done.returncode}"
        return _result(check_id, "fail", [*lines, f"{name} gave no score ({why})"])
    lines.append(f"{name} score: {score.group(1)}")
    if renderer and _SOFTWARE_RENDERER.search(renderer):
        return _result(check_id, "fail", [*lines, f"rendered in software ({renderer}), not on the Apple GPU"])
    if renderer is None:
        return _result(check_id, "fail", [*lines, f"{name} didn't say which GPU it rendered on"])
    if failed:
        return _result(check_id, "fail", [*lines, f"{len(failed)} scene(s) failed"])
    tool = f"{name} {version}" if version else name
    return _result(check_id, "pass", lines, _score(int(score.group(1)), "points", tool))


# -- video decode ------------------------------------------------------------------------

def decode(ctx: Context, evidence: list[str]) -> list[dict]:
    made = ctx.host.run(MAKE_TEMP)
    directory = made.stdout.strip()
    if made.returncode != 0 or not re.fullmatch(re.escape(TEMP_PREFIX) + r"[A-Za-z0-9]+", directory):
        return [_skip(check_id, "couldn't make a temporary directory for the clips", evidence) for _, _, check_id, _ in CODECS]
    ctx.changes.register("the decode benchmark's clips", ["rm", "-rf", "--", directory])
    return [_decode(ctx, directory, codec, label, check_id, encoder, evidence) for codec, label, check_id, encoder in CODECS]


def _decode(ctx: Context, directory: str, codec: str, label: str, check_id: str, encoder: tuple[str, ...], evidence: list[str]) -> dict:
    lines = [*evidence, f"clip: {label} {SIZE}, {CLIP_FRAMES} frames ({CLIP_SOURCE.split('=')[0]}, {encoder[1]} {encoder[3]}), "
                        f"decoded {1 + LOOPS} times to a null output (suite {SUITE})"]
    made = ctx.host.run(clip_argv(directory, codec, encoder))
    if made.returncode == NOT_INSTALLED:
        return _skip(check_id, "ffmpeg isn't installed", lines)
    if made.returncode != 0:
        return _skip(check_id, f"ffmpeg couldn't make the {label} clip ({_last_line(made.stderr) or f'exit {made.returncode}'})", lines)

    software = ctx.host.run(software_decode_argv(directory, codec))
    soft = _throughput(software)
    lines.append(f"software decode: {_words(soft)}" if soft else f"software decode: not measured ({_last_line(software.stderr) or f'exit {software.returncode}'})")

    hardware = ctx.host.run(hardware_decode_argv(directory, codec))
    found = _FFMPEG_VERSION.search(hardware.stderr)
    tool = f"ffmpeg {found.group(1)}" if found else "ffmpeg"
    if _NO_VAAPI.search(hardware.stderr):
        line = _clean(next((line for line in hardware.stderr.splitlines() if _NO_VAAPI.search(line)), ""))
        return _skip(check_id, f"ffmpeg couldn't set up VA-API hardware decode ({line[:200]})", lines)
    if hardware.returncode == TIMED_OUT:
        return _result(check_id, "fail", [*lines, "hardware decode didn't finish in time"])
    hard = _throughput(hardware)
    if hardware.returncode != 0 or hard is None:
        return _result(check_id, "fail", [*lines, f"hardware decode (VA-API) failed ({_last_line(hardware.stderr) or f'exit {hardware.returncode}'})"])
    frames, seconds, fps = hard
    if frames < CLIP_FRAMES * (1 + LOOPS):
        return _result(check_id, "fail", [*lines, f"hardware decode (VA-API) stopped after {frames} of {CLIP_FRAMES * (1 + LOOPS)} frames"])
    lines.append(f"hardware decode (VA-API): {_words(hard)}")
    if soft:
        lines.append(f"hardware decode is {fps / soft[2]:.2f}x software decode")
    lines.append(tool)
    reference = macos_reference.load(ctx.machine.board)
    lines += macos_reference.line(reference, f"{label} hardware decode", round(fps), "fps", (reference or {}).get("metrics", {}).get(f"{codec}_decode_fps"))
    return _result(check_id, "pass", lines, _score(round(fps), "fps", tool))


def _throughput(done: CommandResult) -> tuple[int, float, float] | None:
    """(frames, seconds, frames per second) from ffmpeg -benchmark's last progress line and its bench: rtime."""
    frames = _FRAMES.findall(done.stderr)
    rtime = _RTIME.findall(done.stderr)
    if done.returncode != 0 or not frames or not rtime or float(rtime[-1]) <= 0:
        return None
    count, seconds = int(frames[-1]), float(rtime[-1])
    return count, seconds, count / seconds


def _words(measured: tuple[int, float, float]) -> str:
    frames, seconds, fps = measured
    return f"{frames} frames in {seconds:.2f} s, {fps:.1f} fps"


# -- results -----------------------------------------------------------------------------

def _score(value: int, unit: str, tool: str) -> dict:
    """Whole numbers only: a report has no floating-point numbers (signing.py's canonical form)."""
    return {"value": value, "unit": unit, "tool": tool[:80], "suite": SUITE}


def _clean(line: str) -> str:
    """A tool's message without the memory addresses ffmpeg and Mesa print ("[libx265 @ 0xaaab0c1f2e40] ...")."""
    return _POINTER.sub("0x…", _CONTEXT.sub("", line.strip())).strip()


def _last_line(text: str) -> str:
    lines = [_clean(line) for line in text.replace("\r", "\n").splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _result(check_id: str, status: str, evidence: list[str], score: dict | None = None) -> dict:
    result = {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
    if score is not None:
        result["score"] = score
    return result


def _skip(check_id: str, reason: str, evidence: list[str] = ()) -> dict:
    return _result(check_id, "skip", [*evidence, f"skipped: {reason}"])
