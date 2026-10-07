"""Seam A: the terminal UI, the section list and skipping.

A run at a terminal (the recorded host is given one) looks like Omarchy: the
installed logo in the theme's green, plain bold section titles over a rule, gum
prompts, the live feed. Off a terminal it is plain text.
"""

from __future__ import annotations

import json
import os
import re
import unittest

from omarchy_m_test.app import main
from omarchy_m_test.host import CommandResult, HttpResponse, Terminal
from omarchy_m_test.recording import ENDED, INTERRUPT
from omarchy_m_test.sections import APPLE
from omarchy_m_test.ui import BOLD, FEED_PREFIX, RESET, RULE, Progress, Ui, progress_of
from tests.desktop import (
    ACCENT_RGB, FEED_SECTIONS, GUM_UNANSWERED, OFFERED_OVER_SSH, TITLES, UNANSWERED, UNANSWERED_AT_A_TERMINAL, GREEN_RGB, GREY_RGB, LOGO, TERMINAL, TOKYO_GREEN_RGB,
    bare_desktop, command, host, omarchy_desktop, recording,
)
from tests.schema_validator import errors

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "schema")
REPORT_FILE = "omarchy-m-test-report.json"
ENTER = ""
ALL_KEPT = CommandResult(0, TITLES, "")
UPLOAD_NO = CommandResult(1, "", "")


def read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


GOLDEN = read(os.path.join(SCHEMA_DIR, "golden", "m2-max-image2.json"))
SCHEMA = json.loads(read(os.path.join(SCHEMA_DIR, "report-v1.schema.json")))


def ttys(mac) -> list[tuple[list[str], object]]:
    return [(event[1], event[2]) for event in mac.transcript if event[0] == "tty"]


class ProgressTest(unittest.TestCase):
    def test_the_bar_is_weighted_by_each_sections_checks_and_shows_the_current_one(self):
        running = list(APPLE)
        weights = [len(section.check_ids) for section in running]
        audio = next(n for n, section in enumerate(running, 1) if section.id == "audio")
        found = progress_of(running, audio, {section.id for section in running[:audio - 1]})
        self.assertEqual((found.number, found.count, found.title), (audio, len(running), "Audio"))
        self.assertEqual((found.done, found.current, found.total), (sum(weights[:audio - 1]), weights[audio - 1], sum(weights)))
        self.assertEqual(found.label, f"Section {audio}/{len(running)} · Audio")
        self.assertEqual(sum(found.cells(30)), 30)
        self.assertEqual(Progress(13, 13, "Benchmarks", 99, 1, 100).cells(30), (30, 0, 0))  # never wider than the bar
        self.assertEqual(Progress(1, 2, "Boot", 0, 1, 100).cells(30), (0, 1, 29))
        self.assertEqual(Progress(2, 4, "CPU", 5, 5, 20).plain(), "Section 2/4 - CPU [########=======---------------] 25%")


    def test_on_a_narrow_terminal_the_progress_line_still_fits_one_row(self):
        ui = Ui.for_host(host(omarchy_desktop(recording()), terminal=Terminal(40, 20)))
        for width in (39, 30, 12):
            line = re.sub(r"\033\[[0-9;]*m", "", ui.progress_line(Progress(13, 13, "Benchmarks", 90, 10, 100), width))
            self.assertLessEqual(len(line), width, line)


class OmarchyLookTest(unittest.TestCase):
    def run_omarchy(self, env=None, answers=(ENTER, ALL_KEPT, *GUM_UNANSWERED, UPLOAD_NO), **kwargs):
        mac = host(omarchy_desktop(recording(), env), answers=answers, terminal=TERMINAL, **kwargs)
        status = main([], mac)
        return status, mac

    def test_the_logo_is_the_installed_one_centred_in_the_themes_green_after_a_cleared_screen(self):
        status, mac = self.run_omarchy()

        self.assertEqual(status, 0)
        first = mac.transcript[0][1]
        self.assertTrue(first.startswith("\033[H\033[2J"), first)
        logo_event = next(e[1] for e in mac.transcript if e[0] == "show" and LOGO.splitlines()[1] in e[1])
        self.assertIn(GREEN_RGB, logo_event)
        left = (TERMINAL.width - max(len(line) for line in LOGO.splitlines())) // 2
        self.assertIn(" " * left + LOGO.splitlines()[1], logo_event)

    def test_section_titles_are_plain_bold_text_in_the_themes_accent_over_a_rule(self):
        status, mac = self.run_omarchy()

        left = " " * ((TERMINAL.width - max(len(line) for line in LOGO.splitlines())) // 2)
        shows = [e[1] for e in mac.transcript if e[0] == "show"]
        title = next(i for i, text in enumerate(shows) if text.endswith("Boot" + RESET))
        self.assertEqual(shows[title], left + BOLD + "\033[" + ACCENT_RGB + "mBoot" + RESET)
        self.assertEqual(shows[title + 1], left + "\033[" + ACCENT_RGB + "m" + RULE * 40 + RESET)
        # No FIGlet font: omarchy-ascii is never run, so nothing but the terminal's own font draws a title.
        self.assertFalse(any(argv[:1] == ["omarchy-ascii"] for argv in mac.commands_run))

    def test_each_section_starts_with_where_the_run_is_and_the_bar_stays_under_the_live_feed(self):
        status, mac = self.run_omarchy()

        self.assertEqual(status, 0)
        plain = [re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", e[1]) for e in mac.transcript if e[0] == "show"]
        starts = [text.strip() for text in plain if "\r" not in text and re.match(r"^\n *[━─]+  Section \d+/", text)]
        found = [re.search(r"Section (\d+)/(\d+) · ([A-Za-z ]+?)  \d+%", text).groups() for text in starts]
        count = len(found)  # the sections this run runs (Sleep isn't, without a seat): numbered 1 to count, in order
        self.assertGreater(count, 10)
        titles = [title for _, _, title in found]
        self.assertEqual(found, [(str(n), str(count), title) for n, title in enumerate(titles, 1)])
        self.assertEqual(titles, [section.title for section in APPLE if section.title in titles])
        self.assertTrue(starts[0].endswith("  0%"), starts[0])
        # The live feed redraws with the same line as its last row, in the theme's colours.
        frames = [e[1] for e in mac.transcript if e[0] == "show" and FEED_PREFIX in e[1] and "Section 1/" in e[1]]
        self.assertTrue(frames)
        self.assertTrue(re.sub(r"\033\[[0-9;?]*[A-Za-z]", "", frames[-1]).rstrip().endswith(f"Section 1/{count} · Boot  0%"))
        self.assertIn(ACCENT_RGB, frames[-1].rsplit("\n", 1)[-1])

    def test_long_lines_wrap_inside_the_text_column_under_their_own_start(self):
        status, mac = self.run_omarchy()

        left = (TERMINAL.width - max(len(line) for line in LOGO.splitlines())) // 2
        text = [e[1] for e in mac.transcript if e[0] == "show" and "Checks that need root" in e[1]][0]
        lines = re.sub(r"\033\[[0-9;]*m", "", text).split("\n")
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertTrue(line.startswith(" " * left) and not line[left].isspace(), repr(line))
            self.assertLess(len(line), TERMINAL.width)
        # The section list: a wrapped description carries on under its section's name, past the number and mark.
        listed = [re.sub(r"\033\[[0-9;]*m", "", e[1]) for e in mac.transcript if e[0] == "show" and "(hardware):" in e[1]][0]
        first, *rest = listed.split("\n")
        self.assertTrue(rest, listed)
        hang = first.index("Hardware (hardware)")
        for line in rest:
            self.assertEqual(len(line) - len(line.lstrip()), hang, repr(line))
            self.assertLess(len(line), TERMINAL.width)

    def test_prompts_are_gum_with_omarchys_installer_styling(self):
        status, mac = self.run_omarchy()

        prompts = [argv for argv, _ in ttys(mac)]
        choose, confirm = prompts[0], prompts[-1]
        # sudo for the OpenGL check's temporary package, the charge limit and the benchmarks' package (installed
        # without a question); the human checks are one-line [Y/n/s] prompts, never gum.
        self.assertEqual(prompts[1:-1], [["sudo", "-v"]] * 3)
        human = [e[1] for e in mac.transcript if e[0] == "prompt" and e[1].endswith(" [Y/n/s] ")]
        self.assertEqual(len(human), 13)
        self.assertEqual(choose[:2], ["gum", "choose"])
        self.assertIn("--no-limit", choose)
        self.assertEqual(choose[choose.index("--selected") + 1], "*")
        self.assertEqual(choose[-len(OFFERED_OVER_SSH):], OFFERED_OVER_SSH)
        self.assertEqual(confirm[:3], ["gum", "confirm", "Upload this report to https://omarchy-m-testing.org? [Y/n]"])
        self.assertNotIn("--default=false", confirm)
        self.assertIn("Not uploaded", mac.output)
        self.assertEqual(mac.written, {REPORT_FILE: GOLDEN})

    def test_gum_gets_omarchys_installer_colours_unless_the_user_set_their_own(self):
        seen = {}

        class Recording(type(host(recording()))):
            def run_tty(self, argv, env=None):
                seen[argv[1]] = env
                return super().run_tty(argv, env)

        mac = Recording(omarchy_desktop(recording(), {"GUM_CONFIRM_SELECTED_BACKGROUND": "5"}),
                        answers=[ENTER, ALL_KEPT, *GUM_UNANSWERED, UPLOAD_NO], terminal_size=TERMINAL)
        main([], mac)

        self.assertEqual(seen["choose"]["GUM_CHOOSE_CURSOR_FOREGROUND"], "2")
        self.assertEqual(seen["confirm"]["GUM_CONFIRM_PROMPT_FOREGROUND"], "6")
        self.assertNotIn("GUM_CONFIRM_SELECTED_BACKGROUND", seen["confirm"])
        self.assertTrue(seen["confirm"]["GUM_CONFIRM_PADDING"].startswith("0 0 0 "))

    def test_ctrl_c_in_a_gum_prompt_interrupts_the_run(self):
        status, mac = self.run_omarchy(answers=[ENTER, CommandResult(130, "", "")])

        self.assertEqual(status, 130)
        self.assertIn("Interrupted", mac.output)
        self.assertEqual(mac.written.get(REPORT_FILE), None)

    def test_the_report_is_the_same_as_off_a_terminal(self):
        created = HttpResponse(201, json.dumps({"report_url": "https://x/r/1", "deletion_url": "https://x/r/1/d"}))
        status, mac = self.run_omarchy(answers=[ENTER, ALL_KEPT, *GUM_UNANSWERED, CommandResult(0, "", "")], responses=[created])

        self.assertEqual(status, 0)
        self.assertEqual(mac.posts[0].body, mac.written[REPORT_FILE])
        self.assertEqual(json.loads(mac.written[REPORT_FILE]), json.loads(GOLDEN))
        self.assertEqual(errors(SCHEMA, json.loads(mac.written[REPORT_FILE])), [])


class FallbackLookTest(unittest.TestCase):
    def test_without_omarchy_the_run_uses_tokyo_night_and_a_plain_title(self):
        mac = host(bare_desktop(recording()), answers=[ENTER, ENTER, *UNANSWERED_AT_A_TERMINAL, "n"], terminal=TERMINAL)

        status = main([], mac)

        self.assertEqual(status, 0)
        self.assertIn(TOKYO_GREEN_RGB, mac.output)
        self.assertNotIn(LOGO.splitlines()[1], mac.output)
        # No gum: the section picker and upload question are plain prompts.
        prompts = [e[1] for e in mac.transcript if e[0] == "prompt"]
        self.assertTrue(any("Sections to skip" in p for p in prompts), prompts)
        # The only interactive command is sudo asking for its password (the OpenGL check's temporary package, the
        # charge limit, the benchmarks' package: unanswered here, so asked each time; typed once, sudo caches it).
        self.assertEqual([argv for argv, _ in ttys(mac)], [["sudo", "-v"]] * 3)

    def test_gum_off_omarchy_is_styled_in_tokyo_night(self):
        seen = {}

        class Recording(type(host(recording()))):
            def run_tty(self, argv, env=None):
                seen[argv[1]] = env
                return super().run_tty(argv, env)

        mac = Recording(bare_desktop(recording(), gum=True), answers=[ENTER, ALL_KEPT, *GUM_UNANSWERED, UPLOAD_NO], terminal_size=TERMINAL)
        main([], mac)

        self.assertEqual(seen["confirm"]["GUM_CONFIRM_SELECTED_BACKGROUND"], "#9ece6a")

    def test_an_older_omarchy_theme_directory_is_read_without_omarchy_theme_color(self):
        old = 'color2 = "#a6e3a1"\ncolor4 = "#89b4fa"\ncolor8 = "#6c7086"\ncolor7 = "#cdd6f4"\n'
        mac = host(bare_desktop(recording(), old_theme=old), answers=[ENTER, ENTER, *UNANSWERED_AT_A_TERMINAL, "n"], terminal=TERMINAL)

        main([], mac)

        self.assertIn(GREEN_RGB, mac.output + "".join(e[1] for e in mac.transcript if e[0] == "prompt"))
        self.assertIn(ACCENT_RGB, mac.output)

    def test_off_a_terminal_the_output_is_plain_text(self):
        mac = host(recording(), answers=[ENTER, ENDED])

        main(["--dry-run"], mac)

        self.assertNotIn("\033", mac.output)
        self.assertEqual(mac.written, {REPORT_FILE: GOLDEN})
        # Where the run is, as plain ASCII at each section's start.
        starts = [e[1] for e in mac.transcript if e[0] == "show" and e[1].startswith("Section ")]
        self.assertEqual(starts[0], f"Section 1/{len(starts)} - Boot [{'=' * 5}{'-' * 25}] 0%")
        self.assertTrue(all(re.fullmatch(r"Section \d+/\d+ - [A-Za-z ]+ \[[#=-]{30}\] \d+%", text) for text in starts), starts)
        self.assertTrue(all(text.isascii() for text in starts))


class LiveFeedTest(unittest.TestCase):
    def run_feed(self, rec=None):
        rec = rec or omarchy_desktop(recording())
        mac = host(rec, answers=[ENTER, CommandResult(0, TITLES + "Kernel\n", ""), *GUM_UNANSWERED, UPLOAD_NO], terminal=TERMINAL)
        status = main([], mac, sections=FEED_SECTIONS)
        return status, mac

    def test_automatic_checks_show_what_they_run_in_a_grey_installer_style_tail(self):
        status, mac = self.run_feed()

        self.assertEqual(status, 0)
        frames = [e[1] for e in mac.transcript if e[0] == "show" and "  → " in e[1]]
        self.assertTrue(frames)
        self.assertTrue(any("  → $ uname -r" in f for f in frames))
        last = next(f for f in reversed(frames) if "  → $ journalctl --unit=omarchy-provision-hardware.service" in f)
        self.assertIn("omarchy-provision-hardware", last.split("$ journalctl")[1])  # the command's own output
        # Long lines wrap inside the feed under a two-space hang, never past the terminal's edge.
        rows = [re.sub(r"\033\[[0-9;]*[A-Za-z]|\r", "", row) for row in frames[-1].split("\n")]
        self.assertTrue(any(row.lstrip().startswith("→   ") for row in rows), rows)
        self.assertTrue(all(len(row) < TERMINAL.width for row in rows), rows)
        self.assertIn(GREY_RGB, last)
        # Redrawn in place: each frame moves back up over the previous one.
        self.assertTrue(all(f.startswith("\033[") and "A\r" in f[:8] for f in frames))

    def test_the_feed_never_passes_a_commands_escape_sequences_to_the_terminal(self):
        rec = omarchy_desktop(recording())
        for entry in rec["commands"]:
            if entry["argv"] == ["uname", "-r"]:
                entry["stdout"] = "\033]0;owned\007\033[31mred\033[0m 7.1\n"
        status, mac = self.run_feed(rec)

        frames = "".join(e[1] for e in mac.transcript if e[0] == "show" and "  → " in e[1])
        self.assertIn("  → red 7.1", frames)
        self.assertNotIn("owned", frames)


class SectionsTest(unittest.TestCase):
    def test_sections_are_listed_up_front_before_anything_runs(self):
        mac = host(recording(), answers=[ENTER, ENDED])

        main(["--dry-run"], mac)

        listed = next(i for i, e in enumerate(mac.transcript) if e[0] == "show" and "Boot (boot)" in e[1])
        checked = next(i for i, e in enumerate(mac.transcript) if e[0] == "show" and "system.identity" in e[1])
        self.assertLess(listed, checked)

    def test_a_skipped_section_reports_its_checks_as_skipped_not_failed(self):
        mac = host(recording(), answers=[ENTER, ENDED])

        status = main(["--dry-run", "--skip", "boot"], mac)

        self.assertEqual(status, 0)
        report = json.loads(mac.written[REPORT_FILE])
        self.assertEqual(errors(SCHEMA, report), [])
        check = report["checks"][0]
        self.assertEqual([c["status"] for c in report["checks"][:12]], ["skip"] * 12)
        self.assertEqual((check["id"], check["status"], check["classification"]["outcome"]), ("system.identity", "skip", "not-tested"))
        self.assertNotIn("model: ", json.dumps(check))

    def test_unticking_a_section_in_the_picker_skips_it(self):
        mac = host(omarchy_desktop(recording()), answers=[ENTER, CommandResult(0, "", ""), UPLOAD_NO], terminal=TERMINAL)

        main([], mac)

        report = json.loads(mac.written[REPORT_FILE])
        self.assertEqual(report["checks"][0]["status"], "skip")

    def test_skip_preselects_in_the_picker(self):
        mac = host(omarchy_desktop(recording()), answers=[ENTER, CommandResult(0, "", ""), UPLOAD_NO], terminal=TERMINAL)

        main(["--skip", "boot"], mac)

        choose = ttys(mac)[0][0]
        self.assertEqual(choose[choose.index("--selected") + 1], ",".join(OFFERED_OVER_SSH[1:]))

    def test_an_unknown_section_name_is_refused_before_anything_runs(self):
        mac = host(recording())

        status = main(["--skip", "teleport"], mac)

        self.assertEqual(status, 4)
        self.assertIn("--skip takes section names: boot, hardware, graphics, video, display, audio, network, sleep, input, touch-id, camera, ports, power, cpu", mac.output)
        self.assertEqual(mac.written, {})

    def test_ctrl_c_at_the_disclaimer_leaves_nothing_behind(self):
        mac = host(recording(), answers=[INTERRUPT])

        status = main([], mac)

        self.assertEqual(status, 130)
        self.assertEqual(mac.written, {})


if __name__ == "__main__":
    unittest.main()
