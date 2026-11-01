"""macOS reference files, the capture script, the DVFS ladder check and the "vs macOS" evidence lines."""

from __future__ import annotations

import copy
import glob
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import tempfile
import types
import unittest

from omarchy_m_test import bundled, catalogue, cpu_ladder, hwread, macos_reference, power, storage
from omarchy_m_test.host import HARDWARE_SNAPSHOT
from omarchy_m_test.machine import Machine
from omarchy_m_test.recording import RecordedHost
from omarchy_m_test.session import Changes, Context
from tests.test_core_checks import M2_MAX, results, run
from tests.test_seam_a import REPO
from tests.test_thermal import FakeSysfs

SCRIPT = os.path.join(REPO, "scripts", "macos-reference")
loader = importlib.machinery.SourceFileLoader("macos_reference_script", SCRIPT)
spec = importlib.util.spec_from_loader("macos_reference_script", loader)
script = importlib.util.module_from_spec(spec)
loader.exec_module(script)

SOUND = {"schema": macos_reference.SCHEMA, "version": 1, "board": "j416c", "source": "scripts/macos-reference", "captured": "2026-11-01",
         "macos": {"version": "26.6", "build": "25G83"}, "conditions": {"power": "ac", "brightness": "about 50%", "displays": 1},
         "metrics": {"cpu_ladders": {"e": {"states": 3, "min_mhz": 600, "max_mhz": 2064}, "p": {"states": 4, "min_mhz": 600, "max_mhz": 3504}},
                     "h264_decode_fps": 900, "hevc_decode_fps": 800, "idle_power_w": 3.5},
         "storage": {"1TB": {"read_mbps": 6000, "write_mbps": 5000, "random1_iops": 20000, "random32_iops": 300000}}}


class CommittedFilesTest(unittest.TestCase):
    def test_every_committed_reference_is_sound(self):
        paths = glob.glob(os.path.join(REPO, "catalogue", "macos-reference", "*.json"))

        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path), open(path, encoding="utf-8") as f:
                document = json.load(f)
                self.assertEqual(macos_reference.problems(document), [])
                self.assertEqual(document["board"], os.path.basename(path)[:-5])

    def test_the_imported_j616s_credits_aurora_diag(self):
        self.assertIn("aurora-diag", macos_reference.load("j616s")["source"])

    def test_every_committed_board_is_in_the_catalogue(self):
        boards = catalogue.parse(bundled.catalogue_text()).data["boards"]

        for path in glob.glob(os.path.join(REPO, "catalogue", "macos-reference", "*.json")):
            self.assertIn(os.path.basename(path)[:-5], boards)


class ProblemsTest(unittest.TestCase):
    def test_a_sound_file_has_none(self):
        self.assertEqual(macos_reference.problems(SOUND), [])

    def broken(self, **change) -> list[str]:
        return macos_reference.problems({**SOUND, **change})

    def test_what_it_refuses(self):
        for change in ({"schema": "other"}, {"version": 2}, {"board": "Mac14,6"}, {"captured": "yesterday"}, {"source": ""}, {"macos": {"version": "26"}},
                       {"conditions": {"power": "ac"}}, {"metrics": {"unknown": 1}}, {"storage": {"3TB": {}}}, {"storage": {"1TB": {"other": 1}}},
                       {"serial_number": "C02XK0"}, {"metrics": {"cpu_ladders": {"x": {}}}}):
            with self.subTest(change=change):
                self.assertTrue(self.broken(**change))

    def test_it_refuses_anything_private(self):
        self.assertTrue(self.broken(conditions={"power": "ac", "brightness": "/Users/maralc", "displays": 1}))

    def test_a_file_that_isnt_sound_is_treated_as_absent(self):
        real = bundled.macos_reference_text
        bundled.macos_reference_text = lambda board: b'{"schema": "wrong"}'
        try:
            self.assertIsNone(macos_reference.load("j416c"))
        finally:
            bundled.macos_reference_text = real

    def test_no_file_is_none(self):
        self.assertIsNone(macos_reference.load("j999"))


class LinesTest(unittest.TestCase):
    def test_a_line_names_the_conditions_and_isnt_a_verdict(self):
        lines = macos_reference.line(SOUND, "H.264 hardware decode", 500, "fps", 900)

        self.assertIn("500 fps here, 900 fps on macOS", lines[0])
        self.assertIn("macOS 26.6, on AC, brightness about 50%, 1 display, 2026-11-01", lines[0])
        self.assertIn("not a verdict", lines[0])

    def test_no_number_no_line(self):
        self.assertEqual(macos_reference.line(SOUND, "x", 1, "fps", None), [])
        self.assertEqual(macos_reference.line(None, "x", 1, "fps", 2), [])

    def test_storage_compares_only_at_the_same_capacity(self):
        measured = {"read": 3000, "write": 2500, "random1": 18000, "random32": 240000}

        same = macos_reference.storage_lines(SOUND, "1TB", measured)
        other = macos_reference.storage_lines(SOUND, "2TB", measured)

        self.assertEqual(len(same), 4)
        self.assertIn("1TB SSD, read 3000 MB/s here, 6000 MB/s on macOS", same[0])
        self.assertEqual(other, ["vs macOS: no macOS reference at this capacity (2TB)"])
        self.assertEqual(macos_reference.storage_lines(None, "1TB", measured), [])


def context(found: dict, board: str = "j416c") -> Context:
    machine = Machine("Apple Mac", board, "t6021", "M2 Max", "aarch64", "7.1.12", (f"apple,{board}", "apple,t6021"))
    mac = RecordedHost({"recording_version": 1, "commands": [{"argv": [HARDWARE_SNAPSHOT, "clocks"], "returncode": 0, "stdout": json.dumps(found), "stderr": ""}]})
    return Context(mac, machine, catalogue.parse(bundled.catalogue_text()), Changes(mac))


POLICIES = {"observed": True, "policies": [
    {"driver": "apple-cpufreq", "mhz": [600, 1000, 2064], "min_mhz": 600, "max_mhz": 2064},
    {"driver": "apple-cpufreq", "mhz": [600, 1000, 2000, 3504], "min_mhz": 600, "max_mhz": 3504},
    {"driver": "apple-cpufreq", "mhz": [600, 1000, 2000, 3504], "min_mhz": 600, "max_mhz": 3504},
]}


class LadderTest(unittest.TestCase):
    def check(self, found: dict = POLICIES, reference: dict | None = SOUND) -> dict:
        real = macos_reference.load
        macos_reference.load = lambda board: reference
        try:
            return cpu_ladder.check(context(found))
        finally:
            macos_reference.load = real

    def test_steps_that_match_pass_and_say_so(self):
        found = self.check()

        self.assertEqual(found["status"], "pass")
        self.assertTrue(any("efficiency cluster 3 steps, 600-2064 MHz here, 3 steps, 600-2064 MHz on macOS: the same" in line for line in found["evidence"]))

    def test_different_steps_still_pass_and_say_so(self):
        fewer = {"observed": True, "policies": [{"driver": "x", "mhz": [600, 2064], "min_mhz": 600, "max_mhz": 2064}, {"driver": "x", "mhz": [600, 3000], "min_mhz": 600, "max_mhz": 3000}]}

        found = self.check(fewer)

        self.assertEqual(found["status"], "pass")
        self.assertTrue(any(line.endswith("advertised steps, not clocks reached") and "different" in line for line in found["evidence"]))

    def test_an_ultras_repeated_clusters_count_once(self):
        self.assertEqual(set(cpu_ladder.clusters(POLICIES["policies"])), {"e", "p"})

    def test_a_lone_cluster_isnt_labelled_or_paired_with_macos(self):
        lone = {"observed": True, "policies": [{"driver": "x", "mhz": [660, 3504], "min_mhz": 660, "max_mhz": 3504}]}

        found = self.check(lone)

        self.assertEqual(found["status"], "pass")
        self.assertIn("The only readable cluster here: 2 steps, 660-3504 MHz", found["evidence"])
        self.assertFalse([line for line in found["evidence"] if line.startswith("vs macOS")])

    def test_without_a_reference_it_skips_but_says_what_it_read(self):
        found = self.check(reference=None)

        self.assertEqual(found["status"], "skip")
        self.assertIn("Performance cluster here: 4 steps, 600-3504 MHz", found["evidence"])

    def test_no_steps_fails(self):
        self.assertEqual(self.check({"observed": True, "policies": []})["status"], "fail")

    def test_a_run_that_predates_the_check_skips(self):
        _, _, report = run(M2_MAX)

        self.assertIn("not read in this run", results(report)[cpu_ladder.LADDER]["evidence"][0])

    def test_it_reads_cpufreq(self):
        policy = "/sys/devices/system/cpu/cpufreq/policy0"
        found = hwread.collect(FakeSysfs({
            f"{policy}/scaling_available_frequencies": "600000 1000000 \n", f"{policy}/scaling_boost_frequencies": "3504000\n",
            f"{policy}/cpuinfo_min_freq": "600000\n", f"{policy}/cpuinfo_max_freq": "3504000\n", f"{policy}/scaling_driver": "apple-cpufreq\n",
        }, {"/sys/devices/system/cpu/cpufreq": ["policy0", "ondemand"]}), "clocks")

        self.assertEqual(found["policies"], [{"driver": "apple-cpufreq", "mhz": [600, 1000, 3504], "min_mhz": 600, "max_mhz": 3504}])


class IdleTest(unittest.TestCase):
    def test_macos_idle_power_is_beside_a_pass_only(self):
        real = macos_reference.load
        macos_reference.load = lambda board: SOUND
        try:
            passed = power._vs_macos(context({}), {"id": power.IDLE_DRAW, "status": "pass", "evidence": []})
            skipped = power._vs_macos(context({}), {"id": power.IDLE_DRAW, "status": "skip", "evidence": []})
        finally:
            macos_reference.load = real

        self.assertIn("3.5 W idle on macOS", passed["evidence"][0])
        self.assertEqual(skipped["evidence"], [])


class Fake:
    """macOS's tools, by program."""

    def __init__(self, **answers):
        self.answers, self.commands = answers, []

    def __call__(self, argv, timeout=600):
        self.commands.append(list(argv))
        key = (argv[1] if argv[0] == "sudo" else argv[0]).replace("-", "_")
        found = self.answers.get(key, (127, b"", f"{argv[0]} isn't installed"))
        return found(argv) if callable(found) else found


SAMPLE = plistlib.dumps({"processor": {"combined_power": 3500.0, "clusters": [
    {"name": "E-Cluster", "dvfm_states": [{"freq": 600}, {"freq": 972}, {"freq": 2064}]},
    {"name": "P0-Cluster", "dvfm_states": [{"freq": 660000000}, {"freq": 3504000000}]},
    {"name": "P1-Cluster", "dvfm_states": [{"freq": 660000000}, {"freq": 3504000000}]}]}})
DISKUTIL = plistlib.dumps({"TotalSize": 994662584320, "VolumeName": "Macintosh HD", "DiskUUID": "SECRET-UUID", "IORegistryEntryName": "APPLE SSD AP1024Z Media",
                           "Internal": True, "FilesystemType": "apfs"})
DF = (0, b"Filesystem 512-blocks Used Available Capacity iused ifree %iused Mounted on\n/dev/disk3s5 1 1 1 1% 1 1 1% /System/Volumes/Data\n", "")
FIO = json.dumps({"jobs": [{"jobname": "write", "write": {"bw_bytes": 5_000_000_000}}, {"jobname": "read", "read": {"bw_bytes": 6_000_000_000}},
                           {"jobname": "random1", "read": {"iops": 20000.4}}, {"jobname": "random32", "read": {"iops": 300000.6}}],
                  "fio version": "fio-3.38"}).encode()


def sw_vers(argv):
    return (0, b"26.6\n", "") if "-productVersion" in argv else (0, b"25G83\n", "")


def ffmpeg(argv):
    return (0, b"", "frame=  600 fps=0 q=-0.0 Lsize=N/A time=00:00:20.00\nbench: utime=1.0s stime=0.1s rtime=0.667s\n") if "-benchmark" in argv else (0, b"", "")


FULL = dict(sysctl=(0, b"J416c\n", ""), sw_vers=sw_vers, pmset=(0, b"Now drawing from 'AC Power'\n", ""), powermetrics=(0, SAMPLE + b"\0" + SAMPLE, ""),
            system_profiler=(0, json.dumps({"SPDisplaysDataType": [{"spdisplays_ndrvs": [{"_name": "Colour LCD", "_spdisplays_display-serial-number": "SECRET-SERIAL"}]}]}).encode(), ""),
            ffmpeg=ffmpeg, fio=(0, FIO, ""), diskutil=(0, DISKUTIL, ""), df=DF)


def capture(**answers) -> tuple[dict, list[str], Fake]:
    said: list[str] = []
    fake = Fake(**{**FULL, **answers})
    real = script.power
    script.power = lambda say, runner=script.run: real(say, runner, waiter=lambda seconds: None)
    try:
        with tempfile.TemporaryDirectory() as directory:
            args = types.SimpleNamespace(skip=[], brightness="about 50%", ane_json=None)
            real_storage = script.storage
            script.storage = lambda d, say, runner=script.run: real_storage(d, say, runner, free_bytes=10**12)
            try:
                return script.capture(args, said.append, fake, directory), said, fake
            finally:
                script.storage = real_storage
    finally:
        script.power = real


class ScriptTest(unittest.TestCase):
    def test_a_full_capture_is_a_sound_reference(self):
        document, said, _ = capture()

        self.assertEqual(macos_reference.problems(document), [])
        self.assertEqual(document["board"], "j416c")
        self.assertEqual(document["metrics"]["cpu_ladders"], {"e": {"states": 3, "min_mhz": 600, "max_mhz": 2064}, "p": {"states": 2, "min_mhz": 660, "max_mhz": 3504}})
        self.assertEqual(document["metrics"]["idle_power_w"], 3.5)
        self.assertEqual(document["metrics"]["h264_decode_fps"], 900)
        self.assertEqual(document["storage"], {"1TB": {"write_mbps": 5000, "read_mbps": 6000, "random1_iops": 20000, "random32_iops": 300001}})
        self.assertEqual(document["conditions"], {"power": "ac", "brightness": "about 50%", "displays": 1})

    def test_nothing_private_reaches_the_file(self):
        text = json.dumps(capture()[0])

        for private in ("SECRET", "Macintosh HD", "Colour LCD", "APPLE SSD"):
            self.assertNotIn(private, text)

    def test_what_it_cant_do_it_leaves_out_and_says_why(self):
        document, said, _ = capture(powermetrics=(1, b"", "sudo: a password is required"), fio=(127, b"", "fio isn't installed"), ffmpeg=(127, b"", "ffmpeg isn't installed"))

        self.assertEqual(macos_reference.problems(document), [])
        self.assertEqual(document["metrics"], {})
        self.assertNotIn("storage", document)
        self.assertTrue(any("powermetrics gave nothing" in line for line in said))
        self.assertTrue(any("fio gave no result" in line for line in said))
        self.assertTrue(any("decode left out" in line for line in said))

    def test_a_decode_that_stopped_early_is_left_out(self):
        short = lambda argv: (0, b"", "frame=  100 bench: rtime=0.1s\n") if "-benchmark" in argv else (0, b"", "")

        self.assertNotIn("h264_decode_fps", capture(ffmpeg=short)[0]["metrics"])

    def test_a_temporary_file_off_the_macs_own_ssd_is_left_out(self):
        external = plistlib.dumps({"TotalSize": 994662584320, "Internal": False, "FilesystemType": "apfs"})

        document, said, fake = capture(diskutil=(0, external, ""))

        self.assertNotIn("storage", document)
        self.assertTrue(any("own SSD" in line for line in said))
        self.assertFalse([argv for argv in fake.commands if argv[0] == "fio"])

    def test_a_fio_job_that_failed_is_left_out(self):
        failed = json.loads(FIO)
        failed["jobs"][3]["error"] = 5

        self.assertNotIn("storage", capture(fio=(0, json.dumps(failed).encode(), ""))[0])

    def test_its_jobs_match_the_clis(self):
        ours = script.fio_argv("x")
        theirs = storage.fio_argv("x")

        self.assertEqual([a for a in ours if "ioengine" not in a], [a for a in theirs if "ioengine" not in a])

    def test_its_tiers_match_the_clis(self):
        self.assertEqual(script.TIERS, hwread.TIERS)
        self.assertEqual(tuple(name for name, _ in script.TIERS), macos_reference.TIERS)

    def test_its_clip_matches_the_clis(self):
        from omarchy_m_test import benchmarks

        self.assertEqual((script.CLIP_SOURCE, script.CLIP_SECONDS, script.LOOPS), (benchmarks.CLIP_SOURCE, benchmarks.CLIP_SECONDS, benchmarks.LOOPS))
        self.assertEqual(dict(script.ENCODERS), {codec: encoder for codec, _, _, encoder in benchmarks.CODECS})

    def test_it_refuses_a_mac_that_doesnt_name_its_board(self):
        with self.assertRaises(SystemExit):
            capture(sysctl=(0, b"unknown\n", ""))

    def test_it_uses_only_stdlib_and_never_uploads(self):
        with open(SCRIPT, encoding="utf-8") as f:
            source = f.read()

        for forbidden in ("urllib", "requests", "http.client", "socket"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
