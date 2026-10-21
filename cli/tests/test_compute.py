"""GPU/MLX and Neural Engine checks: what is collected, what passes, what fails and what a Mac without the ANE reports."""

from __future__ import annotations

import copy
import json
import os
import unittest

from omarchy_m_test import bundled, catalogue, compute
from omarchy_m_test.checks import ORDER
from omarchy_m_test.host import COMPUTE_SNAPSHOT, CommandResult, RealHost
from omarchy_m_test.machine import Machine
from omarchy_m_test.privacy import Scrubber
from omarchy_m_test.recording import RecordedHost, RecordingHost
from omarchy_m_test.sections import APPLE
from omarchy_m_test.session import Changes, Context

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
ANE_NODE = compute.DT_BASE + "/soc/ane@284000000"
CONFIG = "CONFIG_DRM_ACCEL=y\nCONFIG_DRM_ACCEL_ANE=m\nCONFIG_RFS_ACCEL=y\nCONFIG_APPLE_DART=y\nCONFIG_SND_COMPRESS_ACCEL=y\n# CONFIG_DRM_ACCEL_ARM_ETHOSU is not set\n"
LOG = (
    "asahi 406400000.gpu: Probing...\n"
    "asahi 406400000.gpu: MMU: IO map: 0x28e3d8000:0x4000 -> 0xffffffaf00870000\n"
    "asahi 406400000.gpu:   Type: G14C\n"
    "[drm] Initialized asahi 0.2.0 for 406400000.gpu on minor 1\n"
    "platform 284000000.ane: Adding to iommu group 0\n"
    "ane_t6021 284000000.ane: loaded ane_t6021 (DRM major 2)\n"
    "[drm] Initialized ane 2.0.0 for 284000000.ane on minor 0\n"
    "memlog: free=73747 top=python3:4425M ane-session:2682M quickshell:301M\n"
    "rtkit-helper 2a9400000.mtp: RTKit: syslog message: accel.c:72: Accel ready\n"
)
VULKAN = (
    "Vulkan Instance Version: 1.4.363\n\nDevices:\n========\nGPU0:\n"
    "\tapiVersion         = 1.4.362\n\tdriverVersion      = 26.3.0\n\tvendorID           = 0x106b\n"
    "\tdeviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\n\tdeviceName         = Apple M2 Max (G14C B1)\n"
    "\tdriverID           = DRIVER_ID_MESA_HONEYKRISP\n\tdriverName         = Honeykrisp\n"
    "\tdriverInfo         = Mesa 26.3.0-devel (git-a5b1797446)\n\tconformanceVersion = 1.4.0.0\n"
)
MLX_RAN = {"installed": True, "version": "0.32.4", "device": "Device(gpu, 0)", "result": 2097152.0,
           "info": {"device_name": "Apple M2 Max (G14C B1)", "driver": "Honeykrisp", "driver_info": "Mesa 26.3.0-devel (git-a5b1797446)", "gpu_cores": 38}}


def fixture(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as f:
        return json.load(f)


class FakeMac(RealHost):
    """A synthetic sysfs, device tree and command set. Anything that would write, or read a private file, fails the test."""

    def __init__(self, *, ane: bool = True, node_status: bytes = b"okay\0", mlx: dict | None = MLX_RAN, vulkan: bool = True, log: str = LOG):
        self.files = {
            "/proc/device-tree/compatible": b"apple,j414c\0apple,t6021\0apple,arm-platform\0",
            "/proc/sys/kernel/osrelease": b"7.1.12-2-ARCH\n",
            f"{compute.DRM}/card1/device/uevent": b"DRIVER=asahi\nOF_NAME=gpu\nOF_FULLNAME=/soc/gpu@406400000\nOF_COMPATIBLE_0=apple,agx-t6021\nOF_COMPATIBLE_1=apple,agx-g14c\nOF_COMPATIBLE_N=2\n",
            f"{compute.DRM}/card0/device/uevent": b"DRIVER=apple-drm\nOF_COMPATIBLE_0=apple,display-subsystem\n",
            ANE_NODE + "/compatible": b"apple,t6021-ane\0",
            ANE_NODE + "/status": node_status,
        }
        self.dirs: dict[str, list[str] | OSError] = {
            compute.DRM: ["card0", "card1", "card1-eDP-1", "version"], "/dev/dri": ["by-path", "card1", "renderD128"],
            ANE_NODE: ["compatible", "iommus", "power-domains", "status", "name", "reg"],
            compute.IOMMU_CLASS: ["apple-dart.285800000.iommu", "apple-dart.285810000.iommu"],
            compute.IOMMU_GROUPS: ["0", "1"], compute.IOMMU_GROUPS + "/0/devices": ["284000000.ane"], compute.IOMMU_GROUPS + "/1/devices": ["285800000.iommu"],
            compute.PLATFORM: ["284000000.ane", "406400000.gpu"], compute.MODULES: ["ane_t6021", "apple_dart", "ext4"],
        }
        if ane:
            self.files.update({
                f"{compute.PLATFORM}/284000000.ane/uevent": b"DRIVER=ane_t6021\nOF_NAME=ane\n",
                f"{compute.MODULES}/ane_t6021/initstate": b"live\n",
                f"{compute.MODULES}/ane_t6021/parameters/call_settle_us": b"100\n",
                f"{compute.MODULES}/ane_t6021/parameters/stats": b"Y\n",
            })
            self.dirs[compute.MODULES + "/ane_t6021/parameters"] = ["call_settle_us", "stats"]
            self.dirs[f"{compute.PLATFORM}/284000000.ane/driver"] = []
            self.dirs[compute.ACCEL_DEV] = ["accel0"]
            self.dirs[compute.ACCEL_CLASS] = ["accel0"]
        else:
            self.dirs[compute.MODULES] = ["apple_dart", "ext4"]
            self.dirs[compute.ACCEL_DEV] = FileNotFoundError("/dev/accel")
        self.mlx, self.vulkan, self.log = mlx, vulkan, log
        self.commands: list[list[str]] = []
        self.reads: list[str] = []

    def read_file(self, path):
        self.reads.append(path)
        if any(secret in path for secret in ("serial", "mesa_calibration", "/var/lib/", "os_uuid")):
            raise AssertionError("attempt to read private data: " + path)
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def list_dir(self, path):
        value = self.dirs.get(path)
        if value is None:
            raise FileNotFoundError(path)
        if isinstance(value, OSError):
            raise value
        return value

    def run(self, argv):
        self.commands.append(list(argv))
        if argv[:4] != ["timeout", "-k", "2", argv[3]]:
            raise AssertionError("unbounded command: " + str(argv))
        command = argv[4:]
        if command[0] in ("rm", "tee", "sudo", "modprobe", "insmod", "rmmod", "reboot", "dd"):
            raise AssertionError("mutating command: " + str(argv))
        if command[:2] == ["vulkaninfo", "--summary"]:
            return CommandResult(0, VULKAN, "") if self.vulkan else CommandResult(127, "", "not found")
        if command[0] == "python3":
            if self.mlx is None:
                return CommandResult(127, "", "not found")
            return CommandResult(0, "noise\n" + json.dumps(self.mlx) + "\n", "")
        if command[0] == "journalctl":
            return CommandResult(0, self.log, "")
        if command[:2] == ["zcat", "/proc/config.gz"]:
            return CommandResult(0, CONFIG, "")
        if command[:2] == ["pacman", "-Q"]:
            return CommandResult(1, "mesa 1:26.2.4-1\nlinux-aurora 7.1.12.aurora2-20261013.18\nprivate-package 9\n", "error: package 'x' was not found")
        if command[0] == "find" and command[1] == compute.DT_BASE:
            return CommandResult(0, ANE_NODE + "\n", "")
        if command[0] == "find" and command[1].startswith("/usr/lib/modules/"):
            return CommandResult(0, command[1] + "/kernel/drivers/accel/ane/ane.ko\n" + command[1] + "/kernel/drivers/accel/ane/ane_t6021.ko\n", "")
        if command[0] == "find" and command[1] == compute.FIRMWARE:
            return CommandResult(0, compute.FIRMWARE + "/apple/ane\n" + compute.FIRMWARE + "/apple/ane/t602x_ane0_fw_selene_rc4x.macho\n", "")
        raise AssertionError("unexpected command: " + str(argv))


def results(found: dict) -> dict[str, dict]:
    return {r["id"]: r for r in (compute.gpu_result(found), compute.ane_result(found))}


def context(mac) -> Context:
    machine = Machine("Apple MacBook Pro (16-inch, M2 Max, 2023)", "j414c", "t6021", "M2 Max", "aarch64", "7.1.12", ("apple,j414c", "apple,t6021"))
    return Context(mac, machine, catalogue.parse(bundled.catalogue_text()), Changes(mac))


class CollectTest(unittest.TestCase):
    def test_collects_the_gpu_and_the_neural_engine(self):
        found = FakeMac().compute_snapshot()

        self.assertIs(found["apple"], True)
        self.assertEqual(found["board"], ["apple,j414c", "apple,t6021", "apple,arm-platform"])
        self.assertEqual(found["gpu"]["drivers"], [{"driver": "asahi", "compatible": ["apple,agx-t6021", "apple,agx-g14c"]}])
        self.assertEqual(found["gpu"]["render_nodes"], ["renderD128"])
        self.assertEqual(found["gpu"]["vulkan"]["devices"][0]["driverName"], "Honeykrisp")
        self.assertEqual(found["gpu"]["vulkan"]["instance"], "1.4.363")
        self.assertEqual(found["mlx"]["state"], "ran")
        ane = found["ane"]
        self.assertEqual(ane["nodes"], [{"path": "/soc/ane", "compatible": ["apple,t6021-ane"], "status": "okay", "properties": ["iommus", "power-domains"]}])
        self.assertEqual(ane["modules"], [{"name": "ane_t6021", "state": "live", "parameters": {"call_settle_us": "100", "stats": "Y"}}])
        self.assertEqual(ane["devices"], [{"name": "ane", "driver": "ane_t6021", "bound": True, "iommu_group": "0"}])
        self.assertEqual((ane["dev_accel"], ane["class_accel"]), (["accel0"], ["accel0"]))
        self.assertEqual(ane["iommu"], {"devices": 2, "apple_dart": 2})
        self.assertEqual(ane["config"], ["# CONFIG_DRM_ACCEL_ARM_ETHOSU is not set", "CONFIG_APPLE_DART=y", "CONFIG_DRM_ACCEL=y", "CONFIG_DRM_ACCEL_ANE=m"])
        self.assertEqual(ane["modules_on_disk"], ["ane.ko", "ane_t6021.ko"])
        self.assertEqual(ane["firmware"], ["apple/ane", "apple/ane/t602x_ane0_fw_selene_rc4x.macho"])
        self.assertEqual(found["packages"], {"mesa": "1:26.2.4-1", "linux-aurora": "7.1.12.aurora2-20261013.18"})

    def test_unit_addresses_are_dropped_from_device_tree_paths(self):
        found = FakeMac().compute_snapshot()

        self.assertNotIn("284000000", json.dumps(found["ane"]["nodes"]))
        self.assertEqual(compute._node_path(compute.DT_BASE + "/soc/dart-ane@284800000"), "/soc/dart-ane")

    def test_only_reads_state_and_never_changes_anything(self):
        mac = FakeMac()
        mac.compute_snapshot()

        allowed = ("/uevent", "/status", "/compatible", "/initstate", "/parameters/", "/osrelease")
        self.assertTrue(all(any(part in path for part in allowed) for path in mac.reads), mac.reads)
        for argv in mac.commands:
            self.assertEqual(argv[:3], ["timeout", "-k", "2"])
        vulkan = next(a for a in mac.commands if "vulkaninfo" in a)
        self.assertEqual(vulkan[3], "10")
        mlx = next(a for a in mac.commands if "python3" in a)
        self.assertEqual(mlx[3], "20")

    def test_the_kernel_log_is_read_as_messages_alone_so_no_host_name_is_in_it(self):
        mac = FakeMac()
        mac.compute_snapshot()

        journal = next(a for a in mac.commands if "journalctl" in a)
        self.assertIn("-o", journal)
        self.assertEqual(journal[journal.index("-o") + 1], "cat")

    def test_other_programs_mentioning_the_ane_are_not_kernel_driver_lines(self):
        ane = FakeMac().compute_snapshot()["ane"]["log"]

        self.assertEqual(ane, ["platform 284000000.ane: Adding to iommu group 0", "ane_t6021 284000000.ane: loaded ane_t6021 (DRM major 2)",
                               "[drm] Initialized ane 2.0.0 for 284000000.ane on minor 0"])

    def test_the_gpu_driver_noise_is_left_out_of_its_log(self):
        gpu = FakeMac().compute_snapshot()["gpu"]["log"]

        self.assertEqual(gpu, ["asahi 406400000.gpu: Probing...", "asahi 406400000.gpu:   Type: G14C", "[drm] Initialized asahi 0.2.0 for 406400000.gpu on minor 1"])

    def test_a_dmesg_fallback_loses_its_time_stamps(self):
        self.assertEqual(compute._kernel_lines("[    4.123456] ane_t6021 284000000.ane: loaded\n", compute.LOG_ANE, 5), ["ane_t6021 284000000.ane: loaded"])

    def test_a_long_log_keeps_its_first_and_last_lines(self):
        text = "".join(f"ane x: line {n}\n" for n in range(40))

        kept = compute._kernel_lines(text, compute.LOG_ANE, 14)

        self.assertEqual((len(kept), kept[0], kept[6], kept[7], kept[-1]), (14, "ane x: line 0", "ane x: line 6", "ane x: line 33", "ane x: line 39"))

    def test_not_an_apple_mac_or_an_unreadable_device_tree_collects_nothing(self):
        mac = FakeMac()
        mac.files["/proc/device-tree/compatible"] = b"acme,box\0"
        self.assertEqual(mac.compute_snapshot(), {"apple": False})
        del mac.files["/proc/device-tree/compatible"]
        self.assertEqual(mac.compute_snapshot(), {"apple": None})

    def test_a_node_status_it_does_not_know_is_not_copied(self):
        found = FakeMac(node_status=b"/home/someone/secret\0").compute_snapshot()

        self.assertEqual(found["ane"]["nodes"][0]["status"], "other")

    def test_a_missing_vulkaninfo_and_a_missing_python_are_said(self):
        found = FakeMac(vulkan=False, mlx=None).compute_snapshot()

        self.assertEqual(found["gpu"]["vulkan"]["state"], "missing")
        self.assertEqual(found["mlx"], {"state": "no-python"})

    def test_a_hung_vulkaninfo_and_a_hung_mlx_op_time_out_without_holding_the_run(self):
        class Hung(FakeMac):
            def run(self, argv):
                if "vulkaninfo" in argv or "python3" in argv:
                    return CommandResult(124, "", "")
                return super().run(argv)

        found = Hung().compute_snapshot()

        self.assertEqual(found["gpu"]["vulkan"]["state"], "timeout")
        self.assertEqual(found["mlx"], {"state": "timeout"})

    def test_mlx_not_installed_and_an_op_that_crashed_are_told_apart(self):
        self.assertEqual(FakeMac(mlx={"installed": False, "error": "ModuleNotFoundError"}).compute_snapshot()["mlx"], {"state": "missing", "error": "ModuleNotFoundError"})
        class Crashed(FakeMac):
            def run(self, argv):
                return CommandResult(1, "", "Traceback\nRuntimeError: no Vulkan device\n") if "python3" in argv else super().run(argv)

        self.assertEqual(Crashed().compute_snapshot()["mlx"], {"state": "error", "returncode": 1, "error": "RuntimeError: no Vulkan device"})

    def test_an_mlx_that_is_installed_but_cannot_import_is_broken_not_missing(self):
        broken = FakeMac(mlx={"installed": False, "error": "OSError", "absent": False, "message": "libmlx.so: cannot open shared object file"})
        self.assertEqual(broken.compute_snapshot()["mlx"], {"state": "broken", "error": "OSError: libmlx.so: cannot open shared object file"})
        other_module = FakeMac(mlx={"installed": False, "error": "ModuleNotFoundError", "absent": False, "message": "No module named 'numpy'"})
        self.assertEqual(other_module.compute_snapshot()["mlx"]["state"], "broken")
        absent = FakeMac(mlx={"installed": False, "error": "ModuleNotFoundError", "absent": True, "message": "No module named 'mlx'"})
        self.assertEqual(absent.compute_snapshot()["mlx"]["state"], "missing")

    def test_the_mlx_subprocess_runs_isolated_so_the_current_directory_is_not_imported(self):
        mac = FakeMac()
        mac.compute_snapshot()
        mlx = next(a for a in mac.commands if "python3" in a)
        self.assertEqual(mlx[4:7], ["python3", "-I", "-c"])


class GpuResultTest(unittest.TestCase):
    def found(self, **kwargs) -> dict:
        return FakeMac(**kwargs).compute_snapshot()

    def test_the_op_on_the_gpu_passes_and_names_the_device(self):
        result = compute.gpu_result(self.found())

        self.assertEqual(result["status"], "pass")
        self.assertIn("default device Device(gpu, 0)", result["evidence"][0])
        self.assertIn("summed to 2097152", result["evidence"][0])
        self.assertIn("device_name: Apple M2 Max (G14C B1)", result["evidence"][1])
        self.assertIn("GPU kernel driver: asahi; device-tree compatible: apple,agx-t6021, apple,agx-g14c", result["evidence"])
        self.assertIn("render node: renderD128", result["evidence"])
        self.assertTrue(any(line.startswith("deviceName=Apple M2 Max (G14C B1)") and "driverInfo=Mesa 26.3.0-devel" in line for line in result["evidence"]))
        self.assertIn("mesa 1:26.2.4-1", result["evidence"])

    def test_mlx_on_the_cpu_fails(self):
        result = compute.gpu_result(self.found(mlx={**MLX_RAN, "device": "Device(cpu, 0)"}))

        self.assertEqual(result["status"], "fail")
        self.assertIn("MLX does not run on the GPU here", result["evidence"][0])

    def test_a_wrong_sum_fails(self):
        result = compute.gpu_result(self.found(mlx={**MLX_RAN, "result": 0.0}))

        self.assertEqual(result["status"], "fail")
        self.assertIn("expected 2097152", result["evidence"][0])

    def test_without_mlx_the_check_is_skipped_and_still_lists_the_gpu(self):
        result = compute.gpu_result(self.found(mlx={"installed": False}))

        self.assertEqual(result["status"], "skip")
        self.assertIn("MLX isn't installed", result["evidence"][0])
        self.assertIn("render node: renderD128", result["evidence"])

    def test_a_crashed_op_fails_and_a_hung_one_fails(self):
        self.assertEqual(compute.gpu_result({**self.found(), "mlx": {"state": "error", "returncode": 1, "error": "boom"}})["status"], "fail")
        self.assertEqual(compute.gpu_result({**self.found(), "mlx": {"state": "timeout"}})["status"], "fail")

    def test_a_broken_mlx_import_fails_and_does_not_skip(self):
        result = compute.gpu_result({**self.found(), "mlx": {"state": "broken", "error": "OSError: libmlx.so: cannot open shared object file"}})
        self.assertEqual(result["status"], "fail")
        self.assertIn("import mlx.core failed", result["evidence"][0])

    def test_no_render_node_and_no_vulkan_device_are_said(self):
        found = self.found(vulkan=False)
        found["gpu"]["render_nodes"] = []
        found["gpu"]["drivers"] = []

        evidence = compute.gpu_result(found)["evidence"]

        self.assertIn("render node: none in /dev/dri", evidence)
        self.assertIn("vulkaninfo --summary: missing", evidence)
        self.assertTrue(any(line.startswith("no DRM device has an apple,agx compatible") for line in evidence))


class AneResultTest(unittest.TestCase):
    def test_a_loaded_bound_driver_with_an_accelerator_device_passes_with_its_parameters(self):
        result = compute.ane_result(FakeMac().compute_snapshot())

        self.assertEqual(result["status"], "pass")
        self.assertIn("module ane_t6021 parameters: call_settle_us=100, stats=Y", result["evidence"])
        self.assertIn("platform device ane: driver ane_t6021, bound yes, IOMMU group 0", result["evidence"])
        self.assertIn("accelerator device /dev/accel: accel0; /sys/class/accel: accel0", result["evidence"])
        self.assertIn("kernel: [drm] Initialized ane 2.0.0 for 284000000.ane on minor 0", result["evidence"])

    def test_a_disabled_node_without_a_module_fails_and_collects_what_enabling_needs(self):
        result = compute.ane_result(FakeMac(ane=False, node_status=b"disabled\0").compute_snapshot())

        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["evidence"][0], "ANE not enabled: the ANE device-tree node is disabled; no ANE module is loaded; no accelerator device exists; "
                                                "the ANE platform device has no driver bound")
        evidence = result["evidence"]
        self.assertIn("device-tree node /soc/ane: compatible apple,t6021-ane; status disabled; properties iommus, power-domains", evidence)
        self.assertIn("no ANE module is loaded", evidence)
        self.assertIn("board: apple,j414c, apple,t6021, apple,arm-platform; kernel: 7.1.12-2-ARCH", evidence)
        self.assertIn("kernel config: # CONFIG_DRM_ACCEL_ARM_ETHOSU is not set; CONFIG_APPLE_DART=y; CONFIG_DRM_ACCEL=y; CONFIG_DRM_ACCEL_ANE=m", evidence)
        self.assertIn("ANE and DART modules on disk: ane.ko, ane_t6021.ko", evidence)
        self.assertIn("ANE firmware files: apple/ane, apple/ane/t602x_ane0_fw_selene_rc4x.macho", evidence)
        self.assertIn("IOMMU: 2 devices, 2 Apple DARTs", evidence)
        self.assertIn("linux-aurora 7.1.12.aurora2-20261013.18", evidence)

    def test_modules_on_disk_that_are_not_loaded_are_the_clue(self):
        evidence = compute.ane_result(FakeMac(ane=False).compute_snapshot())["evidence"]

        self.assertTrue(evidence[0].startswith("ANE not enabled: no ANE module is loaded; no accelerator device exists"))
        self.assertIn("ANE and DART modules on disk: ane.ko, ane_t6021.ko", evidence)

    def test_a_loaded_module_whose_driver_never_bound_fails(self):
        mac = FakeMac()
        del mac.dirs[f"{compute.PLATFORM}/284000000.ane/driver"]

        result = compute.ane_result(mac.compute_snapshot())

        self.assertEqual(result["status"], "fail")
        self.assertIn("the ANE platform device has no driver bound", result["evidence"][0])

    def test_a_mac_whose_device_tree_has_no_ane_node_is_skipped(self):
        class NoNode(FakeMac):
            def run(self, argv):
                if argv[4] == "find" and argv[5] == compute.DT_BASE:
                    return CommandResult(0, "", "")
                return super().run(argv)

        result = compute.ane_result(NoNode(ane=False).compute_snapshot())

        self.assertEqual(result["status"], "skip")
        self.assertIn("no ANE node in the device tree", result["evidence"][0] + " ".join(result["evidence"]))

    def test_an_unreadable_kernel_config_and_missing_firmware_are_said_plainly(self):
        found = FakeMac(ane=False).compute_snapshot()
        found["ane"]["config"], found["ane"]["firmware"], found["ane"]["modules_on_disk"] = None, [], None

        evidence = compute.ane_result(found)["evidence"]

        self.assertIn("kernel config: /proc/config.gz unavailable", evidence)
        self.assertIn("ANE firmware files: none under /usr/lib/firmware", evidence)
        self.assertIn("ANE and DART modules on disk: unknown", evidence)


class ReportShapeTest(unittest.TestCase):
    def test_evidence_fits_the_report_schema_however_much_the_mac_says(self):
        found = FakeMac().compute_snapshot()
        found["ane"]["modules"][0]["parameters"] = {f"parameter_{n}": "Y" * 30 for n in range(40)}
        found["ane"]["nodes"] = found["ane"]["nodes"] * 60
        found["ane"]["log"] = [f"ane {n} " + "x" * 900 for n in range(14)]

        for result in (compute.gpu_result(found), compute.ane_result(found)):
            self.assertLessEqual(len(result["evidence"]), 50)
            self.assertTrue(all(len(line) <= 500 for line in result["evidence"]))
        self.assertIn("more evidence lines left out", compute.ane_result(found)["evidence"][-1])

    def test_a_long_parameter_list_is_split_into_lines_that_keep_every_parameter(self):
        found = FakeMac().compute_snapshot()
        found["ane"]["modules"][0]["parameters"] = {f"p{n:02d}": "Y" for n in range(48)}

        lines = [line for line in compute.ane_evidence(found) if line.startswith("module ane_t6021 parameters:")]

        self.assertGreater(len(lines), 1)
        self.assertEqual(sum(line.count("=Y") for line in lines), 48)

    def test_the_new_checks_are_in_the_report_order_the_sections_and_the_catalogue(self):
        section = next(s for s in APPLE if s.id == "compute")
        cat = catalogue.parse(bundled.catalogue_text())

        self.assertEqual(section.check_ids, compute.CHECK_IDS)
        self.assertEqual(ORDER[-2:], compute.CHECK_IDS)
        self.assertEqual({cat.feature_for_check(c)["id"] for c in compute.CHECK_IDS}, {"gpu", "neural-engine"})
        self.assertEqual(len(ORDER), len(set(ORDER)))
        self.assertEqual([c for s in APPLE for c in s.check_ids if not c.startswith("benchmark")], list(ORDER))

    def test_the_snapshot_is_recorded_and_replayed_as_one_read(self):
        recorder = RecordingHost(FakeMac())

        found = recorder.compute_snapshot()

        recorded = [c for c in recorder.commands if c["argv"] == COMPUTE_SNAPSHOT]
        self.assertEqual(json.loads(recorded[0]["stdout"]), found)
        replay = RecordedHost({"recording_version": 1, "commands": recorded})
        self.assertEqual(replay.compute_snapshot(), found)

    def test_the_section_runs_through_a_recorded_host(self):
        for found, statuses in ((FakeMac().compute_snapshot(), ["pass", "pass"]), ({"apple": False}, ["skip", "skip"]), ({"apple": None}, ["skip", "skip"])):
            mac = RecordedHost({"recording_version": 1, "commands": [{"argv": COMPUTE_SNAPSHOT, "returncode": 0, "stdout": json.dumps(found), "stderr": ""}]})

            out = compute.run(context(mac))

            self.assertEqual([r["id"] for r in out], list(compute.CHECK_IDS))
            self.assertEqual([r["status"] for r in out], statuses)

    def test_scrubbed_evidence_keeps_no_host_name_or_path(self):
        found = FakeMac().compute_snapshot()
        scrubber = Scrubber()
        text = "\n".join(line for r in (compute.gpu_result(found), compute.ane_result(found)) for line in r["evidence"])

        self.assertNotIn("/home/", text)
        self.assertEqual(scrubber.scrub("kernel: ane_t6021 284000000.ane: loaded ane_t6021"), "kernel: ane_t6021 284000000.ane: loaded ane_t6021")


class RealMacTest(unittest.TestCase):
    """Snapshots taken on real Macs running a read-only run (a 13-inch M1 and a 16-inch M2 Max, Aurora kernel 7.1.12)."""

    def test_the_m1_has_the_ane_and_mlx_runs_on_its_gpu(self):
        found = fixture("compute-m1.json")

        out = results(found)

        self.assertEqual({k: v["status"] for k, v in out.items()}, {"gpu.mlx": "pass", "ane.enabled": "pass"})
        self.assertIn("device_name: Apple M1 (G13G B1), marketing_name: Apple M1, driver: Honeykrisp, driver_info: Mesa 26.3.0-devel (git-69bde15340), gpu_cores: 8", out["gpu.mlx"]["evidence"])
        self.assertIn("vulkaninfo --summary: missing", out["gpu.mlx"]["evidence"])
        self.assertIn("module ane parameters: autosuspend_ms=1500, boost_idle_ms=100, dart_contain=Y, map_batch=Y, map_mode=3, stats=Y", out["ane.enabled"]["evidence"])

    def test_the_m2_max_has_the_ane_with_its_settle_parameter_and_a_vulkan_device(self):
        found = fixture("compute-m2-max.json")

        out = results(found)

        self.assertEqual({k: v["status"] for k, v in out.items()}, {"gpu.mlx": "pass", "ane.enabled": "pass"})
        parameters = " ".join(line for line in out["ane.enabled"]["evidence"] if line.startswith("module ane_t6021 parameters:"))
        self.assertIn("call_settle_us=100", parameters)
        self.assertTrue(any(line.startswith("deviceName=Apple M2 Max (G14C B1)") for line in out["gpu.mlx"]["evidence"]))
        self.assertIn("kernel: asahi 406400000.gpu:   Type: G14C", out["gpu.mlx"]["evidence"])

    def test_the_real_m2_snapshot_with_the_driver_removed_reports_what_enabling_needs(self):
        found = copy.deepcopy(fixture("compute-m2-max.json"))
        found["ane"].update({"modules": [], "devices": [{"name": "ane", "driver": None, "bound": False, "iommu_group": "0"}], "dev_accel": None, "class_accel": None, "log": []})

        result = compute.ane_result(found)

        self.assertEqual(result["status"], "fail")
        self.assertIn("ANE and DART modules on disk: ane.ko, ane_t6021.ko", result["evidence"])
        self.assertIn("accelerator device /dev/accel: directory absent; /sys/class/accel: absent", result["evidence"])

    def test_no_real_snapshot_holds_a_host_name_a_user_name_or_a_path(self):
        for name in ("compute-m1.json", "compute-m2-max.json"):
            text = json.dumps(fixture(name))
            for forbidden in ("/home/", "jwm", "jw14", "warren", "joshua", "archlinux"):
                self.assertNotIn(forbidden, text.lower())


if __name__ == "__main__":
    unittest.main()
