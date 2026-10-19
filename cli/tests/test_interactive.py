"""Seam A: human checks, SSH and seat detection, restorers for volume, Wi-Fi and packages, and the safety rules.

The whole CLI runs against the recorded M2 (tests/live_mac.py keeps its
volume, Wi-Fi, packages and login session as a small model, so the tests can
check everything was put back). The human's answers are scripted. The test
sections below use the framework the way the interactive sections do.
"""

from __future__ import annotations

import json
import os
import unittest

from omarchy_m_test import changes, human, packages, sleep
from omarchy_m_test.app import main
from omarchy_m_test.host import CommandResult
from omarchy_m_test.recording import EOF, INTERRUPT
from omarchy_m_test.session import Context, Section
from tests.desktop import CHECKPOINT, TERMINAL, at_the_seat, bare_desktop, omarchy_desktop, recording
from tests.live_mac import CATALOGUE_PATH, NODE, UNLOAD, LiveMac, MacState, live_recording
from tests.schema_validator import errors

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(HERE)), "schema", "report-v1.schema.json")
REPORT_FILE = "omarchy-m-test-report.json"
ENTER = ""
ARGS = ["--dry-run", "--catalogue", CATALOGUE_PATH]
TONE_QUESTION = "Did you hear the tone?"
TONE_PROMPT = TONE_QUESTION + " [Y/n/s] "
WIFI_QUESTION = "Did the Wi-Fi icon go off and come back?"
BENCH_QUESTION = "Did the benchmark window appear?"
INSTALL_NOTICE = "temporary test package(s)"

VOLUME_DOWN = ["wpctl", "set-volume", NODE, "0.30"]
VOLUME_BACK = ["wpctl", "set-volume", NODE, "0.45"]
UNMUTE = ["wpctl", "set-mute", NODE, "0"]
MUTE_BACK = ["wpctl", "set-mute", NODE, "1"]
WIFI_OFF = ["rfkill", "block", "wlan"]
WIFI_ON = ["rfkill", "unblock", "wlan"]
INSTALL = ["sudo", "-n", "pacman", "-S", "--needed", "--noconfirm", "--asdeps", "glmark2"]
REMOVE = ["sudo", "-n", "pacman", "-R", "--noconfirm", "libpng12", "glmark2"]


# -- test sections -----------------------------------------------------------------

def automatic_skip(check_id: str, reason: str) -> dict:
    return {"id": check_id, "kind": "automatic", "status": "skip", "evidence": [f"skipped: {reason}"]}


def _speaker(ctx: Context) -> list[dict]:
    why = changes.set_volume(ctx, 0.8)  # asks for more than the cap on purpose
    if why:
        return [human.skip("test.tone-heard", why)]
    return [human.check(ctx, "test.tone-heard", TONE_QUESTION, ["played a test tone"])]


def _wifi(ctx: Context) -> list[dict]:
    why = changes.drop_wifi(ctx)
    if why:
        return [automatic_skip("test.wifi-rejoin", why)]
    return [human.check(ctx, "test.wifi-rejoin", WIFI_QUESTION)]


def _bench(ctx: Context, names=("glmark2", "mesa")) -> list[dict]:
    ready = packages.temporary(ctx, list(names), "The GPU benchmark")
    if not ready.ready:
        return [human.skip("test.benchmark", ready.skipped, ready.evidence())]
    return [human.check(ctx, "test.benchmark", BENCH_QUESTION, ready.evidence())]


def _everything(ctx: Context) -> list[dict]:
    """Every kind of change, then a question: where the tests interrupt it."""
    assert changes.set_volume(ctx, 0.3) is None
    assert changes.drop_wifi(ctx) is None
    ready = packages.temporary(ctx, ["glmark2"], "The GPU benchmark")
    return [human.check(ctx, "test.benchmark", BENCH_QUESTION, ready.evidence())]


SPEAKER = Section("speaker", "Speaker", "Plays a quiet tone.", ("test.tone-heard",), _speaker, human_checks=("test.tone-heard",))
WIFI = Section("wifi", "Wifi", "Drops Wi-Fi and rejoins.", ("test.wifi-rejoin",), _wifi, human_checks=("test.wifi-rejoin",), disruptive=True)
BENCH = Section("bench", "Bench", "A GPU benchmark.", ("test.benchmark",), _bench, human_checks=("test.benchmark",))
EVERYTHING = Section("everything", "Everything", "Volume, Wi-Fi and packages.", ("test.benchmark",), _everything,
                     human_checks=("test.benchmark",), disruptive=True)


def mac(answers=(), state: MacState | None = None, env=None, checkpoint=None, cls=LiveMac, **kwargs) -> LiveMac:
    return cls(live_recording(env, checkpoint), state=state, answers=list(answers), **kwargs)


def report(host: LiveMac) -> dict:
    return json.loads(host.written[REPORT_FILE])


def check(host: LiveMac, check_id: str) -> dict:
    return next(c for c in report(host)["checks"] if c["id"] == check_id)


def prompts(host: LiveMac) -> list[str]:
    return [event[1] for event in host.transcript if event[0] == "prompt"]


def after(host: LiveMac, argv: list[str]) -> list[list[str]]:
    return host.commands_run[host.commands_run.index(argv) + 1:]


def ran_sudo(host: LiveMac) -> bool:
    return any(argv[:1] == ["sudo"] for argv in host.commands_run)


with open(SCHEMA_PATH, encoding="utf-8") as f:
    SCHEMA = json.load(f)


# -- human checks ------------------------------------------------------------------

class HumanCheckTest(unittest.TestCase):
    def run_speaker(self, *answers, **kwargs) -> LiveMac:
        host = mac([ENTER, *answers], **kwargs)
        self.assertEqual(main(ARGS, host, sections=(SPEAKER,)), 0)
        self.assertEqual(errors(SCHEMA, report(host)), [])
        return host

    def test_yes_no_and_skip_are_recorded_as_human_pass_fail_and_skip(self):
        for answer, status, outcome in (("y", "pass", "works"), ("no", "fail", "fails"), ("s", "skip", "not-tested")):
            with self.subTest(answer=answer):
                result = check(self.run_speaker(answer), "test.tone-heard")

                self.assertEqual((result["kind"], result["status"]), ("human", status))
                self.assertEqual(result["classification"]["outcome"], outcome)
                self.assertIn(f"asked: {TONE_QUESTION}", result["evidence"])
                self.assertIn("played a test tone", result["evidence"])

    def test_a_note_after_the_answer_is_recorded_scrubbed(self):
        result = check(self.run_speaker("n left speaker crackles, router 192.168.1.20"), "test.tone-heard")

        self.assertEqual(result["status"], "fail")
        self.assertIn("answer: no", result["evidence"])
        self.assertIn("note: left speaker crackles, router <ip>", result["evidence"])
        self.assertNotIn("192.168.1.20", json.dumps(report(self.run_speaker("y 192.168.1.20"))))

    def test_the_human_is_told_how_to_answer_before_the_first_question(self):
        host = self.run_speaker("y")

        self.assertLess(host.output.index("Answer y (yes), n (no) or s (skip); Enter alone is yes."), host.output.index(TONE_PROMPT))

    def test_a_typed_y_is_a_confirmed_yes(self):
        result = check(self.run_speaker("y"), "test.tone-heard")

        self.assertEqual(result["status"], "pass")
        self.assertIn("answer: yes", result["evidence"])
        self.assertNotIn(human.DEFAULTED, result)

    def test_enter_alone_is_yes_but_recorded_as_the_default_unconfirmed(self):
        host = self.run_speaker(ENTER)

        result = check(host, "test.tone-heard")
        self.assertEqual((result["status"], result["classification"]["outcome"]), ("pass", "works"))
        self.assertIs(result[human.DEFAULTED], True)
        self.assertIn(human.DEFAULT_ANSWER, result["evidence"])
        self.assertIn("an Enter-only yes is recorded as unconfirmed", host.output)

    def test_no_answer_at_all_is_a_skip_never_a_failure(self):
        result = check(self.run_speaker(EOF), "test.tone-heard")

        self.assertEqual(result["status"], "skip")
        self.assertIn("answer: none (counted as skipped)", result["evidence"])

    def test_an_answer_that_isnt_y_n_or_s_is_asked_again(self):
        host = self.run_speaker("maybe", "y")

        self.assertEqual(prompts(host).count(TONE_PROMPT), 2)
        self.assertIn("Please answer y, n or s (Enter alone is yes).", host.output)
        self.assertEqual(check(host, "test.tone-heard")["status"], "pass")

    def test_three_answers_that_arent_y_n_or_s_skip_the_check(self):
        result = check(self.run_speaker("a", "b", "c"), "test.tone-heard")

        self.assertEqual(result["status"], "skip")

    def test_a_skipped_section_reports_its_human_checks_as_human(self):
        host = mac([ENTER])

        self.assertEqual(main([*ARGS, "--skip", "speaker"], host, sections=(SPEAKER,)), 0)
        self.assertEqual(check(host, "test.tone-heard")["kind"], "human")
        self.assertEqual(check(host, "test.tone-heard")["status"], "skip")

    def test_at_an_omarchy_terminal_the_answer_and_note_are_one_line_so_enter_can_be_told_apart(self):
        rec = omarchy_desktop(live_recording())
        host = LiveMac(rec, answers=[
            ENTER, CommandResult(0, "Speaker\n", ""),  # disclaimer, sections picker
            "n left channel silent",
        ], terminal_size=TERMINAL)

        self.assertEqual(main(ARGS, host, sections=(SPEAKER,)), 0)

        ttys = [event[1] for event in host.transcript if event[0] == "tty"]
        self.assertFalse([argv for argv in ttys if argv[:2] == ["gum", "choose"] and TONE_QUESTION in argv])
        self.assertTrue(any(p.strip().endswith(TONE_PROMPT.strip()) for p in prompts(host)))
        result = check(host, "test.tone-heard")
        self.assertEqual(result["status"], "fail")
        self.assertIn("note: left channel silent", result["evidence"])

    def test_enter_at_an_omarchy_terminal_is_the_unconfirmed_default(self):
        rec = omarchy_desktop(live_recording())
        host = LiveMac(rec, answers=[ENTER, CommandResult(0, "Speaker\n", ""), ENTER], terminal_size=TERMINAL)

        self.assertEqual(main(ARGS, host, sections=(SPEAKER,)), 0)
        result = check(host, "test.tone-heard")
        self.assertEqual(result["status"], "pass")
        self.assertIs(result[human.DEFAULTED], True)


# -- SSH and the local seat --------------------------------------------------------

class PresenceTest(unittest.TestCase):
    def run_wifi(self, answers=(ENTER, "y"), sections=(WIFI,), **kwargs) -> LiveMac:
        host = mac(answers, **kwargs)
        self.assertEqual(main(ARGS, host, sections=sections), 0)
        return host

    def assert_skipped(self, host: LiveMac, reason: str) -> None:
        result = check(host, "test.wifi-rejoin")
        self.assertEqual((result["kind"], result["status"]), ("human", "skip"))
        self.assertEqual(len(result["evidence"]), 1)
        self.assertIn(reason, result["evidence"][0])
        self.assertIn(f"Skipped: {result['evidence'][0][len('skipped: '):]}", host.output)
        self.assertNotIn(WIFI_OFF, host.commands_run)

    def test_at_the_local_seat_the_disruptive_section_runs_and_wifi_is_put_back(self):
        host = self.run_wifi()

        self.assertEqual(after(host, WIFI_OFF), [WIFI_ON])
        self.assertFalse(host.state.wifi_blocked)
        self.assertEqual(check(host, "test.wifi-rejoin")["status"], "pass")

    def test_over_ssh_disruptive_sections_are_skipped_with_the_reason(self):
        for variable in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
            with self.subTest(variable=variable):
                host = self.run_wifi(answers=[ENTER], env={variable: "x"})

                self.assert_skipped(host, "running over SSH")

    def test_a_remote_login_session_counts_as_ssh(self):
        host = self.run_wifi(answers=[ENTER], state=MacState(session={"Remote": "yes", "Seat": "", "Active": "yes"}))

        self.assert_skipped(host, "running over SSH")

    def test_without_a_local_seat_disruptive_sections_are_skipped_with_the_reason(self):
        for session, why in (
            ({"Remote": "no", "Seat": "", "Active": "yes"}, "the session has no seat"),
            ({"Remote": "no", "Seat": "seat0", "Active": "no"}, "isn't the active one"),
            (None, "logind doesn't know this session"),
        ):
            with self.subTest(why=why):
                host = self.run_wifi(answers=[ENTER], state=MacState(session=session))

                self.assert_skipped(host, "no local desktop session")
                self.assert_skipped(host, why)

    def test_other_sections_still_run_over_ssh(self):
        host = self.run_wifi(answers=[ENTER, "y"], env={"SSH_CONNECTION": "x"}, sections=(SPEAKER, WIFI))

        self.assertEqual(check(host, "test.tone-heard")["status"], "pass")
        self.assert_skipped(host, "running over SSH")

    def test_wifi_is_never_dropped_over_ssh_even_by_a_section_not_marked_disruptive(self):
        careless = Section("careless", "Careless", "Forgot it's disruptive.", ("test.wifi-rejoin",), _wifi)
        host = self.run_wifi(answers=[ENTER], env={"SSH_CONNECTION": "x"}, sections=(careless,))

        self.assertNotIn(WIFI_OFF, host.commands_run)
        self.assertIn("skipped: running over SSH", check(host, "test.wifi-rejoin")["evidence"][0])

    def test_without_a_session_id_logind_is_asked_for_the_users_graphical_session(self):
        # A terminal Omarchy's uwsm started runs outside the login session: no XDG_SESSION_ID.
        host = self.run_wifi(env={"XDG_SESSION_ID": ""})

        self.assertIn(["loginctl", "show-session", "auto", "--property=Remote", "--property=Seat", "--property=Active"], host.commands_run)
        self.assertEqual(check(host, "test.wifi-rejoin")["status"], "pass")

    def test_a_session_id_logind_no_longer_knows_falls_back_to_auto(self):
        host = self.run_wifi(state=MacState(session_ids=("auto",)))

        self.assertEqual(check(host, "test.wifi-rejoin")["status"], "pass")

    def test_runs_without_disruptive_sections_never_ask_logind(self):
        host = mac([ENTER, "y"])
        main(ARGS, host, sections=(SPEAKER,))

        self.assertFalse(any(argv[:1] == ["loginctl"] for argv in host.commands_run))

    def test_a_blocked_section_is_not_resumed_into_over_ssh(self):
        interrupted = mac([ENTER, "y", INTERRUPT])
        self.assertEqual(main(ARGS, interrupted, sections=(SPEAKER, WIFI)), 130)

        host = mac([ENTER, ENTER], env={"SSH_CONNECTION": "x"}, checkpoint=interrupted.written[CHECKPOINT])
        main(ARGS, host, sections=(SPEAKER, WIFI))

        self.assertNotIn(WIFI_OFF, host.commands_run)
        self.assertIn("running over SSH", check(host, "test.wifi-rejoin")["evidence"][0])


# -- restorers ---------------------------------------------------------------------

class VolumeTest(unittest.TestCase):
    def test_the_volume_is_capped_at_30_percent_and_put_back(self):
        host = mac([ENTER, "y"])

        main(ARGS, host, sections=(SPEAKER,))

        self.assertIn(VOLUME_DOWN, host.commands_run)
        self.assertEqual(after(host, VOLUME_DOWN)[-1], VOLUME_BACK)
        self.assertEqual((host.state.volume, host.state.muted), ("0.45", False))

    def test_a_muted_speaker_is_unmuted_for_the_check_and_muted_again(self):
        host = mac([ENTER, "y"], state=MacState(muted=True))

        main(ARGS, host, sections=(SPEAKER,))

        self.assertEqual(after(host, UNMUTE), [MUTE_BACK, VOLUME_BACK])
        self.assertEqual((host.state.volume, host.state.muted), ("0.45", True))

    def test_without_a_default_sink_nothing_is_changed_and_the_check_is_skipped(self):
        host = mac([ENTER], state=MacState(has_sink=False))

        main(ARGS, host, sections=(SPEAKER,))

        self.assertFalse(any(argv[:2] == ["wpctl", "set-volume"] for argv in host.commands_run))
        self.assertNotIn(TONE_PROMPT, prompts(host))
        self.assertIn("skipped: no default audio output", check(host, "test.tone-heard")["evidence"][-1])


    def test_a_volume_that_cant_be_set_skips_the_check_and_is_still_put_back(self):
        host = mac([ENTER], state=MacState(volume_fails=True))

        main(ARGS, host, sections=(SPEAKER,))

        self.assertNotIn(TONE_PROMPT, prompts(host))
        self.assertIn("wpctl couldn't set the volume", check(host, "test.tone-heard")["evidence"][-1])
        self.assertIn(VOLUME_BACK, host.commands_run)


class WifiTest(unittest.TestCase):
    def test_wifi_is_only_dropped_when_there_is_a_network_to_rejoin(self):
        for state, why in ((MacState(wifi_up=False), "isn't connected"), (MacState(wifi_blocked=True), "already off")):
            with self.subTest(why=why):
                host = mac([ENTER], state=state)

                main(ARGS, host, sections=(WIFI,))

                self.assertNotIn(WIFI_OFF, host.commands_run)
                self.assertIn(why, check(host, "test.wifi-rejoin")["evidence"][0])


class TemporaryPackagesTest(unittest.TestCase):
    def run_bench(self, answers, state=None, sections=(BENCH,), **kwargs) -> LiveMac:
        host = mac([ENTER, *answers], state=state, **kwargs)
        self.assertEqual(main(ARGS, host, sections=sections), 0)
        return host

    def test_packages_are_installed_without_a_question_shown_and_exactly_those_removed_at_the_end(self):
        host = self.run_bench(["y"])

        self.assertEqual(prompts(host), ["Accept and start? [Y/n] ", BENCH_QUESTION + " [Y/n/s] "])
        self.assertIn("a package that isn't installed: glmark2 (with libpng12)", host.output)
        self.assertIn("Installing 2 temporary test package(s)", host.output)
        self.assertLess(host.output.index(INSTALL_NOTICE), host.output.index(BENCH_QUESTION))
        self.assertLess(host.commands_run.index(INSTALL), host.commands_run.index(REMOVE))
        self.assertEqual(host.state.installed, MacState().installed)  # mesa was there before and stays
        self.assertFalse(any("mesa" in argv for argv in host.commands_run if argv[:4] == ["sudo", "-n", "pacman", "-R"]))
        result = check(host, "test.benchmark")
        self.assertEqual(result["status"], "pass")
        self.assertIn("installed for this run, removed at its end: libpng12 glmark2", result["evidence"])
        self.assertIn("already installed: mesa", result["evidence"])

    def test_packages_already_installed_are_never_installed_or_removed(self):
        host = self.run_bench(["y"], state=MacState(installed={"glmark2", "libpng12", "mesa"}))

        self.assertNotIn(INSTALL_NOTICE, host.output)
        self.assertFalse(ran_sudo(host))
        self.assertEqual(host.state.installed, {"glmark2", "libpng12", "mesa"})

    def test_off_a_terminal_without_cached_sudo_the_check_is_skipped(self):
        host = self.run_bench(["y"], state=MacState(sudo_cached=False))

        self.assertNotIn(INSTALL, host.commands_run)
        self.assertIn("needs sudo", check(host, "test.benchmark")["evidence"][-1])

    def test_at_a_terminal_sudo_asks_for_the_password_there(self):
        rec = bare_desktop(live_recording())
        state = MacState(sudo_cached=False)
        host = LiveMac(rec, state=state, answers=[
            ENTER, ENTER,                     # disclaimer, sections to skip
            CommandResult(0, "", ""),         # sudo -v at the terminal (scripted: no real password)
            "y",
        ], terminal_size=TERMINAL)

        self.assertEqual(main(ARGS, host, sections=(BENCH,)), 0)

        ttys = [event[1] for event in host.transcript if event[0] == "tty"]
        self.assertEqual(ttys, [["sudo", "-v"]])
        self.assertIn(REMOVE, host.commands_run)
        self.assertEqual(state.installed, MacState().installed)

    def test_when_sudo_forgot_the_password_by_removal_it_asks_again_at_the_terminal(self):
        class Forgets(LiveMac):
            def run(self, argv):
                if list(argv) == INSTALL:
                    result = super().run(argv)
                    self.state.sudo_cached = False  # the section took longer than sudo's timeout
                    return result
                return super().run(argv)

        host = Forgets(bare_desktop(live_recording()), answers=[ENTER, ENTER, "y", CommandResult(0, "", "")], terminal_size=TERMINAL)

        self.assertEqual(main(ARGS, host, sections=(BENCH,)), 0)

        self.assertIn(["sudo", "-v"], [event[1] for event in host.transcript if event[0] == "tty"])
        self.assertEqual(host.state.installed, MacState().installed)

    def test_a_failed_install_removes_only_what_it_did_install(self):
        host = self.run_bench(["y"], state=MacState(install_fails_after=1))

        self.assertIn(["sudo", "-n", "pacman", "-R", "--noconfirm", "libpng12"], host.commands_run)
        self.assertEqual(host.state.installed, MacState().installed)
        self.assertIn("pacman couldn't install glmark2", check(host, "test.benchmark")["evidence"][-1])

    def test_the_restorer_is_registered_before_anything_is_installed(self):
        registered = []

        class Watches(LiveMac):
            def run(self, argv):
                if list(argv) == INSTALL:
                    registered.extend(r["argv"] + r.get("packages", []) for r in json.loads(self.written[CHECKPOINT])["restorers"])
                return super().run(argv)

        self.run_bench(["y"], cls=Watches)

        self.assertEqual(registered, [REMOVE])

    def test_packages_that_would_upgrade_installed_ones_are_not_installed(self):
        host = self.run_bench([], state=MacState(outdated={"mesa"}, repository={"glmark2": ["mesa", "glmark2"]}))

        self.assertFalse(ran_sudo(host))
        self.assertIn("would upgrade installed packages (mesa)", check(host, "test.benchmark")["evidence"][-1])

    def test_kernel_firmware_and_boot_packages_are_never_installed(self):
        for names in (("linux-aurora",), ("aura",), ("bootpull",)):  # aura is the MacBook Neo's m1n1; bootpull brings in linux-firmware
            with self.subTest(names=names):
                section = Section("bench", "Bench", "A GPU benchmark.", ("test.benchmark",), lambda ctx: _bench(ctx, names))
                host = self.run_bench([], sections=(section,))

                self.assertFalse(ran_sudo(host))
                self.assertNotIn(INSTALL_NOTICE, host.output)
                self.assertIn("boot packages", check(host, "test.benchmark")["evidence"][-1])

    def test_a_package_the_repositories_dont_have_is_skipped(self):
        section = Section("bench", "Bench", "A GPU benchmark.", ("test.benchmark",), lambda ctx: _bench(ctx, ("nope",)))
        host = self.run_bench([], sections=(section,))

        self.assertFalse(ran_sudo(host))
        self.assertIn("target not found: nope", check(host, "test.benchmark")["evidence"][-1])


class InterruptionTest(unittest.TestCase):
    """Ctrl-C or a killed process in the middle of a section that changed everything."""

    def test_ctrl_c_mid_section_puts_everything_back_newest_first(self):
        state = MacState(muted=True)
        host = mac([ENTER, INTERRUPT], state=state)

        self.assertEqual(main(ARGS, host, sections=(EVERYTHING,)), 130)

        self.assertEqual(after(host, INSTALL)[-4:], [REMOVE, WIFI_ON, MUTE_BACK, VOLUME_BACK])
        self.assertEqual(state, MacState(muted=True))
        self.assertEqual(json.loads(host.written[CHECKPOINT])["restorers"], [])
        self.assertNotIn(REPORT_FILE, host.written)
        self.assertIn("put back", host.output)

    def test_a_run_killed_mid_section_has_everything_put_back_by_the_next_run_first(self):
        class Killed(LiveMac):
            dead = False

            def prompt(self, message):
                if message.startswith(BENCH_QUESTION):
                    self.dead = True
                    raise SystemExit(137)
                return super().prompt(message)

            def run(self, argv):
                if self.dead:
                    raise SystemExit(137)
                return super().run(argv)

        first = mac([ENTER, "y"], cls=Killed)
        with self.assertRaises(SystemExit):
            main(ARGS, first, sections=(EVERYTHING,))
        left = first.written[CHECKPOINT]
        saved = json.loads(left)["restorers"]
        self.assertEqual([r["argv"] + r.get("packages", []) for r in saved], [VOLUME_BACK, WIFI_ON, REMOVE])
        self.assertEqual(saved[-1]["sudo"], True)
        self.assertNotEqual(first.state, MacState())

        # The next run starts on the Mac as the killed one left it, and puts it back before the disclaimer.
        second = mac(["n"], state=first.state.copy(), checkpoint=left)
        self.assertEqual(main(ARGS, second, sections=(EVERYTHING,)), 1)

        self.assertEqual(second.commands_run[-3:], [REMOVE, WIFI_ON, VOLUME_BACK])
        self.assertEqual(second.state, MacState())
        self.assertIn("the temporary packages libpng12 glmark2", second.output)

    def test_a_run_killed_before_the_install_finished_removes_only_what_arrived(self):
        for arrived in (0, 1):
            with self.subTest(arrived=arrived):
                class KilledInstalling(LiveMac):
                    """The process dies while pacman installs: nothing after that reaches the Mac."""
                    dead = False

                    def run(self, argv):
                        if self.dead:
                            raise SystemExit(137)
                        if list(argv) == INSTALL:
                            self.dead = True
                            self.state.installed.update(["libpng12", "glmark2"][:arrived])
                            raise SystemExit(137)
                        return super().run(argv)

                first = mac([ENTER, "y"], cls=KilledInstalling)
                with self.assertRaises(SystemExit):
                    main(ARGS, first, sections=(BENCH,))

                second = mac(["n"], state=first.state.copy(), checkpoint=first.written[CHECKPOINT])
                self.assertEqual(main(ARGS, second, sections=(BENCH,)), 1)

                removals = [argv for argv in second.commands_run if argv[:4] == ["sudo", "-n", "pacman", "-R"]]
                self.assertEqual(removals, [["sudo", "-n", "pacman", "-R", "--noconfirm", "libpng12"]] if arrived else [])
                self.assertEqual(second.state.installed, MacState().installed)
                self.assertNotIn("Couldn't undo", second.output)


# -- what the tool never does ------------------------------------------------------

FORBIDDEN = (
    ["reboot"],
    ["sudo", "-n", "systemctl", "reboot"],
    ["systemctl", "poweroff"],
    ["systemctl", "hibernate"],
    ["systemctl", "start", "reboot.target"],
    ["shutdown", "-r", "now"],
    ["env", "LANG=C", "omarchy-system-reboot"],
    ["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "Reboot", "b", "false"],
    ["bash", "-c", "sleep 1; reboot"],
    ["sudo", "-n", "cryptsetup", "luksAddKey", "/dev/nvme0n1p5"],
    ["sudo", "systemd-cryptenroll", "--tpm2-device=auto", "/dev/nvme0n1p5"],
    ["sudo", "-n", "mkinitcpio", "-P"],
    ["sudo", "-n", "limine-update"],
    ["sudo", "-n", "rm", "-f", "/boot/EFI/Linux/omarchy.efi"],
    ["sudo", "-n", "tee", "/etc/kernel/cmdline"],
    ["sudo", "-n", "efibootmgr", "-o", "0001"],
    ["sudo", "-n", "dd", "if=/dev/zero", "of=/dev/nvme0n1"],
    ["sudo", "-n", "pacman", "-Syu", "--noconfirm"],
    ["sudo", "-n", "pacman", "-S", "--noconfirm", "linux-aurora"],
    ["sudo", "-n", "pacman", "-R", "--noconfirm", "m1n1"],
    ["sudo", "-n", "pacman", "-U", "/tmp/linux.pkg.tar.zst"],
    ["sh", "-c", "echo b > /proc/sysrq-trigger"],
    ["sudo", "-n", "tee", "/proc/sysrq-trigger"],
)
FORBIDDEN_WRITES = ("/boot/limine.conf", "/efi/EFI/BOOT/BOOTAA64.EFI", "/etc/crypttab", "/proc/sysrq-trigger", "/dev/nvme0n1")


def _forbidden(ctx: Context) -> list[dict]:
    evidence = []
    for argv in FORBIDDEN:
        result = ctx.host.run(argv)
        evidence.append(f"{argv[0]}: {result.returncode}")
    ctx.change("a forbidden change", ["reboot"], ["systemctl", "kexec"])
    tty = ctx.host.run_tty(["sudo", "reboot"])
    evidence.append(f"tty: {tty.returncode}")
    for path in FORBIDDEN_WRITES:
        try:
            ctx.host.write_file(path, "x")
            evidence.append(f"wrote {path}")
        except PermissionError:
            evidence.append("refused write")
        try:
            ctx.host.remove_file(path)
            evidence.append(f"removed {path}")
        except PermissionError:
            evidence.append("refused remove")
    return [{"id": "test.forbidden", "kind": "automatic", "status": "fail", "evidence": evidence}]


FORBIDDEN_SECTION = Section("forbidden", "Forbidden", "Tries what the tool must never do.", ("test.forbidden",), _forbidden)

_POWER = {"reboot", "poweroff", "shutdown", "halt", "kexec", "hibernate", "isolate"}
_ENCRYPTION_OR_BOOT = ("cryptsetup", "cryptenroll", "mkinitcpio", "dracut", "limine", "grub", "efibootmgr", "bootctl", "kernel-install", "mkfs")
_BOOT_PACKAGES = ("linux", "m1n1", "aura", "limine", "uboot", "mkinitcpio")


def touches_what_it_never_may(argv: list[str]) -> bool:
    """The test's own, independent reading of the rule, applied to every command a run sent."""
    rest = list(argv)
    while rest and rest[0] in ("sudo", "-n", "env"):
        rest = rest[1:]
    if not rest:
        return False
    program, args = rest[0].rsplit("/", 1)[-1], rest[1:]
    if program in _POWER or (program == "systemctl" and args[:1] and args[0] in _POWER):
        return True
    if program == "dd" or (any(part in program for part in _ENCRYPTION_OR_BOOT) and args[:1] != ["status"]):
        return True
    if program == "pacman":
        flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
        targets = [a for a in args if not a.startswith("-")]
        if "U" in flags or ("S" in flags and ("y" in flags or "u" in flags)):
            return True
        changes = ("S" in flags and "p" not in flags) or "R" in flags
        return changes and any(t.startswith(_BOOT_PACKAGES) or "firmware" in t for t in targets)
    return any(a.startswith(("/boot", "/efi")) for a in args)


class NeverRebootsOrTouchesBootTest(unittest.TestCase):
    def test_commands_that_reboot_or_touch_encryption_or_boot_files_never_reach_the_mac(self):
        host = mac([ENTER])

        self.assertEqual(main(ARGS, host, sections=(FORBIDDEN_SECTION,)), 0)

        for argv in FORBIDDEN:
            self.assertNotIn(argv, host.commands_run)
        self.assertNotIn(["reboot"], host.commands_run)
        self.assertNotIn(["systemctl", "kexec"], host.commands_run)
        self.assertEqual([e[1] for e in host.transcript if e[0] == "tty"], [])
        self.assertEqual([p for p in FORBIDDEN_WRITES if p in host.written or p in host.removed], [])
        evidence = check(host, "test.forbidden")["evidence"]
        self.assertEqual([line for line in evidence if line.endswith(": 126")], [f"{argv[0]}: 126" for argv in FORBIDDEN] + ["tty: 126"])
        self.assertEqual(evidence.count("refused write") + evidence.count("refused remove"), 2 * len(FORBIDDEN_WRITES))
        self.assertIn("it never reboots", host.output)  # the refused restorer is reported, with why

    def test_whole_runs_never_send_such_a_command(self):
        for name in ("m2-max-image2", "m1-pro-mx-mac"):
            with self.subTest(recording=name):
                host = LiveMac(live_recording(base=recording(name)), answers=[ENTER, *at_the_seat(name, reload="y", sleep=("y", "y", "y")), "y", "y", "y", "y"])

                main(ARGS, host, sections=(*_apple(), SPEAKER, WIFI, BENCH))

                self.assertIn(REPORT_FILE, host.written)
                self.assertIn(UNLOAD, host.commands_run)  # the Wi-Fi driver reload ran, and passed the guard
                self.assertEqual(sum(argv[:3] == sleep.lid_watch_argv()[:3] for argv in host.commands_run), 2)  # both lid steps ran
                self.assertEqual([argv for argv in host.commands_run if touches_what_it_never_may(argv)], [])
                self.assertEqual([p for p in host.written if p.startswith(("/boot", "/efi", "/dev/"))], [])

    def test_the_guard_reads_a_recording_as_it_reads_a_mac(self):
        self.assertTrue(touches_what_it_never_may(["sudo", "-n", "systemctl", "reboot"]))
        self.assertTrue(touches_what_it_never_may(["sudo", "-n", "pacman", "-S", "linux-aurora"]))
        self.assertFalse(touches_what_it_never_may(INSTALL))
        self.assertFalse(touches_what_it_never_may(REMOVE))
        self.assertFalse(touches_what_it_never_may(["pacman", "-Q", "linux-aurora", "m1n1"]))


def _apple():
    from omarchy_m_test.sections import APPLE
    return APPLE


if __name__ == "__main__":
    unittest.main()
