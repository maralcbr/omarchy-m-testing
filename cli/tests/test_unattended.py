"""Seam A: an unattended run (--unattended), with nobody at the Mac.

The whole CLI runs against the recorded M2 Max at its desktop, where an
attended run asks its human checks, plays a tone, steps the brightness and
offers the disruptive sections. Unattended, it asks only for the disclaimer,
does none of that, and the report says it was unattended.
"""

from __future__ import annotations

import unittest

from omarchy_m_test import audio, display, presence
from omarchy_m_test.app import main
from omarchy_m_test.consent import ACCEPT_PROMPT
from omarchy_m_test.recording import ENDED
from omarchy_m_test.sections import APPLE
from tests.live_mac import LiveMac, live_recording
from tests.schema_validator import errors
from tests.test_audio_display import SINK
from tests.test_interactive import ARGS, ENTER, SCHEMA, prompts, report


def unattended_run() -> LiveMac:
    host = LiveMac(live_recording(), answers=[ENTER, ENDED])
    assert main([*ARGS, "--unattended"], host) == 0, host.output
    return host


class UnattendedRunTest(unittest.TestCase):
    host = None

    @classmethod
    def setUpClass(cls):
        cls.host = unattended_run()

    def test_only_the_disclaimer_is_asked(self):
        self.assertEqual(prompts(self.host), [ACCEPT_PROMPT])

    def test_the_report_says_it_was_unattended_and_validates(self):
        found = report(self.host)
        self.assertIs(found["unattended"], True)
        self.assertEqual(errors(SCHEMA, found), [])

    def test_every_human_check_is_skipped_as_unattended(self):
        human = [check for check in report(self.host)["checks"] if check["kind"] == "human"]
        self.assertTrue(human)
        for check in human:
            with self.subTest(check=check["id"]):
                self.assertEqual(check["status"], "skip")
        asked_here = {check["id"] for check in human if any(presence.UNATTENDED in line for line in check["evidence"])}
        self.assertTrue({"audio.speaker-tone", "display.brightness-steps", "display.cursor", "input.function-keys"} <= asked_here)

    def test_nothing_a_person_would_see_or_hear_happens(self):
        self.assertNotIn(audio.tone_argv(SINK), self.host.commands_run)
        self.assertFalse(any(argv[:2] == ["wpctl", "set-volume"] for argv in self.host.commands_run))
        self.assertFalse(any(argv[:1] == ["brightnessctl"] and "set" in argv for argv in self.host.commands_run))

    def test_the_disruptive_sections_are_skipped_as_unattended(self):
        disruptive = [section for section in APPLE if section.disruptive]
        self.assertTrue(disruptive)
        found = {check["id"]: check for check in report(self.host)["checks"]}
        for section in disruptive:
            for check_id in section.check_ids:
                with self.subTest(check=check_id):
                    self.assertEqual(found[check_id]["status"], "skip")
                    self.assertIn(presence.UNATTENDED, " ".join(found[check_id]["evidence"]))

    def test_an_attended_run_doesnt_say_unattended(self):
        host = LiveMac(live_recording(), answers=[ENTER, ENDED])
        main(ARGS, host)
        self.assertNotIn("unattended", report(host))


if __name__ == "__main__":
    unittest.main()
