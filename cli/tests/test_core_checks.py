"""Seam A: the core automatic checks, stack detection and the candidate set.

The whole CLI runs against the M1 and M2 recordings, and against variants of
them that change what one part of the Mac answers (the M2 before its offline
first-boot setup was rerun, no passwordless sudo, a reference distro, a
candidate image). Assertions are on the report and on what the human saw.
"""

from __future__ import annotations

import copy
import json
import os
import unittest

from omarchy_m_test.app import main
from omarchy_m_test.consent import ACCEPT_PROMPT
from omarchy_m_test.recording import ENDED, RecordedHost
from omarchy_m_test.host import CommandResult
from omarchy_m_test.packages import install_command
from omarchy_m_test.sections import APPLE
from tests.desktop import recording
from tests.live_mac import LiveMac, MacState, live_recording
from tests.schema_validator import errors
from tests.test_seam_a import ENTER, RECORDINGS, REPORT_FILE, SCHEMA, golden, read

M2_MAX = json.loads(read(os.path.join(RECORDINGS, "m2-max-image2.json")))
M1_PRO = json.loads(read(os.path.join(RECORDINGS, "m1-pro-mx-mac.json")))
SECTIONS_BY_ID = {section.id: section for section in APPLE}

MAC_CHECK = ["bundled:mac-check"]
PACKAGES = next(c["argv"] for c in M2_MAX["commands"] if c["argv"][:2] == ["pacman", "-Q"])
FIRST_BOOT = ["journalctl", "--unit=omarchy-provision-hardware.service", "--output=short-iso", "--no-pager"]
FEDORA = 'NAME="Fedora Linux Asahi Remix"\nID=fedora\nVARIANT_ID=asahi\n'


def command(rec: dict, argv: list[str]) -> dict:
    return next(c for c in rec["commands"] if c["argv"] == argv)


def answer(rec: dict, argv: list[str], returncode=0, stdout="", stderr="") -> None:
    """Make the recorded Mac answer argv this way (adding the command if it isn't recorded)."""
    entry = {"argv": argv, "returncode": returncode, "stdout": stdout, "stderr": stderr}
    rec["commands"] = [c for c in rec["commands"] if c["argv"] != argv] + [entry]


def edit_lines(rec: dict, argv: list[str], change) -> None:
    entry = command(rec, argv)
    entry["stdout"] = "".join(line + "\n" for line in change(entry["stdout"].splitlines()))


def run(rec: dict) -> tuple[int, RecordedHost, dict]:
    mac = RecordedHost(copy.deepcopy(rec), answers=[ENTER, ENDED])
    status = main(["--dry-run"], mac)
    report = json.loads(mac.written[REPORT_FILE])
    return status, mac, report


def results(report: dict) -> dict[str, dict]:
    return {check["id"]: check for check in report["checks"]}


def m2_before_the_rerun() -> dict:
    """The M2 Max right after its offline first boot: vulkan.sh failed and vulkan-asahi never got installed."""
    rec = copy.deepcopy(M2_MAX)
    edit_lines(rec, PACKAGES, lambda lines: [line for line in lines if not line.startswith("vulkan-asahi ")])
    command(rec, PACKAGES)["stderr"] += "error: package 'vulkan-asahi' was not found\n"
    rec["dirs"]["/usr/share/vulkan/icd.d"] = []
    edit_lines(rec, FIRST_BOOT, lambda lines: [line for line in lines if line < "2026-09-26T08:40"])
    return rec


def reference(rec: dict) -> dict:
    """The same Mac running a reference distro: no pacman, no Omarchy."""
    rec = copy.deepcopy(rec)
    answer(rec, PACKAGES, 127, stderr="pacman: command not found\n")
    answer(rec, ["omarchy-version"], 127, stderr="omarchy-version: command not found\n")
    rec["files"]["/etc/os-release"] = {"text": FEDORA}
    return rec


class GoldenRunsTest(unittest.TestCase):
    def test_the_m2_max_on_the_converged_image(self):
        status, mac, report = run(M2_MAX)

        self.assertEqual(status, 0)
        self.assertEqual(mac.written[REPORT_FILE], golden("m2-max-image2"))
        self.assertEqual(errors(SCHEMA, report), [])
        system = report["system"]
        self.assertEqual((system["stack"], system["distro"], system["boot_loader"], system["encryption"]),
                         ("converged", "omarchy", "limine", "on"))
        self.assertIn({"name": "omarchy-mac", "version": "0.1.0-5.361571887310001"}, system["packages"])
        self.assertNotIn("candidate_set", system)  # image 2's target record names no set
        self.assertIn("(Omarchy, converged image)", mac.output)

    def test_every_hardware_area_is_checked(self):
        _, _, report = run(M2_MAX)

        prefixes = {check["id"].split(".")[0] for check in report["checks"]}
        self.assertEqual(prefixes, {"system", "boot", "packages", "setup", "hardware", "gpu", "video", "display", "audio", "network", "sleep", "input", "sep", "touch-id", "camera", "ports", "power", "cpu", "benchmark"})
        humans = {check["id"] for check in report["checks"] if check["kind"] == "human"}
        self.assertEqual(humans, {"display.notch-bar", "display.brightness-steps", "display.cursor",
                                  "audio.speaker-tone", "audio.headphone-detection", "network.bluetooth-pairing",
                                  "input.keyboard-light-follows-room", "input.function-keys", "input.trackpad-gestures",
                                  "touch-id.unlock", "camera.image", "ports.devices-work", "ports.external-display-picture"})

    def test_the_offline_first_boot_failure_on_the_m2_is_a_failure_even_after_the_rerun(self):
        _, mac, report = run(M2_MAX)

        setup = results(report)["setup.first-boot-hardware"]
        self.assertEqual(setup["status"], "fail")
        self.assertEqual(setup["classification"]["outcome"], "fails")
        self.assertEqual(setup["classification"]["layer"], "omarchy")
        self.assertIn("Deferred hardware step failed: install/hardware/vulkan.sh", setup["evidence"][0])
        # The journal line on its own line, as the journal has it: the scrubber only finds a hostname there.
        self.assertEqual(setup["evidence"][-2], "a later run completed:")
        self.assertTrue(setup["evidence"][-1].split()[1] == "<hostname>", setup["evidence"][-1])
        self.assertIn("FAIL  setup.first-boot-hardware  doesn't work, but should on this Mac (First-boot hardware setup)", mac.output)

    def test_mac_check_lines_become_classified_results_with_their_lines_as_evidence(self):
        _, mac, report = run(M2_MAX)

        found = results(report)
        self.assertEqual(found["boot.chain"]["evidence"], ["PASS  boot-check       running linux-aurora 7.1.12-2-2-ARCH; installed boot files match"])
        self.assertEqual(found["boot.chain"]["classification"]["feature"], "boot-chain")
        self.assertEqual(found["network.wifi-backend"]["classification"]["outcome"], "works")
        self.assertEqual(len(found["boot.files"]["evidence"]), 3)
        self.assertEqual(found["display.outputs"]["classification"]["feature"], "main-display")
        self.assertNotIn("migrations", json.dumps(report))  # software state, not a hardware result
        self.assertEqual(mac.commands_run.count(MAC_CHECK), 1)

    def test_omarchy_macs_audio_and_display_checks_run_read_only_on_the_converged_image(self):
        _, mac, report = run(M2_MAX)

        self.assertIn(["bundled:apple-audio-check", "--no-sound"], mac.commands_run)
        self.assertIn(["bundled:apple-display-check", "--read-only"], mac.commands_run)
        found = results(report)
        self.assertEqual(found["display.notch-strip"]["evidence"], ["ok - appledrm is showing the notch strip"])
        self.assertEqual(found["audio.speaker-dsp"]["classification"]["outcome"], "works")

    def test_the_m1_pro_on_mx_mac(self):
        status, mac, report = run(M1_PRO)

        self.assertEqual(status, 0)
        self.assertEqual(mac.written[REPORT_FILE], golden("m1-pro-mx-mac"))
        system = report["system"]
        self.assertEqual((system["stack"], system["boot_loader"], system["encryption"]), ("mx-mac", "grub", "off"))
        found = results(report)
        # mx-mac lacks two pieces of the converged integration mac-check expects.
        self.assertEqual(found["setup.vendor-firmware"]["classification"]["outcome"], "fails")
        self.assertEqual(found["network.wifi-backend"]["classification"]["outcome"], "fails")
        # No deferred first-boot setup, no encryption: not tested, never failed.
        self.assertEqual(found["setup.first-boot-hardware"]["classification"]["outcome"], "not-tested")
        self.assertEqual(found["boot.encryption"]["classification"]["outcome"], "not-tested")
        # omarchy-mac's audio and display scripts target the converged image.
        self.assertNotIn(["bundled:apple-audio-check", "--no-sound"], mac.commands_run)
        self.assertEqual(found["audio.microphone-mapping"]["status"], "skip")
        self.assertIn("converged image", found["audio.microphone-mapping"]["evidence"][0])


class MissingPackagesTest(unittest.TestCase):
    def test_before_the_rerun_the_missing_vulkan_driver_fails_three_checks(self):
        _, mac, report = run(m2_before_the_rerun())

        found = results(report)
        self.assertEqual(errors(SCHEMA, report), [])
        self.assertEqual(found["packages.hardware"]["status"], "fail")
        self.assertEqual(found["packages.hardware"]["evidence"][0], "not installed: vulkan-asahi")
        self.assertEqual(found["gpu.vulkan"]["status"], "fail")
        self.assertIn("vulkan-asahi is not installed", found["gpu.vulkan"]["evidence"])
        self.assertEqual(found["gpu.vulkan"]["classification"]["outcome"], "fails")
        self.assertEqual(found["setup.first-boot-hardware"]["evidence"][-1], "no later run completed")
        self.assertNotIn({"name": "vulkan-asahi", "version": "1:26.2.3-1"}, report["system"]["packages"])
        self.assertEqual(found["gpu.driver"]["status"], "pass")  # the kernel driver is there regardless


class RootChecksTest(unittest.TestCase):
    def test_without_passwordless_sudo_root_checks_are_skipped_not_failed(self):
        rec = copy.deepcopy(M2_MAX)

        def no_sudo(lines):
            kept = [line for line in lines if " boot-file " not in line and " boot-check " not in line and " snapshots " not in line]
            at = next(i for i, line in enumerate(kept) if " boot-loader " in line)
            return kept[:at] + [
                "SKIP  boot-check       needs passwordless sudo",
                kept[at],
                "SKIP  boot-file        hashing the boot files needs passwordless sudo",
            ] + kept[at + 1:-1] + ["SKIP  snapshots        needs passwordless sudo", kept[-1]]
        edit_lines(rec, MAC_CHECK, no_sudo)

        status, mac, report = run(rec)

        self.assertEqual(status, 0)
        found = results(report)
        for check_id in ("boot.chain", "boot.files", "system.snapshots"):
            self.assertEqual(found[check_id]["status"], "skip", check_id)
            self.assertEqual(found[check_id]["classification"]["outcome"], "not-tested")
            self.assertIn("passwordless sudo", found[check_id]["evidence"][0])
        # Nothing ran as root: sudo was only asked whether it has cached credentials (the OpenGL check's and the
        # benchmarks' temporary packages, then the charge limit's probe).
        self.assertEqual([argv for argv in mac.commands_run if argv[0] == "sudo"], [["sudo", "-n", "true"]] * 3)
        asked = [e[1] for e in mac.transcript if e[0] == "prompt"]
        # Never asks for a password: the disclaimer, then only the human checks' yes/no/skip questions.
        self.assertEqual([prompt for prompt in asked if not prompt.endswith(" [Y/n/s] ")], [ACCEPT_PROMPT])
        self.assertIn("passwordless sudo", mac.output)

    def test_when_mac_check_cant_run_its_checks_are_skipped_with_the_reason(self):
        rec = copy.deepcopy(M2_MAX)
        answer(rec, MAC_CHECK, 127, stderr="mac-check isn't installed next to omarchy-m-test\n")

        _, _, report = run(rec)

        found = results(report)
        self.assertEqual(found["network.wifi"]["status"], "skip")
        self.assertEqual(found["network.wifi"]["evidence"], ["mac-check didn't run: mac-check isn't installed next to omarchy-m-test"])
        self.assertEqual(report["system"]["boot_loader"], "unknown")
        self.assertEqual(found["gpu.driver"]["status"], "pass")


class ScriptFailureTest(unittest.TestCase):
    def test_a_failed_boot_file_fails_the_boot_files_check(self):
        rec = copy.deepcopy(M2_MAX)
        edit_lines(rec, MAC_CHECK, lambda lines: [
            "FAIL  boot-file        /boot/efi/m1n1/boot.bin is missing on a limine Mac" if line.endswith("/boot/efi/m1n1/boot.bin") else line
            for line in lines
        ])

        _, _, report = run(rec)

        files = results(report)["boot.files"]
        self.assertEqual(files["status"], "fail")
        self.assertEqual(len(files["evidence"]), 3)
        self.assertEqual(files["classification"]["outcome"], "fails")

    def test_when_the_audio_or_display_script_cant_run_their_checks_are_skipped(self):
        rec = copy.deepcopy(M2_MAX)
        answer(rec, ["bundled:apple-audio-check", "--no-sound"], 127, stderr="apple-audio-check isn't installed next to omarchy-m-test\n")
        answer(rec, ["bundled:apple-display-check", "--read-only"], 124, stderr="apple-display-check: timed out after 300s\n")

        _, _, report = run(rec)

        found = results(report)
        self.assertEqual(found["audio.speaker-dsp"]["evidence"], ["apple-audio-check didn't run: apple-audio-check isn't installed next to omarchy-m-test"])
        self.assertEqual(found["display.notch-strip"]["evidence"], ["apple-display-check didn't run: apple-display-check: timed out after 300s"])
        self.assertEqual({found[i]["status"] for i in ("audio.speaker-dsp", "display.notch-strip", "input.ambient-light")}, {"skip"})


class StackDetectionTest(unittest.TestCase):
    def test_a_checkout_without_omarchy_packages_is_legacy_omarchy_mac(self):
        rec = copy.deepcopy(M1_PRO)
        edit_lines(rec, PACKAGES, lambda lines: [line for line in lines if not line.startswith("omarchy")])
        answer(rec, ["omarchy-version"], 0, stdout="3.1.1\n")

        _, mac, report = run(rec)

        self.assertEqual(report["system"]["stack"], "legacy-omarchy-mac")
        self.assertIn("(Omarchy, legacy omarchy-mac)", mac.output)
        self.assertEqual(errors(SCHEMA, report), [])

    def test_omacoms_own_omarchy_dev_beside_omarchy_mac_is_still_converged(self):
        rec = copy.deepcopy(M2_MAX)
        edit_lines(rec, PACKAGES, lambda lines: [line.replace("omarchy ", "omarchy-dev ", 1) if line.startswith("omarchy ") else line for line in lines])

        _, mac, report = run(rec)

        self.assertEqual(report["system"]["stack"], "converged")
        self.assertIn(["bundled:apple-audio-check", "--no-sound"], mac.commands_run)

    def test_another_distro_is_a_reference_run_that_checks_only_the_hardware(self):
        _, mac, report = run(reference(M2_MAX))

        system = report["system"]
        self.assertEqual((system["stack"], system["distro"], system["packages"]), ("reference", "fedora", []))
        self.assertIn("(reference run on fedora)", mac.output)
        found = results(report)
        for check in report["checks"]:
            if check["classification"]["layer"] == "omarchy":
                self.assertEqual(check["status"], "skip", check["id"])
                # The recorded run is over SSH, so the Sleep section isn't run at all and says why.
                why = "skipped: running over SSH, where it could cut the connection" if check["id"].startswith("sleep.") else \
                    "reference run on fedora: Omarchy integration isn't checked"
                self.assertEqual(check["evidence"], [why])
        self.assertEqual(found["network.wifi"]["classification"]["outcome"], "works")
        self.assertEqual(found["cpu.frequency-scaling"]["classification"]["outcome"], "works")
        self.assertEqual(found["packages.hardware"]["status"], "skip")
        self.assertEqual(errors(SCHEMA, report), [])


class CandidateSetTest(unittest.TestCase):
    TARGET = "/var/lib/omarchy/image/target"
    BOOTED = "/var/lib/omarchy/image/target.booted"
    SEAL = "/var/lib/omarchy/factory-sealed"
    SET = "apple-test-1937418f520b-20260926"

    def candidate(self, target: str | None = None, booted: str | None = None, seal: str | None = None) -> dict:
        rec = copy.deepcopy(M2_MAX)
        for path, text in ((self.TARGET, target), (self.BOOTED, booted), (self.SEAL, seal)):
            rec["files"][path] = None if text is None else {"text": text}
        _, _, report = run(rec)
        self.assertEqual(errors(SCHEMA, report), [])
        return report["system"]

    def test_the_target_record_tags_the_run_with_its_candidate_set(self):
        record = f"format=1\nplatform=apple-silicon\ncandidate_set={self.SET}\n"

        self.assertEqual(self.candidate(target=record)["candidate_set"], self.SET)
        self.assertEqual(self.candidate(booted=record)["candidate_set"], self.SET)  # retired by the first boot

    def test_a_factory_reset_image_names_its_set_in_the_factory_seal(self):
        system = self.candidate(booted="format=1\nplatform=apple-silicon\n",
                                seal=f"format=2\ncandidate_set={self.SET}\ncandidate_source_commit=1937418f520b\n")

        self.assertEqual(system["candidate_set"], self.SET)

    def test_without_a_target_record_nothing_is_tagged(self):
        system = self.candidate(seal=f"format=2\ncandidate_set={self.SET}\n")

        self.assertNotIn("candidate_set", system)

    def test_a_malformed_tag_is_not_recorded(self):
        system = self.candidate(target="format=1\ncandidate_set=../../etc/passwd x\n")

        self.assertNotIn("candidate_set", system)


VULKANINFO = """\
Devices:
========
GPU0:
\tapiVersion         = 1.4.328
\tdriverVersion      = 26.2.3
\tdeviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
\tdeviceName         = Apple M2 Max (G14C B1)
\tdriverID           = DRIVER_ID_MESA_HONEYKRISP
\tdriverName         = Honeykrisp
\tdriverInfo         = Mesa 26.2.3
GPU1:
\tapiVersion         = 1.4.328
\tdeviceName         = llvmpipe (LLVM 21.1.0, 128 bits)
\tdriverID           = DRIVER_ID_MESA_LLVMPIPE
\tdriverName         = llvmpipe
"""
EGLINFO_SOFTWARE = """\
GBM platform:
EGL API version: 1.5
OpenGL core profile renderer: llvmpipe (LLVM 21.1.0, 128 bits)
OpenGL core profile version: 4.5 (Core Profile) Mesa 26.2.3
"""


class GpuTest(unittest.TestCase):
    def test_vulkan_and_opengl_versions_are_read_when_the_tools_are_installed(self):
        rec = copy.deepcopy(M2_MAX)
        answer(rec, ["vulkaninfo", "--summary"], 0, stdout=VULKANINFO)
        answer(rec, ["eglinfo", "-B"], 0, stdout=EGLINFO_SOFTWARE.replace("llvmpipe (LLVM 21.1.0, 128 bits)", "Apple M2 Max (G14C B1)")
               .replace("4.5 (Core Profile)", "4.6 (Core Profile)") + "OpenGL ES profile version: OpenGL ES 3.2 Mesa 26.2.3\n")

        _, _, report = run(rec)

        found = results(report)
        self.assertEqual(found["gpu.vulkan"]["evidence"], ["Apple M2 Max (G14C B1): Honeykrisp, Mesa 26.2.3, Vulkan 1.4.328"])
        self.assertEqual(found["gpu.opengl"]["status"], "pass")
        self.assertEqual(found["gpu.opengl"]["evidence"], [
            "renderer: Apple M2 Max (G14C B1)",
            "OpenGL core profile version: 4.6 (Core Profile) Mesa 26.2.3",
            "OpenGL ES profile version: OpenGL ES 3.2 Mesa 26.2.3",
        ])

    def test_software_rendering_is_a_gpu_failure(self):
        rec = copy.deepcopy(M2_MAX)
        answer(rec, ["eglinfo", "-B"], 0, stdout=EGLINFO_SOFTWARE)

        _, _, report = run(rec)

        opengl = results(report)["gpu.opengl"]
        self.assertEqual(opengl["status"], "fail")
        self.assertIn("llvmpipe", opengl["evidence"][0])
        self.assertEqual(opengl["classification"]["outcome"], "fails")

    def test_no_bound_gpu_driver_fails(self):
        rec = copy.deepcopy(M2_MAX)
        rec["dirs"]["/sys/bus/platform/drivers/asahi"] = None

        _, _, report = run(rec)

        self.assertEqual(results(report)["gpu.driver"]["evidence"], ["the asahi GPU driver isn't loaded"])


class OpenGlToolTest(unittest.TestCase):
    """No eglinfo (the M2 Max at its desk, 2026-09-27): mesa-utils is offered as a temporary test package."""

    class Installs(LiveMac):
        def run(self, argv):
            if list(argv) == ["eglinfo", "-B"] and "mesa-utils" in self.state.installed:
                self.commands_run.append(list(argv))
                return CommandResult(0, EGLINFO_SOFTWARE.replace("llvmpipe (LLVM 21.1.0, 128 bits)", "Apple M2 Max (G14C B1)"), "")
            return super().run(argv)

    def graphics(self, answers, state=None) -> LiveMac:
        host = self.Installs(live_recording(base=recording("m2-max-converged")), state=state, answers=[ENTER, *answers, ENDED])
        self.assertEqual(main(["--dry-run"], host, sections=(SECTIONS_BY_ID["graphics"],)), 0)
        return host

    def test_mesa_utils_is_installed_for_the_check_without_a_question_and_removed_after(self):
        host = self.graphics([])

        self.assertEqual([e[1] for e in host.transcript if e[0] == "prompt"][1:], [])
        self.assertIn("Installing 1 temporary test package(s)", host.output)

        found = results(json.loads(host.written[REPORT_FILE]))["gpu.opengl"]
        self.assertEqual(found["status"], "pass")
        self.assertEqual(found["evidence"], [
            "renderer: Apple M2 Max (G14C B1)", "OpenGL core profile version: 4.5 (Core Profile) Mesa 26.2.3",
            "installed for this run, removed at its end: mesa-utils",
        ])
        commands = host.commands_run
        self.assertLess(commands.index(install_command(["mesa-utils"])), commands.index(["sudo", "-n", "pacman", "-R", "--noconfirm", "mesa-utils"]))
        self.assertNotIn("mesa-utils", host.state.installed)

    def test_without_sudo_it_is_skipped_with_why(self):
        host = self.graphics([], state=MacState(sudo_cached=False))

        found = results(json.loads(host.written[REPORT_FILE]))["gpu.opengl"]
        self.assertEqual((found["status"], found["evidence"]),
                         ("skip", ["eglinfo (mesa-utils) isn't installed", "skipped: installing test packages needs sudo, and it wasn't given"]))
        self.assertFalse(any(argv[:4] == ["sudo", "-n", "pacman", "-S"] for argv in host.commands_run))

    def test_nothing_is_installed_when_it_would_upgrade_an_installed_package(self):
        state = MacState(repository={"mesa-utils": ["libdrm", "mesa-utils"]}, installed={"libdrm"}, outdated={"libdrm"})
        host = self.graphics([], state=state)

        found = results(json.loads(host.written[REPORT_FILE]))["gpu.opengl"]
        self.assertEqual(found["status"], "skip")
        self.assertIn("would upgrade installed packages (libdrm)", found["evidence"][-1])
        self.assertFalse(any(argv[:4] == ["sudo", "-n", "pacman", "-S"] for argv in host.commands_run))


def converged_m2() -> dict:
    rec = json.loads(read(os.path.join(RECORDINGS, "m2-max-converged.json")))
    rec["files"][f"{rec['env']['HOME']}/.local/state/omarchy-m-test/checkpoint.json"] = None
    return rec


class ConvergedFirstBootTest(unittest.TestCase):
    """The converged image runs the deferred hardware setup from omarchy-mac-first-boot, not its own unit."""

    def test_the_converged_m2s_first_boot_passes(self):
        _, _, report = run(converged_m2())

        setup = results(report)["setup.first-boot-hardware"]
        self.assertEqual(setup["status"], "pass")
        self.assertIn("omarchy-mac-first-boot runs the deferred hardware setup", setup["evidence"][0])
        self.assertIn("Finished Omarchy first boot", setup["evidence"][-2])
        self.assertEqual(setup["evidence"][-1], "nothing left queued (/var/lib/omarchy/image/deferred-steps is gone)")

    def test_first_boot_done_with_steps_still_queued_fails(self):
        """omarchy-mac-first-boot finishes (exit 0) when a step needs the network, leaving it queued for the unit."""
        rec = converged_m2()
        rec["files"]["/var/lib/omarchy/image/deferred-steps"] = {"text": "install/hardware/vulkan.sh\n"}
        edit_lines(rec, ["journalctl", "--unit=omarchy-mac-first-boot.service", "--output=short-iso", "--no-pager"], lambda lines: [
            *lines[:2], lines[1].replace("Creating this Mac's package keyring...", "Some hardware setup could not finish yet "
                                         "(see /var/log/omarchy/mac-first-boot.log and /var/log/omarchy-install.log); it is retried after first boot."),
            *lines[2:]])
        setup = results(run(rec)[2])["setup.first-boot-hardware"]
        self.assertEqual(setup["status"], "fail")
        self.assertIn("Some hardware setup could not finish yet", setup["evidence"][1])
        self.assertEqual(setup["evidence"][-1], "still queued in /var/lib/omarchy/image/deferred-steps: install/hardware/vulkan.sh")

    def test_an_old_hostname_in_first_boot_lines_is_still_scrubbed(self):
        rec = converged_m2()
        edit_lines(rec, ["journalctl", "--unit=omarchy-mac-first-boot.service", "--output=short-iso", "--no-pager"], lambda lines: [
            line.replace(" <hostname> ", " old-personal-host ").replace("Deactivated successfully.", "Failed with result 'exit-code'.")
            for line in lines])
        report = run(rec)[2]
        self.assertNotIn("old-personal-host", json.dumps(report))

    def test_a_first_boot_still_pending_or_failed_says_so(self):
        base = converged_m2()
        pending = copy.deepcopy(base)
        pending["files"]["/var/lib/omarchy/mac-first-boot/pending"] = {"text": ""}
        failed = copy.deepcopy(base)
        edit_lines(failed, ["journalctl", "--unit=omarchy-mac-first-boot.service", "--output=short-iso", "--no-pager"], lambda lines: [
            line.replace("Deactivated successfully.", "Failed with result 'exit-code'.") for line in lines if "Finished" not in line])
        for rec, status, said in ((pending, "skip", "first boot hasn't finished yet"), (failed, "fail", "Failed with result")):
            with self.subTest(status=status):
                setup = results(run(rec)[2])["setup.first-boot-hardware"]
                self.assertEqual(setup["status"], status)
                self.assertTrue(any(said in line for line in setup["evidence"]), setup["evidence"])

    def test_an_install_without_either_never_ran_it(self):
        setup = results(run(M1_PRO)[2])["setup.first-boot-hardware"]
        self.assertEqual(setup["evidence"], ["omarchy-provision-hardware.service never ran: this install has no deferred first-boot hardware setup"])


class PowerAndCpuTest(unittest.TestCase):
    def test_one_scaling_cluster_is_not_enough(self):
        rec = copy.deepcopy(M2_MAX)
        rec["dirs"]["/sys/devices/system/cpu/cpufreq"] = ["policy0"]

        _, _, report = run(rec)

        cpu = results(report)["cpu.frequency-scaling"]
        self.assertEqual(cpu["status"], "fail")
        self.assertIn("only one kind of core scales", cpu["evidence"][-1])

    def test_a_missing_battery_fails_on_a_laptop_and_is_not_applicable_on_a_mac_mini(self):
        laptop = copy.deepcopy(M2_MAX)
        laptop["dirs"]["/sys/class/power_supply"] = []
        mini = copy.deepcopy(laptop)
        mini["files"]["/proc/device-tree/compatible"] = {"text": "apple,j473\0apple,t8112\0apple,arm-platform\0"}
        mini["files"]["/proc/device-tree/model"] = {"text": "Apple Mac mini (M2, 2023)\0"}

        _, _, on_laptop = run(laptop)
        _, _, on_mini = run(mini)

        self.assertEqual(results(on_laptop)["power.battery"]["classification"]["outcome"], "fails")
        self.assertEqual(results(on_mini)["power.battery"]["classification"]["outcome"], "not-applicable")


class EveryVariantTest(unittest.TestCase):
    """Every SoC the catalogue lists is named, explained against its own chip and reported within the schema."""

    MACS = [
        ("j313", "t8103", "Apple MacBook Air (M1, 2020)", "M1"),
        ("j375d", "t6002", "Apple Mac Studio (M1 Ultra, 2022)", "M1 Ultra"),
        ("j473", "t8112", "Apple Mac mini (M2, 2023)", "M2"),
        ("j180d", "t6022", "Apple Mac Pro (M2 Ultra, 2023)", "M2 Ultra"),
        ("j613", "t8122", "Apple MacBook Air (13-inch, M3, 2024)", "M3"),
        ("j514s", "t6030", "Apple MacBook Pro (14-inch, M3 Pro, Nov 2023)", "M3 Pro"),
        ("j516m", "t6034", "Apple MacBook Pro (16-inch, M3 Max, Nov 2023)", "M3 Max"),
        ("j700", "t8140", "Apple MacBook Neo (2026)", "A18 Pro"),
    ]

    def test_each_mac_is_identified_and_its_report_is_valid(self):
        for board, soc, model, chip in self.MACS:
            with self.subTest(board=board):
                rec = copy.deepcopy(M2_MAX)
                rec["files"]["/proc/device-tree/compatible"] = {"text": f"apple,{board}\0apple,{soc}\0apple,arm-platform\0"}
                rec["files"]["/proc/device-tree/model"] = {"text": f"{model}\0"}

                _, _, report = run(rec)

                self.assertEqual((report["machine"]["board"], report["machine"]["chip"]), (board, chip))
                self.assertEqual(errors(SCHEMA, report), [])
                outcomes = {c["classification"]["outcome"] for c in report["checks"]}
                self.assertNotIn("unknown-hardware", outcomes)


if __name__ == "__main__":
    unittest.main()
