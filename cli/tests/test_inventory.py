"""Seam A: the hardware inventory and gap map, through the whole CLI.

The whole CLI runs against the M1 and M2 recordings, and against variants of
them that change what one part of the Mac answers (the device tree, which
drivers are bound, the kernel log, the kernel's build options, the
catalogue). Assertions are on the report's inventory block, the Hardware
section's results and what the human saw.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import unittest

from omarchy_m_test import inventory
from omarchy_m_test.app import main
from omarchy_m_test.inventory import DEVICES, KERNEL_CONFIG, KERNEL_LOG, NODE_PROPERTIES
from omarchy_m_test.recording import ENDED, INTERRUPT, RecordedHost
from tests.desktop import CHECKPOINT, SECTIONS, UNANSWERED, host as desktop_host, with_home, with_section_commands
from tests.schema_validator import errors
from tests.test_core_checks import M1_PRO, M2_MAX, answer, command, results, run
from tests.test_seam_a import ENTER, REPORT_FILE, SCHEMA, golden

HERE = os.path.dirname(os.path.abspath(__file__))
CATALOGUE = os.path.join(os.path.dirname(os.path.dirname(HERE)), "catalogue", "catalogue.json")
HARDWARE_CHECKS = ("hardware.drivers", "hardware.firmware", "hardware.probe-errors", "hardware.kernel-config")
DT = "/sys/firmware/devicetree/base"


def hardware(report: dict) -> dict[str, dict]:
    return {check_id: check for check_id, check in results(report).items() if check_id in HARDWARE_CHECKS}


def unknown_hardware(report: dict) -> list[tuple[str, int]]:
    return [(entry["compatible"], entry["count"]) for entry in report["inventory"]["unclaimed"] if entry["outcome"] == "unknown-hardware"]


def edit(rec: dict, argv: list[str], change) -> dict:
    rec = copy.deepcopy(rec)
    entry = command(rec, argv)
    entry["stdout"] = change(entry["stdout"])
    return rec


class GoldenInventoryTest(unittest.TestCase):
    def test_the_m2_max_maps_its_hardware_and_lists_what_no_driver_claims(self):
        status, mac, report = run(M2_MAX)

        self.assertEqual(status, 0)
        self.assertEqual(mac.written[REPORT_FILE], golden("m2-max-image2"))
        inventory = report["inventory"]
        # Nothing is unknown: the DisplayPort audio controllers have no driver, and no Asahi one exists yet.
        self.assertEqual(unknown_hardware(report), [])
        # The video decoder has a driver whose probe failed (its firmware is missing): the catalogue knows it.
        self.assertEqual(inventory["unclaimed"], [
            {"compatible": "apple,t6020-avd", "count": 1, "outcome": "fails", "feature": "video-decoder", "layer": "asahi"},
            {"compatible": "apple,t6020-dpaudio", "count": 2, "outcome": "not-in-asahi", "feature": "dp-audio", "layer": "asahi"},
        ])
        nodes = {(n["compatible"], n["status"], n["driver"]): n["count"] for n in inventory["nodes"]}
        self.assertEqual(nodes[("apple,agx-t6021", "okay", "bound")], 1)
        self.assertEqual(nodes[("apple,t6020-pmgr-pwrstate", "okay", "bound")], 235)
        self.assertEqual(nodes[("apple,t6020-aic", "okay", "none")], 1)  # set up by the kernel core, no device
        # The power manager's register blocks: no driver of their own, but their power domains have one. Not unclaimed.
        self.assertEqual(nodes[("apple,t6020-pmgr", "okay", "unbound")], 5)
        self.assertIn(("apple,t6020-avd", "okay", "unbound"), nodes)
        self.assertTrue(any(status == "disabled" for _, status, _ in nodes))

    def test_the_m2_maxs_hardware_results(self):
        _, mac, report = run(M2_MAX)

        found = hardware(report)
        self.assertEqual({k: (v["status"], v["classification"]["outcome"]) for k, v in found.items()}, {
            "hardware.drivers": ("fail", "not-in-aurora"),
            "hardware.firmware": ("fail", "fails"),
            "hardware.probe-errors": ("fail", "not-in-aurora"),
            "hardware.kernel-config": ("pass", "works"),
        })
        self.assertEqual(found["hardware.drivers"]["evidence"], [
            "424 hardware nodes: 366 claimed by a driver, 27 with no device of their own, "
            "5 bus or register containers with no driver of their own, 23 disabled, 3 unclaimed",
            "unclaimed: apple,t6020-avd (1 node): doesn't work, but should on this Mac (Video decoder)",
            "unclaimed: apple,t6020-dpaudio (2 nodes): not yet supported by Asahi (DisplayPort audio)",
        ])
        self.assertEqual(found["hardware.firmware"]["evidence"], [
            "avd 287080000.avd: Direct firmware load for apple/avd-fw-v3-t1.bin failed with error -2",
            "avd 287080000.avd: failed to load firmware: -2",
        ])
        self.assertEqual(found["hardware.probe-errors"]["evidence"], ["avd 287080000.avd: probe with driver avd failed with error -2"])
        self.assertIn("GAP   hardware.drivers  not yet supported by Aurora (A driver for every hardware node)", mac.output)

    def test_the_m2_maxs_kernel_build_options_against_asahis(self):
        _, _, report = run(M2_MAX)

        config = report["inventory"]["kernel_config"]
        self.assertEqual(config["reference"], "linux-asahi 7.1.13.asahi3-2 (asahi-alarm/PKGBUILDs@d585b07)")
        differences = {d["option"]: (d["asahi"], d["kernel"]) for d in config["differences"]}
        # Aurora's USB4 additions, built only here; the function tracer Asahi turned on after this kernel was built.
        for option in ("CONFIG_USB4", "CONFIG_USB4_APPLE_SOC", "CONFIG_RESET_APPLE_CIO"):
            asahi, ours = differences[option]
            self.assertEqual((asahi, ours in ("y", "m")), ("n", True), option)
        self.assertEqual(differences["CONFIG_FUNCTION_TRACER"], ("y", "n"))
        self.assertNotIn("CONFIG_CC_VERSION_TEXT", differences)  # strings and toolchain options aren't compared
        self.assertEqual(len(differences), 22)
        self.assertEqual(config["omitted"], 0)
        evidence = results(report)["hardware.kernel-config"]["evidence"]
        self.assertEqual(evidence[1], "22 options differ: 4 built here but not by Asahi, 15 built by Asahi but not here, 3 set differently")
        self.assertIn(f"CONFIG_USB4: Asahi n, this kernel {differences['CONFIG_USB4'][1]}", evidence)

    def test_the_m1_pro_on_mx_mac(self):
        status, mac, report = run(M1_PRO)

        self.assertEqual(status, 0)
        self.assertEqual(mac.written[REPORT_FILE], golden("m1-pro-mx-mac"))
        self.assertEqual(unknown_hardware(report), [])
        self.assertEqual(report["inventory"]["unclaimed"], [
            {"compatible": "apple,t6000-dpaudio", "count": 1, "outcome": "not-in-asahi", "feature": "dp-audio", "layer": "asahi"},
        ])
        found = hardware(report)
        # The M1's recording has no kernel log: not tested, never failed.
        self.assertEqual(found["hardware.firmware"]["classification"]["outcome"], "not-tested")
        self.assertEqual(found["hardware.probe-errors"]["classification"]["outcome"], "not-tested")
        # linux-asahi built before Asahi's reference turned the function tracer on; nothing built here that Asahi doesn't.
        differences = {d["option"]: (d["asahi"], d["kernel"]) for d in report["inventory"]["kernel_config"]["differences"]}
        self.assertEqual(differences["CONFIG_FUNCTION_TRACER"], ("y", "n"))
        self.assertFalse([o for o, (asahi, ours) in differences.items() if ours in "ym" and asahi not in "ym"])

    def test_the_m1_pro_on_the_converged_image_lists_only_what_really_lacks_a_driver(self):
        rec = json.loads(open(os.path.join(HERE, "recordings", "m1-pro-converged.json"), encoding="utf-8").read())
        rec["files"][f"{rec['env']['HOME']}/.local/state/omarchy-m-test/checkpoint.json"] = None
        status, mac, report = run(rec)

        self.assertEqual(status, 0)
        self.assertEqual(mac.written[REPORT_FILE], golden("m1-pro-converged"))
        # The real M1: its video decoder's firmware is missing. The CPU frequency clusters, the
        # architected timer and U-Boot's SMBIOS node have unbound devices, by design: not unclaimed.
        self.assertEqual(report["inventory"]["unclaimed"], [
            {"compatible": "apple,t6000-avd", "count": 1, "outcome": "fails", "feature": "video-decoder", "layer": "asahi"},
        ])
        nodes = {(n["compatible"], n["status"], n["driver"]): n["count"] for n in report["inventory"]["nodes"]}
        self.assertEqual(nodes[("apple,t6000-cluster-cpufreq", "okay", "unbound")], 3)
        self.assertEqual(results(report)["cpu.frequency-scaling"]["status"], "pass")
        self.assertEqual(hardware(report)["hardware.drivers"]["evidence"][0],
                         "361 hardware nodes: 310 claimed by a driver, 31 with no device of their own, "
                         "5 that no driver binds by design (CPU frequency clusters, timer, SMBIOS), 14 disabled, 1 unclaimed")

    def test_soc_specific_cpu_frequency_clusters_are_unbound_by_design(self):
        # The MacBook Neo's device tree names only apple,t8140-cluster-cpufreq; the M3 names t8112's.
        from omarchy_m_test.inventory import Node
        for compatibles in (("apple,t8140-cluster-cpufreq",), ("apple,t8122-cluster-cpufreq", "apple,t8112-cluster-cpufreq")):
            node = Node(compatibles, "okay", "unbound")
            self.assertTrue(node.unbound_by_design)
            self.assertFalse(node.unclaimed)
        self.assertTrue(Node(("apple,t8140-dart",), "okay", "unbound").unclaimed)

    def test_the_sep_is_unclaimed_only_when_the_kernel_has_no_on_request_module_for_it(self):
        # apple-sep has no module alias: Omarchy loads it when Touch ID is turned on (the Touch ID section checks it).
        from omarchy_m_test.inventory import Node, _mark_opt_in, _state
        sep, dart = Node(("apple,sep",), "okay", "unbound"), Node(("apple,t8140-dart",), "okay", "unbound")
        for config, opt_in in (({"CONFIG_APPLE_SEP": "m"}, True), ({"CONFIG_APPLE_SEP": "y"}, False), ({}, False), (None, False)):
            with self.subTest(config=config):
                marked_sep, marked_dart = _mark_opt_in([sep, dart], config)
                self.assertEqual((marked_sep.opt_in, marked_sep.unclaimed), (opt_in, not opt_in))
                self.assertEqual(_state(marked_sep), "opt-in" if opt_in else "unbound")
                self.assertTrue(marked_dart.unclaimed)
        bound = _mark_opt_in([Node(("apple,sep",), "okay", "bound")], {"CONFIG_APPLE_SEP": "m"})[0]
        self.assertEqual(_state(bound), "bound")

    def test_the_m2_max_on_the_converged_image_has_a_driver_for_every_node(self):
        rec = json.loads(open(os.path.join(HERE, "recordings", "m2-max-converged.json"), encoding="utf-8").read())
        rec["files"][f"{rec['env']['HOME']}/.local/state/omarchy-m-test/checkpoint.json"] = None
        status, mac, report = run(rec)

        self.assertEqual(status, 0)
        self.assertEqual(mac.written[REPORT_FILE], golden("m2-max-converged"))
        # The real M2 on image 4: the video decoder's firmware is there now, and nothing is unclaimed.
        self.assertEqual(report["inventory"]["unclaimed"], [])
        found = hardware(report)
        self.assertEqual({k: v["status"] for k, v in found.items()}, {
            "hardware.drivers": "pass", "hardware.firmware": "pass", "hardware.probe-errors": "pass", "hardware.kernel-config": "pass",
        })
        # The display coprocessors' Type-C routes waited in the initramfs for the display crossbar's module
        # (mux_apple_display_crossbar, on the root filesystem); every one of them is bound now, so it's said, not failed.
        evidence = found["hardware.probe-errors"]["evidence"]
        self.assertEqual(evidence[0], "7 deferred probe(s) pending early in this boot, bound to their drivers since "
                                      "(waiting for a module the initramfs didn't carry):")
        self.assertIn("since bound: platform 315c00000.dcp: deferred probe pending: apple-dcp: /soc/dcp@315c00000/typec-routes/route@0: "
                      "failed to get display crossbar", evidence)

    def test_a_deferred_probe_whose_device_is_still_unbound_fails(self):
        rec = json.loads(open(os.path.join(HERE, "recordings", "m2-max-converged.json"), encoding="utf-8").read())
        rec["files"][f"{rec['env']['HOME']}/.local/state/omarchy-m-test/checkpoint.json"] = None
        for entry in rec["commands"]:
            if entry["argv"] == inventory.DEVICES:
                entry["stdout"] = entry["stdout"].replace("/sys/devices/platform/soc/315c00000.dcp/uevent:DRIVER=apple-dcp\n", "")
        status, mac, report = run(rec)

        found = hardware(report)["hardware.probe-errors"]
        self.assertEqual(found["status"], "fail")
        self.assertEqual(found["evidence"][0], "platform 315c00000.dcp: deferred probe pending: apple-dcp: "
                                               "/soc/dcp@315c00000/typec-routes/route@0: failed to get display crossbar")
        self.assertEqual(found["evidence"][1], "6 deferred probe(s) pending early in this boot, bound to their drivers since "
                                               "(waiting for a module the initramfs didn't carry):")


class PrivacyTest(unittest.TestCase):
    def test_the_inventory_holds_only_node_types_statuses_and_driver_states(self):
        for rec in (M2_MAX, M1_PRO):
            _, _, report = run(rec)
            inventory = report["inventory"]
            with self.subTest(machine=report["machine"]["board"]):
                self.assertEqual(set(inventory), {"nodes", "unclaimed", "kernel_config"})
                for node in inventory["nodes"]:
                    self.assertEqual(set(node), {"compatible", "status", "driver", "count"})
                text = json.dumps({k: inventory[k] for k in ("nodes", "unclaimed")})
                self.assertNotIn("/", text)  # no node path
                self.assertNotIn("@", text)  # no unit address

    def test_other_property_values_never_reach_the_report(self):
        serial = "C02ZZ1ABCDEF"

        def add_properties(stdout: str) -> str:
            return stdout + (
                f"{DT}/soc/gpu@406400000/serial-number:{serial}\0\n"
                f"{DT}/chosen/compatible:apple,{serial}\0\n"  # a subtree the inventory skips
                f"{DT}/soc/weird@1/compatible:has spaces and \"quotes\"\0\n"
            )

        _, mac, report = run(edit(M2_MAX, NODE_PROPERTIES, add_properties))

        self.assertNotIn(serial, mac.written[REPORT_FILE])
        self.assertNotIn("quotes", mac.written[REPORT_FILE])
        self.assertEqual(errors(SCHEMA, report), [])

    def test_kernel_log_evidence_is_scrubbed(self):
        line = "Sep 26 10:00:00 kestrels-mac kernel: brcmfmac 0000:01:00.0: Direct firmware load for brcm/brcmfmac4388-pcie.apple,kestrels-mac.bin failed with error -2 (fe80::1c2b:3d4e:5f60:7a8b)"
        rec = edit(M2_MAX, KERNEL_LOG, lambda stdout: stdout + line + "\n")
        rec["files"]["/proc/sys/kernel/hostname"] = {"text": "kestrels-mac\n"}

        _, mac, report = run(rec)

        evidence = results(report)["hardware.firmware"]["evidence"]
        self.assertIn("brcmfmac 0000:01:00.0: Direct firmware load for brcm/brcmfmac4388-pcie.apple,<hostname>.bin failed with error -2 (<ip>)", evidence)
        self.assertNotIn("kestrels-mac", mac.written[REPORT_FILE])


class GapMapTest(unittest.TestCase):
    def test_hardware_the_catalogue_doesnt_know_is_unknown_hardware(self):
        with open(CATALOGUE, encoding="utf-8") as f:
            catalogue = json.load(f)
        del catalogue["hardware"]["apple,t*-avd"]
        rec = copy.deepcopy(M2_MAX)
        rec["files"]["draft.json"] = {"text": json.dumps(catalogue)}
        mac = RecordedHost(rec, answers=[ENTER, ENDED])

        main(["--dry-run", "--catalogue", "draft.json"], mac)

        report = json.loads(mac.written[REPORT_FILE])
        self.assertEqual(unknown_hardware(report), [("apple,t6020-avd", 1)])

    def test_a_driver_bound_everywhere_leaves_nothing_unclaimed(self):
        def bind_all(stdout: str) -> str:
            lines = stdout.splitlines()
            devices = [line.split("/uevent:")[0] for line in lines if "/uevent:OF_FULLNAME=" in line]
            return "\n".join(lines + [f"{device}/uevent:DRIVER=bound" for device in devices]) + "\n"

        _, _, report = run(edit(M2_MAX, DEVICES, bind_all))

        self.assertEqual(report["inventory"]["unclaimed"], [])
        self.assertEqual(results(report)["hardware.drivers"]["status"], "pass")
        self.assertNotIn("unbound", {node["driver"] for node in report["inventory"]["nodes"]})

    def test_a_clean_kernel_log_passes(self):
        def clean(stdout: str) -> str:
            return "".join(line + "\n" for line in stdout.splitlines() if "avd" not in line)

        _, _, report = run(edit(M2_MAX, KERNEL_LOG, clean))

        found = hardware(report)
        self.assertEqual(found["hardware.firmware"]["status"], "pass")
        self.assertEqual(found["hardware.firmware"]["evidence"], ["no firmware-load failures in this boot's kernel log"])
        self.assertEqual(found["hardware.probe-errors"]["status"], "pass")


class UnreadableTest(unittest.TestCase):
    def test_without_a_device_tree_or_device_list_the_drivers_check_is_skipped(self):
        for argv in (NODE_PROPERTIES, DEVICES):
            with self.subTest(argv=argv[-1]):
                rec = copy.deepcopy(M2_MAX)
                answer(rec, argv, returncode=2, stderr="grep: permission denied\n")

                status, _, report = run(rec)

                self.assertEqual(status, 0)
                self.assertEqual(results(report)["hardware.drivers"]["status"], "skip")
                self.assertNotIn("nodes", report["inventory"])
                self.assertIn("kernel_config", report["inventory"])
                self.assertEqual(errors(SCHEMA, report), [])

    def test_without_proc_config_the_build_options_are_not_compared(self):
        rec = copy.deepcopy(M2_MAX)
        answer(rec, KERNEL_CONFIG, returncode=1, stderr="gzip: /proc/config.gz: No such file or directory\n")

        _, _, report = run(rec)

        self.assertEqual(results(report)["hardware.kernel-config"]["status"], "skip")
        self.assertEqual(results(report)["hardware.kernel-config"]["evidence"], ["the running kernel's build options aren't readable (/proc/config.gz)"])
        self.assertNotIn("kernel_config", report["inventory"])
        self.assertEqual(errors(SCHEMA, report), [])

    def test_skipping_the_hardware_section_leaves_the_inventory_out(self):
        mac = RecordedHost(copy.deepcopy(M2_MAX), answers=[ENTER, ENDED])

        main(["--dry-run", "--skip", "hardware"], mac)

        report = json.loads(mac.written[REPORT_FILE])
        self.assertNotIn("inventory", report)
        self.assertEqual({c["status"] for c in hardware(report).values()}, {"skip"})
        self.assertNotIn(DEVICES, mac.commands_run)
        self.assertEqual(errors(SCHEMA, report), [])


class ReferenceConfigTest(unittest.TestCase):
    def test_the_pinned_asahi_config_is_the_file_its_source_names(self):
        base = os.path.join(os.path.dirname(CATALOGUE), "asahi-kernel")
        with open(os.path.join(base, "source.json"), encoding="utf-8") as f:
            source = json.load(f)
        with open(os.path.join(base, "config"), "rb") as f:
            config = f.read()

        self.assertEqual(hashlib.sha256(config).hexdigest(), source["sha256"])
        self.assertIn(f"# Linux/arm64 {source['version'].split('.asahi')[0]} Kernel Configuration", config.decode())


class ResumeTest(unittest.TestCase):
    def test_a_resumed_run_keeps_the_inventory_without_mapping_again(self):
        first = desktop_host(with_section_commands(with_home(M2_MAX)), answers=[ENTER, *UNANSWERED, INTERRUPT])
        main(["--dry-run"], first, sections=SECTIONS)
        second = desktop_host(with_section_commands(with_home(M2_MAX, checkpoint=first.written[CHECKPOINT])), answers=[ENTER, ENTER, "y"])

        main(["--dry-run"], second, sections=SECTIONS)

        self.assertNotIn(DEVICES, second.commands_run)
        self.assertEqual(json.loads(second.written[REPORT_FILE])["inventory"], json.loads(golden("m2-max-image2"))["inventory"])


if __name__ == "__main__":
    unittest.main()
