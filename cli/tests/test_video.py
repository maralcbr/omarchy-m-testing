"""Seam A: the on-screen video check, through the whole CLI against the recorded M2 at its desktop.

The screenshots are the M2 Max's own: tests/corpus/m2-max-image2/onscreen-grid.png
is the grim capture from its on-screen runs (2026-09-26, the H.264 colour-bar
card full screen on a USB-C monitor, four runs in a 2x2 grid at half size).
The correct capture is TEST 4 (copy-back decode, vo=gpu over OpenGL: the
shipped default), the green-frame capture TEST 6 (zero-copy decode through
gpu-next over Vulkan), each cropped to the video. Its bars are the six the
tool's test card draws, in the same order, and the same capture stands in for
the HEVC card (both codecs went green the same way on the M2). The mpv logs
are written the way mpv logs, with the modes those runs reported.

Everything else is the recorded M2 (tests/live_mac.py keeps its packages and
login session), with a Wayland session added.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import unittest
import zlib

from omarchy_m_test import png
from omarchy_m_test.app import main
from omarchy_m_test.external_display import LAYERS, MONITORS
from omarchy_m_test.host import bundled_argv
from omarchy_m_test.sections import APPLE
from tests.desktop import HOME, command
from tests.live_mac import CATALOGUE_PATH, LiveMac, MacState, live_recording
from tests.schema_validator import errors

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(HERE)), "schema", "report-v1.schema.json")
GRID_PATH = os.path.join(HERE, "corpus", "m2-max-image2", "onscreen-grid.png")
REPORT_FILE = "omarchy-m-test-report.json"
RECORD_FILE = "recording.json"
ENTER = ""
ARGS = ["--dry-run", "--catalogue", CATALOGUE_PATH]
VIDEO = tuple(section for section in APPLE if section.id == "video")

TEMP = "/tmp/omarchy-m-test-video.Q7c2Xf9a"
MAKE_TEMP = ["mktemp", "-d", "/tmp/omarchy-m-test-video.XXXXXXXX"]
REMOVE_TEMP = ["rm", "-rf", "--", TEMP]
SYSTEM_CONF = "/etc/mpv/mpv.conf"
USER_CONF = f"{HOME}/.config/mpv/mpv.conf"
CHECKS = ("video.h264-on-screen", "video.hevc-on-screen")
CODECS = ("h264", "hevc")

SHIPPED_CONF = "# omarchy-mac: Apple Silicon video decode\nhwdec=vaapi-copy\nhwdec-codecs=h264,hevc,vp9\nvo=gpu\ngpu-api=opengl\n"
ZERO_COPY_CONF = "hwdec=vaapi\nvo=gpu-next\n"

# `hyprctl -j monitors all`, the part the lock check reads: the reasons as Hyprland 0.57 names them.
def monitors(*blocked_by: list[str]) -> str:
    return json.dumps([{"name": f"eDP-{i + 1}", "width": 3024, "height": 1964, "scale": 2.0, "transform": 0, "solitaryBlockedBy": reasons}
                       for i, reasons in enumerate(blocked_by)])


UNLOCKED = monitors(["WINDOWED", "CANDIDATE"])
LOCKED = monitors(["LOCK", "WINDOWED", "CANDIDATE"])


# `hyprctl -j layers`: what an Omarchy desktop keeps there (Hyprland 0.57, a 3024x1964 screen at scale 2) and what's on top.
def layers(*overlays: dict, output: str = "eDP-1") -> str:
    return json.dumps({output: {"levels": {
        "0": [{"namespace": "omarchy-background", "x": 0, "y": 0, "w": 1512, "h": 982}],
        "1": [],
        "2": [{"namespace": "omarchy-bar", "x": 0, "y": 0, "w": 1512, "h": 982}],
        "3": [{"namespace": "omarchy-notifications", "x": 1113, "y": 0, "w": 399, "h": 982}, *overlays],
    }}})


NO_OVERLAY = layers()
SCREENSAVER = layers({"namespace": "org.omarchy.screensaver", "x": 0, "y": 0, "w": 1512, "h": 982})
# Omarchy's OSD: transparent and full-screen (shell/plugins/osd/Osd.qml, Ui/OverlayWindow.qml), with its card low down.
TRANSPARENT = layers({"namespace": "omarchy-osd", "x": 0, "y": 0, "w": 1512, "h": 982})
OTHER_SCREENSAVER = layers({"namespace": "swaylock-plugin-saver", "x": 0, "y": 0, "w": 1512, "h": 982})

with open(SCHEMA_PATH, encoding="utf-8") as f:
    SCHEMA = json.load(f)


# -- the M2's captures, as grim would write them ------------------------------------

def _grid() -> png.Image:
    with open(GRID_PATH, "rb") as f:
        return png.decode(f.read())


GRID = _grid()


def _crop(x0: int, y0: int, x1: int, y1: int) -> png.Image:
    return png.Image(x1 - x0, y1 - y0, tuple(row[3 * x0:3 * x1] for row in GRID.rows[y0:y1]))


def encode(image: png.Image, alpha: bool = False) -> bytes:
    """A PNG as a screenshot tool writes one: every row filter in turn, RGB or RGBA."""
    step = 4 if alpha else 3
    rows = [row if not alpha else b"".join(row[x:x + 3] + b"\xff" for x in range(0, len(row), 3)) for row in image.rows]
    raw = bytearray()
    previous = bytes(len(rows[0]))
    for y, row in enumerate(rows):
        kind = y % 5
        raw.append(kind)
        for i, value in enumerate(row):
            a = row[i - step] if i >= step else 0
            b = previous[i]
            c = previous[i - step] if i >= step else 0
            if kind == 1:
                value -= a
            elif kind == 2:
                value -= b
            elif kind == 3:
                value -= (a + b) >> 1
            elif kind == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                value -= a if pa <= pb and pa <= pc else b if pb <= pc else c
            raw.append(value & 0xFF)
        previous = row

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", image.width, image.height, 8, 6 if alpha else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


CORRECT = encode(_crop(61, 201, 419, 402))                    # TEST 4: copy-back, vo=gpu (OpenGL)
GREEN = encode(_crop(541, 201, 899, 402), alpha=True)         # TEST 6: zero-copy, gpu-next (Vulkan)


def mpv_log(hwdec: str | None, vo: str, context: str, codec: str = "h264", end: str = "screenshot taken") -> str:
    """mpv 0.41's verbose log of a run of the test card, the lines that matter."""
    lines = [
        "[   0.000][v][cplayer] mpv v0.41.0 Copyright \u00a9 2000-2025 mpv/MPlayer/mplayer2 projects",
        f"[   0.005][v][cplayer] Reading config file {SYSTEM_CONF}",
        f"[   0.010][v][vd] Looking at hwdec {codec}-{hwdec or 'vaapi'}...",
    ]
    if hwdec:
        lines += [
            f"[   0.011][v][vd] Trying hardware decoding via {codec}-{hwdec}.",
            f"[   0.012][v][vd] Selected decoder: {codec} - {'H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10' if codec == 'h264' else 'H.265 / HEVC'}",
            f"[   0.031][i][vd] Using hardware decoding ({hwdec}).",
        ]
    else:
        lines.append("[   0.012][v][vd] Using software decoding.")
    frame = "vaapi[nv12]" if hwdec and not hwdec.endswith("-copy") else "nv12" if hwdec else "yuv420p"
    lines += [
        f"[   0.140][v][vo/{vo}] Probing for best GPU context.",
        f"[   0.141][v][vo/{vo}] Initializing GPU context '{context}'",
        f"[   0.152][i][cplayer] VO: [{vo}] 1920x1080 {frame}",
        f"[   1.662][v][testcard] Starting subprocess: [timeout, 10, grim, -s, 0.5, -o, eDP-1, {TEMP}/{codec}.png]",
        f"[   1.702][i][testcard] omarchy-m-test: {end}",
        "[   1.704][i][cplayer] Exiting... (Quit)",
    ]
    return "\n".join(lines) + "\n"


def desktop(
    shot: bytes | None = CORRECT, log: str | None = None, system_conf: str | None = SHIPPED_CONF, user_conf: str | None = None,
    hwdec_args: tuple[str, ...] = (), returncode: int = 0, stderr: str = "", env: dict | None = None, screen: tuple[str, ...] = (),
    overlays: tuple[str, ...] = (NO_OVERLAY,),
) -> dict:
    """The recorded M2 at its Hyprland desktop, with the answers the video section gets (`screen`, `overlays`: hyprctl's monitors and layers, in turn)."""
    rec = live_recording({"WAYLAND_DISPLAY": "wayland-1", **(env or {})})
    if screen:
        rec["commands"] = [entry for entry in rec["commands"] if entry["argv"] != MONITORS] + [command(MONITORS, answer) for answer in screen]
        rec["commands"] += [command(LAYERS, answer) for answer in overlays]
    log = mpv_log("vaapi-copy", "gpu", "wayland") if log is None else log
    rec["commands"] += [
        command(MAKE_TEMP, TEMP + "\n"),
        command(REMOVE_TEMP),
        *(command(bundled_argv("video-card", [TEMP, codec, *hwdec_args]), "output: eDP-1\n" if returncode not in (4, 5) else "",
                  returncode, stderr) for codec in CODECS),
    ]
    rec["files"][SYSTEM_CONF] = None if system_conf is None else {"text": system_conf}
    rec["files"][USER_CONF] = None if user_conf is None else {"text": user_conf}
    for codec in CODECS:
        rec["files"][f"{TEMP}/{codec}.log"] = {"text": log.replace("h264", codec)}
        rec["files"][f"{TEMP}/{codec}.png"] = None if shot is None else {"base64": base64.b64encode(shot).decode("ascii")}
    return rec


def at_desktop(rec: dict, answers=(), state: MacState | None = None) -> LiveMac:
    return LiveMac(rec, state=state or MacState(installed={"mesa", "pipewire", "mpv", "grim", "ffmpeg"}), answers=[ENTER, *answers])


def run(host: LiveMac, args=ARGS) -> dict:
    status = main(args, host, sections=VIDEO)
    assert status == 0, host.output
    report = json.loads(host.written[REPORT_FILE])
    assert errors(SCHEMA, report) == [], errors(SCHEMA, report)
    return {check["id"]: check for check in report["checks"]}


def played(host: LiveMac) -> list[list[str]]:
    return [argv for argv in host.commands_run if argv[:1] == ["bundled:video-card"]]


# -- tests ------------------------------------------------------------------------------

class OnScreenColoursTest(unittest.TestCase):
    def test_the_green_frame_capture_fails_both_codecs_and_names_the_zero_copy_mode(self):
        host = at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), system_conf=ZERO_COPY_CONF))

        checks = run(host)

        for check_id in CHECKS:
            with self.subTest(check_id):
                check = checks[check_id]
                self.assertEqual((check["kind"], check["status"]), ("automatic", "fail"))
                self.assertEqual(check["classification"]["outcome"], "fails")
                self.assertEqual(check["classification"]["feature"], "video-decoder")
                evidence = "\n".join(check["evidence"])
                self.assertIn("hardware decode as mpv is configured: hwdec=vaapi (/etc/mpv/mpv.conf)", evidence)
                self.assertIn("mode (from mpv's log): hardware decode vaapi (zero-copy), video output gpu-next, GPU context waylandvk", evidence)
                self.assertIn("red bar: expected #ff0000, saw #006f00 (wrong)", evidence)
                self.assertIn("in the wrong colour", evidence)
                self.assertIn("green, blocky or garbled frames", evidence)
                self.assertNotIn("hardware decode: not used", evidence)

    def test_the_correct_capture_passes_with_hardware_decode_and_records_the_mode(self):
        host = at_desktop(desktop(CORRECT, mpv_log("vaapi-copy", "gpu", "wayland")))

        checks = run(host)

        for check_id in CHECKS:
            with self.subTest(check_id):
                check = checks[check_id]
                self.assertEqual(check["status"], "pass")
                self.assertEqual(check["classification"]["outcome"], "works")
                self.assertIn("mode (from mpv's log): hardware decode vaapi-copy (copy-back to memory), video output gpu, GPU context wayland",
                              check["evidence"])
                self.assertIn("screen: eDP-1 (built-in), " + ("H.264" if "h264" in check_id else "HEVC") + " test card full screen, paused",
                              check["evidence"])
                self.assertIn("on screen: every bar in its colour", check["evidence"])
                self.assertIn("red bar: expected #ff0000, saw #ff1800", check["evidence"])
        self.assertEqual(played(host), [bundled_argv("video-card", [TEMP, "h264"]), bundled_argv("video-card", [TEMP, "hevc"])])

    def test_correct_colours_from_software_decode_fail_hardware_decode(self):
        checks = run(at_desktop(desktop(CORRECT, mpv_log(None, "gpu", "wayland"))))

        check = checks["video.h264-on-screen"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("mode (from mpv's log): software decode, video output gpu, GPU context wayland", check["evidence"])
        self.assertIn("on screen: every bar in its colour", check["evidence"])
        self.assertIn("hardware decode: not used, mpv decoded the test card in software", check["evidence"])

    def test_hardware_decode_that_falls_back_to_software_fails_hardware_decode(self):
        log = mpv_log("vaapi", "gpu", "wayland").replace(
            "[   0.140]", "[   0.050][w][vd] Error while decoding frame (hardware decoding)!\n"
            "[   0.051][v][vd] Falling back to software decoding.\n[   0.140]", 1)
        checks = run(at_desktop(desktop(CORRECT, log)))

        check = checks["video.h264-on-screen"]
        self.assertEqual(check["status"], "fail")
        self.assertIn("mode (from mpv's log): software decode, video output gpu, GPU context wayland", check["evidence"])

    def test_the_screenshots_and_test_cards_are_removed_when_the_section_ends(self):
        host = at_desktop(desktop())

        run(host)

        self.assertEqual(host.commands_run.count(REMOVE_TEMP), 1)
        self.assertGreater(host.commands_run.index(REMOVE_TEMP), max(host.commands_run.index(argv) for argv in played(host)))


class HardwareDecodeRequestTest(unittest.TestCase):
    def test_a_configuration_that_leaves_hardware_decode_off_gets_auto_safe(self):
        for system_conf, user_conf, why in (
            (None, None, "mpv's configuration leaves hardware decode off"),
            ("vo=gpu\n", None, "mpv's configuration leaves hardware decode off"),
            (SHIPPED_CONF, "hwdec=no\n", "hwdec=no in your mpv.conf"),
        ):
            with self.subTest(user_conf=user_conf, system_conf=system_conf):
                host = at_desktop(desktop(system_conf=system_conf, user_conf=user_conf, hwdec_args=("--hwdec=auto-safe",)))

                checks = run(host)

                self.assertEqual(played(host)[0], bundled_argv("video-card", [TEMP, "h264", "--hwdec=auto-safe"]))
                self.assertIn(f"hardware decode asked for with --hwdec=auto-safe ({why})", checks["video.h264-on-screen"]["evidence"])
                self.assertEqual(checks["video.h264-on-screen"]["status"], "pass")

    def test_the_users_hwdec_overrides_the_system_one_and_profiles_dont_count(self):
        user = "# mine\n[fast]\nhwdec=no\n[default]\nhwdec = \"auto-copy\"  # copy-back\n"
        checks = run(at_desktop(desktop(user_conf=user)))

        self.assertIn("hardware decode as mpv is configured: hwdec=auto-copy (your mpv.conf)", checks["video.h264-on-screen"]["evidence"])


class NotRunTest(unittest.TestCase):
    def assert_skipped(self, checks: dict, reason: str) -> None:
        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "skip")
            self.assertEqual(checks[check_id]["classification"]["outcome"], "not-tested")
            self.assertIn(reason, checks[check_id]["evidence"][-1])

    def test_over_ssh_nothing_is_played(self):
        for wayland in (True, False):
            with self.subTest(wayland=wayland):
                rec = desktop(env={"SSH_CONNECTION": "10.0.0.2 51000 10.0.0.9 22"})
                if not wayland:
                    del rec["env"]["WAYLAND_DISPLAY"]
                host = at_desktop(rec)

                self.assert_skipped(run(host), "running over SSH")
                self.assertEqual(played(host), [])
                self.assertNotIn(MAKE_TEMP, host.commands_run)

    def test_without_a_wayland_session_nothing_is_asked_or_played(self):
        rec = desktop()
        del rec["env"]["WAYLAND_DISPLAY"]
        host = at_desktop(rec)

        self.assert_skipped(run(host), "no Wayland desktop session")
        self.assertEqual(played(host), [])

    def test_without_a_built_in_screen_both_are_skipped(self):
        self.assert_skipped(run(at_desktop(desktop(returncode=4, stderr="no built-in screen among Hyprland's monitors\n"))),
                            "no built-in screen (a desktop Mac, or the lid is closed)")

    def test_a_card_that_times_out_is_skipped_not_failed(self):
        rec = desktop()
        for entry in rec["commands"]:
            if entry["argv"][:1] == ["bundled:video-card"]:
                entry.update(returncode=124, stdout="", stderr="video-card: timed out after 300s\n", timed_out=300)
        checks = run(at_desktop(rec))

        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "skip")
            self.assertIn("test card timed out after 300s", checks[check_id]["evidence"][-1])

    def test_a_card_ffmpeg_cant_encode_is_skipped_not_failed(self):
        checks = run(at_desktop(desktop(returncode=3, stderr="Unknown encoder 'libx265'\n")))

        self.assertIn("ffmpeg couldn't make the HEVC test card (Unknown encoder 'libx265')", checks["video.hevc-on-screen"]["evidence"][-1])
        self.assertEqual(checks["video.hevc-on-screen"]["status"], "skip")

    def test_a_screenshot_grim_couldnt_take_is_skipped(self):
        log = mpv_log("vaapi-copy", "gpu", "wayland").replace(
            "[i][testcard] omarchy-m-test: screenshot taken", "[e][testcard] omarchy-m-test: grim failed (exit 1): compositor doesn't support wlr-screencopy")
        self.assert_skipped(run(at_desktop(desktop(shot=None, log=log, returncode=6))),
                            "grim couldn't take a screenshot (testcard: omarchy-m-test: grim failed (exit 1): compositor doesn't support wlr-screencopy)")

    def test_a_test_card_mpv_never_shows_fails(self):
        log = "[   0.1][e][vo/gpu-next] Failed initializing any suitable GPU context!\n[  20.0][e][testcard] omarchy-m-test: the test card never started playing\n"
        checks = run(at_desktop(desktop(shot=None, log=log, returncode=7)))

        self.assertEqual(checks["video.h264-on-screen"]["status"], "fail")
        self.assertIn("mpv never showed the test card (testcard: omarchy-m-test: the test card never started playing)",
                      checks["video.h264-on-screen"]["evidence"])

    def test_missing_players_without_sudo_skip_the_checks(self):
        host = at_desktop(desktop(), state=MacState(installed={"mesa", "pipewire", "ffmpeg"}, sudo_cached=False))
        host.state.repository.update({"mpv": ["libplacebo", "mpv"], "grim": ["grim"]})

        checks = run(host)

        self.assert_skipped(checks, "installing test packages needs sudo, and it wasn't given")
        self.assertIn("mpv grim (with libplacebo)", host.output)
        self.assertEqual(played(host), [])
        self.assertFalse(any(argv[:2] == ["sudo", "-n"] and argv != ["sudo", "-n", "true"] for argv in host.commands_run))


class LockedScreenTest(unittest.TestCase):
    def assert_locked(self, checks: dict) -> None:
        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "skip")
            self.assertIn("the screen is locked", checks[check_id]["evidence"][-1])

    def test_a_locked_screen_plays_and_installs_nothing(self):
        host = at_desktop(desktop(screen=(LOCKED,)), state=MacState(installed={"mesa", "pipewire", "ffmpeg"}))
        host.state.repository.update({"mpv": ["mpv"], "grim": ["grim"]})

        self.assert_locked(run(host))

        self.assertEqual(played(host), [])
        self.assertNotIn(MAKE_TEMP, host.commands_run)
        self.assertFalse(any(argv[:2] == ["pacman", "-Sp"] for argv in host.commands_run))

    def test_wrong_colours_on_a_screen_that_locks_during_the_check_are_skipped_with_the_failure_kept(self):
        checks = run(at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), screen=(UNLOCKED, LOCKED))))

        self.assert_locked(checks)
        self.assertIn("in the wrong colour", " ".join(checks[CHECKS[0]]["evidence"]))
        self.assertIn("red bar: expected #ff0000, saw #006f00 (wrong)", checks[CHECKS[0]]["evidence"])

    def test_only_wrong_colours_get_a_second_look(self):
        never_shown = {"shot": None, "log": "[  20.0][e][testcard] omarchy-m-test: the test card never started playing\n", "returncode": 7}
        mpv_stopped = {"shot": None, "log": "[   1.0][e][vo/gpu] no frame\n", "returncode": 1}
        software = {"shot": CORRECT, "log": mpv_log(None, "gpu", "wayland")}
        for name, outcome in (("never shown", never_shown), ("mpv stopped", mpv_stopped), ("software decode", software)):
            with self.subTest(name):
                host = at_desktop(desktop(screen=(UNLOCKED, LOCKED), overlays=(NO_OVERLAY, SCREENSAVER), **outcome))

                self.assertEqual(run(host)[CHECKS[0]]["status"], "fail")
                self.assertEqual(host.commands_run.count(MONITORS), 1)

    def test_a_pass_stays_a_pass_when_the_second_read_would_say_locked_or_covered(self):
        for screen, overlays in (((UNLOCKED, LOCKED), (NO_OVERLAY,)), ((UNLOCKED,), (NO_OVERLAY, SCREENSAVER))):
            with self.subTest(screen=screen, overlays=overlays):
                host = at_desktop(desktop(screen=screen, overlays=overlays))

                self.assertEqual(run(host)[CHECKS[1]]["status"], "pass")
                self.assertEqual(host.commands_run.count(MONITORS), 1)

    def test_on_a_screen_that_stays_unlocked_a_wrong_picture_still_fails(self):
        checks = run(at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), screen=(UNLOCKED,))))

        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "fail")

    def test_a_lock_on_any_monitor_counts_and_a_monitor_with_no_workspace_says_nothing(self):
        for screen, status in ((monitors(["WORKSPACE"], ["LOCK"]), "skip"), (monitors(["WORKSPACE"], ["WINDOWED"]), "pass")):
            with self.subTest(screen):
                self.assertEqual(run(at_desktop(desktop(screen=(screen,))))[CHECKS[0]]["status"], status)

    def test_at_the_mac_a_lock_hyprland_cant_report_doesnt_stop_the_check(self):
        for screen in (monitors(["WORKSPACE"]), '[{"name": "eDP-1"}]', "not json"):
            with self.subTest(screen):
                self.assertEqual(run(at_desktop(desktop(screen=(screen,))))[CHECKS[0]]["status"], "pass")


class FullScreenOverlayTest(unittest.TestCase):
    def assert_covered(self, checks: dict) -> None:
        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "skip")
            self.assertIn("a full-screen overlay", checks[check_id]["evidence"][-1])

    def test_a_screensaver_plays_nothing(self):
        host = at_desktop(desktop(screen=(UNLOCKED,), overlays=(SCREENSAVER,)))

        self.assert_covered(run(host))

        self.assertEqual(played(host), [])
        self.assertNotIn(MAKE_TEMP, host.commands_run)

    def test_wrong_colours_under_omarchys_screensaver_starting_during_the_check_are_skipped_with_the_failure_kept(self):
        checks = run(at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), screen=(UNLOCKED,), overlays=(NO_OVERLAY, SCREENSAVER))))

        self.assert_covered(checks)
        self.assertIn("in the wrong colour", " ".join(checks[CHECKS[0]]["evidence"]))

    def test_at_the_second_look_any_other_full_screen_surface_leaves_the_failure_visible(self):
        for name, overlay in (("transparent OSD", TRANSPARENT), ("another screensaver", OTHER_SCREENSAVER)):
            with self.subTest(name):
                checks = run(at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), screen=(UNLOCKED,), overlays=(NO_OVERLAY, overlay))))

                for check_id in CHECKS:
                    self.assertEqual(checks[check_id]["status"], "fail")

    def test_before_the_card_a_transparent_full_screen_surface_is_a_skip(self):
        host = at_desktop(desktop(screen=(UNLOCKED,), overlays=(TRANSPARENT,)))

        self.assert_covered(run(host))
        self.assertEqual(played(host), [])

    def test_a_fractional_scale_is_measured_in_logical_pixels(self):
        fractional = json.loads(UNLOCKED)
        fractional[0].update(width=2880, height=1864, scale=1.8)
        for name, surface, status in (("covers 1600x1036", {"w": 1600, "h": 1036}, "skip"), ("too small at 1.8, enough at 2", {"w": 1350, "h": 900}, "pass")):
            with self.subTest(name):
                checks = run(at_desktop(desktop(screen=(json.dumps(fractional),), overlays=(layers(surface),))))
                self.assertEqual(checks[CHECKS[0]]["status"], status)

    def test_with_no_overlay_a_wrong_picture_still_fails(self):
        checks = run(at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), screen=(UNLOCKED,))))

        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "fail")

    def test_what_doesnt_cover_the_built_in_screen_doesnt_count(self):
        full = {"namespace": "other", "x": 0, "y": 0, "w": 1512, "h": 982}
        for name, overlay in (
            ("notifications and a full-screen bar below", NO_OVERLAY),
            ("a dialog", layers({"namespace": "dialog", "x": 400, "y": 300, "w": 700, "h": 400})),
            ("a strip across the whole width", layers({"namespace": "strip", "x": 0, "y": 0, "w": 1512, "h": 200})),
            ("full screen on an external display", layers(full, output="HDMI-A-1")),
        ):
            with self.subTest(name):
                self.assertEqual(run(at_desktop(desktop(screen=(UNLOCKED,), overlays=(overlay,))))[CHECKS[0]]["status"], "pass")

    def test_a_full_screen_overlay_on_an_external_display_only_doesnt_count(self):
        both = json.loads(UNLOCKED)
        both.append({**both[0], "name": "HDMI-A-1"})
        overlay = {"HDMI-A-1": {"levels": {"3": [{"namespace": "other", "w": 1512, "h": 982}]}}, "eDP-1": {"levels": {"3": []}}}

        checks = run(at_desktop(desktop(screen=(json.dumps(both),), overlays=(json.dumps(overlay),))))

        self.assertEqual(checks[CHECKS[0]]["status"], "pass")

    def test_what_nearly_covers_it_counts_and_a_rotated_screen_is_measured_rotated(self):
        rotated = json.loads(UNLOCKED)
        rotated[0]["transform"] = 1
        for name, screen, surface in (
            ("room for a reserved edge", UNLOCKED, {"w": 1512, "h": 950}),
            ("rotated", json.dumps(rotated), {"w": 982, "h": 1512}),
        ):
            with self.subTest(name):
                self.assert_covered(run(at_desktop(desktop(screen=(screen,), overlays=(layers(surface),)))))

    def test_at_the_mac_layers_hyprland_cant_report_dont_stop_the_check(self):
        self.assertEqual(run(at_desktop(desktop(screen=(UNLOCKED,), overlays=("not json",))))[CHECKS[0]]["status"], "pass")

    def test_a_recording_keeps_the_layers_sizes_and_no_name_but_the_screensavers(self):
        host = at_desktop(desktop(screen=(UNLOCKED,), overlays=(SCREENSAVER,)))

        self.assertEqual(main([*ARGS, "--record", RECORD_FILE], host, sections=VIDEO), 0)

        recording = json.loads(host.written[RECORD_FILE])
        kept = json.loads(next(entry["stdout"] for entry in recording["commands"] if entry["argv"] == LAYERS))["eDP-1"]["levels"]["3"]
        self.assertEqual(kept, [{"w": 399, "h": 982}, {"w": 1512, "h": 982, "namespace": "org.omarchy.screensaver"}])

    def test_a_recording_keeps_only_hyprlands_own_reasons(self):
        host = at_desktop(desktop(screen=(monitors(["LOCK", "WINDOWED", "SYNTHMODEL"]),)))

        self.assertEqual(main([*ARGS, "--record", RECORD_FILE], host, sections=VIDEO), 0)

        recording = json.loads(host.written[RECORD_FILE])
        kept = json.loads(next(entry["stdout"] for entry in recording["commands"] if entry["argv"] == MONITORS))
        self.assertEqual(kept[0]["solitaryBlockedBy"], ["LOCK", "WINDOWED"])

    def test_a_recording_replays_a_lock_and_a_screensaver_as_it_saw_them(self):
        for name, screen, overlay, reason in (("locked", LOCKED, NO_OVERLAY, "the screen is locked"), ("screensaver", UNLOCKED, SCREENSAVER, "a full-screen overlay")):
            with self.subTest(name):
                host = at_desktop(desktop(screen=(screen,), overlays=(overlay,)))
                self.assertEqual(main([*ARGS, "--record", RECORD_FILE], host, sections=VIDEO), 0)

                saved = json.loads(host.written[RECORD_FILE])["commands"]
                kept_screen = next(entry["stdout"] for entry in saved if entry["argv"] == MONITORS)
                # A locked screen never gets as far as asking for the layers.
                kept_overlay = next((entry["stdout"] for entry in saved if entry["argv"] == LAYERS), NO_OVERLAY)

                checks = run(at_desktop(desktop(screen=(kept_screen,), overlays=(kept_overlay,))))

                self.assertEqual(checks[CHECKS[0]]["status"], "skip")
                self.assertIn(reason, checks[CHECKS[0]]["evidence"][-1])


class RecordModeTest(unittest.TestCase):
    def test_the_screenshot_is_recorded_as_it_was_taken(self):
        host = at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), system_conf=ZERO_COPY_CONF))

        self.assertEqual(main([*ARGS, "--record", RECORD_FILE], host, sections=VIDEO), 0)

        recording = json.loads(host.written[RECORD_FILE])
        self.assertEqual(base64.b64decode(recording["files"][f"{TEMP}/h264.png"]["base64"]), GREEN)
        self.assertIn("Using hardware decoding (vaapi)", recording["files"][f"{TEMP}/h264.log"]["text"])


if __name__ == "__main__":
    unittest.main()
