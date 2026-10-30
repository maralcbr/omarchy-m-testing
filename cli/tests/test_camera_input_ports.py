"""Seam A: the camera, the function keys and trackpad, and the USB-C, Thunderbolt/USB4 and external-display checks.

The whole CLI runs one section against the recorded Macs, with scripted
answers: the M2 Max (its ISP, the Thunderbolt lab link to another Mac and the
3440x1440 display on USB-2), the M1 Pro on mx-mac (Hyprland's own list of
its SPI keyboard and trackpad, nothing plugged in) and the M1 Pro on a fresh
converged image (only a charger plugged in: no USB controller, no
Thunderbolt domain). tests/live_mac.py puts
the run at a local seat; the plain recordings are over SSH.
"""

from __future__ import annotations

import copy
import json
import unittest

from omarchy_m_test import camera, human, inputs, ports
from omarchy_m_test.app import main
from omarchy_m_test.host import LONG_RUNNING, WATCH_TIMEOUT_SECONDS, timeout_for
from omarchy_m_test.recording import EOF, RECORDED_SOURCES, RecordedHost
from tests.desktop import command, recording
from tests.live_mac import LiveMac, live_recording
from tests.test_audio_display import SECTIONS, answer, neo, run
from tests.test_interactive import ARGS, ENTER, check, prompts, report

M1 = "m1-pro-mx-mac"
M1_FRESH = "m1-pro-converged-fresh"
# The fresh M1's USB-C ports with only a charger in: the USB controllers aren't there.
NO_CONTROLLER = ("typec 0 partner=no data=device power=sink\ntypec 1 partner=yes data=device power=sink\n"
                 "typec 2 partner=no data=device power=sink\n")
STICK_IN = NO_CONTROLLER + "usb root speed=480 class=09\nusb root speed=10000 class=09\nusb device speed=5000 class=08\n"
IMAGE_PROMPT = camera.IMAGE_QUESTION + " [Y/n/s] "
KEYS_PROMPT = inputs.FUNCTION_KEYS_QUESTION + " [Y/n/s] "
# Omarchy's default config (as the recordings hold it): the three-finger swipe is off, so not asked; taps never are.
ASKED_GESTURES = (inputs.CLICK, inputs.TWO_FINGER_CLICK, inputs.SCROLL)
GESTURE_PROMPTS = [inputs.GESTURE_QUESTIONS[name] + " [Y/n/s] " for name in ASKED_GESTURES]
ALL_ON = "gesture 3 horizontal workspace\nfiles 2\n"
DEVICES_PROMPT = ports.DEVICES_QUESTION + " [Y/n/s] "
PICTURE_PROMPT = ports.PICTURE_QUESTION + " [Y/n/s] "
PREVIEW = camera.preview_argv("video0")
FRAMES = camera.frames_argv("video0")
AT_THE_DESKTOP = {"WAYLAND_DISPLAY": "wayland-1"}


def results(host) -> dict[str, dict]:
    return {c["id"]: c for c in report(host)["checks"]}


def questions(host) -> list[str]:
    return [p for p in prompts(host) if p.endswith("[Y/n/s] ")]


def section_ids(host, prefix: str) -> list[str]:
    return [c["id"] for c in report(host)["checks"] if c["id"].startswith(prefix)]


TB_PARTNER = "partner 1 usb_mode= tbt=yes\n"
DP_MONITOR = "partner 1 usb_mode= tbt=no\n"


def with_command(rec: dict, argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> dict:
    return answer(rec, argv, returncode, stdout, stderr)


def over_ssh(section: str, answers=(), rec: dict | None = None) -> RecordedHost:
    """The plain recording (the M2's runs are over SSH): no seat, so nothing is shown on its screen."""
    host = RecordedHost(rec or recording(), answers=[ENTER, *answers])
    assert main(["--dry-run"], host, sections=(SECTIONS[section],)) == 0, host.output
    return host


def studio(rec: dict) -> dict:
    """The recorded M2 Max as a Mac Studio (j475c): no built-in camera, keyboard or trackpad."""
    rec = copy.deepcopy(rec)
    rec["files"]["/proc/device-tree/compatible"] = {"text": "apple,j475c\x00apple,t6021\x00apple,arm-platform\x00"}
    rec["files"]["/proc/device-tree/model"] = {"text": "Apple Mac Studio (2023)\x00"}
    return rec


# -- the camera ------------------------------------------------------------------------

class CameraTest(unittest.TestCase):
    def test_the_m2s_isp_gives_frames_and_over_ssh_the_picture_isnt_asked(self):
        host = over_ssh("camera")

        found = results(host)
        self.assertEqual(section_ids(host, "camera."), ["camera.isp", "camera.frames", "camera.image"])
        self.assertEqual((found["camera.isp"]["status"], found["camera.isp"]["classification"]["outcome"]), ("pass", "works"))
        self.assertEqual((found["camera.isp"]["classification"]["feature"], found["camera.isp"]["classification"]["layer"]),
                         ("isp-camera", "aurora"))
        self.assertEqual(found["camera.isp"]["evidence"], ["camera: /dev/video0, from the ISP driver (apple-isp)"])
        self.assertEqual(found["camera.frames"]["status"], "pass")
        self.assertEqual(found["camera.frames"]["evidence"], [
            "asked ffmpeg for 30 frames from /dev/video0 (read and discarded, none kept)",
            "stream: 1920x1080, nv12",
            "frames: 30 of 30",
        ])
        self.assertEqual((found["camera.image"]["kind"], found["camera.image"]["status"]), ("human", "skip"))
        self.assertEqual(found["camera.image"]["evidence"], ["skipped: no Wayland desktop session to show the camera in"])
        self.assertNotIn(PREVIEW, host.commands_run)
        self.assertNotIn(IMAGE_PROMPT, prompts(host))

    def test_at_the_desktop_the_camera_is_shown_live_and_the_human_confirms_the_image(self):
        rec = with_command(live_recording(env=AT_THE_DESKTOP), PREVIEW, returncode=camera.TIMED_OUT)
        host = run("camera", ["y looks fine"], rec=rec)

        result = check(host, "camera.image")
        self.assertEqual((result["kind"], result["status"], result["classification"]["outcome"]), ("human", "pass", "works"))
        self.assertEqual(result["evidence"][0], "showed /dev/video0 live for 10 s (mpv)")
        self.assertEqual(result["evidence"][-1], "note: looks fine")
        self.assertLess(host.commands_run.index(FRAMES), host.commands_run.index(PREVIEW))
        self.assertLess(host.output.index("A window shows the camera"), host.output.index(IMAGE_PROMPT))

    def test_a_bad_picture_is_the_humans_failure(self):
        rec = with_command(live_recording(env=AT_THE_DESKTOP), PREVIEW, returncode=camera.TIMED_OUT)
        result = check(run("camera", ["n upside down"], rec=rec), "camera.image")

        self.assertEqual((result["status"], result["classification"]["outcome"]), ("fail", "fails"))

    def test_without_the_isp_device_the_camera_fails_and_a_usb_webcam_is_named_by_driver_only(self):
        rec = with_command(recording(), camera.DEVICES, stdout="video2 uvcvideo\nvideo3 uvcvideo\n")
        host = over_ssh("camera", rec=rec)

        found = results(host)
        self.assertEqual((found["camera.isp"]["status"], found["camera.isp"]["classification"]["outcome"]), ("fail", "fails"))
        self.assertEqual(found["camera.isp"]["evidence"], [
            "no camera device from the ISP driver (apple-isp)",
            "other video devices: video2 (uvcvideo), video3 (uvcvideo)",
        ])
        self.assertEqual(found["camera.frames"]["evidence"], ["skipped: no camera device (see camera.isp)"])
        self.assertEqual(found["camera.image"]["status"], "skip")
        self.assertFalse(any(argv[:3] == ["timeout", "20", "ffmpeg"] for argv in host.commands_run))

    def test_a_camera_that_gives_no_frames_fails_and_isnt_shown(self):
        stderr = "[video4linux2,v4l2 @ 0x1] ioctl(VIDIOC_STREAMON): Input/output error\n/dev/video0: Input/output error\n"
        rec = with_command(live_recording(env=AT_THE_DESKTOP), FRAMES, returncode=1, stderr=stderr)
        host = run("camera", [], rec=rec)

        frames = check(host, "camera.frames")
        self.assertEqual((frames["status"], frames["classification"]["outcome"]), ("fail", "fails"))
        self.assertEqual(frames["evidence"][-2:], ["frames: 0 of 30", "ffmpeg: /dev/video0: Input/output error"])
        self.assertEqual(check(host, "camera.image")["evidence"], ["skipped: the camera gave no frames (see camera.frames)"])
        self.assertNotIn(PREVIEW, host.commands_run)

    def test_without_ffmpeg_or_mpv_the_checks_that_need_them_are_skipped(self):
        no_ffmpeg = with_command(recording(), FRAMES, returncode=127, stderr="timeout: failed to run command 'ffmpeg'\n")
        self.assertEqual(results(over_ssh("camera", rec=no_ffmpeg))["camera.frames"]["evidence"], ["skipped: ffmpeg isn't installed"])

        no_mpv = with_command(live_recording(env=AT_THE_DESKTOP), PREVIEW, returncode=127)
        host = run("camera", [], rec=no_mpv)
        self.assertEqual(check(host, "camera.image")["evidence"], ["skipped: mpv isn't installed"])
        self.assertNotIn(IMAGE_PROMPT, prompts(host))

    def test_a_mac_without_a_built_in_camera_skips_without_opening_anything(self):
        host = over_ssh("camera", rec=studio(recording()))

        found = results(host)
        self.assertEqual({k: v["status"] for k, v in found.items() if k.startswith("camera.")},
                         {"camera.isp": "skip", "camera.frames": "skip", "camera.image": "skip"})
        self.assertEqual(found["camera.isp"]["evidence"], ["skipped: this Mac has no built-in camera"])
        self.assertNotIn(camera.DEVICES, host.commands_run)


# -- the function keys and the trackpad ----------------------------------------------------

class KeysAndTrackpadTest(unittest.TestCase):
    def run_input(self, answers, rec=None) -> LiveMac:
        # The M1 on mx-mac has no light sensor: the keyboard-light question isn't asked first.
        return run("input", answers, rec=rec or live_recording(base=recording(M1)))

    def test_the_m1s_spi_keyboard_and_trackpad_are_named_and_the_human_answers(self):
        host = self.run_input(["y", "y", "y", "n two-finger scroll jumps"])

        keys, gestures = check(host, "input.function-keys"), check(host, "input.trackpad-gestures")
        self.assertEqual((keys["kind"], keys["status"], keys["classification"]["feature"]), ("human", "pass", "keyboard"))
        self.assertEqual(keys["evidence"][0], "built-in keyboard (Hyprland): apple-spi-keyboard")
        self.assertEqual((gestures["status"], gestures["classification"]["feature"], gestures["classification"]["outcome"]),
                         ("fail", "touchpad", "fails"))
        self.assertEqual(gestures["evidence"][0], "built-in trackpad (Hyprland): apple-spi-trackpad")
        self.assertEqual(gestures["evidence"][1], "Hyprland config: three-finger workspace swipe off")
        self.assertIn("two-finger scroll: answer: no; note: two-finger scroll jumps", gestures["evidence"])
        self.assertIn("three-finger swipe: skipped: off in your config", gestures["evidence"])
        self.assertEqual(prompts(host)[-4:], [KEYS_PROMPT, *GESTURE_PROMPTS])
        self.assertEqual(host.commands_run.count(inputs.DEVICES), 1)  # read once for both

    def test_the_m2s_mtp_keyboard_and_what_the_top_row_sends(self):
        rec = with_command(live_recording(), inputs.DEVICES, stdout="apple-mtp-keyboard\napple-mtp-multi-touch\n")
        rec["files"][inputs.FNMODE] = {"text": "2\n"}
        host = run("input", [EOF, "s", "s", "s", "s"], rec=rec)

        self.assertEqual(check(host, "input.function-keys")["evidence"][:2], [
            "built-in keyboard (Hyprland): apple-mtp-keyboard",
            "hid_apple fnmode: 2 (F-keys first, media keys with Fn)",
        ])
        self.assertEqual(check(host, "input.trackpad-gestures")["evidence"][0], "built-in trackpad (Hyprland): apple-mtp-multi-touch")

    def test_keyboards_and_mice_people_plug_in_or_pair_never_leave_the_mac(self):
        # The script on the Mac keeps only the built-in drivers' names from Hyprland's list, so a
        # paired "kestrel's-magic-keyboard" never reaches a recording; the CLI filters again.
        self.assertIn("apple-(spi|mtp|internal)-", inputs.BUILT_IN_SCRIPT)
        self.assertNotIn("hyprctl devices -j", [" ".join(argv) for argv in RECORDED_SOURCES])
        rec = with_command(live_recording(base=recording(M1)), inputs.DEVICES,
                           stdout="apple-spi-keyboard\nkestrel's-magic-keyboard\napple-spi-trackpad\n")
        host = self.run_input(["s", "s", "s", "s"], rec=rec)

        self.assertNotIn("magic-keyboard", json.dumps(report(host)))
        self.assertNotIn(["hyprctl", "devices", "-j"], host.commands_run)

    def test_over_ssh_hyprland_cant_list_the_devices_and_the_questions_are_still_asked(self):
        host = over_ssh("input", [EOF, "s", "s", "s", "s"])

        self.assertEqual(results(host)["input.function-keys"]["evidence"][0],
                         "Hyprland couldn't list the input devices (run from the desktop to see them)")
        self.assertEqual(prompts(host)[-4:], [KEYS_PROMPT, *GESTURE_PROMPTS])

    def test_gestures_the_config_turns_on_are_asked_one_by_one(self):
        rec = with_command(live_recording(base=recording(M1)), inputs.GESTURE_CONFIG, stdout=ALL_ON)
        host = self.run_input(["y", "y", "y", "y", "n"], rec=rec)

        gestures = check(host, "input.trackpad-gestures")
        self.assertEqual(prompts(host)[-4:], [inputs.GESTURE_QUESTIONS[name] + " [Y/n/s] " for name in inputs.GESTURE_QUESTIONS])
        self.assertEqual(gestures["evidence"][1], "Hyprland config: three-finger workspace swipe on")
        self.assertIn("three-finger swipe: answer: no", gestures["evidence"])
        self.assertEqual(gestures["status"], "fail")

    def test_a_gesture_off_in_the_config_is_skipped_never_failed(self):
        host = self.run_input(["y", "y", "y", "y"])

        gestures = check(host, "input.trackpad-gestures")
        self.assertEqual(gestures["status"], "pass")
        self.assertNotIn(human.DEFAULTED, gestures)
        self.assertNotIn(inputs.GESTURE_QUESTIONS[inputs.SWIPE] + " [Y/n/s] ", prompts(host))

    def test_tapping_is_never_asked_or_read(self):
        # Tap-to-click, two-finger tap and tap-and-drag aren't features: no question, no config reading, no evidence.
        rec = with_command(live_recording(base=recording(M1)), inputs.GESTURE_CONFIG, stdout=ALL_ON)
        host = self.run_input(["y", "y", "y", "y", "y"], rec=rec)

        self.assertEqual(list(inputs.GESTURE_QUESTIONS), [inputs.CLICK, inputs.TWO_FINGER_CLICK, inputs.SCROLL, inputs.SWIPE])
        self.assertFalse([p for p in prompts(host) if "tap" in p.lower()])
        self.assertNotIn("tap", inputs.GESTURE_CONFIG_SCRIPT.lower())
        self.assertNotIn("tap", json.dumps(check(host, "input.trackpad-gestures")).lower())

    def test_a_config_it_cant_read_skips_the_swipe(self):
        rec = with_command(live_recording(base=recording(M1)), inputs.GESTURE_CONFIG, stdout="files 0\n")
        host = self.run_input(["y", "y", "y", "y"], rec=rec)

        gestures = check(host, "input.trackpad-gestures")
        self.assertEqual(gestures["evidence"][1], "Hyprland config: three-finger workspace swipe unknown")
        self.assertIn("three-finger swipe: skipped: " + inputs.UNKNOWN_CONFIG, gestures["evidence"])
        self.assertEqual(prompts(host)[-4:], [KEYS_PROMPT, *GESTURE_PROMPTS])
        self.assertEqual(gestures["status"], "pass")

    def test_one_enter_among_the_gestures_leaves_the_pass_unconfirmed(self):
        host = self.run_input(["y", "y", ENTER, "y"])

        self.assertIs(check(host, "input.trackpad-gestures")[human.DEFAULTED], True)

    def test_the_gesture_script_reads_only_on_off_facts(self):
        self.assertNotIn("name", inputs.GESTURE_CONFIG_SCRIPT)  # no device names, only gesture lines
        self.assertIs(inputs.parse_gesture_config("files 1\n").swipe, False)

    def test_a_later_statement_turns_a_gesture_back_off_and_a_live_option_wins(self):
        later_off = "gesture 3 horizontal workspace\ngesture 3 horizontal unset\nfiles 2\n"
        self.assertIs(inputs.parse_gesture_config(later_off).swipe, False)
        # Hyprland's unset matches a gesture's direction: unsetting another one leaves the workspace swipe on.
        other_unset = "gesture 3 horizontal workspace\ngesture 3 vertical other\ngesture 3 vertical unset\nfiles 1\n"
        self.assertIs(inputs.parse_gesture_config(other_unset).swipe, True)
        self.assertIs(inputs.parse_gesture_config("workspace-swipe 1\nworkspace-swipe 0\nfiles 1\n").swipe, False)
        self.assertIs(inputs.parse_gesture_config("swipe-live 1\nworkspace-swipe 0\nfiles 1\n").swipe, True)
        self.assertIsNone(inputs.parse_gesture_config("files 0\n").swipe)

    def test_a_mac_without_a_built_in_keyboard_or_trackpad_doesnt_ask(self):
        host = over_ssh("input", [EOF], rec=studio(recording()))

        found = results(host)
        self.assertEqual(found["input.function-keys"]["evidence"], ["skipped: this Mac has no built-in keyboard"])
        self.assertEqual(found["input.trackpad-gestures"]["evidence"], ["skipped: this Mac has no built-in trackpad"])
        self.assertNotIn(KEYS_PROMPT, prompts(host))


# -- USB-C, Thunderbolt/USB4 and external displays ---------------------------------------------

class PortsTest(unittest.TestCase):
    def test_at_the_mac_the_human_plugs_in_first_then_the_m2s_ports_link_and_display_are_listed(self):
        host = run("ports", [ENTER, "y", "y"])

        messages = [event[1] for event in host.transcript if event[0] == "prompt"]
        self.assertEqual(messages[1:], [ports.READY, DEVICES_PROMPT, PICTURE_PROMPT])
        first_script = min(host.commands_run.index(argv) for argv in (ports.USB, ports.THUNDERBOLT_LIST, ports.DISPLAYS_LIST))
        self.assertGreater(first_script, 0)
        self.assertLess(host.output.index(ports.PLUG_IN), host.output.index("Found:"))
        self.assertEqual(section_ids(host, "ports."), list(ports.CHECK_IDS))

        found = results(host)
        self.assertEqual({k: (v["status"], v["classification"]["feature"], v["classification"]["outcome"])
                          for k, v in found.items() if k.startswith("ports.")}, {
            "ports.usb-c": ("pass", "usb3-tb-ports", "works"),
            "ports.thunderbolt": ("pass", "thunderbolt", "works"),
            "ports.external-displays": ("pass", "usb4-displays", "works"),
            "ports.devices-work": ("pass", "usb3-tb-ports", "works"),
            "ports.external-display-picture": ("pass", "usb4-displays", "works"),
        })
        self.assertEqual(found["ports.usb-c"]["evidence"], [
            "USB-C ports: 3 (USB-PD controller, /sys/class/typec), 2 with something plugged in",
            "port 0: plugged in (data host, power source)",
            "port 1: plugged in (data host, power source)",
            "USB controllers: 6 root hubs (480 Mbps, 10000 Mbps)",
            "USB devices: none",
        ])
        self.assertEqual(found["ports.thunderbolt"]["evidence"], [
            "Thunderbolt/USB4 controllers: 3 (domains registered by thunderbolt-apple-nhi)",
            "linked: another computer, host-to-host (10.0 Gb/s, 2 lanes)",
            "retimers: 1",
        ])
        self.assertEqual(found["ports.external-displays"]["evidence"], [
            "USB-C display connectors: 3 (USB-1, USB-2, USB-3)",
            "USB-2: in use, 3440x1440 preferred",
        ])
        self.assertEqual(found["ports.external-display-picture"]["evidence"][0], "display on USB-C: USB-2 at 3440x1440")

    def test_over_ssh_nobody_is_asked_to_plug_anything_in(self):
        host = over_ssh("ports", [EOF, EOF])

        self.assertNotIn(ports.READY, [event[1] for event in host.transcript if event[0] == "prompt"])
        self.assertEqual(prompts(host)[-2:], [DEVICES_PROMPT, PICTURE_PROMPT])

    def test_usb_devices_are_listed_by_class_and_speed(self):
        usb = ("typec 0 partner=yes data=host power=source\ntypec 1 partner=no data=device power=sink\n"
               "usb root speed=480 class=09\nusb root speed=10000 class=09\n"
               "usb device speed=5000 class=08\nusb device speed=480 class=09\nusb device speed=12 class=03\n")
        host = over_ssh("ports", [EOF], rec=with_command(recording(M1), ports.USB, stdout=usb))

        found = results(host)
        self.assertEqual(found["ports.usb-c"]["evidence"][-1],
                         "USB devices: 3: storage at 5000 Mbps, hub at 480 Mbps, input (HID) at 12 Mbps")
        self.assertEqual(found["ports.devices-work"]["evidence"][0], "listed: 1 USB-C port(s) in use, 3 USB device(s), 0 Thunderbolt/USB4 link(s)")
        # No display on USB-C: its picture isn't asked about.
        self.assertEqual(found["ports.external-display-picture"]["evidence"], ["skipped: no display in use on a USB-C port"])

    def test_a_thunderbolt_device_shows_its_generation_speed_lanes_and_whether_it_is_authorized(self):
        tb = ("thunderbolt 0-0 thunderbolt_device generation=4 rx_speed=20.0Gb/s rx_lanes=2 authorized=1\n"
              "thunderbolt 0-1 thunderbolt_device generation=3 rx_speed=20.0Gb/s rx_lanes=2 authorized=0\n"
              "thunderbolt domain0 thunderbolt_domain\n")
        host = over_ssh("ports", [EOF], rec=with_command(recording(M1), ports.THUNDERBOLT_LIST, stdout=tb))

        self.assertEqual(results(host)["ports.thunderbolt"]["evidence"], [
            "Thunderbolt/USB4 controllers: 1 (domains registered by thunderbolt-apple-nhi)",
            "linked: a Thunderbolt or USB4 device (generation 3, 20.0 Gb/s, 2 lanes, not authorized)",
        ])

    def test_nothing_plugged_in_asks_nothing(self):
        host = over_ssh("ports", [], rec=recording(M1))

        found = results(host)
        self.assertEqual(found["ports.devices-work"]["evidence"], ["skipped: nothing plugged into the USB-C ports"])
        self.assertEqual(found["ports.external-displays"]["status"], "skip")
        self.assertEqual(found["ports.external-displays"]["evidence"][-1], "skipped: no display plugged into a USB-C port")
        self.assertEqual(questions(host), [])

    def test_without_the_thunderbolt_bus_the_m2_fails_and_without_a_domain_over_ssh_it_is_skipped(self):
        host = over_ssh("ports", [EOF, EOF], rec=with_command(recording(), ports.THUNDERBOLT_LIST, stdout="nobus\n"))
        result = results(host)["ports.thunderbolt"]
        self.assertEqual((result["status"], result["classification"]["outcome"]), ("fail", "fails"))
        self.assertEqual(result["classification"]["expected"]["aurora"], "supported")
        self.assertIn("no Thunderbolt bus in this kernel", result["evidence"][0])

        host = over_ssh("ports", [EOF, EOF], rec=with_command(recording(), ports.THUNDERBOLT_LIST))
        self.assertEqual(results(host)["ports.thunderbolt"]["status"], "skip")

    def test_a_display_connected_but_not_lit_fails_and_its_picture_isnt_asked(self):
        drm = "connector card1-eDP-1 connected enabled 3456x2234\nconnector card2-USB-2 connected disabled 3440x1440\n"
        host = over_ssh("ports", [EOF], rec=with_command(recording(), ports.DISPLAYS_LIST, stdout=drm))

        found = results(host)
        self.assertEqual((found["ports.external-displays"]["status"], found["ports.external-displays"]["classification"]["outcome"]),
                         ("fail", "fails"))
        self.assertIn("USB-2: connected but not in use, 3440x1440 preferred", found["ports.external-displays"]["evidence"])
        self.assertEqual(found["ports.external-display-picture"]["status"], "skip")
        self.assertNotIn(PICTURE_PROMPT, prompts(host))

    def test_a_kernel_without_usb_c_display_connectors_fails_and_hdmi_is_only_noted(self):
        drm = "connector card0-eDP-1 connected enabled 3456x2234\nconnector card0-HDMI-A-1 connected enabled 1920x1080\n"
        host = over_ssh("ports", [EOF], rec=with_command(recording(), ports.DISPLAYS_LIST, stdout=drm))

        result = results(host)["ports.external-displays"]
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"], [
            "no display connectors on the USB-C ports (no DP alt mode in this kernel)",
            "HDMI-A-1: connected (HDMI, not USB-C: see display.outputs)",
        ])

    def test_without_the_usb_pd_controller_usb_c_fails(self):
        usb = "usb root speed=480 class=09\n"
        result = results(over_ssh("ports", [EOF, EOF], rec=with_command(recording(), ports.USB, stdout=usb)))["ports.usb-c"]

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"][0], "no USB-C ports registered (the USB-PD controller, tipd, didn't come up)")

    def test_the_fresh_m1_over_ssh_with_nothing_attached_skips_usb_and_thunderbolt_is_an_expected_gap(self):
        rec = recording(M1_FRESH)
        del rec["env"]["HOME"]  # no checkpoint: this run is only the one section
        host = over_ssh("ports", [EOF], rec=rec)

        found = results(host)
        usb = found["ports.usb-c"]
        self.assertEqual((usb["status"], usb["classification"]["outcome"]), ("skip", "not-tested"))
        self.assertEqual(usb["evidence"], [
            "USB-C ports: 4 (USB-PD controller, /sys/class/typec), 1 with something plugged in",
            "port 1: plugged in (data device, power sink)",
            "no USB controller (no root hub in /sys/bus/usb)",
            "USB devices: none",
            "skipped: no USB device attached (the USB controller only comes up while something is plugged into a USB-C port)",
        ])
        self.assertNotIn(ports.USB_WAIT, host.commands_run)
        self.assertNotIn(ports.PLUG_READY + " [Y/n] ", [event[1] for event in host.transcript if event[0] == "prompt"])
        # No domain either: the Thunderbolt/USB4 host only comes up with a USB4 or Thunderbolt partner attached.
        links = found["ports.thunderbolt"]
        self.assertEqual((links["status"], links["classification"]["outcome"]), ("skip", "not-tested"))
        self.assertEqual(links["evidence"], [
            "no Thunderbolt/USB4 domain registered (thunderbolt-apple-nhi)",
            "skipped: no Thunderbolt/USB4 device attached (the Thunderbolt/USB4 host only comes up "
            "while a USB4 or Thunderbolt device is plugged in)",
        ])
        self.assertNotIn(ports.THUNDERBOLT_WAIT, host.commands_run)

    def test_thunderbolt_failing_on_an_m1_is_an_expected_gap_not_a_should_work(self):
        # Aurora's Thunderbolt host hasn't been seen working on a real M1 yet: the catalogue says unknown.
        host = over_ssh("ports", [EOF], rec=with_command(recording(M1), ports.THUNDERBOLT_LIST, stdout="nobus\n"))

        links = results(host)["ports.thunderbolt"]
        self.assertEqual((links["status"], links["classification"]["outcome"]), ("fail", "not-in-asahi"))
        self.assertEqual(links["classification"]["expected"], {"asahi": "wip", "aurora": "unknown"})

    def test_on_the_macbook_neo_thunderbolt_is_not_applicable_and_nothing_asks_for_a_thunderbolt_device(self):
        host = run("ports", [ENTER, "s", "s"], rec=neo(live_recording()))

        self.assertNotIn(ports.THUNDERBOLT_LIST, host.commands_run)
        self.assertIn(ports.PLUG_IN_NO_THUNDERBOLT, host.output)
        self.assertNotIn(ports.PLUG_THUNDERBOLT, host.output)
        links = results(host)["ports.thunderbolt"]
        self.assertEqual((links["status"], links["classification"]["outcome"]), ("skip", "not-applicable"))
        self.assertEqual(links["evidence"], [f"skipped: {ports.NO_THUNDERBOLT_PORT}"])

    def test_at_the_mac_with_no_thunderbolt_domain_the_human_plugs_a_device_in_and_the_host_comes_up(self):
        linked = "thunderbolt 0-0 thunderbolt_device generation=4 rx_speed=20.0Gb/s rx_lanes=2 authorized=1\nthunderbolt domain0 thunderbolt_domain\n"
        rec = with_command(with_command(live_recording(), ports.THUNDERBOLT_LIST), ports.THUNDERBOLT_WAIT, stdout=linked)
        host = run("ports", [ENTER, ENTER, "y", "y"], rec=rec)

        messages = [event[1] for event in host.transcript if event[0] == "prompt"]
        self.assertEqual(messages[1:3], [ports.READY, ports.PLUG_READY + " [Y/n] "])
        self.assertIn(ports.PLUG_THUNDERBOLT, host.output)
        self.assertNotIn(ports.PLUG_USB, host.output)  # the M2's USB controllers are up
        links = results(host)["ports.thunderbolt"]
        self.assertEqual((links["status"], links["classification"]["outcome"]), ("pass", "works"))
        self.assertEqual(links["evidence"][-1], "the Thunderbolt/USB4 host came up once a device was plugged in")

    def test_at_the_mac_a_thunderbolt_device_whose_host_never_comes_up_fails(self):
        rec = with_command(with_command(live_recording(), ports.THUNDERBOLT_LIST), ports.THUNDERBOLT_WAIT)
        rec = with_command(rec, ports.PARTNERS, stdout=TB_PARTNER)
        host = run("ports", [ENTER, ENTER, "y", "y"], rec=rec)

        links = results(host)["ports.thunderbolt"]
        self.assertEqual((links["status"], links["classification"]["outcome"]), ("fail", "fails"))  # the M2's host is supported
        self.assertIn("a Thunderbolt/USB4 device is attached (port 1: Thunderbolt alt mode)", links["evidence"])
        self.assertEqual(links["evidence"][-1], f"no domain came up within {ports.WAIT_SECONDS} s of plugging in a Thunderbolt/USB4 device")
        self.assertNotIn(ports.PLUG_REAL_THUNDERBOLT, host.output)

    def test_a_usb4_partner_counts_as_a_thunderbolt_device(self):
        self.assertEqual(ports.parse_partners("partner 0 usb_mode=usb2,usb3,[usb4] tbt=no\n"), ["port 0: USB4 mode"])
        self.assertEqual(ports.parse_partners("partner 0 usb_mode=[usb2],usb3,usb4 tbt=no\n"), [])

    def test_at_the_mac_a_display_that_isnt_thunderbolt_is_skipped_after_asking_for_a_real_device(self):
        # The M1's run: a DP alt-mode monitor with a USB 2 hub never enters Thunderbolt/USB4 mode, so no domain.
        for again, waits in ((EOF, 1), ("n", 1), (ENTER, 2)):
            with self.subTest(again=again):
                rec = with_command(with_command(live_recording(), ports.THUNDERBOLT_LIST), ports.THUNDERBOLT_WAIT)
                rec = with_command(rec, ports.PARTNERS, stdout=DP_MONITOR)
                host = run("ports", [ENTER, ENTER, again, "y", "y"], rec=rec)

                links = results(host)["ports.thunderbolt"]
                self.assertEqual((links["status"], links["classification"]["outcome"]), ("skip", "not-tested"))
                self.assertIn(ports.NOT_THUNDERBOLT, links["evidence"][-1])
                self.assertIn(ports.PLUG_REAL_THUNDERBOLT, host.output)
                self.assertEqual(host.commands_run.count(ports.THUNDERBOLT_WAIT), waits)

    def test_at_the_mac_plugging_in_can_be_skipped(self):
        for answer_ in ("s", "n", EOF):
            with self.subTest(answer=answer_):
                rec = with_command(with_command(live_recording(), ports.THUNDERBOLT_LIST), ports.THUNDERBOLT_WAIT)
                host = run("ports", [ENTER, answer_, "y", "y"], rec=rec)

                self.assertEqual(results(host)["ports.thunderbolt"]["status"], "skip")
                self.assertNotIn(ports.THUNDERBOLT_WAIT, host.commands_run)

    def test_at_the_mac_with_no_usb_controller_the_human_plugs_a_device_in_and_the_controller_comes_up(self):
        rec = with_command(with_command(live_recording(), ports.USB, stdout=NO_CONTROLLER), ports.USB_WAIT, stdout=STICK_IN)
        host = run("ports", [ENTER, ENTER, "y", "y"], rec=rec)

        messages = [event[1] for event in host.transcript if event[0] == "prompt"]
        self.assertEqual(messages[1:3], [ports.READY, ports.PLUG_READY + " [Y/n] "])
        self.assertLess(host.output.index(ports.PLUG_USB), host.output.index("Found:"))
        self.assertLess(host.commands_run.index(ports.USB), host.commands_run.index(ports.USB_WAIT))
        usb = results(host)["ports.usb-c"]
        self.assertEqual((usb["status"], usb["classification"]["outcome"]), ("pass", "works"))
        self.assertEqual(usb["evidence"][-3:], [
            "USB controllers: 2 root hubs (480 Mbps, 10000 Mbps)",
            "USB devices: 1: storage at 5000 Mbps",
            "the USB controller came up once a USB device was plugged in",
        ])

    def test_at_the_mac_a_controller_that_never_comes_up_after_plugging_in_fails(self):
        rec = with_command(with_command(live_recording(), ports.USB, stdout=NO_CONTROLLER), ports.USB_WAIT, stdout=NO_CONTROLLER)
        usb = results(run("ports", [ENTER, ENTER, "y", "y"], rec=rec))["ports.usb-c"]

        self.assertEqual((usb["status"], usb["classification"]["outcome"]), ("fail", "fails"))
        self.assertEqual(usb["evidence"][-1], f"no USB controller came up within {ports.WAIT_SECONDS} s of plugging in a USB device")

    def test_at_the_mac_the_human_can_skip_plugging_a_usb_device_in(self):
        for skip in ("s", EOF):
            with self.subTest(skip=skip):
                rec = with_command(live_recording(), ports.USB, stdout=NO_CONTROLLER)
                host = run("ports", [ENTER, skip, "y", "y"], rec=rec)

                usb = results(host)["ports.usb-c"]
                self.assertEqual(usb["status"], "skip")
                self.assertTrue(usb["evidence"][-1].endswith("plugging one in was skipped"), usb["evidence"][-1])
                self.assertNotIn(ports.USB_WAIT, host.commands_run)

    def test_the_waits_for_the_usb_controller_and_the_thunderbolt_host_are_watches_with_the_long_timeout(self):
        for argv in (ports.USB_WAIT, ports.THUNDERBOLT_WAIT):
            self.assertTrue(argv[2].startswith(LONG_RUNNING))
            self.assertEqual(timeout_for(argv), WATCH_TIMEOUT_SECONDS)

    def test_the_scripts_never_read_a_name_vendor_string_or_serial(self):
        for script in (ports.USB_SCRIPT, ports.USB_WAIT_SCRIPT, ports.THUNDERBOLT_WAIT_SCRIPT, ports.THUNDERBOLT_SCRIPT, ports.PARTNERS_SCRIPT, ports.DISPLAYS_SCRIPT, camera.DEVICES_SCRIPT):
            for private in ("product", "manufacturer", "serial", "device_name", "vendor_name", "unique_id", "/name", "edid"):
                self.assertNotIn(private, script)


if __name__ == "__main__":
    unittest.main()
