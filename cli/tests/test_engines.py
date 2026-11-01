"""The Neural Engine's status and benchmark, and hardware H.264 encode."""

from __future__ import annotations

import json
import unittest

from omarchy_m_test import bundled, catalogue, engines, hwread, macos_reference
from omarchy_m_test.host import HARDWARE_SNAPSHOT, CommandResult
from omarchy_m_test.machine import Machine
from omarchy_m_test.recording import RecordedHost
from omarchy_m_test.session import Changes, Context
from tests.test_core_checks import M2_MAX, results, run
from tests.test_seam_a import SCHEMA
from tests.schema_validator import errors
from tests.test_thermal import FakeSysfs

DEVICE = {"driver": "ane_t6021", "generation": "H14", "firmware": "1.2", "firmware_source": "driver", "power": "suspended", "wedged": 0, "errors": {}, "node_present": True}
GOOD = {"observed": True, "ane": [DEVICE], "ane_node": True, "encoder": "apple-ave", "ave_node": True}
SMOKE_OUT = json.dumps({"name": "add", "chip": "t6021", "die": 0, "available": True, "errors": 0, "min_ms": 0.4, "median_ms": 0.5})


def ctx(found: dict = GOOD, smoke: dict | None = None, encode: dict | None = None, board: str = "j416c") -> Context:
    commands = [{"argv": [HARDWARE_SNAPSHOT, "engines"], "returncode": 0, "stdout": json.dumps(found), "stderr": ""}]
    if smoke:
        commands.append({"argv": engines.SMOKE, "stderr": "", **smoke})
    if encode:
        commands.append({"argv": engines.ENCODE_ARGV, "stdout": "", **encode})
    mac = RecordedHost({"recording_version": 1, "commands": commands})
    machine = Machine("Apple Mac", board, "t6021", "M2 Max", "aarch64", "7.1.12", (f"apple,{board}", "apple,t6021"))
    return Context(mac, machine, catalogue.parse(bundled.catalogue_text()), Changes(mac))


def fake_sysfs(**extra) -> FakeSysfs:
    path = "/sys/class/accel/accel0/device"
    return FakeSysfs({
        f"{path}/uevent": "DRIVER=ane_t6021\nOF_NAME=ane\n", f"{path}/of_node/compatible": "apple,t6021-ane\0apple,ane\0",
        f"{path}/firmware_version": "1.2\n", f"{path}/power/runtime_status": "suspended\n", f"{path}/wedged": "0\n",
        f"{path}/ane_stats": "submissions 40\nerrors: 2\ntimeouts=0\n", "/sys/class/video4linux/video3/name": "apple-ave\n",
        "/sys/class/video4linux/video0/name": "apple-isp\n", **extra,
    }, {"/sys/class/accel": ["accel0"], "/dev/accel": ["accel0"], "/sys/class/video4linux": ["video0", "video3"],
        "/sys/firmware/devicetree/base/soc": ["ane@26a000000", "ave@23e000000", "soc-thing"]})


class CollectTest(unittest.TestCase):
    def test_it_reads_the_engine_without_starting_it(self):
        host = fake_sysfs()

        found = hwread.collect(host, "engines")

        self.assertEqual(found["ane"][0]["driver"], "ane_t6021")
        self.assertEqual(found["ane"][0]["generation"], "H14")
        self.assertEqual((found["ane"][0]["firmware"], found["ane"][0]["firmware_source"]), ("1.2", "driver"))
        self.assertEqual(found["ane"][0]["errors"], {"errors": 2, "timeouts": 0})
        self.assertEqual((found["ane_node"], found["ave_node"], found["encoder"]), (True, True, "apple-ave"))
        self.assertFalse(hasattr(host, "run"))  # nothing here can run a command

    def test_firmware_from_the_device_tree_says_so(self):
        path = "/sys/class/accel/accel0/device"
        host = fake_sysfs(**{f"{path}/firmware_version": "", f"{path}/of_node/apple,firmware-version": "9.9\n"})

        self.assertIn("device tree", hwread.collect(host, "engines")["ane"][0]["firmware_source"])

    def test_another_accelerator_isnt_taken_for_the_ane(self):
        path = "/sys/class/accel/accel0/device"
        host = fake_sysfs(**{f"{path}/uevent": "DRIVER=habanalabs\n", f"{path}/of_node/compatible": "other,gaudi\0"})
        host.run = lambda argv, **_: CommandResult(1, "", "")
        found = hwread.collect(host, "engines")

        self.assertEqual(found["ane"], [])

    def test_an_unknown_chip_isnt_guessed(self):
        path = "/sys/class/accel/accel0/device"
        found = hwread.collect(fake_sysfs(**{f"{path}/of_node/compatible": "apple,t9999-ane\0"}), "engines")

        self.assertIsNone(found["ane"][0]["generation"])

    def test_nothing_exposed_is_empty(self):
        self.assertEqual(hwread.collect(FakeSysfs({}, {}), "engines"), {"observed": True, "ane": [], "ane_node": False, "encoder": None, "ave_node": False})


class StatusTest(unittest.TestCase):
    def status(self, **device) -> dict:
        return engines._status({**GOOD, "ane": [{**DEVICE, **device}]})

    def test_a_healthy_engine_passes(self):
        found = self.status()

        self.assertEqual(found["status"], "pass")
        self.assertIn("driver ane_t6021, generation H14, firmware 1.2 (driver), power suspended", found["evidence"][0])

    def test_runtime_suspend_is_normal(self):
        self.assertEqual(self.status(power="suspended")["status"], "pass")

    def test_wedged_fails(self):
        self.assertEqual(self.status(wedged=1)["status"], "fail")

    def test_a_power_error_fails(self):
        self.assertEqual(self.status(power="error")["status"], "fail")

    def test_no_dev_node_fails(self):
        self.assertEqual(self.status(node_present=False)["status"], "fail")

    def test_cumulative_errors_are_said_but_dont_fail(self):
        found = self.status(errors={"errors": 2, "timeouts": 0})

        self.assertEqual(found["status"], "pass")
        self.assertTrue(any("errors 2" in line and "timeouts" not in line for line in found["evidence"]))

    def test_a_node_with_no_device_fails(self):
        found = engines._status({**GOOD, "ane": []})

        self.assertEqual(found["status"], "fail")
        self.assertIn("no accelerator device", found["evidence"][0])

    def test_a_failed_probe_says_why_there_is_no_device(self):
        probe = {"driver": "ane_t6021", "error": -2, "missing_firmware": "apple/ane/t602x_ane0_fw_selene_rc4x.macho"}
        found = engines._status({**GOOD, "ane": [], "ane_probe": probe})

        self.assertEqual(found["status"], "fail")
        self.assertIn("driver ane_t6021 failed to probe, error -2", found["evidence"])
        self.assertIn("its firmware isn't installed: apple/ane/t602x_ane0_fw_selene_rc4x.macho", found["evidence"])

    def test_the_probe_reason_keeps_only_the_driver_errno_and_firmware_file(self):
        class Log:
            stdout = ("Nov 01 host kernel: ane_t6021 284000000.ane: Direct firmware load for apple/ane/t602x_ane0_fw_selene_rc4x.macho failed with error -2\n"
                      "Nov 01 host kernel: ane_t6021 284000000.ane: probe with driver ane_t6021 failed with error -2\n")
        class Host:
            def run(self, argv, **_):
                return Log

        self.assertEqual(hwread._ane_probe(Host()), {"driver": "ane_t6021", "error": -2, "missing_firmware": "apple/ane/t602x_ane0_fw_selene_rc4x.macho"})

    def test_no_node_on_a_mac_the_catalogue_knows_fails(self):
        found = engines._status({**GOOD, "ane": [], "ane_node": False})

        self.assertEqual(found["status"], "fail")
        self.assertIn("though this Mac has a Neural Engine", found["evidence"][0])

    def test_no_node_on_an_unknown_board_skips(self):
        self.assertEqual(engines._status({**GOOD, "ane": [], "ane_node": False}, known_board=False)["status"], "skip")

    def test_a_missing_engine_on_a_known_mac_is_classified_not_hidden(self):
        found = engines.run(ctx({**GOOD, "ane": [], "ane_node": False, "encoder": None}))[0]
        outcome = catalogue.parse(bundled.catalogue_text()).classify(found, "t6021", "j416c")["outcome"]

        self.assertNotEqual(outcome, "not-tested")


class BenchmarkTest(unittest.TestCase):
    def bench(self, smoke: dict, found: dict = GOOD, board: str = "j416c") -> dict:
        return engines._benchmark(ctx(found, smoke, board=board), found)

    def test_a_clean_run_scores_runs_per_second(self):
        found = self.bench({"returncode": 0, "stdout": SMOKE_OUT})

        self.assertEqual(found["status"], "pass")
        self.assertEqual(found["score"], {"value": 2000, "unit": "ops", "tool": "omarchy-ane-smoke", "suite": engines.SUITE})
        self.assertEqual(errors(SCHEMA["properties"]["checks"]["items"]["properties"]["score"], found["score"]), [])

    def test_not_installed_skips_and_nothing_is_installed(self):
        host_ctx = ctx(GOOD, {"returncode": 127, "stdout": ""})

        found = engines._benchmark(host_ctx, GOOD)

        self.assertEqual(found["status"], "skip")
        self.assertFalse([argv for argv in host_ctx.host.commands_run if argv[0] == "pacman"])

    def test_no_device_doesnt_run_it(self):
        host_ctx = ctx({**GOOD, "ane": []})

        self.assertEqual(engines._benchmark(host_ctx, {**GOOD, "ane": []})["status"], "skip")
        self.assertEqual(host_ctx.host.commands_run, [])

    def test_errors_fail(self):
        self.assertEqual(self.bench({"returncode": 0, "stdout": json.dumps({**json.loads(SMOKE_OUT), "errors": 3})})["status"], "fail")

    def test_a_nonzero_exit_fails(self):
        self.assertEqual(self.bench({"returncode": 1, "stdout": SMOKE_OUT})["status"], "fail")

    def test_garbage_fails(self):
        self.assertEqual(self.bench({"returncode": 139, "stdout": "Segmentation fault"})["status"], "fail")

    def test_no_median_fails(self):
        out = json.loads(SMOKE_OUT)
        del out["median_ms"]

        self.assertEqual(self.bench({"returncode": 0, "stdout": json.dumps(out)})["status"], "fail")

    def test_it_says_when_it_cant_run(self):
        self.assertEqual(self.bench({"returncode": 0, "stdout": json.dumps({"available": False})})["status"], "skip")

    def test_a_different_workload_isnt_compared_with_macos(self):
        found = self.bench({"returncode": 0, "stdout": SMOKE_OUT}, board="j616s")

        self.assertTrue(any("a different workload" in line for line in found["evidence"]))

    def test_the_same_model_is_compared_with_macos(self):
        out = {"model": "mobilenetv1-1.0-224-random", "batch": 1, "p50_ms": 0.4, "errors": 0}

        found = self.bench({"returncode": 0, "stdout": json.dumps(out)}, board="j616s")

        self.assertTrue(any("0.4 ms here, 0.302 ms on macOS" in line for line in found["evidence"]))

    def test_a_name_that_isnt_plain_is_dropped(self):
        out = {**json.loads(SMOKE_OUT), "name": "/home/tester/x"}

        self.assertNotIn("/home", json.dumps(self.bench({"returncode": 0, "stdout": json.dumps(out)})))


class EncodeTest(unittest.TestCase):
    def encode(self, result: dict | None, found: dict = GOOD) -> dict:
        return engines._encode(ctx(found, encode=result), found)

    def test_it_scores_frames_per_second(self):
        found = self.encode({"returncode": 0, "stderr": "frame=  150 fps=0 bench: utime=0.1s stime=0.1s rtime=0.500s\n"})

        self.assertEqual(found["status"], "pass")
        self.assertEqual(found["score"]["value"], 300)

    def test_no_device_skips_and_doesnt_run_ffmpeg(self):
        found = self.encode(None, {**GOOD, "encoder": None})

        self.assertEqual(found["status"], "skip")
        self.assertIn("no V4L2 encoder device", found["evidence"][0])

    def test_no_node_says_so(self):
        self.assertIn("no video encoder node", self.encode(None, {**GOOD, "encoder": None, "ave_node": False})["evidence"][0])

    def test_an_ffmpeg_without_the_encoder_skips(self):
        self.assertEqual(self.encode({"returncode": 1, "stderr": "Unknown encoder 'h264_v4l2m2m'\n"})["status"], "skip")

    def test_ffmpeg_missing_skips(self):
        self.assertEqual(self.encode({"returncode": 127, "stderr": ""})["status"], "skip")

    def test_a_device_that_fails_to_encode_fails(self):
        self.assertEqual(self.encode({"returncode": 1, "stderr": "Could not open encoder\n"})["status"], "fail")


class HistoricalTest(unittest.TestCase):
    def test_a_run_that_predates_them_skips(self):
        _, _, report = run(M2_MAX)

        for check_id in engines.CHECK_IDS:
            self.assertIn("not read in this run", results(report)[check_id]["evidence"][0])

    def test_macos_reference_module_has_the_j616s_ane(self):
        self.assertEqual(macos_reference.load("j616s")["metrics"]["ane"]["batch"], 1)


if __name__ == "__main__":
    unittest.main()
