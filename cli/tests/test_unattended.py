"""Seam A: an unattended run (--unattended), with nobody at the Mac.

The whole CLI runs against the recorded M2 Max at its desktop, where an
attended run asks its human checks, plays a tone, steps the brightness and
offers the disruptive sections. Unattended, it asks only for the disclaimer,
does none of that, and the report says it was unattended. The on-screen video
check needs no person, only a screen: it still runs where logind finds an
active local seat and the screen isn't locked or covered, and asks nobody for anything.
"""

from __future__ import annotations

import unittest

from omarchy_m_test import audio, display, presence
from omarchy_m_test.app import main
from omarchy_m_test.consent import ACCEPT_PROMPT
from omarchy_m_test.recording import ENDED
from omarchy_m_test.sections import APPLE
from tests.desktop import TERMINAL, bare_desktop
from tests.live_mac import LiveMac, MacState, live_recording
from tests.schema_validator import errors
from tests.test_audio_display import SINK
from tests.test_interactive import ARGS, ENTER, SCHEMA, prompts, report
from tests.test_video import CHECKS, GREEN, LOCKED, MAKE_TEMP, NO_OVERLAY, REMOVE_TEMP, SCREENSAVER, UNLOCKED, at_desktop, desktop, monitors, mpv_log, played
from tests.test_video import run as run_video


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


UNATTENDED_VIDEO = [*ARGS, "--unattended"]


class UnattendedVideoTest(unittest.TestCase):
    def run_it(self, host: LiveMac) -> dict:
        return run_video(host, UNATTENDED_VIDEO)

    def assert_skipped_without_playing(self, host: LiveMac, reason: str) -> None:
        checks = self.run_it(host)
        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "skip")
            self.assertIn(reason, checks[check_id]["evidence"][-1])
        self.assertEqual(played(host), [])
        self.assertNotIn(MAKE_TEMP, host.commands_run)

    def test_with_an_active_seat_it_runs_and_asks_only_the_disclaimer(self):
        host = at_desktop(desktop(screen=(UNLOCKED,)))

        checks = self.run_it(host)

        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "pass")
            self.assertIn("on screen: every bar in its colour", checks[check_id]["evidence"])
        self.assertEqual(len(played(host)), 2)
        self.assertEqual(prompts(host), [ACCEPT_PROMPT])
        self.assertIs(report(host)["unattended"], True)
        self.assertEqual(host.commands_run.count(REMOVE_TEMP), 1)

    def test_a_real_wrong_picture_still_fails(self):
        checks = self.run_it(at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), screen=(UNLOCKED,))))

        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "fail")

    def test_over_ssh_nothing_is_played(self):
        for wayland in (True, False):
            with self.subTest(wayland=wayland):
                rec = desktop(env={"SSH_CONNECTION": "10.0.0.2 51000 10.0.0.9 22"}, screen=(UNLOCKED,))
                if not wayland:
                    del rec["env"]["WAYLAND_DISPLAY"]
                self.assert_skipped_without_playing(at_desktop(rec), "running over SSH")

    def test_a_session_logind_calls_remote_is_over_ssh(self):
        state = MacState(installed={"mesa", "pipewire", "mpv", "grim", "ffmpeg"}, session={"Remote": "yes", "Seat": "", "Active": "yes"})
        self.assert_skipped_without_playing(at_desktop(desktop(screen=(UNLOCKED,)), state=state), "running over SSH")

    def test_without_a_wayland_session_nothing_is_played(self):
        rec = desktop(screen=(UNLOCKED,))
        del rec["env"]["WAYLAND_DISPLAY"]
        self.assert_skipped_without_playing(at_desktop(rec), "no Wayland desktop session")

    def test_without_an_active_local_seat_nothing_is_played(self):
        for session, reason in (
            ({"Remote": "no", "Seat": "", "Active": "yes"}, "the session has no seat"),
            ({"Remote": "no", "Seat": "seat0", "Active": "no"}, "the session isn't the active one on its seat"),
            (None, "logind doesn't know this session"),
        ):
            with self.subTest(reason):
                state = MacState(installed={"mesa", "pipewire", "mpv", "grim", "ffmpeg"}, session=session)
                self.assert_skipped_without_playing(at_desktop(desktop(screen=(UNLOCKED,)), state=state), f"no local desktop session ({reason})")

    def test_on_a_locked_screen_nothing_is_played(self):
        self.assert_skipped_without_playing(at_desktop(desktop(screen=(LOCKED,))), "the screen is locked")

    def test_a_screen_that_locks_during_the_check_is_skipped_not_failed(self):
        checks = self.run_it(at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), screen=(UNLOCKED, LOCKED))))

        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "skip")
            self.assertIn("the screen is locked", checks[check_id]["evidence"][-1])

    def test_under_a_full_screen_overlay_nothing_is_played(self):
        self.assert_skipped_without_playing(at_desktop(desktop(screen=(UNLOCKED,), overlays=(SCREENSAVER,))), "a full-screen overlay")

    def test_a_screensaver_that_starts_during_the_check_is_skipped_not_failed(self):
        checks = self.run_it(at_desktop(desktop(GREEN, mpv_log("vaapi", "gpu-next", "waylandvk"), screen=(UNLOCKED,), overlays=(NO_OVERLAY, SCREENSAVER))))

        for check_id in CHECKS:
            self.assertEqual(checks[check_id]["status"], "skip")
            self.assertIn("a full-screen overlay", checks[check_id]["evidence"][-1])

    def test_where_hyprland_cant_say_whats_on_screen_nothing_is_played(self):
        for screen, overlays in ((monitors(["WORKSPACE"]), NO_OVERLAY), ('[{"name": "eDP-1"}]', NO_OVERLAY), ("not json", NO_OVERLAY), (UNLOCKED, "not json")):
            with self.subTest(screen=screen, overlays=overlays):
                self.assert_skipped_without_playing(at_desktop(desktop(screen=(screen,), overlays=(overlays,))), "doesn't say whether the screen is locked or covered")

    def test_missing_players_without_cached_sudo_skip_at_a_terminal_without_asking(self):
        state = MacState(installed={"mesa", "pipewire", "ffmpeg"}, sudo_cached=False)
        state.repository.update({"mpv": ["mpv"], "grim": ["grim"]})
        host = LiveMac(bare_desktop(desktop(screen=(UNLOCKED,))), state=state, answers=[ENTER, ENDED], terminal_size=TERMINAL)

        self.assert_skipped_without_playing(host, "this unattended run doesn't ask for a password")

        self.assertNotIn(["sudo", "-v"], [event[1] for event in host.transcript if event[0] == "tty"])
        self.assertFalse(any(argv[:3] == ["sudo", "-n", "pacman"] for argv in host.commands_run))

    def test_missing_players_install_with_cached_sudo_and_are_removed(self):
        state = MacState(installed={"mesa", "pipewire", "ffmpeg"}, sudo_cached=True)
        state.repository.update({"mpv": ["mpv"], "grim": ["grim"]})
        host = at_desktop(desktop(screen=(UNLOCKED,)), state=state)

        checks = self.run_it(host)

        self.assertEqual(checks[CHECKS[0]]["status"], "pass")
        self.assertIn("installed for this run, removed at its end: mpv grim", checks[CHECKS[0]]["evidence"])
        self.assertNotIn("mpv", host.state.installed)
        self.assertEqual(prompts(host), [ACCEPT_PROMPT])

    def test_a_whole_run_at_a_desktop_changes_nothing_else(self):
        # Hyprland's monitors for the video section; then what the Display section's own read answers on the recorded M2.
        host = LiveMac(desktop(screen=(UNLOCKED, "")), state=MacState(installed={"mesa", "pipewire", "mpv", "grim", "ffmpeg"}), answers=[ENTER, ENDED])

        self.assertEqual(main([*UNATTENDED_VIDEO, "--skip", "benchmarks"], host), 0, host.output)

        found = {check["id"]: check for check in report(host)["checks"]}
        for check_id in CHECKS:
            self.assertEqual(found[check_id]["status"], "pass")
        self.assertEqual(prompts(host), [ACCEPT_PROMPT])
        self.assertEqual(found["camera.image"]["status"], "skip")
        self.assertNotIn(audio.tone_argv(SINK), host.commands_run)
        self.assertFalse(any(argv[:2] == ["wpctl", "set-volume"] for argv in host.commands_run))
        self.assertFalse(any(argv[:1] == ["brightnessctl"] and "set" in argv for argv in host.commands_run))
        for section in APPLE:
            if section.disruptive:
                for check_id in section.check_ids:
                    with self.subTest(check=check_id):
                        self.assertEqual(found[check_id]["status"], "skip")
                        self.assertIn(presence.UNATTENDED, " ".join(found[check_id]["evidence"]))
        for check in found.values():
            if check["kind"] == "human":
                with self.subTest(check=check["id"]):
                    self.assertEqual(check["status"], "skip")


if __name__ == "__main__":
    unittest.main()
