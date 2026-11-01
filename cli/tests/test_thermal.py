"""The Thermal section: what the kernel exposes, what crosses the host boundary, and the verdicts."""

from __future__ import annotations

import copy
import json
import unittest

from omarchy_m_test import hwread
from omarchy_m_test.host import HARDWARE_SNAPSHOT
from omarchy_m_test.recording import RecordedHost, RecordingHost
from tests.test_core_checks import M1_PRO, M2_MAX, answer, results, run
from tests.test_seam_a import SCHEMA
from tests.schema_validator import errors

GOOD = {
    "observed": True,
    "hwmon": [{"name": "macsmc_hwmon", "unreadable": 1,
               "temps": [{"label": "CPU core 1", "c": 48.5}, {"label": "Palm rest", "c": 30.0}],
               "fans": [{"label": "Fan 1", "rpm": 2317, "target": 0, "min": 1200, "max": 5700}]}],
    "zones": [{"type": "cpu-thermal", "c": 49.0, "policy": "step_wise", "trips": [{"type": "critical", "c": 105.0}]}],
    "cooling": [{"type": "cpufreq-p", "state": 0, "max": 15}],
}


class FakeSysfs:
    def __init__(self, files: dict[str, str], dirs: dict[str, list[str]]):
        self.files, self.dirs, self.reads = files, dirs, []

    def read_file(self, path: str) -> bytes:
        self.reads.append(path)
        if path not in self.files:
            raise FileNotFoundError(path)
        value = self.files[path]
        if isinstance(value, OSError):
            raise value
        return value.encode()

    def list_dir(self, path: str) -> list[str]:
        if path not in self.dirs:
            raise FileNotFoundError(path)
        return self.dirs[path]


def sysfs() -> FakeSysfs:
    hwmon = "/sys/class/hwmon/hwmon0"
    zone = "/sys/class/thermal/thermal_zone0"
    cooling = "/sys/class/thermal/cooling_device0"
    return FakeSysfs({
        f"{hwmon}/name": "macsmc_hwmon\n",
        f"{hwmon}/temp1_input": "48500\n", f"{hwmon}/temp1_label": "CPU core 1\n",
        f"{hwmon}/temp2_input": OSError("no data"),
        f"{hwmon}/temp3_input": "30000\n", f"{hwmon}/temp3_label": "owner's <b>laptop</b>\n",
        f"{hwmon}/fan1_input": "2317\n", f"{hwmon}/fan1_label": "Fan 1\n", f"{hwmon}/fan1_target": "0\n",
        f"{hwmon}/fan1_min": "1200\n", f"{hwmon}/fan1_max": "5700\n",
        f"{zone}/type": "cpu-thermal\n", f"{zone}/temp": "49000\n", f"{zone}/policy": "step_wise\n",
        f"{zone}/trip_point_0_temp": "105000\n", f"{zone}/trip_point_0_type": "critical\n",
        f"{cooling}/type": "cpufreq-p\n", f"{cooling}/cur_state": "0\n", f"{cooling}/max_state": "15\n",
    }, {
        "/sys/class/hwmon": ["hwmon0", "not-hwmon"],
        hwmon: ["name", "temp1_input", "temp1_label", "temp2_input", "temp3_input", "temp3_label", "fan1_input", "fan1_label"],
        "/sys/class/thermal": ["thermal_zone0", "cooling_device0"],
        zone: ["type", "temp", "policy", "trip_point_0_temp", "trip_point_0_type"],
    })


def snapshot(rec: dict, found: dict) -> dict:
    rec = copy.deepcopy(rec)
    answer(rec, [HARDWARE_SNAPSHOT, "thermal"], stdout=json.dumps(found))
    return rec


class CollectTest(unittest.TestCase):
    def test_it_reads_temperatures_fans_zones_and_cooling(self):
        found = hwread.collect(sysfs(), "thermal")

        self.assertTrue(found["observed"])
        device = found["hwmon"][0]
        self.assertEqual(device["name"], "macsmc_hwmon")
        self.assertEqual(device["fans"], [{"label": "Fan 1", "rpm": 2317, "target": 0, "min": 1200, "max": 5700}])
        self.assertEqual(found["zones"], [{"type": "cpu-thermal", "c": 49.0, "policy": "step_wise", "trips": [{"type": "critical", "c": 105.0}]}])
        self.assertEqual(found["cooling"], [{"type": "cpufreq-p", "state": 0, "max": 15}])

    def test_a_channel_that_wont_read_is_counted_not_fatal(self):
        device = hwread.collect(sysfs(), "thermal")["hwmon"][0]

        self.assertEqual(device["unreadable"], 1)
        self.assertEqual([t["c"] for t in device["temps"]], [48.5, 30.0])

    def test_a_label_that_isnt_a_plain_name_doesnt_cross_the_boundary(self):
        device = hwread.collect(sysfs(), "thermal")["hwmon"][0]

        self.assertEqual([t["label"] for t in device["temps"]], ["CPU core 1", "temp3"])
        self.assertNotIn("owner", json.dumps(device))

    def test_a_mac_with_nothing_exposed_is_empty_not_an_error(self):
        self.assertEqual(hwread.collect(FakeSysfs({}, {}), "thermal"), {"observed": True, "hwmon": [], "zones": [], "cooling": []})


class VerdictTest(unittest.TestCase):
    def check(self, rec: dict, check_id: str) -> dict:
        _, _, report = run(rec)
        self.assertEqual(errors(SCHEMA, report), [])
        return results(report)[check_id]

    def test_temperatures_are_evidence_and_pass(self):
        check = self.check(snapshot(M2_MAX, GOOD), "thermal.sensors")

        self.assertEqual(check["status"], "pass")
        self.assertIn("hottest: CPU core 1 (macsmc_hwmon) 48.5 °C", check["evidence"])
        self.assertIn("zone cpu-thermal: 49.0 °C, governor step_wise, critical 105.0 °C", check["evidence"])
        self.assertIn("cooling cpufreq-p: state 0 of 15", check["evidence"])
        self.assertIn("1 hwmon temperature channels don't read", check["evidence"])

    def test_a_hot_mac_still_passes(self):
        hot = copy.deepcopy(GOOD)
        hot["hwmon"][0]["temps"][0]["c"] = 109.0

        self.assertEqual(self.check(snapshot(M2_MAX, hot), "thermal.sensors")["status"], "pass")

    def test_a_zone_alone_is_enough(self):
        only_zone = {**GOOD, "hwmon": []}

        self.assertEqual(self.check(snapshot(M2_MAX, only_zone), "thermal.sensors")["status"], "pass")

    def test_no_temperature_fails_and_says_why(self):
        check = self.check(snapshot(M2_MAX, {"observed": True, "hwmon": [{"name": "nvme", "temps": [], "fans": [], "unreadable": 2}], "zones": [], "cooling": []}), "thermal.sensors")

        self.assertEqual(check["status"], "fail")
        self.assertIn("hwmon devices: nvme", check["evidence"][0])

    def test_an_ssds_temperature_alone_isnt_the_macs_sensors(self):
        nvme_only = {"observed": True, "hwmon": [{"name": "nvme", "temps": [{"label": "Composite", "c": 34.0}], "fans": [], "unreadable": 0}], "zones": [], "cooling": []}

        check = self.check(snapshot(M2_MAX, nvme_only), "thermal.sensors")

        self.assertEqual(check["status"], "fail")
        self.assertIn("only other parts' own: 1 readings", check["evidence"][0])

    def test_a_usb_fan_controller_isnt_the_macs_fan(self):
        usb = {**GOOD, "hwmon": [{**GOOD["hwmon"][0], "fans": []}, {"name": "corsaircpro", "temps": [], "fans": GOOD["hwmon"][0]["fans"], "unreadable": 0}]}

        check = self.check(snapshot(M2_MAX, usb), "thermal.fans")

        self.assertEqual(check["status"], "fail")
        self.assertIn("1 fans on other devices aren't the Mac's own and aren't looked at", check["evidence"])

    def test_a_fanless_mac_with_a_usb_fan_still_skips(self):
        usb = {**GOOD, "hwmon": [{**GOOD["hwmon"][0], "fans": []}, {"name": "corsaircpro", "temps": [], "fans": GOOD["hwmon"][0]["fans"], "unreadable": 0}]}

        check = self.check(with_board(snapshot(M2_MAX, usb), "j413"), "thermal.fans")

        self.assertEqual(check["status"], "skip")
        self.assertIn("this Mac has no fans", check["evidence"])

    def test_fans_are_read_on_a_mac_with_fans(self):
        check = self.check(snapshot(M2_MAX, GOOD), "thermal.fans")

        self.assertEqual(check["status"], "pass")
        self.assertIn("Fan 1: 2317 rpm, target 0, limits 1200 to 5700", check["evidence"])

    def test_a_mac_with_fans_that_shows_none_fails(self):
        none = copy.deepcopy(GOOD)
        none["hwmon"][0]["fans"] = []

        check = self.check(snapshot(M2_MAX, none), "thermal.fans")

        self.assertEqual(check["status"], "fail")
        self.assertIn("this Mac has fans", check["evidence"])

    def test_a_fanless_mac_skips(self):
        rec = snapshot(M2_MAX, {**GOOD, "hwmon": [{**GOOD["hwmon"][0], "fans": []}]})
        fanless = with_board(rec, "j413")

        check = self.check(fanless, "thermal.fans")

        self.assertEqual((check["status"], check["evidence"]), ("skip", ["this Mac has no fans"]))

    def test_a_run_that_predates_the_section_skips_both(self):
        _, _, report = run(M2_MAX)
        found = results(report)

        for check_id in ("thermal.sensors", "thermal.fans"):
            self.assertEqual(found[check_id]["status"], "skip")
            self.assertIn("not read in this run", found[check_id]["evidence"][0])

    def test_the_m1_pro_recording_runs_it_too(self):
        self.assertEqual(self.check(snapshot(M1_PRO, GOOD), "thermal.sensors")["status"], "pass")


class RecordingTest(unittest.TestCase):
    def test_a_recording_keeps_only_the_snapshot_and_replays_it(self):
        class Inner:
            def hardware_snapshot(self, area):
                return {"observed": True, "area": area}

        recorder = RecordingHost.__new__(RecordingHost)
        kept = []
        recorder.inner = Inner()
        recorder._keep = lambda argv, result: kept.append((argv, result.stdout))

        self.assertEqual(recorder.hardware_snapshot("thermal"), {"observed": True, "area": "thermal"})
        self.assertEqual(kept[0][0], [HARDWARE_SNAPSHOT, "thermal"])
        replay = RecordedHost({"recording_version": 1, "commands": [{"argv": kept[0][0], "returncode": 0, "stdout": kept[0][1], "stderr": ""}]})
        self.assertEqual(replay.hardware_snapshot("thermal"), {"observed": True, "area": "thermal"})


def with_board(rec: dict, board: str) -> dict:
    rec = copy.deepcopy(rec)
    text = rec["files"]["/proc/device-tree/compatible"]["text"]
    first = text.split("\0")[0]
    rec["files"]["/proc/device-tree/compatible"]["text"] = text.replace(first, f"apple,{board}", 1)
    return rec


if __name__ == "__main__":
    unittest.main()
