"""Seam A: the Audio, Display and Input sections' live checks, with scripted answers.

The whole CLI runs against the recorded M2 Max (tests/live_mac.py keeps its
volume as a small model, so the tests can check it was capped and put back);
only the section under test runs. Variations edit what the recording
answers: speaker protection down, another default output, a silent
microphone, a backlight that can't be set, a Mac without a notch.
"""

from __future__ import annotations

import copy
import json
import os
import unittest

from omarchy_m_test import audio, display, human, inputs, network, ports
from omarchy_m_test.app import main
from omarchy_m_test.host import CommandResult
from omarchy_m_test.inventory import KERNEL_LOG
from omarchy_m_test.recording import ENDED, EOF, INTERRUPT
from omarchy_m_test.sections import APPLE
from tests.desktop import at_the_seat
from tests.live_mac import NODE, LiveMac, MacState, live_recording
from tests.schema_validator import errors
from tests.test_core_checks import reference
from tests.test_interactive import ARGS, ENTER, SCHEMA, check, prompts, report

SECTIONS = {section.id: section for section in APPLE}
SINK = "audio_effect.j416-convolver"
SOURCE = "omarchy_asahi_mic.monitor"
TONE = audio.tone_argv(SINK)
VOLUME_DOWN = ["wpctl", "set-volume", NODE, "0.30"]
VOLUME_BACK = ["wpctl", "set-volume", NODE, "0.45"]
PANEL_BACK = display.set_argv("apple-panel-bl", 250)
STEPS = [display.set_argv("apple-panel-bl", level) for level in (175, 112, 62)]
LUX = "/sys/bus/iio/devices/iio:device0/in_illuminance_input"
KEYS = ["brightnessctl", "--machine-readable", "--device=kbd_backlight", "info"]

TONE_PROMPT = audio.TONE_QUESTION + " [Y/n/s] "
HEADPHONE_PROMPT = audio.HEADPHONE_QUESTION + " [Y/n/s] "
NOTCH_PROMPT = display.NOTCH_QUESTION + " [Y/n/s] "
BRIGHTNESS_PROMPT = display.BRIGHTNESS_QUESTION + " [Y/n/s] "
CURSOR_PROMPT = display.CURSOR_QUESTION + " [Y/n/s] "
KEYBOARD_PROMPT = display.KEYBOARD_QUESTION + " [Y/n/s] "
# The Input section's function-key and trackpad questions, after the keyboard light's.
KEYS_AND_TRACKPAD = ["s", "s", "s", "s"]  # the function keys, then the three gestures Omarchy leaves on


def answer(rec: dict, argv: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> dict:
    """The recording, answering `argv` this way (added if it wasn't recorded)."""
    rec = copy.deepcopy(rec)
    rec["commands"] = [entry for entry in rec["commands"] if entry["argv"] != argv]
    rec["commands"].append({"argv": argv, "returncode": returncode, "stdout": stdout, "stderr": stderr})
    return rec


def run(section: str, answers=(), rec: dict | None = None, state: MacState | None = None, cls=LiveMac, status: int = 0) -> LiveMac:
    host = cls(rec or live_recording(), state=state, answers=[ENTER, *answers])
    self_status = main(ARGS, host, sections=(SECTIONS[section],))
    assert self_status == status, (self_status, host.output)
    if status == 0:
        assert errors(SCHEMA, report(host)) == [], errors(SCHEMA, report(host))
    return host


# -- the speaker tone ------------------------------------------------------------------

class SpeakerToneTest(unittest.TestCase):
    def test_with_speaker_protection_active_the_tone_plays_at_30_percent_and_the_volume_is_put_back(self):
        host = run("audio", ["y", "s"])

        commands = host.commands_run
        self.assertLess(commands.index(["systemctl", "is-active", "speakersafetyd"]), commands.index(VOLUME_DOWN))
        self.assertLess(commands.index(VOLUME_DOWN), commands.index(TONE))
        self.assertEqual(commands[-1], VOLUME_BACK)
        self.assertEqual((host.state.volume, host.state.muted), ("0.45", False))
        self.assertLess(host.output.index("a short tone plays"), host.output.index(TONE_PROMPT))
        result = check(host, "audio.speaker-tone")
        self.assertEqual((result["kind"], result["status"], result["classification"]["outcome"]), ("human", "pass", "works"))
        self.assertEqual(result["classification"]["feature"], "speakers")
        self.assertEqual(result["evidence"][:4], [
            "speakersafetyd: active",
            "kernel log: Speaker volumes unlocked",
            f"default output: {SINK} (the speakers' DSP sink)",
            f"played a 2 s 440 Hz tone at 30% volume on {SINK}",
        ])

    def test_the_volume_never_goes_above_30_percent_even_from_a_muted_quiet_speaker(self):
        host = run("audio", ["n crackles", "s"], state=MacState(volume="0.05", muted=True))

        self.assertIn(VOLUME_DOWN, host.commands_run)
        self.assertFalse(any(argv[:3] == ["wpctl", "set-volume", NODE] and float(argv[3]) > 0.30 for argv in host.commands_run))
        self.assertEqual((host.state.volume, host.state.muted), ("0.05", True))
        self.assertIn("note: crackles", check(host, "audio.speaker-tone")["evidence"])

    def assert_not_played(self, host: LiveMac, reason: str) -> dict:
        result = check(host, "audio.speaker-tone")
        self.assertEqual((result["kind"], result["status"]), ("human", "skip"))
        self.assertIn(reason, result["evidence"][-1])
        self.assertTrue(result["evidence"][-1].endswith("so nothing was played"), result["evidence"][-1])
        self.assertNotIn(TONE, host.commands_run)
        self.assertFalse(any(argv[:2] == ["wpctl", "set-volume"] for argv in host.commands_run))
        self.assertNotIn(TONE_PROMPT, prompts(host))
        self.assertEqual(host.state.volume, "0.45")
        return result

    def test_without_speakersafetyd_running_no_tone_is_played(self):
        for unit in (CommandResult(3, "inactive\n", ""), CommandResult(3, "failed\n", ""), CommandResult(4, "", "Unit speakersafetyd.service could not be found.\n")):
            with self.subTest(unit=unit):
                rec = answer(live_recording(), ["systemctl", "is-active", "speakersafetyd"], unit.returncode, unit.stdout, unit.stderr)
                rec = answer(rec, ["systemctl", "is-active", "speakerguardd"], 4, "", "Unit speakerguardd.service could not be found.\n")
                host = run("audio", ["s"], rec=rec)

                result = self.assert_not_played(host, "speaker protection isn't active")
                self.assertEqual(result["evidence"][0], f"speakersafetyd: {unit.stdout.strip() or unit.stderr.strip()}")
                self.assertEqual(result["evidence"][1], "speakerguardd: Unit speakerguardd.service could not be found.")

    def test_on_the_macbook_neo_speakerguardd_protects_the_speakers(self):
        # J700: speakersafetyd's drop-in skips it and speakerguardd's feed-forward model protects the speakers.
        rec = answer(live_recording(), ["systemctl", "is-active", "speakersafetyd"], 3, "inactive\n", "")
        rec = answer(rec, ["systemctl", "is-active", "speakerguardd"], 0, "active\n", "")
        host = run("audio", ["y", "s"], rec=rec)

        self.assertIn(TONE, host.commands_run)
        result = check(host, "audio.speaker-tone")
        self.assertEqual((result["kind"], result["status"]), ("human", "pass"))
        self.assertEqual(result["evidence"][:3], [
            "speakersafetyd: inactive", "speakerguardd: active", "kernel log: Speaker volumes unlocked"])

    def test_when_the_kernel_log_doesnt_say_the_amps_are_unlocked_no_tone_is_played(self):
        rec = answer(live_recording(), KERNEL_LOG, stdout="-- No entries --\n")
        rec = answer(rec, ["sudo", "-n", *KERNEL_LOG], 1, stderr="sudo: a password is required\n")
        host = run("audio", ["s"], rec=rec)

        self.assert_not_played(host, "doesn't say 'Speaker volumes unlocked'")

    def test_a_kernel_log_only_root_can_read_is_read_through_passwordless_sudo(self):
        unlocked = "Sep 26 08:49:17 kernel: snd-soc-macaudio sound: Speaker volumes unlocked\n"
        rec = answer(live_recording(), KERNEL_LOG, stdout="-- No entries --\n")
        rec = answer(rec, ["sudo", "-n", *KERNEL_LOG], stdout=unlocked)
        host = run("audio", ["y", "s"], rec=rec)

        self.assertIn(TONE, host.commands_run)
        self.assertEqual(check(host, "audio.speaker-tone")["status"], "pass")

    def test_when_the_default_output_isnt_the_speakers_dsp_sink_no_tone_is_played(self):
        for sink in ("alsa_output.platform-sound.HiFi__Speaker__sink", "bluez_output.F0_C0_7B_98_E6_D4.1"):
            with self.subTest(sink=sink):
                host = run("audio", ["s"], rec=answer(live_recording(), audio.DEFAULT_SINK, stdout=sink + "\n"))

                self.assert_not_played(host, "the default output isn't the speakers' protected DSP sink")
                self.assertIn("default output: another output (its name isn't recorded)", check(host, "audio.speaker-tone")["evidence"])
                self.assertNotIn("F0_C0", json.dumps(report(host)))

    def test_a_tone_that_cant_play_is_skipped_and_the_volume_still_put_back(self):
        host = run("audio", ["s"], rec=answer(live_recording(), TONE, 1, stderr="Connection failure: Connection refused\n"))

        result = check(host, "audio.speaker-tone")
        self.assertEqual(result["status"], "skip")
        self.assertIn("the tone couldn't be played (Connection failure: Connection refused)", result["evidence"][-1])
        self.assertNotIn(TONE_PROMPT, prompts(host))
        self.assertEqual(host.state.volume, "0.45")

    def test_ctrl_c_at_the_tone_question_puts_the_volume_back(self):
        host = run("audio", [INTERRUPT], status=130)

        self.assertEqual(host.commands_run[-1], VOLUME_BACK)
        self.assertEqual(host.state.volume, "0.45")

    def test_the_tone_and_microphone_commands_pass_the_safety_guard(self):
        from omarchy_m_test.safety import refusal

        self.assertIsNone(refusal(TONE))
        self.assertIsNone(refusal(audio.mic_argv(SOURCE)))


# -- the microphone and the headphone jack ----------------------------------------------------

class MicrophoneTest(unittest.TestCase):
    def test_the_built_in_microphone_carries_signal(self):
        host = run("audio", ["s", "s"])

        self.assertIn("say something now. The sound is measured in memory and never kept.", host.output)
        result = check(host, "audio.microphone-signal")
        self.assertEqual((result["kind"], result["status"], result["classification"]["feature"]), ("automatic", "pass", "microphones"))
        self.assertEqual(result["evidence"], [f"default input: {SOURCE}", "captured 144000 samples: peak 178, rms 39.4 (16-bit)"])

    def test_silence_fails(self):
        rec = answer(live_recording(), audio.mic_argv(SOURCE), stdout="samples 144000 peak 3 rms 0.9\n")
        result = check(run("audio", ["s", "s"], rec=rec), "audio.microphone-signal")

        self.assertEqual((result["status"], result["classification"]["outcome"]), ("fail", "fails"))
        self.assertFalse(any(line.startswith(human.NOT_BACKED) for line in result["evidence"]))
        self.assertIn("silence: the peak stayed under 8", result["evidence"])

    def test_nothing_captured_is_skipped_not_failed(self):
        rec = answer(live_recording(), audio.mic_argv(SOURCE), stdout="samples 0 peak 0 rms 0.0\n", stderr="Stream error: No such entity\n")
        result = check(run("audio", ["s", "s"], rec=rec), "audio.microphone-signal")

        self.assertEqual(result["status"], "skip")
        self.assertIn("skipped: nothing was captured (Stream error: No such entity)", result["evidence"])

    def test_another_default_input_isnt_tested_or_named(self):
        usb = "alsa_input.usb-Blue_Microphones_Yeti_Stereo_Microphone_REV8_1234567890-00.analog-stereo"
        host = run("audio", ["s", "s"], rec=answer(live_recording(), audio.DEFAULT_SOURCE, stdout=usb + "\n"))

        result = check(host, "audio.microphone-signal")
        self.assertEqual(result["status"], "skip")
        self.assertIn("isn't the built-in microphone", result["evidence"][0])
        self.assertFalse(any(argv[:2] == ["sh", "-c"] and "parec" in argv[2] for argv in host.commands_run))
        self.assertNotIn("Yeti", json.dumps(report(host)))


class HeadphoneTest(unittest.TestCase):
    def test_plugging_headphones_in_is_asked_after_the_tone(self):
        host = run("audio", ["y", "y plugged in, sound moved"])

        self.assertLess(prompts(host).index(TONE_PROMPT), prompts(host).index(HEADPHONE_PROMPT))
        result = check(host, "audio.headphone-detection")
        self.assertEqual((result["kind"], result["status"], result["classification"]["feature"]), ("human", "pass", "headphone-jack"))
        self.assertIn("note: plugged in, sound moved", result["evidence"])
        self.assertEqual(result["evidence"][-2], f"default output after: {SINK} (the speakers' DSP sink)")
        # The sound stayed on the speakers: the yes stands, noted as not backed by what the Mac reads.
        self.assertEqual(result["evidence"][-1], f"{human.NOT_BACKED} (the default output afterwards isn't the headphone jack)")
        self.assertIn("You can unplug the headphones.", host.output)

    def test_no_answer_is_skipped_never_failed(self):
        result = check(run("audio", ["y", ENDED]), "audio.headphone-detection")

        self.assertEqual(result["status"], "skip")


# -- the display ----------------------------------------------------------------------------

def no_notch(rec: dict) -> dict:
    """The recording as a MacBook Pro 13-inch M2 (j493): no notch."""
    rec = copy.deepcopy(rec)
    rec["files"]["/proc/device-tree/compatible"] = {"text": "apple,j493\u0000apple,t8112\u0000apple,arm-platform\u0000"}
    rec["files"]["/proc/device-tree/model"] = {"text": "Apple MacBook Pro (13-inch, M2, 2022)\u0000"}
    return rec


class NotchAndCursorTest(unittest.TestCase):
    def test_the_notch_bar_and_cursor_are_asked_on_a_notched_mac(self):
        host = run("display", ["y", "s", "n trails over the bar"])

        notch, pointer = check(host, "display.notch-bar"), check(host, "display.cursor")
        self.assertEqual((notch["kind"], notch["status"], notch["classification"]["feature"]), ("human", "pass", "notch-bar"))
        self.assertEqual(notch["classification"]["layer"], "omarchy")
        self.assertEqual((pointer["status"], pointer["classification"]["outcome"]), ("fail", "fails"))
        self.assertIn("note: trails over the bar", pointer["evidence"])
        self.assertEqual(prompts(host)[1:], [NOTCH_PROMPT, BRIGHTNESS_PROMPT, CURSOR_PROMPT])

    def test_a_mac_without_a_notch_isnt_asked_about_it(self):
        host = run("display", ["s", "s"], rec=no_notch(live_recording()))

        self.assertNotIn(NOTCH_PROMPT, prompts(host))
        result = check(host, "display.notch-bar")
        self.assertEqual((result["status"], result["evidence"]), ("skip", ["skipped: this Mac has no notch"]))

    def test_a_reference_run_isnt_asked_about_omarchys_bar_or_keyboard_light(self):
        rec = reference(live_recording())
        for section in ("display", "input"):
            with self.subTest(section=section):
                host = run(section, [ENDED], rec=rec)

                self.assertNotIn(NOTCH_PROMPT, prompts(host))
                self.assertNotIn(KEYBOARD_PROMPT, prompts(host))
        self.assertIn(BRIGHTNESS_PROMPT, prompts(run("display", [ENDED], rec=rec)))


class BrightnessTest(unittest.TestCase):
    def test_the_backlight_steps_three_times_and_goes_back_before_the_question(self):
        host = run("display", ["s", "y", "s"])

        sets = [argv for argv in host.commands_run if argv[:1] == ["brightnessctl"] and "set" in argv]
        self.assertEqual(sets, [*STEPS, PANEL_BACK])
        self.assertEqual(host.commands_run.count(display.PAUSE), 3)
        self.assertLess(host.output.index("Watch the built-in screen"), host.output.index(BRIGHTNESS_PROMPT))
        result = check(host, "display.brightness-steps")
        self.assertEqual((result["kind"], result["status"], result["classification"]["feature"]), ("human", "pass", "brightness"))
        self.assertEqual(result["evidence"][:3], [
            "backlight apple-panel-bl: 250/500 at the start",
            "stepped to 175 (read back 175), 112 (read back 112), 62 (read back 62)",
            "put back to 250",
        ])

    def test_a_dim_screen_steps_up_and_never_goes_off(self):
        self.assertEqual(display.steps(250, 500), [175, 112, 62])
        self.assertEqual(display.steps(50, 500), [125, 200, 275])
        self.assertEqual(display.steps(20, 25), [14, 9, 5])
        self.assertTrue(all(level > 0 for value in range(0, 501, 7) for level in display.steps(value, 500)))

    def test_a_backlight_that_cant_be_set_is_skipped_and_left_as_it_was(self):
        # The first step took, the second didn't: put back once, then nothing is pending.
        rec = answer(live_recording(), STEPS[1], 1, stderr="Failed to set brightness: Permission denied\n")
        host = run("display", ["s", "s"], rec=rec)

        result = check(host, "display.brightness-steps")
        self.assertEqual(result["status"], "skip")
        self.assertIn("brightnessctl couldn't set the backlight (Failed to set brightness: Permission denied)", result["evidence"][-1])
        self.assertNotIn(BRIGHTNESS_PROMPT, prompts(host))
        self.assertEqual(host.commands_run.count(PANEL_BACK), 1)

    def test_a_backlight_it_may_not_set_at_all_is_skipped_with_nothing_to_put_back(self):
        # Over SSH brightnessctl gets no seat: no step takes, and putting it back would fail the same way.
        denied = "Failed to set brightness: Operation not permitted\n"
        rec = answer(answer(live_recording(), STEPS[0], 1, stderr=denied), PANEL_BACK, 1, stderr=denied)
        host = run("display", ["s", "s"], rec=rec)

        result = check(host, "display.brightness-steps")
        self.assertEqual(result["status"], "skip")
        self.assertNotIn(PANEL_BACK, host.commands_run)
        self.assertNotIn("Couldn't undo", host.output)

    def test_a_backlight_that_doesnt_take_its_value_fails_without_asking(self):
        rec = answer(live_recording(), STEPS[1], stdout="apple-panel-bl,backlight,175,35%,500\n")
        host = run("display", ["s", "s"], rec=rec)

        result = check(host, "display.brightness-steps")
        self.assertEqual((result["kind"], result["status"]), ("human", "fail"))
        self.assertEqual(result["evidence"][-1], "not asked: the backlight didn't take 112")
        self.assertNotIn(BRIGHTNESS_PROMPT, prompts(host))
        self.assertIn(PANEL_BACK, host.commands_run)

    def test_ctrl_c_during_the_steps_puts_the_brightness_back(self):
        class Stopped(LiveMac):
            def run(self, argv):
                if list(argv) == display.PAUSE:
                    self.commands_run.append(list(argv))
                    raise KeyboardInterrupt
                return super().run(argv)

        host = run("display", ["s"], cls=Stopped, status=130)

        self.assertEqual(host.commands_run[-1], PANEL_BACK)
        self.assertIn("put back", host.output)

    def test_a_mac_without_brightnessctl_is_skipped(self):
        rec = answer(live_recording(), display.LIST_LIGHTS, 127, stderr="brightnessctl: command not found\n")
        result = check(run("display", ["s", "s"], rec=rec), "display.brightness-steps")

        self.assertEqual(result["evidence"], ["skipped: brightnessctl couldn't list the lights (brightnessctl: command not found)"])


# -- the keyboard light ------------------------------------------------------------------------

class Covered(LiveMac):
    """The sensor reads dark and the keys light up once the human has been asked to cover it."""

    covered = False

    def prompt(self, message):
        self.covered = self.covered or message == KEYBOARD_PROMPT
        return super().prompt(message)

    def read_file(self, path):
        if path == LUX and self.covered:
            return b"2\n"
        return super().read_file(path)

    def run(self, argv):
        if list(argv) == KEYS and self.covered:
            self.commands_run.append(list(argv))
            return CommandResult(0, "kbd_backlight,leds,128,50%,255\n", "")
        return super().run(argv)


class KeyboardLightTest(unittest.TestCase):
    def test_covering_the_sensor_lights_the_keyboard(self):
        host = run("input", ["y", *KEYS_AND_TRACKPAD], cls=Covered)

        self.assertLess(host.output.index("Cover the camera and notch"), host.output.index(KEYBOARD_PROMPT))
        self.assertIn("You can uncover it now.", host.output)
        result = check(host, "input.keyboard-light-follows-room")
        self.assertEqual((result["kind"], result["status"], result["classification"]["feature"]), ("human", "pass", "auto-keyboard-light"))
        self.assertEqual(result["evidence"][0], "before: ambient light 733 lux, keyboard light 0/255")
        self.assertEqual(result["evidence"][-1], "while covered: ambient light 2 lux, keyboard light 128/255")

    def test_a_yes_the_readings_dont_back_is_noted_and_still_passes(self):
        result = check(run("input", ["y", *KEYS_AND_TRACKPAD]), "input.keyboard-light-follows-room")

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["evidence"][-1], f"{human.NOT_BACKED} (the keyboard light didn't come on or get brighter: "
                                                  "ambient light 733 to 733 lux, keyboard light 0/255 to 0/255)")

    def test_a_no_while_the_keys_lit_up_is_noted_and_still_fails(self):
        result = check(run("input", ["n", *KEYS_AND_TRACKPAD], cls=Covered), "input.keyboard-light-follows-room")

        self.assertEqual(result["status"], "fail")
        self.assertTrue(result["evidence"][-1].startswith(f"{human.NOT_BACKED} (the keyboard light got brighter"))

    def test_a_keyboard_light_that_stays_off_is_a_failure_the_human_reports(self):
        result = check(run("input", ["n stayed dark", *KEYS_AND_TRACKPAD]), "input.keyboard-light-follows-room")

        self.assertEqual((result["status"], result["classification"]["outcome"]), ("fail", "fails"))

    def test_without_the_sensor_or_the_keyboard_light_it_isnt_asked(self):
        no_sensor = copy.deepcopy(live_recording())
        no_sensor["dirs"]["/sys/bus/iio/devices"] = []
        no_keys = answer(live_recording(), display.LIST_LIGHTS, stdout="apple-panel-bl,backlight,250,50%,500\n")
        for rec, why in ((no_sensor, "no ambient light sensor"), (no_keys, "no keyboard light")):
            with self.subTest(why=why):
                host = run("input", KEYS_AND_TRACKPAD, rec=rec)

                self.assertNotIn(KEYBOARD_PROMPT, prompts(host))
                self.assertIn(why, check(host, "input.keyboard-light-follows-room")["evidence"][0])


# -- a whole run -----------------------------------------------------------------------------

class WholeRunTest(unittest.TestCase):
    def test_the_m2_asks_its_questions_in_section_order_and_leaves_everything_as_it_was(self):
        host = LiveMac(live_recording(), answers=[ENTER, *at_the_seat("m2-max-image2")])

        self.assertEqual(main(ARGS, host), 0)

        self.assertEqual([p for p in prompts(host) if p.endswith("[Y/n/s] ")], [
            NOTCH_PROMPT, BRIGHTNESS_PROMPT, CURSOR_PROMPT, TONE_PROMPT, HEADPHONE_PROMPT, network.PAIRING_QUESTION + " [Y/n/s] ", KEYBOARD_PROMPT,
            *(question + " [Y/n/s] " for question in (inputs.FUNCTION_KEYS_QUESTION,
                                                      *(inputs.GESTURE_QUESTIONS[name] for name in (inputs.CLICK, inputs.TWO_FINGER_CLICK, inputs.SCROLL)),
                                                      ports.DEVICES_QUESTION, ports.PICTURE_QUESTION)),
        ])
        self.assertEqual(host.state.volume, "0.45")
        self.assertEqual([argv for argv in host.commands_run if argv[:1] == ["brightnessctl"] and "set" in argv][-1], PANEL_BACK)
        self.assertEqual(errors(SCHEMA, report(host)), [])

    def test_the_golden_reports_play_the_tone_only_where_protection_is_confirmed(self):
        m2, m1 = (golden(name) for name in ("m2-max-image2", "m1-pro-mx-mac"))

        self.assertIn(f"played a 2 s 440 Hz tone at 30% volume on {SINK}", m2["audio.speaker-tone"]["evidence"])
        self.assertIn("answer: none (counted as skipped)", m2["audio.speaker-tone"]["evidence"])
        self.assertEqual(m2["audio.microphone-signal"]["status"], "pass")
        # No kernel log of the M1 was ever saved: its amps can't be confirmed unlocked, so nothing played.
        self.assertIn("so nothing was played", m1["audio.speaker-tone"]["evidence"][-1])
        self.assertFalse(any("played a" in line for line in m1["audio.speaker-tone"]["evidence"]))


def golden(name: str) -> dict[str, dict]:
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "schema", "golden", f"{name}.json")
    with open(path, encoding="utf-8") as f:
        return {c["id"]: c for c in json.load(f)["checks"]}


if __name__ == "__main__":
    unittest.main()
