"""Passive Touch ID diagnostics, privacy, legacy kernels and human unlock evidence."""

from __future__ import annotations

import copy
import json
import unittest

from omarchy_m_test import bundled, catalogue, presence, touchid
from omarchy_m_test.app import main
from omarchy_m_test.host import TOUCHID_SNAPSHOT, CommandResult, RealHost, Terminal
from omarchy_m_test.machine import Machine
from omarchy_m_test.privacy import Scrubber
from omarchy_m_test.recording import ENDED, RecordedHost, RecordingHost
from omarchy_m_test.sections import APPLE
from omarchy_m_test.session import Changes, Context
from tests.desktop import recording

NODE = touchid.PLATFORM + "/396400000.sep"
SENSOR = touchid.SPI + "/spi1.0/of_node"
GOOD = {
    "apple": True, "sep": True, "driver": True, "device": True,
    "xart_writes": True, "provision_keybag": False,
    "firmware": {"asahi,system-fw-version": "26.6.1", "asahi,iboot1-version": "mBoot-18000.161.10",
                 "asahi,os-fw-version": "13.5"},
    "diag": {"abi": "1", "profile": "T6020/J414s", "boot": "warm", "protocol": "variant5",
             "attach": "attached", "endpoints": 7, "keystore": "open", "keybag": "present",
             "sensor": "online", "touchid": "ready", "xart": "enabled"},
    "log_available": True, "log": [], "calibration_bytes": 512,
    "packages": {"libfprint": "1.94.100-1"}, "services": {"fprintd.service": "inactive"},
}


class PassiveHost(RealHost):
    """Synthetic sysfs/DT tree. Any attempted secret read or mutating command fails."""

    def __init__(self):
        self.files = {
            "/proc/device-tree/compatible": b"apple,j416c\0apple,t6021\0",
            NODE + "/of_node/compatible": b"apple,sep\0",
            SENSOR + "/compatible": b"apple,mesa-fingerprint\0",
            SENSOR + "/status": b"okay\0",
            touchid.PARAMETERS + "/xart_writes": b"Y\n",
            touchid.PARAMETERS + "/provision_keybag": b"N\n",
            **{NODE + "/diag/" + k: str(v).encode() + b"\n" for k, v in GOOD["diag"].items()},
            **{touchid.CHOSEN + "/" + k: v.encode() + b"\0" for k, v in GOOD["firmware"].items()},
        }
        self.dirs = {touchid.PLATFORM: ["396400000.sep"], NODE + "/driver": [], touchid.SPI: ["spi1.0"]}
        self.reads, self.commands = [], []
        self.log = "kernel: apple_sep 396400000.sep: attach: 7 endpoints advertised in 8 messages\n"
        self.log_exit = 0

    def read_file(self, path):
        self.reads.append(path)
        if any(part in path for part in ("os_uuid", "apfs-preboot", "serial", "mesa_calibration.bin", "/var/lib/")):
            raise AssertionError("attempt to read private data: " + path)
        if path not in self.files:
            raise FileNotFoundError(path)
        value = self.files[path]
        if isinstance(value, OSError):
            raise value
        return value

    def list_dir(self, path):
        if path not in self.dirs:
            raise FileNotFoundError(path)
        value = self.dirs[path]
        if isinstance(value, OSError):
            raise value
        return value

    def run(self, argv):
        self.commands.append(list(argv))
        if argv[:2] != ["timeout", "3"]:
            raise AssertionError("unbounded command: " + str(argv))
        command = argv[2:]
        if command == ["sh", "-c", "test -c /dev/sep-bio"]:
            return CommandResult(0, "", "")
        if command[0] == "systemctl" and command[1] == "show":
            return CommandResult(0, "inactive\n", "")
        if command[:2] == ["pacman", "-Q"]:
            return CommandResult(1, "libfprint 1.94.100-1\nfprintd 1.94.5-2\nother-user private\n", "private error")
        if command[0] == "stat":
            return CommandResult(0, "512\n", "")
        if command[0] in ("journalctl", "dmesg"):
            return CommandResult(self.log_exit, self.log, "private error")
        raise AssertionError("unexpected command: " + str(argv))


def host(found=None, answers=(), terminal=False, env=None):
    rec = {"recording_version": 1, "commands": [{"argv": TOUCHID_SNAPSHOT, "returncode": 0,
            "stdout": json.dumps(GOOD if found is None else found), "stderr": ""}], "env": env or {}}
    return RecordedHost(rec, answers=list(answers), terminal_size=Terminal(80, 24) if terminal else None)


def context(mac):
    found = catalogue.parse(bundled.catalogue_text())
    machine = Machine("Apple MacBook Pro (16-inch, M2 Max, 2023)", "j416c", "t6021", "M2 Max", "aarch64", "7.1.12", ("apple,j416c", "apple,t6021"))
    ctx = Context(mac, machine, found, Changes(mac))
    ctx.cache[presence.CACHE_KEY] = presence.Presence(False, True)
    return ctx


class PassiveDiagnosticsTest(unittest.TestCase):
    def test_reads_only_cached_state_and_allowlisted_provenance(self):
        mac = PassiveHost()
        found = mac.touchid_snapshot()
        self.assertEqual(found["diag"], GOOD["diag"])
        self.assertEqual(found["firmware"]["asahi,system-fw-version"], "26.6.1")
        self.assertEqual(found["calibration_bytes"], 512)
        self.assertTrue(found["sensor_node"])
        self.assertEqual(found["packages"], {"libfprint": "1.94.100-1", "fprintd": "1.94.5-2"})
        self.assertEqual(touchid.ready(found)["status"], "pass")
        self.assertNotIn("other-user", json.dumps(found))
        self.assertNotIn("private error", json.dumps(found))
        self.assertTrue(all(command[2] not in ("modprobe", "fprintd-verify", "fprintd-enroll", "fprintd-list") for command in mac.commands))

    def test_actual_sepos_is_not_inferred_from_stub_or_profile(self):
        found = copy.deepcopy(GOOD)
        found["firmware"] = {"asahi,os-fw-version": "13.5"}
        result = touchid.attachment(found)
        self.assertEqual(result["status"], "pass")
        self.assertIn("stub firmware (not running sepOS): 13.5", result["evidence"])
        self.assertIn("actual sepOS version/build: unavailable (not exposed by the kernel)", result["evidence"])

    def test_iboot_is_usable_provenance_when_system_version_unknown(self):
        mac = PassiveHost()
        mac.files[touchid.CHOSEN + "/asahi,system-fw-version"] = b"unknown\0"
        found = mac.touchid_snapshot()
        self.assertIsNone(found["firmware"]["asahi,system-fw-version"])
        self.assertEqual(touchid.attachment(found)["status"], "pass")
        found["diag"] = {}
        self.assertEqual(touchid.attachment(found)["status"], "skip")

    def test_cold_m1_profile_and_warm_m2_profile_are_driver_properties(self):
        for profile, boot, protocol in (("T8103/J313", "cold", "sepos13"), ("T6000/J316s", "cold", "sepos13"),
                                       ("T6020/J414s", "warm", "variant5"), ("T8112/J415", "warm", "variant5")):
            with self.subTest(profile=profile):
                mac = PassiveHost()
                for name, value in (("profile", profile), ("boot", boot), ("protocol", protocol)):
                    mac.files[NODE + "/diag/" + name] = value.encode()
                found = mac.touchid_snapshot()
                self.assertEqual(touchid.ready(found)["status"], "pass")
                self.assertEqual(found["diag"]["profile"], profile)

    def test_lab_m1_air_abi1_reading_passes_with_later_advertised_endpoints(self):
        # J313, already activated on the 11.25 candidate at 821603affb27;
        # confirmed on released 11.25 at 1fa2e36e8bf1.
        diag = {
            "abi": "1", "profile": "T8103/J313", "boot": "cold", "protocol": "sepos13",
            "xart": "enabled", "attach": "attached", "endpoints": 12, "keystore": "open",
            "keybag": "present", "sensor": "online", "touchid": "ready",
        }
        mac = PassiveHost()
        node = touchid.PLATFORM + "/242400000.sep"
        mac.files = {k.replace(NODE, node): v for k, v in mac.files.items() if not k.startswith(touchid.CHOSEN + "/")}
        mac.dirs = {k.replace(NODE, node): v for k, v in mac.dirs.items()}
        mac.dirs[touchid.PLATFORM] = ["242400000.sep"]
        mac.files["/proc/device-tree/compatible"] = b"apple,j313\0apple,t8103\0"
        for name, value in diag.items():
            mac.files[node + "/diag/" + name] = str(value).encode() + b"\n"
        mac.log = "kernel: apple_sep 242400000.sep: attach: 7 endpoints advertised in 8 messages\n"
        found = mac.touchid_snapshot()
        self.assertEqual(found["diag"], diag)
        self.assertEqual(touchid.attachment(found)["status"], "pass")
        result = touchid.ready(found)
        self.assertEqual(result["status"], "pass")
        self.assertIn("SEP endpoints: 12", result["evidence"])
        self.assertIn("a fingerprint match was not tested", result["evidence"][0])
        self.assertTrue(all(command[2] not in ("fprintd-list", "fprintd-verify", "fprintd-enroll") for command in mac.commands))

    def test_discovers_sep_by_compatible_not_mmio_address(self):
        mac = PassiveHost()
        new = touchid.PLATFORM + "/242400000.sep"
        mac.files = {k.replace(NODE, new): v for k, v in mac.files.items()}
        mac.dirs = {k.replace(NODE, new): v for k, v in mac.dirs.items()}
        mac.dirs[touchid.PLATFORM] = ["242400000.sep"]
        self.assertEqual(touchid.ready(mac.touchid_snapshot())["status"], "pass")

    def test_missing_permissions_malformed_and_future_values_are_unknown(self):
        for value in (b"new-state\n", b"ready\nprivate-name\n", b"\xff", b"a" * 300, PermissionError()):
            with self.subTest(value=value):
                mac = PassiveHost()
                mac.files[NODE + "/diag/touchid"] = value
                found = mac.touchid_snapshot()
                self.assertIsNone(found["diag"]["touchid"])
                self.assertEqual(touchid.ready(found)["status"], "skip")

    def test_future_abi_is_not_interpreted_as_readiness(self):
        mac = PassiveHost()
        mac.files[NODE + "/diag/abi"] = b"2\n"
        found = mac.touchid_snapshot()
        self.assertEqual(found["diag"], {"abi": "2"})
        self.assertEqual(touchid.ready(found)["status"], "skip")

    def test_old_kernel_keeps_passive_evidence_without_inventing_pass(self):
        mac = PassiveHost()
        mac.files.pop(NODE + "/diag/abi")
        result = touchid.ready(mac.touchid_snapshot())
        self.assertEqual(result["status"], "skip")
        self.assertIn("legacy evidence does not prove readiness", result["evidence"][0])
        self.assertIn("/dev/sep-bio present: yes", result["evidence"])

    def test_no_sep_no_mac_and_unavailable_device_tree_skip(self):
        for found in ({"apple": True, "sep": False}, {"apple": False}, {"apple": None}):
            with self.subTest(found=found):
                result = touchid.run(context(host(found)))
                self.assertEqual(result[1]["status"], "skip")
                self.assertEqual(result[2]["status"], "skip")
        mac = PassiveHost()
        mac.files["/proc/device-tree/compatible"] = b"other,board\0"
        self.assertEqual(mac.touchid_snapshot(), {"apple": False})
        self.assertEqual(mac.commands, [])

    def test_unbound_disabled_and_unreadable_platform_state_skip(self):
        for field, value in (("sep", None), ("node_status", True)):
            found = copy.deepcopy(GOOD)
            found[field] = value
            self.assertEqual(touchid.ready(found)["status"], "skip")
        mac = PassiveHost()
        mac.dirs[touchid.PLATFORM] = PermissionError()
        self.assertIsNone(mac.touchid_snapshot()["sep"])

    def test_missing_keybag_and_read_only_are_setup_skips(self):
        for field, value in (("keybag", "missing"), ("xart", "disabled")):
            found = copy.deepcopy(GOOD)
            found["diag"][field] = value
            self.assertEqual(touchid.ready(found)["status"], "skip")

    def test_explicit_initialization_failures_fail(self):
        for field, value in (("attach", "failed"), ("keystore", "closed"), ("keybag", "failed"), ("sensor", "failed")):
            with self.subTest(field=field):
                found = copy.deepcopy(GOOD)
                found["diag"][field] = value
                self.assertEqual(touchid.ready(found)["status"], "fail")

    def test_final_touchid_failure_survives_collection_and_other_healthy_states(self):
        for sensor in ("bound", "online", "unbound"):
            with self.subTest(sensor=sensor):
                mac = PassiveHost()
                mac.files[NODE + "/diag/touchid"] = b"failed\n"
                mac.files[NODE + "/diag/sensor"] = sensor.encode() + b"\n"
                found = mac.touchid_snapshot()
                self.assertEqual(found["diag"]["touchid"], "failed")
                self.assertEqual(touchid.attachment(found)["status"], "pass")
                result = touchid.ready(found)
                self.assertEqual(result["status"], "fail")
                self.assertIn("failed for this boot", result["evidence"][0])
                self.assertIn("SEP touchid: failed", result["evidence"])

    def test_intentionally_unprovisioned_final_activation_failure_skips(self):
        for field, value in (("xart", "disabled"), ("keybag", "missing")):
            with self.subTest(field=field):
                found = copy.deepcopy(GOOD)
                found["diag"].update({"touchid": "failed", field: value})
                found["provision_keybag"] = False
                result = touchid.ready(found)
                self.assertEqual(result["status"], "skip")
                self.assertTrue("read-only" in result["evidence"][0] or "setup is required" in result["evidence"][0])

    def test_explicit_boot_failures_are_not_hidden_by_an_unbound_sensor(self):
        for field, value in (("attach", "failed"), ("keystore", "closed"), ("keybag", "failed")):
            with self.subTest(field=field):
                found = copy.deepcopy(GOOD)
                found["diag"].update({"sensor": "unbound", field: value})
                found["device"] = False
                self.assertEqual(touchid.ready(found)["status"], "fail")

    def test_not_ready_alone_and_unbound_sensor_are_not_regressions(self):
        for state in ({"touchid": "not-ready"}, {"sensor": "unbound"}, {"touchid": "not-ready", "sensor": "unbound"}):
            with self.subTest(state=state):
                found = copy.deepcopy(GOOD)
                found["diag"].update(state)
                self.assertEqual(touchid.ready(found)["status"], "skip")

    def test_setup_skip_reasons_take_precedence_over_not_ready_and_stale_failures(self):
        for field, value in (("keybag", "missing"), ("xart", "disabled")):
            found = copy.deepcopy(GOOD)
            found["diag"].update({field: value, "touchid": "not-ready", "sensor": "failed"})
            self.assertEqual(touchid.ready(found)["status"], "skip")

    def test_m2_keybag_failure_keeps_system_firmware_compatibility_context(self):
        found = copy.deepcopy(GOOD)
        found["diag"]["keybag"] = "failed"
        found["firmware"]["asahi,system-fw-version"] = "26.2"
        result = touchid.ready(found)
        self.assertEqual(result["status"], "fail")
        self.assertTrue(any("firmware compatibility" in line for line in result["evidence"]))
        found["firmware"]["asahi,system-fw-version"] = None
        found["firmware"]["asahi,os-fw-version"] = "13.5"
        self.assertFalse(any("firmware compatibility" in line for line in touchid.ready(found)["evidence"]))

    def test_idle_fprintd_service_is_informational_not_a_failure(self):
        self.assertEqual(touchid.ready(GOOD)["status"], "pass")
        self.assertIn("fprintd.service: inactive", touchid.ready(GOOD)["evidence"])

    def test_missing_biometric_device_does_not_pass(self):
        found = copy.deepcopy(GOOD)
        found["device"] = False
        self.assertEqual(touchid.ready(found)["status"], "skip")

    def test_kernel_event_categories_strip_all_private_content_and_are_bounded(self):
        mac = PassiveHost()
        mac.log = "\n".join(f"kernel: apple_sep 396400000.sep: sensor: private-user right-index-finger UUID-secret status -{i}" for i in range(25))
        found = mac.touchid_snapshot()
        self.assertEqual(len(found["log"]), 20)
        self.assertEqual(found["log"][-1], "SEP: sensor setup outcome (error -24)")
        for secret in ("private-user", "right-index-finger", "UUID-secret", "396400000"):
            self.assertNotIn(secret, json.dumps(found))

    def test_log_denied_or_timeout_is_unavailable_not_a_failure(self):
        for code in (1, 124, 127):
            mac = PassiveHost()
            mac.log_exit = code
            found = mac.touchid_snapshot()
            self.assertFalse(found["log_available"])
            self.assertEqual(found["log"], [])
            self.assertEqual(touchid.ready(found)["status"], "pass")

    def test_record_and_replay_contains_only_derived_snapshot_and_safe_kernel_events(self):
        mac = PassiveHost()
        mac.log = "kernel: apple_sep 396400000.sep: sensor: private-user right-index-finger UUID-secret\n"
        recorder = RecordingHost(mac)
        result = recorder.touchid_snapshot()
        recorder.run(["timeout", "3", "journalctl", "-k", "-b"])
        # Extra source capture, outside the derived snapshot, is also sanitized.
        recorder._keep(["journalctl", "--dmesg"], CommandResult(0, mac.log, ""))
        saved = recorder.recording(Scrubber())
        text = json.dumps(saved)
        for secret in ("private-user", "right-index-finger", "UUID-secret", "396400000"):
            self.assertNotIn(secret, text)
        replay = RecordedHost(saved)
        self.assertEqual(replay.touchid_snapshot(), result)
        self.assertEqual(saved["files"], {})
        self.assertEqual(saved["dirs"], {})


class TouchIdRunTest(unittest.TestCase):
    def test_normal_section_includes_three_registered_checks(self):
        rec = recording()
        rec["commands"] = [entry for entry in rec["commands"] if entry["argv"] != TOUCHID_SNAPSHOT]
        rec["commands"].append({"argv": TOUCHID_SNAPSHOT, "returncode": 0, "stdout": json.dumps(GOOD), "stderr": ""})
        mac = RecordedHost(rec, answers=["", ENDED])
        section = next(s for s in APPLE if s.id == "touch-id")
        self.assertEqual(main(["--dry-run"], mac, sections=(section,)), 0)
        checks = json.loads(mac.written["omarchy-m-test-report.json"])["checks"]
        self.assertEqual([check["id"] for check in checks], list(touchid.CHECK_IDS))
        self.assertEqual([check["status"] for check in checks], ["pass", "pass", "skip"])

    def test_unattended_and_ssh_never_prompt_or_authenticate(self):
        for terminal, env in ((False, {}), (True, {"SSH_CONNECTION": "remote"})):
            mac = host(terminal=terminal, env=env)
            checks = touchid.run(context(mac))
            self.assertEqual(checks[2]["status"], "skip")
            self.assertEqual(mac.transcript, [])
            self.assertEqual(mac.commands_run, [TOUCHID_SNAPSHOT])

    def test_unattended_final_activation_failure_is_reported_without_waiting(self):
        found = copy.deepcopy(GOOD)
        found["diag"].update({"sensor": "bound", "touchid": "failed"})
        found["device"] = False
        mac = host(found)
        result = touchid.run(context(mac))
        self.assertEqual([r["status"] for r in result], ["pass", "fail", "skip"])
        self.assertEqual(mac.slept, [])
        self.assertEqual(mac.transcript, [])
        self.assertEqual(mac.commands_run, [TOUCHID_SNAPSHOT])

    def test_existing_unlock_is_a_human_observation_with_no_authentication_command(self):
        for answer, status in (("y", "pass"), ("n", "fail"), ("s", "skip"), (ENDED, "skip")):
            with self.subTest(answer=answer):
                mac = host(answers=[answer], terminal=True)
                result = touchid.run(context(mac))[2]
                self.assertEqual((result["kind"], result["status"]), ("human", status))
                self.assertEqual(mac.commands_run, [TOUCHID_SNAPSHOT] * (2 if status in ("pass", "fail") else 1))
                self.assertEqual(mac.slept, [])

    def test_defaulted_unlock_is_unconfirmed(self):
        mac = host(answers=[""], terminal=True)
        result = touchid.run(context(mac))[2]
        self.assertTrue(result["answered_by_default"])

    def test_no_local_seat_skips_observation(self):
        mac = host(terminal=True)
        ctx = context(mac)
        ctx.cache[presence.CACHE_KEY] = presence.Presence(False, False)
        self.assertEqual(touchid.run(ctx)[2]["status"], "skip")
        self.assertEqual(mac.transcript, [])

    def test_unlock_observation_rereads_lazy_activation_without_starting_it(self):
        lazy = copy.deepcopy(GOOD)
        lazy["diag"].update({"sensor": "unbound", "touchid": "unknown"})
        class Activated(RecordedHost):
            def touchid_snapshot(self):
                self.reads += 1
                return lazy if self.reads == 1 else GOOD
        mac = Activated({"recording_version": 1}, answers=["y"], terminal_size=Terminal(80, 24))
        mac.reads = 0
        result = touchid.run(context(mac))
        self.assertEqual([r["status"] for r in result], ["pass", "pass", "pass"])
        self.assertEqual(mac.reads, 2)
        self.assertFalse(any("not backed" in line for line in result[2]["evidence"]))
        self.assertEqual(mac.commands_run, [])

    def test_a_desktop_without_builtin_touchid_skips_even_with_sep_diagnostics(self):
        from dataclasses import replace
        mac = host()
        ctx = context(mac)
        ctx = replace(ctx, machine=replace(ctx.machine, board="j473", soc="t8112", chip="M2"))
        result = touchid.run(ctx)
        self.assertEqual([r["status"] for r in result], ["pass", "skip", "skip"])

    def test_pending_state_settles_once_and_is_reread(self):
        class Settling(RecordedHost):
            def touchid_snapshot(self):
                self.reads += 1
                return pending if self.reads == 1 else GOOD
        pending = copy.deepcopy(GOOD)
        pending["diag"]["attach"] = "pending"
        mac = Settling({"recording_version": 1})
        mac.reads = 0
        checks = touchid.run(context(mac))
        self.assertEqual(checks[1]["status"], "pass")
        self.assertEqual(mac.slept, [2])
        self.assertEqual(mac.reads, 2)

    def test_persistently_pending_state_skips_after_bounded_settle(self):
        pending = copy.deepcopy(GOOD)
        pending["diag"]["attach"] = "pending"
        mac = host(pending)
        self.assertEqual(touchid.run(context(mac))[1]["status"], "skip")
        self.assertEqual(mac.slept, [2])

    def test_recording_retains_final_settled_snapshot(self):
        lazy = copy.deepcopy(GOOD)
        lazy["diag"].update({"attach": "pending", "touchid": "unknown"})
        class Settling(PassiveHost):
            def touchid_snapshot(self):
                return self.states.pop(0)
        mac = Settling()
        mac.states = [lazy, GOOD]
        recorder = RecordingHost(mac)
        self.assertEqual(recorder.touchid_snapshot(), lazy)
        self.assertEqual(recorder.touchid_snapshot(), GOOD)
        self.assertEqual(len(recorder.commands), 1)
        replay = RecordedHost(recorder.recording(Scrubber()))
        self.assertEqual(replay.touchid_snapshot(), GOOD)
