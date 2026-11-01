"""Seam A: the Benchmarks section, through the whole CLI against a recorded Mac.

The Mac is the recorded converged M2 Max (tests/live_mac.py keeps its
packages and login session). Its benchmark tools print what's below.

Provenance: no glmark2, vkmark or ffmpeg benchmark run has been saved from
the M1 Pro or the M2 Max yet (both were busy with the convergence work when
this section was written), so these outputs are reconstructed, not
captured: each is in the exact format its tool prints (glmark2 2023.01 and
vkmark 2025.01, the versions in Arch Linux ARM's extra repository, and ffmpeg
-benchmark's progress and bench: lines), with the M2 Max's GPU name and Mesa
version from its converged image, and made-up scores. The next dogfood run
on each Mac records the real ones (--record) and replaces them.

schema/golden/benchmarks/m2-max-converged.json is the report of the section
run at the M2's desktop with every tool installed; Seam B posts it. After a
change to the section, the catalogue or the recording, write it again with
cli/scripts/reseed_recordings.py (or from cli/: python3 -m tests.test_benchmarks regenerate).
"""

from __future__ import annotations

import json
import os
import sys
import unittest

from omarchy_m_test import engines, benchmarks
from omarchy_m_test.app import main
from omarchy_m_test.sections import APPLE
from tests.desktop import command, recording
from tests.live_mac import CATALOGUE_PATH, LiveMac, MacState, live_recording
from tests.schema_validator import errors

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "schema")
GOLDEN = os.path.join(SCHEMA_DIR, "golden", "benchmarks", "m2-max-converged.json")
REPORT_FILE = "omarchy-m-test-report.json"
ENTER = ""
ARGS = ["--dry-run", "--catalogue", CATALOGUE_PATH]
BENCHMARKS = tuple(section for section in APPLE if section.id == "benchmarks")
TOOLS = {"glmark2", "vkmark", "ffmpeg"}
RUNTIME = "/run/user/1000"

TEMP = "/tmp/omarchy-m-test-bench.K3v9Qm2x"
MAKE_TEMP = ["mktemp", "-d", "/tmp/omarchy-m-test-bench.XXXXXXXX"]
REMOVE_TEMP = ["rm", "-rf", "--", TEMP]

with open(os.path.join(SCHEMA_DIR, "report-v1.schema.json"), encoding="utf-8") as f:
    SCHEMA = json.load(f)


# -- what the tools print ----------------------------------------------------------------

GPU = "Apple M2 Max (G14C B1)"
LINE = "=" * 55


def glmark2_output(renderer: str = GPU, score: int | None = 2987, fps: tuple[int, ...] = (4120, 3890, 3310, 2870, 2460, 2230, 2750, 280)) -> str:
    """glmark2-wayland 2023.01 --off-screen: its banner, the scenes (FPS per scene) and the score."""
    lines = [
        LINE, "    glmark2 2023.01", LINE,
        "    OpenGL Information",
        "    GL_VENDOR:      Mesa",
        f"    GL_RENDERER:    {renderer}",
        "    GL_VERSION:     4.6 (Compatibility Profile) Mesa 26.2.3",
        "    Surface Config: buf=32 r=8 g=8 b=8 a=8 depth=24 stencil=0 samples=0",
        "    Surface Size:   1920x1080 off-screen",
        LINE,
    ]
    for scene, value in zip(benchmarks.GLMARK2_SCENES, fps):
        name, _, options = scene.partition(":")
        lines.append(f"[{name}] {options or '<default>'}: FPS: {value} FrameTime: {1000 / value:.3f} ms")
    if score is not None:
        lines += [LINE, f"                                  glmark2 Score: {score} ", LINE]
    return "\n".join(lines) + "\n"


def vkmark_output(device: str = GPU, score: int | None = 6124, failed: str | None = None,
                  fps: tuple[int, ...] = (9870, 7310, 6480, 6620, 3140, 5210, 4240)) -> str:
    """vkmark 2025.01 --winsys headless: its device (the UUID stays out of reports), the scenes and the score."""
    lines = [
        LINE, "    vkmark 2025.01", LINE,
        "    Vendor ID:      0x10005",
        "    Device ID:      0x0",
        f"    Device Name:    {device}",
        "    Driver Version: 109055491",
        "    Device UUID:    9f1c33b6a5e04d1e8c1f2b7a44d09e61",
        LINE,
    ]
    for scene, value in zip(benchmarks.VKMARK_SCENES, fps):
        name, _, options = scene.partition(":")
        if failed == name:
            lines.append(f"[{name}] {options or '<default>'}: Failed with exception: vk::Queue::submit: ErrorDeviceLost")
            break
        lines.append(f"[{name}] {options or '<default>'}: FPS: {value} FrameTime: {1000 / value:.3f} ms")
    if score is not None:
        lines += [LINE, f"                                   vkmark Score: {score}", LINE]
    return "\n".join(lines) + "\n"


FFMPEG_BANNER = (
    "ffmpeg version n9.0.2 Copyright (c) 2000-2026 the FFmpeg developers\n"
    "  built with gcc 15.2.1 (GCC) 20260103\n"
    "  libavutil      60. 22.100 / 60. 22.100\n"
)


def ffmpeg_decode(frames: int, seconds: float, codec: str, hardware: bool) -> str:
    """ffmpeg -benchmark decoding the clip to the null muxer: stream lines, \\r progress, bench: lines."""
    decoder = "h264" if codec == "h264" else "hevc"
    text = FFMPEG_BANNER if hardware else ""
    text += (
        f"Input #0, mov,mp4,m4a,3gp,3g2,mj2, from '{TEMP}/clip-{codec}.mp4':\n"
        f"  Stream #0:0[0x1](und): Video: {decoder}, yuv420p(tv, progressive), 1920x1080, 30 fps\n"
        "Stream mapping:\n"
        f"  Stream #0:0 -> #0:0 ({decoder} (native) -> wrapped_avframe (native))\n"
        "Output #0, null, to 'pipe:':\n"
        f"frame=  {frames // 2} fps={frames / seconds:.0f} q=-0.0 size=N/A time=00:00:10.00 bitrate=N/A speed=  10x    \r"
        f"frame=  {frames} fps={frames / seconds:.0f} q=-0.0 Lsize=N/A time=00:00:20.00 bitrate=N/A speed=13.7x    \n"
        f"bench: utime=0.412s stime=0.301s rtime={seconds:.3f}s\n"
        "bench: maxrss=98304KiB\n"
    )
    return text


NO_VAAPI = (
    FFMPEG_BANNER
    + "[AVHWDeviceContext @ 0xaaab0c1f2e40] No VA display found for any default device.\n"
    "Device creation failed: -22.\n"
    "Failed to set value 'vaapi' for option 'hwaccel': Invalid argument\n"
    "Error parsing global options: Invalid argument\n"
)

FRAMES = benchmarks.CLIP_FRAMES * (1 + benchmarks.LOOPS)
# (hardware seconds, software seconds) per codec for FRAMES frames.
DECODE_SECONDS = {"h264": (0.912, 1.456), "hevc": (1.024, 1.842)}


# -- the Mac -----------------------------------------------------------------------------

def bench_mac(glmark2: str | None = None, vkmark: str | None = None, hardware: dict | None = None, env: dict | None = None,
              clip_fails: str | None = None, installed: set[str] | None = None, answers=(), runtime: list[str] | None = None,
              state: MacState | None = None) -> LiveMac:
    """The recorded M2 at its desktop, answering the section's commands as the tools above print."""
    base = recording("m2-max-converged")
    # Its corpus stand-ins for this section are replaced by the answers below and the Mac's package state.
    base["commands"] = [entry for entry in base["commands"] if not ({"vkmark", "assimp"} & set(entry["argv"]))]
    base["env"] = {name: value for name, value in base["env"].items() if name != "SSH_CONNECTION"}
    rec = live_recording({"WAYLAND_DISPLAY": "wayland-1", **(env or {})}, base=base)
    rec["env"] = {name: value for name, value in rec["env"].items() if value is not None}
    wayland = () if rec["env"].get("WAYLAND_DISPLAY") else ("env", "WAYLAND_DISPLAY=wayland-1")
    rec["commands"] += [
        command(MAKE_TEMP, TEMP + "\n"),
        command(REMOVE_TEMP),
        command(benchmarks.glmark2_argv(benchmarks.Wayland(wayland)), glmark2 if glmark2 is not None else glmark2_output()),
        command(benchmarks.vkmark_argv(), vkmark if vkmark is not None else vkmark_output()),
    ]
    if runtime is not None:
        rec["dirs"][RUNTIME] = runtime
    for codec, _, _, encoder in benchmarks.CODECS:
        hard, soft = DECODE_SECONDS[codec]
        if clip_fails == codec:
            rec["commands"].append(command(benchmarks.clip_argv(TEMP, codec, encoder), "", 1, "Unknown encoder 'libx265'\n"))
            continue
        rec["commands"] += [
            command(benchmarks.clip_argv(TEMP, codec, encoder)),
            command(benchmarks.software_decode_argv(TEMP, codec), "", 0, ffmpeg_decode(FRAMES, soft, codec, hardware=False)),
        ]
        answer = (hardware or {}).get(codec)
        if answer is None:
            answer = command([], "", 0, ffmpeg_decode(FRAMES, hard, codec, hardware=True))
        rec["commands"].append({**answer, "argv": benchmarks.hardware_decode_argv(TEMP, codec)})
    mac_state = state or MacState(installed={"mesa", "pipewire", *(TOOLS if installed is None else installed)})
    mac_state.repository.update({"vkmark": ["assimp", "vkmark"], "glmark2": ["glmark2"], "ffmpeg": ["ffmpeg"]})
    return LiveMac(rec, state=mac_state, answers=[ENTER, *answers])


def run(host: LiveMac) -> tuple[dict, dict]:
    status = main(ARGS, host, sections=BENCHMARKS)
    assert status == 0, host.output
    report = json.loads(host.written[REPORT_FILE])
    assert errors(SCHEMA, report) == [], errors(SCHEMA, report)
    return report, {check["id"]: check for check in report["checks"]}


def ran(host: LiveMac, tool: str) -> list[list[str]]:
    return [argv for argv in host.commands_run if tool in argv[:3]]


def golden_report() -> str:
    """The section's report at the M2's desktop with every tool installed (the Seam B golden)."""
    host = bench_mac()
    main(ARGS[:1], host, sections=BENCHMARKS)
    return host.written[REPORT_FILE]


def regenerate() -> None:
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as f:
        f.write(golden_report())


# -- tests -------------------------------------------------------------------------------

class ScoresTest(unittest.TestCase):
    def test_each_benchmark_passes_with_its_score_in_the_report(self):
        report, checks = run(bench_mac())

        self.assertEqual([check["id"] for check in report["checks"]], [*benchmarks.CHECK_IDS, *engines.CHECK_IDS])
        self.assertEqual(checks["benchmark.opengl"]["score"], {"value": 2987, "unit": "points", "tool": "glmark2 2023.01", "suite": 1})
        self.assertEqual(checks["benchmark.vulkan"]["score"], {"value": 6124, "unit": "points", "tool": "vkmark 2025.01", "suite": 1})
        self.assertEqual(checks["benchmark.h264-decode"]["score"], {"value": round(FRAMES / 0.912), "unit": "fps", "tool": "ffmpeg n9.0.2", "suite": 1})
        self.assertEqual(checks["benchmark.hevc-decode"]["score"]["value"], round(FRAMES / 1.024))
        for check_id in benchmarks.CHECK_IDS:
            check = checks[check_id]
            with self.subTest(check_id):
                self.assertEqual((check["kind"], check["status"], check["classification"]["outcome"]), ("automatic", "pass", "works"))
        self.assertEqual(checks["benchmark.opengl"]["classification"]["feature"], "gpu")
        self.assertEqual(checks["benchmark.h264-decode"]["classification"]["feature"], "video-decoder")

    def test_the_run_and_explain_show_each_score(self):
        host = bench_mac()
        run(host)

        self.assertIn("  PASS  benchmark.opengl  works (GPU), score 2987 points (glmark2 2023.01)", host.output)
        self.assertIn(f"  PASS  benchmark.h264-decode  works (Video decoder), score {round(FRAMES / 0.912)} fps (ffmpeg n9.0.2)", host.output)
        explained = LiveMac({"recording_version": 1, "files": {"r.json": {"text": host.written[REPORT_FILE]}, CATALOGUE_PATH: host.recording["files"][CATALOGUE_PATH]}})
        self.assertEqual(main(["--explain", "r.json", "--catalogue", CATALOGUE_PATH], explained), 0)
        self.assertIn("  PASS  benchmark.vulkan  works (GPU), score 6124 points (vkmark 2025.01)", explained.output)

    def test_the_evidence_names_the_gpu_scenes_and_both_decode_speeds(self):
        _, checks = run(bench_mac())

        opengl = checks["benchmark.opengl"]["evidence"]
        self.assertIn("glmark2-wayland --off-screen at 1920x1080, 8 scenes of 3 s (suite 1)", opengl)
        self.assertIn(f"renderer: {GPU}, OpenGL 4.6 (Compatibility Profile) Mesa 26.2.3", opengl)
        self.assertIn("[build] use-vbo=true: 4120 fps (0.243 ms a frame)", opengl)
        self.assertIn("glmark2 score: 2987", opengl)
        vulkan = checks["benchmark.vulkan"]["evidence"]
        self.assertIn(f"device: {GPU}", vulkan)
        self.assertIn("vkmark score: 6124", vulkan)
        self.assertFalse(any("UUID" in line or "9f1c33b6" in line for line in vulkan))
        h264 = checks["benchmark.h264-decode"]["evidence"]
        self.assertIn(f"hardware decode (VA-API): {FRAMES} frames in 0.91 s, {FRAMES / 0.912:.1f} fps", h264)
        self.assertIn(f"software decode: {FRAMES} frames in 1.46 s, {FRAMES / 1.456:.1f} fps", h264)
        self.assertIn(f"hardware decode is {1.456 / 0.912:.2f}x software decode", h264)
        self.assertIn("already installed: glmark2 vkmark ffmpeg", h264)

    def test_the_benchmarks_render_off_screen_for_about_a_minute(self):
        host = bench_mac()
        run(host)

        glmark2, = ran(host, "glmark2-wayland")
        self.assertIn("--off-screen", glmark2)
        vkmark, = ran(host, "vkmark")
        self.assertEqual(vkmark[1:3], ["--winsys", "headless"])
        scenes = len(benchmarks.GLMARK2_SCENES) + len(benchmarks.VKMARK_SCENES)
        self.assertLessEqual(scenes * benchmarks.SCENE_SECONDS, 50)
        self.assertIn("about a minute", host.output)

    def test_the_clips_are_removed_when_the_section_ends(self):
        host = bench_mac()
        run(host)

        self.assertEqual(host.commands_run.count(REMOVE_TEMP), 1)
        self.assertGreater(host.commands_run.index(REMOVE_TEMP), max(host.commands_run.index(argv) for argv in ran(host, "ffmpeg")))

    def test_the_golden_report_is_what_the_section_writes(self):
        with open(GOLDEN, encoding="utf-8") as f:
            self.assertEqual(golden_report(), f.read())


class FailuresTest(unittest.TestCase):
    def test_software_rendering_fails_and_carries_no_score(self):
        _, checks = run(bench_mac(glmark2=glmark2_output(renderer="llvmpipe (LLVM 21.1.0, 128 bits)"),
                                  vkmark=vkmark_output(device="llvmpipe (LLVM 21.1.0, 128 bits)")))

        for check_id in ("benchmark.opengl", "benchmark.vulkan"):
            with self.subTest(check_id):
                check = checks[check_id]
                self.assertEqual((check["status"], check["classification"]["outcome"]), ("fail", "fails"))
                self.assertNotIn("score", check)
                self.assertIn("rendered in software (llvmpipe (LLVM 21.1.0, 128 bits)), not on the Apple GPU", check["evidence"])

    def test_a_lost_device_fails_the_vulkan_benchmark(self):
        _, checks = run(bench_mac(vkmark=vkmark_output(score=None, failed="effect2d")))

        check = checks["benchmark.vulkan"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("[effect2d] kernel=blur: failed (vk::Queue::submit: ErrorDeviceLost)", check["evidence"])
        self.assertIn("vkmark gave no score", check["evidence"][-1])

    def test_without_va_api_decode_is_skipped_with_the_software_speed(self):
        _, checks = run(bench_mac(hardware={"h264": command([], "", 1, NO_VAAPI), "hevc": command([], "", 1, NO_VAAPI)}))

        check = checks["benchmark.h264-decode"]
        self.assertEqual((check["status"], check["classification"]["outcome"]), ("skip", "not-tested"))
        self.assertNotIn("score", check)
        self.assertIn(f"software decode: {FRAMES} frames in 1.46 s, {FRAMES / 1.456:.1f} fps", check["evidence"])
        self.assertIn("skipped: ffmpeg couldn't set up VA-API hardware decode (No VA display found for any default device.)", check["evidence"])

    def test_decode_that_stops_early_fails(self):
        _, checks = run(bench_mac(hardware={"hevc": command([], "", 0, ffmpeg_decode(212, 0.9, "hevc", hardware=True))}))

        self.assertEqual(checks["benchmark.hevc-decode"]["status"], "fail")
        self.assertIn(f"hardware decode (VA-API) stopped after 212 of {FRAMES} frames", checks["benchmark.hevc-decode"]["evidence"])
        self.assertEqual(checks["benchmark.h264-decode"]["status"], "pass")

    def test_a_clip_ffmpeg_cant_encode_is_skipped_not_failed(self):
        _, checks = run(bench_mac(clip_fails="hevc"))

        self.assertEqual(checks["benchmark.hevc-decode"]["status"], "skip")
        self.assertIn("skipped: ffmpeg couldn't make the HEVC clip (Unknown encoder 'libx265')", checks["benchmark.hevc-decode"]["evidence"])

    def test_the_tools_memory_addresses_never_reach_the_evidence(self):
        failed = FFMPEG_BANNER + "[hevc @ 0xaaab0c1f2e40] Failed to end picture decode issue: 23 (internal decoding error).\n"
        _, checks = run(bench_mac(hardware={"hevc": command([], "", 187, failed)}))

        check = checks["benchmark.hevc-decode"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("hardware decode (VA-API) failed (Failed to end picture decode issue: 23 (internal decoding error).)", check["evidence"])
        self.assertFalse(any("0xaaab" in line for line in check["evidence"]))


class WhereItRunsTest(unittest.TestCase):
    def test_over_ssh_glmark2_uses_the_desktops_own_wayland_socket(self):
        host = bench_mac(env={"WAYLAND_DISPLAY": None, "SSH_CONNECTION": "10.0.0.2 51000 10.0.0.9 22", "XDG_RUNTIME_DIR": RUNTIME},
                         runtime=["bus", "wayland-1", "wayland-1.lock", "hypr", "pipewire-0"])

        _, checks = run(host)

        glmark2, = ran(host, "glmark2-wayland")
        self.assertEqual(glmark2[:2], ["env", "WAYLAND_DISPLAY=wayland-1"])
        self.assertEqual(checks["benchmark.opengl"]["status"], "pass")
        self.assertFalse(any(RUNTIME in line for check in checks.values() for line in check["evidence"]))

    def test_without_a_wayland_session_only_opengl_is_skipped_and_glmark2_isnt_offered(self):
        host = bench_mac(env={"WAYLAND_DISPLAY": None, "SSH_CONNECTION": "10.0.0.2 51000 10.0.0.9 22"}, installed={"ffmpeg"}, )

        _, checks = run(host)

        self.assertEqual(checks["benchmark.opengl"]["status"], "skip")
        self.assertIn("skipped: no Wayland session to render in", checks["benchmark.opengl"]["evidence"][-1])
        self.assertEqual(ran(host, "glmark2-wayland"), [])
        self.assertIn("The Benchmarks section needs a package that isn't installed: vkmark (with assimp)", host.output)
        self.assertEqual([checks[c]["status"] for c in benchmarks.CHECK_IDS[1:]], ["pass", "pass", "pass"])


class TemporaryPackagesTest(unittest.TestCase):
    def test_missing_tools_are_installed_without_a_question_and_removed_afterwards(self):
        host = bench_mac(installed=set())

        _, checks = run(host)

        self.assertIn("The Benchmarks section needs packages that aren't installed: glmark2 vkmark ffmpeg (with assimp)", host.output)
        installs = [argv for argv in host.commands_run if argv[:4] == ["sudo", "-n", "pacman", "-S"]]
        self.assertEqual(installs, [["sudo", "-n", "pacman", "-S", "--needed", "--noconfirm", "--asdeps", "glmark2", "vkmark", "ffmpeg"]])
        self.assertFalse(any("-Sy" in argv or "-Syu" in argv for argv in host.commands_run))
        self.assertEqual(host.state.installed, {"mesa", "pipewire"})  # exactly what it installed was removed
        self.assertIn("installed for this run, removed at its end: glmark2 assimp vkmark ffmpeg", checks["benchmark.vulkan"]["evidence"])
        self.assertEqual(checks["benchmark.vulkan"]["status"], "pass")
        # Nothing runs before the install, and the removal comes after the last benchmark.
        removal = next(i for i, argv in enumerate(host.commands_run) if argv[:4] == ["sudo", "-n", "pacman", "-R"])
        self.assertGreater(removal, max(host.commands_run.index(argv) for argv in ran(host, "ffmpeg")))

    def test_without_sudo_every_benchmark_is_skipped_and_nothing_changes(self):
        host = bench_mac(installed={"ffmpeg"}, state=MacState(installed={"mesa", "pipewire", "ffmpeg"}, sudo_cached=False))

        _, checks = run(host)

        for check_id in benchmarks.CHECK_IDS:
            self.assertEqual(checks[check_id]["status"], "skip")
            self.assertIn("installing test packages needs sudo", checks[check_id]["evidence"][-1])
        self.assertFalse(any(argv[:2] == ["sudo", "-n"] and argv != ["sudo", "-n", "true"] for argv in host.commands_run))
        self.assertEqual(ran(host, "vkmark"), [])

    def test_a_tool_that_needs_a_system_upgrade_first_is_skipped_without_installing_or_asking(self):
        # The synced repositories' vkmark needs a newer assimp than the one installed: that's -Syu territory.
        state = MacState(installed={"mesa", "pipewire", "glmark2", "ffmpeg", "assimp"}, outdated={"assimp"})
        host = bench_mac(state=state)

        _, checks = run(host)

        for check_id in benchmarks.CHECK_IDS:
            self.assertEqual(checks[check_id]["status"], "skip")
        self.assertIn("would upgrade installed packages (assimp); update the system first", checks["benchmark.vulkan"]["evidence"][-1])
        self.assertFalse(any(argv[:2] == ["sudo", "-n"] for argv in host.commands_run))
        self.assertNotIn("Install", host.output)


if __name__ == "__main__":
    if sys.argv[1:] == ["regenerate"]:
        regenerate()
    else:
        unittest.main()
