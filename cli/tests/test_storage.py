"""The Storage section: what crosses the host boundary, the verdicts, the capacity tier and the benchmark."""

from __future__ import annotations

import copy
import json
import unittest

from omarchy_m_test import bundled, catalogue, hwread, privacy, storage
from omarchy_m_test.host import HARDWARE_SNAPSHOT, CommandResult
from omarchy_m_test.machine import Machine
from omarchy_m_test.recording import RecordedHost
from omarchy_m_test.safety import refusal
from omarchy_m_test.session import Changes, Context
from tests.schema_validator import errors
from tests.test_core_checks import M2_MAX, answer, results, run
from tests.test_seam_a import SCHEMA
from tests.test_thermal import FakeSysfs

SERIAL, EUI, NGUID = "C02XK0ZZJG5H", "0100000000000000", "01000000000000000000000000000000"
SMART_JSON = json.dumps({
    "serial_number": SERIAL, "model_name": "APPLE SSD AP1024Z", "wwn": {"naa": 5, "id": 12345},
    "nvme_namespaces": [{"id": 1, "eui64": {"oui": 1, "ext_id": EUI}, "nguid": NGUID, "capacity": {"bytes": 994662584320}}],
    "nvme_smart_health_information_log": {
        "critical_warning": 0, "temperature": 34, "available_spare": 100, "available_spare_threshold": 99, "percentage_used": 2,
        "data_units_read": 39062500, "data_units_written": 19531250, "power_cycles": 321, "power_on_hours": 4567,
        "unsafe_shutdowns": 12, "media_errors": 0, "num_err_log_entries": 0,
    },
})
HEALTHY = {"critical_warning": 0, "temperature": 34, "available_spare": 100, "percentage_used": 2, "data_read_tb": 20, "data_written_tb": 10,
           "power_cycles": 321, "power_on_hours": 4567, "unsafe_shutdowns": 12, "media_errors": 0}
CONTROLLER = {"model": "APPLE SSD AP1024Z", "firmware": "874.140.1", "state": "live", "namespaces": 3, "smart": HEALTHY, "smart_missing": ""}
GOOD = {"observed": True, "controllers": [CONTROLLER], "other_controllers": 0, "any_nvme": True}
FIO_OUT = json.dumps({"jobs": [
    {"jobname": "write", "write": {"bw_bytes": 2_500_000_000}, "read": {"bw_bytes": 0}},
    {"jobname": "read", "read": {"bw_bytes": 3_100_000_000, "iops": 2956}, "write": {"bw_bytes": 0}},
    {"jobname": "random1", "read": {"iops": 18000.4}}, {"jobname": "random32", "read": {"iops": 240000.6}},
], "global options": {"filename": "/home/tester/.cache/omarchy-m-test/storage-benchmark.tmp"}})
CACHE = "/home/tester/.cache/omarchy-m-test"
PRIVATE = f"{CACHE}/bench.AbC123xy"
PATH = f"{PRIVATE}/{storage.FILE_NAME}"


class Sysfs(FakeSysfs):
    def __init__(self, smart: CommandResult, *args):
        super().__init__(*args)
        self.smart, self.commands = smart, []

    def run(self, argv):
        self.commands.append(list(argv))
        return CommandResult(127, "", "") if argv == hwread.SMART_VERSION and self.smart.returncode == 127 else (CommandResult(0, "smartctl 7.4", "") if argv == hwread.SMART_VERSION else self.smart)


def sysfs(smart: CommandResult | None = None, model: str = "APPLE SSD AP1024Z\n", size: str = "1942700360\n", firmware: str = "874.140.1\n") -> Sysfs:
    nvme = "/sys/class/nvme/nvme0"
    return Sysfs(smart or CommandResult(0, SMART_JSON, ""), {
        f"{nvme}/model": model, f"{nvme}/firmware_rev": firmware, f"{nvme}/state": "live\n", f"{nvme}/serial": SERIAL,
        "/sys/block/nvme0n1/size": size, "/sys/block/nvme0n2/size": "2048\n",
    }, {"/sys/class/nvme": ["nvme0"], nvme: ["model", "serial", "nvme0n1", "nvme0n2", "ng0n1"]})




class CollectTest(unittest.TestCase):
    def test_only_the_allowlisted_smart_values_cross_the_boundary(self):
        found = hwread.collect(sysfs(), "storage")

        self.assertEqual(found["controllers"][0]["smart"]["data_read_tb"], 20)
        self.assertEqual(found["controllers"][0]["smart"]["data_written_tb"], 10)
        self.assertEqual(found["controllers"][0]["namespaces"], 2)
        text = json.dumps(found)
        for private in (SERIAL, EUI, NGUID, "994662584320", "wwn", "nguid", "eui64"):
            self.assertNotIn(private, text)

    def test_it_never_reads_the_serial_file(self):
        host = sysfs()
        hwread.collect(host, "storage")

        self.assertFalse([path for path in host.reads if path.endswith("/serial")])

    def test_another_nvme_isnt_taken_for_the_macs_own_ssd(self):
        found = hwread.collect(sysfs(model="Samsung 990 PRO\n"), "storage")

        self.assertEqual((found["controllers"], found["other_controllers"], found["any_nvme"]), ([], 1, True))

    def test_a_firmware_string_that_isnt_plain_is_dropped(self):
        self.assertIsNone(hwread.collect(sysfs(firmware="<x y>\n"), "storage")["controllers"][0]["firmware"])

    def test_why_the_health_log_is_missing(self):
        for result, why in ((CommandResult(127, "", ""), "not-installed"),
                            (CommandResult(1, "", "sudo: a password is required"), "needs-root"),
                            (CommandResult(4, "{}", ""), "no-log"), (CommandResult(0, "not json", ""), "no-log")):
            with self.subTest(why=why):
                controller = hwread.collect(sysfs(result), "storage")["controllers"][0]
                self.assertEqual((controller["smart"], controller["smart_missing"]), (None, why))


class CapacityTest(unittest.TestCase):
    def test_sizes_round_to_the_tier_apple_sells(self):
        for size, name in ((251_000_000_000, "256GB"), (494_000_000_000, "512GB"), (994_662_584_320, "1TB"), (2_001_111_162_880, "2TB"),
                           (4_000_000_000_000, "4TB"), (8_001_000_000_000, "8TB")):
            with self.subTest(name=name):
                self.assertEqual(hwread.tier(size), name)

    def test_a_size_no_tier_is_close_to_has_none(self):
        self.assertIsNone(hwread.tier(3_000_000_000_000))
        self.assertIsNone(hwread.tier(0))

    def test_the_capacity_area_carries_only_the_tier(self):
        self.assertEqual(hwread.collect(sysfs(), "capacity"), {"observed": True, "tier": "1TB"})

    def test_no_apple_ssd_has_no_tier(self):
        self.assertEqual(hwread.collect(sysfs(model="Samsung\n"), "capacity")["tier"], None)

    def test_the_report_records_capacity_even_with_the_storage_section_skipped(self):
        rec = copy.deepcopy(M2_MAX)
        answer(rec, [HARDWARE_SNAPSHOT, "capacity"], stdout=json.dumps({"observed": True, "tier": "2TB"}))

        _, _, report = run(rec)

        self.assertEqual(report["machine"]["storage"], "2TB")
        self.assertEqual(errors(SCHEMA, report), [])

    def test_a_report_without_it_leaves_it_out(self):
        _, _, report = run(M2_MAX)

        self.assertNotIn("storage", report["machine"])
        self.assertEqual(errors(SCHEMA, report), [])

    def test_the_allowlist_keeps_it(self):
        self.assertTrue(privacy.REPORT_ALLOWLIST["machine"]["storage"])


class HealthTest(unittest.TestCase):
    def judge(self, **smart) -> dict:
        return storage._judge({**CONTROLLER, "smart": {**HEALTHY, **smart}}, 0, [])

    def test_a_healthy_ssd_passes_and_its_readings_are_evidence(self):
        found = self.judge()

        self.assertEqual(found["status"], "pass")
        self.assertIn("APPLE SSD AP1024Z, firmware 874.140.1, live, 3 namespaces", found["evidence"])
        self.assertIn("percentage used: 2%", found["evidence"])
        self.assertIn("data written: 10 TB", found["evidence"])

    def test_a_worn_ssd_still_passes(self):
        self.assertEqual(self.judge(percentage_used=99, available_spare=5)["status"], "pass")

    def test_media_errors_fail(self):
        found = self.judge(media_errors=3)

        self.assertEqual(found["status"], "fail")
        self.assertIn("the SSD reports 3 media errors", found["evidence"])

    def test_a_critical_warning_fails(self):
        self.assertEqual(self.judge(critical_warning=4)["status"], "fail")

    def test_without_the_log_it_skips_and_says_why(self):
        found = storage._judge({**CONTROLLER, "smart": None, "smart_missing": "needs-root"}, 0, [])

        self.assertEqual(found["status"], "skip")
        self.assertIn("needs root", found["evidence"][-1])

    def test_no_apple_ssd_fails_and_says_why(self):
        mac = recorded({"observed": True, "controllers": [], "other_controllers": 1, "any_nvme": True})

        found = storage._health(context(mac), {"observed": True, "controllers": [], "other_controllers": 1, "any_nvme": True})

        self.assertEqual(found["status"], "fail")
        self.assertIn("none of them Apple's", found["evidence"][0])

    def test_a_run_that_predates_the_section_skips(self):
        _, _, report = run(M2_MAX)

        for check_id in storage.CHECK_IDS:
            self.assertIn("not read in this run", results(report)[check_id]["evidence"][0])


def recorded(found: dict, commands: list[dict] = ()) -> RecordedHost:
    return RecordedHost({"recording_version": 1, "env": {"HOME": "/home/tester"}, "commands": [
        {"argv": [HARDWARE_SNAPSHOT, "storage"], "returncode": 0, "stdout": json.dumps(found), "stderr": ""}, *commands]})


def context(mac: RecordedHost) -> Context:
    machine = Machine("Apple MacBook Pro (16-inch, M2 Max, 2023)", "j416c", "t6021", "M2 Max", "aarch64", "7.1.12", ("apple,j416c", "apple,t6021"))
    return Context(mac, machine, catalogue.parse(bundled.catalogue_text()), Changes(mac))


def command(argv: list[str], stdout: str = "", returncode: int = 0) -> dict:
    return {"argv": argv, "returncode": returncode, "stdout": stdout, "stderr": ""}


def bench(fio: dict | None = None, free: str = "     Avail\n500000000000\n") -> tuple[dict, RecordedHost, Context]:
    mac = recorded(GOOD, [
        command(["mkdir", "-p", "--", CACHE]), command(["df", "--output=avail", "-B1", "--", CACHE], free),
        command(["pacman", "-Q", "fio"], "fio 3.38-1\n"), command(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", CACHE], "btrfs\n"),
        command(storage.make_temp_argv(CACHE), PRIVATE + "\n"),
        fio or command(storage.fio_argv(PATH), FIO_OUT), command(["fio", "--version"], "fio-3.38\n"),
    ])
    ctx = context(mac)
    return storage.benchmark(ctx), mac, ctx


class BenchmarkTest(unittest.TestCase):
    def test_it_scores_the_sequential_read_and_reports_the_rest(self):
        found, _, _ = bench()

        self.assertEqual(found["status"], "pass")
        self.assertEqual(found["score"], {"value": 3100, "unit": "MBps", "tool": "fio 3.38", "suite": storage.SUITE})
        self.assertIn("sequential write: 2500 MB/s", found["evidence"])
        self.assertIn("4K random read, queue depth 32: 240001 IOPS", found["evidence"])
        self.assertIn("filesystem: btrfs", found["evidence"])
        self.assertIn("root filesystem encryption: unknown", found["evidence"])

    def test_the_score_fits_the_schema(self):
        score_schema = SCHEMA["properties"]["checks"]["items"]["properties"]["score"]

        self.assertEqual(errors(score_schema, bench()[0]["score"]), [])

    def test_no_path_reaches_the_evidence(self):
        self.assertNotIn("/home", json.dumps(bench()[0]))

    def test_the_file_is_removed_by_a_restorer_registered_before_fio_runs(self):
        order = []

        class Watching(RecordedHost):
            def run(self, argv):
                order.append(list(argv))
                return super().run(argv)

        mac = Watching(recorded(GOOD, [
            command(["mkdir", "-p", "--", CACHE]), command(["df", "--output=avail", "-B1", "--", CACHE], "500000000000\n"),
            command(["pacman", "-Q", "fio"], "fio 3.38-1\n"), command(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", CACHE], "btrfs\n"),
            command(storage.make_temp_argv(CACHE), PRIVATE + "\n"),
            command(storage.fio_argv(PATH), FIO_OUT), command(["fio", "--version"], "fio-3.38\n"), command(["rm", "-rf", "--", PRIVATE]),
        ]).recording)
        ctx = context(mac)
        registered_before_fio = []
        real_run = mac.run

        def spying(argv):
            if argv[:1] == ["fio"] and "--version" not in argv:
                registered_before_fio.append([r.argv for r in ctx.changes.pending])
            return real_run(argv)

        mac.run = spying
        storage.benchmark(ctx)

        self.assertEqual(registered_before_fio, [[("rm", "-rf", "--", PRIVATE)]])
        ctx.changes.restore()
        self.assertEqual(order[-1], ["rm", "-rf", "--", PRIVATE])
        self.assertEqual(ctx.changes.pending, [])

    def test_a_failed_run_still_removes_the_file(self):
        found, _, ctx = bench(command(storage.fio_argv(PATH), "", 1))

        self.assertEqual(found["status"], "fail")
        self.assertEqual([r.argv for r in ctx.changes.pending], [("rm", "-rf", "--", PRIVATE)])

    def test_a_job_that_failed_fails_though_the_others_measured(self):
        out = json.loads(FIO_OUT)
        out["jobs"][3].update(error=5)

        self.assertEqual(bench(command(storage.fio_argv(PATH), json.dumps(out)))[0]["status"], "fail")

    def test_a_nonzero_exit_fails_though_there_are_numbers(self):
        self.assertEqual(bench(command(storage.fio_argv(PATH), FIO_OUT, 1))[0]["status"], "fail")

    def test_a_directory_mktemp_didnt_make_where_asked_is_refused(self):
        mac = recorded(GOOD, [
            command(["mkdir", "-p", "--", CACHE]), command(["df", "--output=avail", "-B1", "--", CACHE], "500000000000\n"),
            command(["pacman", "-Q", "fio"], "fio 3.38-1\n"), command(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", CACHE], "btrfs\n"),
            command(storage.make_temp_argv(CACHE), "/dev/nvme0n1\n"),
        ])
        ctx = context(mac)

        found = storage.benchmark(ctx)

        self.assertEqual(found["status"], "skip")
        self.assertFalse([argv for argv in mac.commands_run if argv[0] == "fio"])
        self.assertEqual(ctx.changes.pending, [])

    def test_the_file_is_always_in_the_fresh_directory(self):
        self.assertIn(f"--filename={PATH}", storage.fio_argv(PATH))

    def test_garbage_from_fio_fails(self):
        self.assertEqual(bench(command(storage.fio_argv(PATH), "{not json"))[0]["status"], "fail")

    def test_too_little_free_space_skips_and_nothing_is_written(self):
        found, mac, ctx = bench(free="1000\n")

        self.assertEqual(found["status"], "skip")
        self.assertIn("GiB free is needed", found["evidence"][-1])
        self.assertFalse([argv for argv in mac.commands_run if argv[0] == "fio"])
        self.assertEqual(ctx.changes.pending, [])

    def test_it_only_ever_names_a_file_never_a_block_device(self):
        argv = storage.fio_argv(PATH)

        self.assertEqual([a for a in argv if a.startswith("--filename=")], [f"--filename={PATH}"])
        self.assertFalse([a for a in argv if "/dev/" in a])
        self.assertIsNone(refusal(argv))
        self.assertIsNotNone(refusal(["dd", "if=/dev/zero", "of=/dev/nvme0n1"]))

    def test_the_cache_directory_comes_from_xdg_first(self):
        mac = RecordedHost({"recording_version": 1, "commands": [], "env": {"XDG_CACHE_HOME": "/cache", "HOME": "/home/tester"}})

        self.assertEqual(storage.directory(context(mac)), "/cache/omarchy-m-test")




class ScrubbedPathTest(unittest.TestCase):
    def test_the_tools_own_cache_path_survives_for_replay_without_the_user(self):
        found = privacy.Scrubber().scrub("--filename=/home/sam/.cache/omarchy-m-test/bench.AbC123/fio.dat")

        self.assertEqual(found, "--filename=<home>/.cache/omarchy-m-test/bench.AbC123/fio.dat")

    def test_anything_else_under_home_is_still_hidden(self):
        for path in ("/home/sam/.cache/other/x", "/home/sam/.cache/omarchy-m-test/my secret", "/home/sam/.cache/omarchy-m-test/a$b"):
            self.assertNotIn("sam", privacy.Scrubber().scrub(path))

if __name__ == "__main__":
    unittest.main()
