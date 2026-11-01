"""The Sensors section: IIO discovery, plausibility limits and the lid-angle observation."""

from __future__ import annotations

import copy
import json
import math
import unittest

from omarchy_m_test import bundled, catalogue, hwread, sensors, sleep
from omarchy_m_test.host import HARDWARE_SNAPSHOT
from omarchy_m_test.machine import Machine
from omarchy_m_test.recording import RecordedHost
from omarchy_m_test.session import Changes, Context
from tests.schema_validator import errors
from tests.test_core_checks import M2_MAX, answer, results, run
from tests.test_seam_a import SCHEMA
from tests.test_thermal import FakeSysfs, with_board

ACCEL = {"name": "aop-sensors-accel", "accel": {"x": 0.1, "y": 0.2, "z": 9.79}, "accel_unreadable": 0}
GYRO = {"name": "aop-sensors-gyro", "gyro": {"x": 0.0, "y": 0.01, "z": 0.0}, "gyro_unreadable": 0}
LAS = {"name": "aop-sensors-las", "angle": 112.5}
ALS = {"name": "aop-sensors-als", "lux": 321.0, "colour_temperature": 5100.0}
GOOD = {"observed": True, "iio": [ACCEL, GYRO, LAS, ALS], "aop_node": True}


def snapshot(found: dict, rec: dict = M2_MAX) -> dict:
    rec = copy.deepcopy(rec)
    answer(rec, [HARDWARE_SNAPSHOT, "sensors"], stdout=json.dumps(found))
    return rec


def check(rec: dict, check_id: str) -> dict:
    _, _, report = run(rec)
    assert errors(SCHEMA, report) == []
    return results(report)[check_id]


def iio(files: dict[str, str]) -> FakeSysfs:
    base = "/sys/bus/iio/devices/iio:device0"
    entries = sorted(files)
    return FakeSysfs({f"{base}/{name}": value for name, value in files.items()},
                     {"/sys/bus/iio/devices": ["iio:device0", "trigger0"], base: entries, "/sys/firmware/devicetree/base/soc": ["aop@2a6c00000"]})


class CollectTest(unittest.TestCase):
    def test_raw_channels_are_scaled(self):
        found = hwread.collect(iio({"name": "aop-sensors-accel\n", "in_accel_x_raw": "100\n", "in_accel_y_raw": "0\n", "in_accel_z_raw": "1000\n", "in_accel_scale": "0.0098\n"}), "sensors")

        self.assertEqual(found["iio"][0]["accel"], {"x": 0.98, "y": 0.0, "z": 9.8})
        self.assertTrue(found["aop_node"])

    def test_a_channel_that_wont_read_is_counted(self):
        found = hwread.collect(iio({"name": "aop-sensors-gyro\n", "in_anglvel_x_raw": OSError("no data"), "in_anglvel_y_raw": "2\n"}), "sensors")

        self.assertEqual(found["iio"][0]["gyro"], {"y": 2.0})
        self.assertEqual(found["iio"][0]["gyro_unreadable"], 1)

    def test_a_scaled_angle_is_radians_and_comes_out_in_degrees(self):
        found = hwread.collect(iio({"name": "aop-sensors-las\n", "in_angl_raw": "2\n", "in_angl_scale": "1\n"}), "sensors")

        self.assertEqual(found["iio"][0]["angle"], round(math.degrees(2), 1))

    def test_an_unscaled_angle_is_kept_as_it_is(self):
        found = hwread.collect(iio({"in_angl_input": "95\n"}), "sensors")

        self.assertEqual(found["iio"][0]["angle"], 95.0)

    def test_light_and_colour_temperature(self):
        found = hwread.collect(iio({"in_illuminance_input": "321\n", "in_colortemp_input": "5100\n"}), "sensors")

        self.assertEqual((found["iio"][0]["lux"], found["iio"][0]["colour_temperature"]), (321.0, 5100.0))

    def test_a_name_that_isnt_plain_doesnt_cross_the_boundary(self):
        found = hwread.collect(iio({"name": "<script>\n", "in_illuminance_input": "1\n"}), "sensors")

        self.assertEqual(found["iio"][0]["name"], "unnamed")

    def test_no_iio_at_all_is_empty(self):
        self.assertEqual(hwread.collect(FakeSysfs({}, {}), "sensors"), {"observed": True, "iio": [], "aop_node": False})


class MotionTest(unittest.TestCase):
    def test_gravity_and_stillness_pass(self):
        found = check(snapshot(GOOD), sensors.MOTION)

        self.assertEqual(found["status"], "pass")
        self.assertIn("magnitude 9.79", found["evidence"][0])
        self.assertIn("gyroscope aop-sensors-gyro: x 0.0, y 0.01, z 0.0 rad/s", found["evidence"])

    def test_a_reading_that_isnt_gravity_fails(self):
        off = {**ACCEL, "accel": {"x": 0.1, "y": 0.2, "z": 3.0}}

        self.assertEqual(check(snapshot({**GOOD, "iio": [off, GYRO]}), sensors.MOTION)["status"], "fail")

    def test_two_axes_arent_enough(self):
        off = {**ACCEL, "accel": {"y": 0.2, "z": 9.8}}

        self.assertEqual(check(snapshot({**GOOD, "iio": [off, GYRO]}), sensors.MOTION)["status"], "fail")

    def test_a_spinning_gyroscope_fails(self):
        spinning = {**GYRO, "gyro": {"x": 1.0, "y": 0.0, "z": 0.0}}

        self.assertEqual(check(snapshot({**GOOD, "iio": [ACCEL, spinning]}), sensors.MOTION)["status"], "fail")

    def test_channels_that_dont_read_fail_and_say_so(self):
        silent = {"name": "aop-sensors-accel", "accel": {}, "accel_unreadable": 3}

        found = check(snapshot({**GOOD, "iio": [silent]}), sensors.MOTION)

        self.assertEqual(found["status"], "fail")
        self.assertIn("don't read", found["evidence"][0])

    def test_no_accelerometer_on_a_macbook_fails_and_says_why(self):
        found = check(snapshot({**GOOD, "iio": [ALS], "aop_node": False}), sensors.MOTION)

        self.assertEqual(found["status"], "fail")
        self.assertIn("no accelerometer", found["evidence"][0])
        self.assertIn("no always-on processor", found["evidence"][0])

    def test_a_missing_gyroscope_says_why_but_doesnt_fail(self):
        found = check(snapshot({**GOOD, "iio": [ACCEL]}), sensors.MOTION)

        self.assertEqual(found["status"], "pass")
        self.assertTrue(any("no gyroscope" in line for line in found["evidence"]))

    def test_a_desktop_mac_skips(self):
        found = check(with_board(snapshot({**GOOD, "iio": []}), "j274"), sensors.MOTION)

        self.assertEqual((found["status"], found["evidence"]), ("skip", ["a desktop Mac has no motion sensors"]))


class LidAngleTest(unittest.TestCase):
    def lid(self, closing=None, found=None) -> dict:
        machine = Machine("Apple MacBook Pro (16-inch, M2 Max, 2023)", "j416c", "t6021", "M2 Max", "aarch64", "7.1.12", ("apple,j416c", "apple,t6021"))
        mac = RecordedHost({"recording_version": 1, "commands": [{"argv": [HARDWARE_SNAPSHOT, "sensors"], "returncode": 0, "stdout": json.dumps(found or GOOD), "stderr": ""}]})
        ctx = Context(mac, machine, catalogue.parse(bundled.catalogue_text()), Changes(mac))
        if closing:
            ctx.shared[sleep.LID_ANGLE] = closing
        return next(r for r in sensors.run(ctx) if r["id"] == sensors.LID)

    def test_without_a_lid_close_the_reading_is_enough(self):
        found = self.lid()

        self.assertEqual(found["status"], "pass")
        self.assertIn("wasn't seen", found["evidence"][1])

    def test_a_reading_that_fell_as_the_lid_closed_passes(self):
        found = self.lid({"before": 112.0, "lowest": 4.0})

        self.assertEqual(found["status"], "pass")
        self.assertIn("it followed the lid", found["evidence"][1])

    def test_a_reading_that_stayed_put_as_the_lid_closed_fails(self):
        found = self.lid({"before": 112.0, "lowest": 112.0})

        self.assertEqual(found["status"], "fail")
        self.assertIn("didn't follow the lid", found["evidence"][1])

    def test_an_impossible_angle_fails(self):
        self.assertEqual(self.lid(found={**GOOD, "iio": [{**LAS, "angle": 400.0}]})["status"], "fail")

    def test_a_sensor_that_doesnt_read_fails(self):
        self.assertEqual(self.lid(found={**GOOD, "iio": [{**LAS, "angle": None}]})["status"], "fail")

    def test_a_mac_without_a_lid_skips(self):
        found = check(with_board(snapshot({**GOOD, "iio": []}), "j274"), sensors.LID)

        self.assertEqual(found["status"], "skip")

    def test_the_lid_watch_reports_the_angles_it_saw(self):
        lid = sleep.parse_lid("start 1000000000\nangle 0 112\nclosed 300\nangle 310 40\nangle 320 4.5\nangle 330 not-a-number\nopen 900\nend 1500\n")

        self.assertEqual(lid.angles, [112.0, 40.0, 4.5])

    def test_the_lid_watch_only_reads_the_sensor(self):
        self.assertIn("cat /sys/bus/iio/devices/iio:device*/in_angl_raw", sleep.LID_WATCH_SCRIPT)
        self.assertNotIn("> /sys", sleep.LID_WATCH_SCRIPT)


class LightTest(unittest.TestCase):
    def test_light_and_colour_temperature_pass(self):
        found = check(snapshot(GOOD), sensors.LIGHT)

        self.assertEqual(found["evidence"], ["ambient light sensor aop-sensors-als: 321.0 lux, colour temperature 5100.0 K"])

    def test_colour_temperature_isnt_required(self):
        found = check(snapshot({**GOOD, "iio": [{"name": "aop-sensors-als", "lux": 2.0}]}), sensors.LIGHT)

        self.assertEqual(found["status"], "pass")
        self.assertIn("not exposed", found["evidence"][0])

    def test_a_mac_with_a_screen_and_no_sensor_fails(self):
        self.assertEqual(check(snapshot({**GOOD, "iio": []}), sensors.LIGHT)["status"], "fail")

    def test_a_mac_without_a_screen_skips(self):
        found = check(with_board(snapshot({**GOOD, "iio": []}), "j274"), sensors.LIGHT)

        self.assertEqual((found["status"], found["evidence"]), ("skip", ["this Mac has no built-in screen"]))


class HistoricalTest(unittest.TestCase):
    def test_a_run_that_predates_the_section_skips(self):
        _, _, report = run(M2_MAX)

        for check_id in sensors.CHECK_IDS:
            self.assertIn("not read in this run", results(report)[check_id]["evidence"][0])


if __name__ == "__main__":
    unittest.main()
