"""Seam A: results explained against the feature catalogue.

The whole CLI runs against a recorded Mac. Variants of the M2 Max recording
(a failing kernel query, another chip) and draft catalogues passed with
--catalogue drive each classification outcome; --explain covers results a
run can't produce yet (skipped human checks). Assertions are on the report
and on what the human saw.
"""

from __future__ import annotations

import copy
import json
import os
import unittest

from omarchy_m_test.app import main
from omarchy_m_test.catalogue import Catalogue, status_label
from omarchy_m_test.recording import ENDED, RecordedHost
from tests.schema_validator import errors
from tests.test_seam_a import ENTER, REPO, REPORT_FILE, RECORDINGS, SCHEMA, golden, read

CATALOGUE = json.loads(read(os.path.join(REPO, "catalogue", "catalogue.json")))
M2_MAX = json.loads(read(os.path.join(RECORDINGS, "m2-max-image2.json")))
DRAFT = "/drafts/catalogue.json"
SAVED = "/reports/saved.json"


def recording(kernel_fails=False, soc=None, files=None) -> dict:
    """The M2 Max recording, optionally with `uname -r` failing (so system.identity fails) or another SoC."""
    rec = copy.deepcopy(M2_MAX)
    if kernel_fails:
        for command in rec["commands"]:
            if command["argv"] == ["uname", "-r"]:
                command.update(returncode=1, stdout="", stderr="uname: failed\n")
    if soc:
        rec["files"]["/proc/device-tree/compatible"] = {"text": f"apple,j416c\0apple,{soc}\0apple,arm-platform\0"}
    rec["files"].update(files or {})
    return rec


def draft(change=None, version=None) -> dict:
    """A copy of the bundled catalogue, edited the way a catalogue pull request would."""
    catalogue = copy.deepcopy(CATALOGUE)
    if version:
        catalogue["catalogue_version"] = version
    if change:
        change(catalogue)
    return catalogue


def feature(catalogue: dict, feature_id: str) -> dict:
    return next(f for f in catalogue["features"] if f["id"] == feature_id)


def devicetree_on_m2_max(**states):
    def change(catalogue):
        feature(catalogue, "devicetree")["chips"]["m2-pro-max-ultra"].update(states)
    return change


def identity_checks(feature_id, **states):
    def change(catalogue):
        catalogue["checks"]["system.identity"] = feature_id
        if states:
            feature(catalogue, feature_id)["chips"]["m2-pro-max-ultra"].update(states)
    return change


def run(rec: dict, argv=("--dry-run",), catalogue: dict | None = None) -> tuple[int, RecordedHost]:
    rec = copy.deepcopy(rec)
    argv = list(argv)
    if catalogue is not None:
        rec["files"][DRAFT] = {"text": json.dumps(catalogue)}
        argv += ["--catalogue", DRAFT]
    mac = RecordedHost(rec, answers=[ENTER, ENDED])
    return main(argv, mac), mac


def written(mac: RecordedHost) -> dict:
    return json.loads(mac.written[REPORT_FILE])


class ReportRecordsTheCatalogueTest(unittest.TestCase):
    def test_every_report_records_the_catalogue_version_it_used(self):
        _, bundled = run(recording())
        _, drafted = run(recording(), catalogue=draft(version=CATALOGUE["catalogue_version"] + 1))

        self.assertEqual(written(bundled)["catalogue_version"], CATALOGUE["catalogue_version"])
        self.assertEqual(written(drafted)["catalogue_version"], CATALOGUE["catalogue_version"] + 1)
        self.assertEqual(errors(SCHEMA, written(drafted)), [])

    def test_a_passing_check_works_and_says_so_in_plain_words(self):
        status, mac = run(recording())

        self.assertEqual(status, 0)
        self.assertEqual(mac.written[REPORT_FILE], golden("m2-max-image2"))
        self.assertEqual(written(mac)["checks"][0]["classification"], {
            "outcome": "works", "feature": "devicetree", "layer": "asahi",
            "expected": {"asahi": "upstream", "aurora": "asahi"},
        })
        self.assertIn("PASS  system.identity  works (Device tree)", mac.output)


class FailureClassificationTest(unittest.TestCase):
    """A failed check is matched against Aurora's expected state first, then Asahi's."""

    CASES = [
        # (why, recording, draft catalogue or None for the bundled one, outcome, words shown)
        ("Aurora tracks linux-asahi, which supports it", recording(kernel_fails=True), None,
         "fails", "doesn't work, but should on this Mac"),
        ("Aurora supports it even though Asahi doesn't: Aurora wins", recording(kernel_fails=True),
         draft(devicetree_on_m2_max(asahi={"status": "wip", "cell": "WIP"}, aurora={"status": "supported", "version": "7.1.12"})),
         "fails", "doesn't work, but should on this Mac"),
        ("Asahi supports it, Aurora's state is unknown", recording(kernel_fails=True),
         draft(devicetree_on_m2_max(aurora={"status": "unknown"})),
         "not-in-aurora", "not yet supported by Aurora"),
        ("Asahi supports it, Aurora doesn't", recording(kernel_fails=True),
         draft(devicetree_on_m2_max(aurora={"status": "unsupported"})),
         "not-in-aurora", "not yet supported by Aurora"),
        ("an Aurora addition Aurora doesn't have on this chip yet", recording(kernel_fails=True),
         draft(identity_checks("vrr", aurora={"status": "unknown"})),
         "not-in-aurora", "not yet supported by Aurora"),
        ("an Aurora addition whose Asahi counterpart is WIP is still Aurora's to add", recording(kernel_fails=True),
         draft(identity_checks("usb4-displays", aurora={"status": "unknown"})),
         "not-in-aurora", "not yet supported by Aurora"),
        ("Asahi is still working on it", recording(kernel_fails=True),
         draft(devicetree_on_m2_max(asahi={"status": "wip", "cell": "WIP"})),
         "not-in-asahi", "not yet supported by Asahi"),
        ("an M4 Pro, where Asahi's device tree is WIP", recording(kernel_fails=True, soc="t6040"), None,
         "not-in-asahi", "not yet supported by Asahi"),
        ("a MacBook Neo (A18 Pro), where Asahi has the device tree and Aurora's state is unknown",
         recording(kernel_fails=True, soc="t8140"), None,
         "not-in-aurora", "not yet supported by Aurora"),
        ("an M3, where Asahi has it and Aurora's state is unknown", recording(kernel_fails=True, soc="t8122"), None,
         "not-in-aurora", "not yet supported by Aurora"),
        ("the kernel has it, Omarchy's integration doesn't", recording(kernel_fails=True),
         draft(identity_checks("notch-bar", omarchy={"status": "unsupported"})),
         "not-in-omarchy", "not yet supported by Omarchy"),
        ("this board has no such hardware", recording(kernel_fails=True),
         draft(lambda c: feature(c, "devicetree").setdefault("models", {}).update({"j416c": {"asahi": {"status": "absent", "cell": "-"}}})),
         "not-applicable", "this Mac doesn't have this hardware"),
        ("an M5, which no Asahi table covers yet", recording(kernel_fails=True, soc="t8142"), None,
         "not-in-asahi", "not yet supported by Asahi"),
        ("a chip the catalogue doesn't know", recording(soc="t9999"), None,
         "unknown-hardware", "unknown hardware"),
    ]

    def test_each_failure_outcome(self):
        for why, rec, catalogue, outcome, words in self.CASES:
            with self.subTest(why):
                status, mac = run(rec, catalogue=catalogue)

                report = written(mac)
                self.assertEqual(status, 0)
                self.assertEqual(errors(SCHEMA, report), [])
                self.assertEqual(report["checks"][0]["status"], "fail")
                self.assertEqual(report["checks"][0]["classification"]["outcome"], outcome)
                # A failure the catalogue expects reads GAP (not yet supported) or N/A, not FAIL.
                label = {"not-applicable": "N/A "}.get(outcome, "GAP " if outcome.startswith("not-in-") else "FAIL")
                self.assertIn(f"{label}  system.identity  {words} (", mac.output)

    def test_the_report_names_the_layer_and_the_expected_states(self):
        _, mac = run(recording(kernel_fails=True), catalogue=draft(identity_checks("notch-bar")))

        self.assertEqual(written(mac)["checks"][0]["classification"], {
            "outcome": "fails", "feature": "notch-bar", "layer": "omarchy",
            "expected": {"asahi": "linux-asahi", "aurora": "asahi", "omarchy": "supported"},
        })

    def test_an_m5_names_its_chip(self):
        _, mac = run(recording(soc="t8142"))

        self.assertEqual(written(mac)["machine"]["chip"], "M5")

    def test_missing_temperatures_on_a_mac_asahi_supports_are_a_failure_that_can_regress(self):
        found = Catalogue(CATALOGUE).classify({"id": "thermal.sensors", "status": "fail"}, "t6021", "j416c")

        self.assertEqual(found["outcome"], "fails")

    def test_an_unknown_chip_is_explained_without_expected_states(self):
        _, mac = run(recording(soc="t9999"))

        self.assertEqual(written(mac)["checks"][0]["classification"]["expected"], {})


def saved_report(checks: list[dict], catalogue_version=1) -> dict:
    report = json.loads(golden("m2-max-image2"))
    report["catalogue_version"] = catalogue_version
    report["checks"] = [{"kind": "human", "evidence": [], **c} for c in checks]
    return report


def with_human_checks(catalogue):
    catalogue["checks"].update({"display.notch-bar": "notch-bar", "audio.speakers-heard": "speaker-protection"})


class MacWithoutTheHardwareTest(unittest.TestCase):
    catalogue = Catalogue(CATALOGUE)

    def outcome(self, check_id: str, status: str, soc: str, board: str) -> str:
        return self.catalogue.classify({"id": check_id, "status": status}, soc, board)["outcome"]

    def test_on_the_macbook_neo_thunderbolt_and_the_notch_are_not_applicable_whether_failed_or_skipped(self):
        for check_id in ("ports.thunderbolt", "sleep.thunderbolt-after-resume", "display.notch-strip", "display.notch-bar"):
            for status in ("fail", "skip"):
                with self.subTest(check=check_id, status=status):
                    self.assertEqual(self.outcome(check_id, status, "t8140", "j700"), "not-applicable")

    def test_a_skip_where_the_mac_has_the_hardware_is_not_tested(self):
        self.assertEqual(self.outcome("ports.thunderbolt", "skip", "t6021", "j416c"), "not-tested")
        self.assertEqual(self.outcome("ports.usb-c", "skip", "t8140", "j700"), "not-tested")

    def test_a_skip_on_hardware_the_mac_lacks_reads_n_a(self):
        self.assertEqual(status_label("skip", "not-applicable"), "N/A")
        self.assertEqual(status_label("fail", "not-applicable"), "N/A")
        self.assertEqual(status_label("skip", "not-tested"), "SKIP")


class ExplainTest(unittest.TestCase):
    def explain(self, report: dict, catalogue: dict | None = None) -> tuple[int, RecordedHost]:
        rec = recording(files={SAVED: {"text": json.dumps(report)}})
        rec["commands"] = []  # explaining runs nothing on the machine
        return run(rec, argv=["--explain", SAVED], catalogue=catalogue)

    def test_a_skipped_human_check_is_not_tested_never_a_failure(self):
        report = saved_report([
            {"id": "display.notch-bar", "status": "skip"},
            {"id": "audio.speakers-heard", "status": "skip"},
        ])

        status, mac = self.explain(report, draft(with_human_checks))

        self.assertEqual(status, 0)
        self.assertIn("SKIP  display.notch-bar  not tested (Bar clear of the notch)", mac.output)
        self.assertIn("SKIP  audio.speakers-heard  not tested (Speaker protection)", mac.output)
        self.assertNotIn("doesn't work", mac.output)

    def test_a_human_no_is_a_failure_and_a_yes_works(self):
        report = saved_report([
            {"id": "display.notch-bar", "status": "fail"},
            {"id": "audio.speakers-heard", "status": "pass"},
        ])

        _, mac = self.explain(report, draft(with_human_checks))

        self.assertIn("FAIL  display.notch-bar  doesn't work, but should on this Mac", mac.output)
        self.assertIn("PASS  audio.speakers-heard  works", mac.output)

    def test_explain_uses_the_current_catalogue_and_says_which_the_report_used(self):
        report = saved_report([{"id": "system.identity", "status": "fail"}], catalogue_version=1)

        _, mac = self.explain(report, draft(devicetree_on_m2_max(aurora={"status": "unsupported"}), version=2))

        self.assertIn("explained with feature catalogue v2 (the report was made with v1)", mac.output)
        self.assertIn("GAP   system.identity  not yet supported by Aurora", mac.output)

    def test_explain_runs_no_checks_asks_nothing_and_writes_nothing(self):
        status, mac = self.explain(saved_report([{"id": "made.up", "status": "fail"}]))

        self.assertEqual(status, 0)
        self.assertIn(f"made.up  not in feature catalogue v{CATALOGUE['catalogue_version']}", mac.output)
        self.assertEqual(mac.commands_run, [])
        self.assertEqual([e for e in mac.transcript if e[0] == "prompt"], [])
        self.assertEqual(mac.written, {})

    def test_something_that_isnt_a_report_is_refused(self):
        rec = recording(files={SAVED: {"text": "{\"hello\": 1}"}})
        status, mac = run(rec, argv=["--explain", SAVED])

        self.assertEqual(status, 4)
        self.assertIn("isn't an omarchy-m-test report", mac.output)


class BrokenCatalogueTest(unittest.TestCase):
    def test_a_broken_catalogue_stops_the_run_before_anything_happens(self):
        broken = [
            draft(lambda c: c["checks"].update({"system.identity": "no-such-feature"})),
            draft(lambda c: feature(c, "devicetree")["chips"]["m1"].update(asahi={"status": "works-great"})),
            draft(lambda c: c.pop("catalogue_version")),
            draft(lambda c: c["boards"]["j416c"].update(soc="t8103")),
            draft(lambda c: c["boards"]["j416c"]["hardware"].update(fans="yes")),
            draft(lambda c: c["boards"]["j416c"]["hardware"].pop("lid")),
            draft(lambda c: c["boards"]["j416c"].update(lineup=c["boards"]["j274"]["lineup"])),
        ]
        for catalogue in broken:
            with self.subTest(catalogue=catalogue.get("checks")):
                status, mac = run(recording(), catalogue=catalogue)

                self.assertEqual(status, 4)
                self.assertIn("can't be used", mac.output)
                self.assertEqual([e for e in mac.transcript if e[0] == "prompt"], [])
                self.assertEqual(mac.written, {})


if __name__ == "__main__":
    unittest.main()
