"""The Vulkan and GL spec checks: what crosses the host boundary, and the verdicts."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import unittest

from omarchy_m_test import bundled, gpu_spec, hwread
from omarchy_m_test.host import HARDWARE_SNAPSHOT, CommandResult
from tests.schema_validator import errors
from tests.test_core_checks import M2_MAX, answer, results, run
from tests.test_seam_a import SCHEMA

TEMP = "/tmp/omarchy-m-test-vulkan.AbC123"
ALL_14 = {name: True for name in gpu_spec.required_for((1, 4))}
GOOD_LIMITS = {**gpu_spec.GL46_MIN}


def profile(features: dict = ALL_14, api: object = "1.4.311", device_type: object = "VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU", conformance: object = None) -> dict:
    return {"capabilities": {"device": {
        "features": {"VkPhysicalDeviceFeatures": features, "VkPhysicalDeviceVulkan13Features": {"somethingElse": True, "dynamicRendering": features.get("dynamicRendering", False)}},
        "properties": {
            "VkPhysicalDeviceProperties": {"deviceName": "Apple M2 Max (G14C B1)", "apiVersion": api, "deviceType": device_type,
                                           "pipelineCacheUUID": "secret-uuid"},
            "VkPhysicalDeviceVulkan12Properties": {"conformanceVersion": conformance or {"major": 1, "minor": 4, "subminor": 1, "patch": 2}},
            "VkPhysicalDeviceIDProperties": {"deviceUUID": "secret-uuid"},
        }}}}


class Host:
    """vulkaninfo writing its profile file, and the GL probe."""

    def __init__(self, profiles=(), gl: CommandResult | None = None, vulkaninfo_exit: int | None = None, temp: str = TEMP):
        self.profiles, self.gl, self.vulkaninfo_exit, self.temp = list(profiles), gl or CommandResult(0, "{}", ""), vulkaninfo_exit, temp
        self.commands: list[list[str]] = []
        self.files: dict[str, bytes] = {}

    def run(self, argv):
        self.commands.append(list(argv))
        if argv[0] == "mktemp":
            return CommandResult(0, self.temp + "\n", "")
        if argv[0] == "sh":
            if self.vulkaninfo_exit is not None:
                return CommandResult(self.vulkaninfo_exit, "", "")
            if not self.profiles:
                return CommandResult(1, "", "no such device")
            self.files[f"VP_{argv[-1]}.json"] = json.dumps(self.profiles.pop(0)).encode()
            return CommandResult(0, "", "")
        if argv[:2] == ["rm", "-f"]:
            self.files.pop(argv[-1].rsplit("/", 1)[-1], None)
        return CommandResult(0, "", "")

    def list_dir(self, path):
        return sorted(self.files)

    def read_file(self, path):
        return self.files[path.rsplit("/", 1)[-1]]

    def run_bundled(self, name, args=()):
        assert name == "gl-spec"
        return self.gl


def rec_with(found: dict) -> dict:
    rec = copy.deepcopy(M2_MAX)
    answer(rec, [HARDWARE_SNAPSHOT, "gpu-spec"], stdout=json.dumps(found))
    return rec


def check(found: dict, check_id: str) -> dict:
    _, _, report = run(rec_with(found))
    assert errors(SCHEMA, report) == []
    return results(report)[check_id]


DEVICE = {"name": "Apple M2 Max (G14C B1)", "api": "1.4.311", "conformance": "1.4.1 patch 2", "checked": 53, "missing": []}
CONTEXT = {"version": "4.6 (Core Profile) Mesa 25.1", "renderer": "Apple M2 Max (G14C B1)", "path": "native", "limits": GOOD_LIMITS}
ES_CONTEXT = {"version": "OpenGL ES 3.2 Mesa 25.1", "renderer": "Apple M2 Max (G14C B1)", "path": "native", "limits": {**gpu_spec.GLES32_MIN}}
GOOD = {"observed": True, "vulkan": {"devices": [DEVICE]}, "gl": {"contexts": {"gl_core": CONTEXT, "gl_compat": CONTEXT, "gles": ES_CONTEXT}}}


class VulkanCollectTest(unittest.TestCase):
    def vulkan(self, *profiles) -> dict:
        return hwread._vulkan(Host(profiles))

    def test_it_reads_the_device_and_its_mandatory_features(self):
        device = self.vulkan(profile())["devices"][0]

        self.assertEqual(device["name"], "Apple M2 Max (G14C B1)")
        self.assertEqual(device["api"], "1.4.311")
        self.assertEqual(device["conformance"], "1.4.1 patch 2")
        self.assertEqual(device["checked"], len(gpu_spec.required_for((1, 4))))
        self.assertEqual(device["missing"], [])

    def test_a_vulkan_1_1_driver_reports_conformance_through_its_driver_properties(self):
        older = profile()
        properties = older["capabilities"]["device"]["properties"]
        properties["VkPhysicalDeviceDriverPropertiesKHR"] = properties.pop("VkPhysicalDeviceVulkan12Properties")

        self.assertEqual(self.vulkan(older)["devices"][0]["conformance"], "1.4.1 patch 2")

    def test_the_conformance_version_survives_the_recording_scrubber(self):
        from omarchy_m_test.privacy import Scrubber

        self.assertNotIn("<ip>", Scrubber().scrub(json.dumps(self.vulkan(profile()))))

    def test_a_missing_feature_is_named(self):
        features = {**ALL_14, "maintenance5": False}

        self.assertEqual(self.vulkan(profile(features))["devices"][0]["missing"], ["maintenance5"])

    def test_a_feature_counts_if_any_structure_reports_it(self):
        features = {**ALL_14, "dynamicRendering": True}

        self.assertEqual(self.vulkan(profile(features))["devices"][0]["missing"], [])

    def test_only_what_the_claimed_version_requires_is_checked(self):
        device = self.vulkan(profile({name: True for name in gpu_spec.required_for((1, 2))}, api="1.2.0"))["devices"][0]

        self.assertEqual((device["checked"], device["missing"]), (len(gpu_spec.required_for((1, 2))), []))

    def test_a_packed_api_version_is_decoded(self):
        packed = (1 << 22) | (4 << 12) | 311

        self.assertEqual(self.vulkan(profile(api=packed))["devices"][0]["api"], "1.4.311")

    def test_a_software_device_is_left_out(self):
        self.assertEqual(self.vulkan(profile(device_type="VK_PHYSICAL_DEVICE_TYPE_CPU"))["devices"], [])
        self.assertEqual(self.vulkan(profile(device_type=4))["devices"], [])

    def test_uuids_and_other_structures_dont_cross_the_boundary(self):
        self.assertNotIn("secret-uuid", json.dumps(self.vulkan(profile())))

    def test_the_temporary_directory_is_always_removed(self):
        host = Host([profile()])
        hwread._vulkan(host)

        self.assertEqual(host.commands[-1], ["rm", "-rf", "--", TEMP])

    def test_a_directory_mktemp_didnt_make_where_asked_is_refused(self):
        host = Host([profile()], temp="/home/tester")

        self.assertIn("temporary directory", hwread._vulkan(host)["missing"])
        self.assertFalse([argv for argv in host.commands if argv[0] == "rm"])

    def test_no_vulkaninfo_says_so(self):
        self.assertIn("isn't installed", hwread._vulkan(Host(vulkaninfo_exit=127))["missing"])

    def test_a_profile_that_isnt_json_is_ignored(self):
        host = Host([profile()])
        real = host.read_file
        host.read_file = lambda path: b"not json"

        self.assertEqual(hwread._vulkan(host)["devices"], [])
        host.read_file = real


class GlCollectTest(unittest.TestCase):
    def gl(self, found: object, returncode: int = 0) -> dict:
        return hwread._gl(Host(gl=CommandResult(returncode, json.dumps(found) if not isinstance(found, str) else found, "")))

    def test_it_keeps_the_versions_and_only_the_limits_it_compares(self):
        found = self.gl({"platform": "surfaceless", "gl_core": {"version": "4.6 Mesa", "renderer": "Apple M2 Max", "path": "native",
                                                                "limits": {"MAX_TEXTURE_SIZE": 16384, "SOMETHING_ELSE": 7}}, "gles": None})

        self.assertEqual(found["contexts"]["gl_core"]["limits"], {"MAX_TEXTURE_SIZE": 16384})
        self.assertNotIn("gles", found["contexts"])

    def test_a_probe_that_found_no_context_says_so(self):
        self.assertIn("no GL context", self.gl({"error": "no EGL platform worked"}, 1)["missing"])

    def test_a_probe_that_crashed_says_so(self):
        self.assertIn("gave no answer", self.gl("Segmentation fault", 139)["missing"])

    def test_the_probe_is_bundled_and_answers_in_json_without_egl(self):
        path = bundled.script_path("gl-spec").replace("run.sh", "glprobe.py")
        done = subprocess.run([sys.executable, "-I", path], capture_output=True, text=True, timeout=30, check=False)

        self.assertIsInstance(json.loads(done.stdout), dict)


class VerdictTest(unittest.TestCase):
    def test_a_complete_driver_passes_and_says_its_a_spot_check(self):
        found = check(GOOD, gpu_spec.VULKAN)

        self.assertEqual(found["status"], "pass")
        self.assertIn("conformance version 1.4.1 patch 2", found["evidence"][0])
        self.assertEqual(found["evidence"][-1], gpu_spec.SPOT)

    def test_a_missing_mandatory_feature_fails(self):
        found = check({**GOOD, "vulkan": {"devices": [{**DEVICE, "missing": ["maintenance5"]}]}}, gpu_spec.VULKAN)

        self.assertEqual(found["status"], "fail")
        self.assertIn("missing: maintenance5", found["evidence"])

    def test_no_conformant_submission_is_said_but_doesnt_fail(self):
        found = check({**GOOD, "vulkan": {"devices": [{**DEVICE, "conformance": "0.0.0 patch 0"}]}}, gpu_spec.VULKAN)

        self.assertEqual(found["status"], "pass")
        self.assertIn("no conformant submission claimed", found["evidence"][0])

    def test_no_vulkan_device_skips(self):
        found = check({**GOOD, "vulkan": {"missing": "vulkaninfo (vulkan-tools) isn't installed"}}, gpu_spec.VULKAN)

        self.assertEqual((found["status"], found["evidence"]), ("skip", ["vulkaninfo (vulkan-tools) isn't installed"]))

    def test_limits_at_their_minimums_pass(self):
        found = check(GOOD, gpu_spec.GL)

        self.assertEqual(found["status"], "pass")
        self.assertIn(f"OpenGL core: 4.6 (Apple M2 Max (G14C B1), native), {len(gpu_spec.GL46_MIN)} limits checked, 0 under the minimum", found["evidence"])

    def test_a_limit_under_its_minimum_fails_and_is_named(self):
        low = {**CONTEXT, "limits": {**GOOD_LIMITS, "MAX_TEXTURE_SIZE": 8192}}

        found = check({**GOOD, "gl": {"contexts": {"gl_core": low}}}, gpu_spec.GL)

        self.assertEqual(found["status"], "fail")
        self.assertIn("under the minimum: MAX_TEXTURE_SIZE 8192 (minimum 16384)", found["evidence"])

    def test_a_limit_the_driver_doesnt_report_fails(self):
        limits = {k: v for k, v in GOOD_LIMITS.items() if k != "MAX_SAMPLES"}

        self.assertEqual(check({**GOOD, "gl": {"contexts": {"gl_core": {**CONTEXT, "limits": limits}}}}, gpu_spec.GL)["status"], "fail")

    def test_a_version_without_a_table_isnt_checked(self):
        older = {**CONTEXT, "version": "4.1 Metal", "limits": {}}

        found = check({**GOOD, "gl": {"contexts": {"gl_core": older}}}, gpu_spec.GL)

        self.assertEqual(found["status"], "skip")
        self.assertTrue(any("not checked" in line for line in found["evidence"]))

    def test_a_software_rasteriser_says_nothing_about_the_gpu(self):
        software = {**CONTEXT, "path": "llvmpipe"}

        self.assertEqual(check({**GOOD, "gl": {"contexts": {"gl_core": software}}}, gpu_spec.GL)["status"], "skip")

    def test_zink_is_checked_and_named(self):
        found = check({**GOOD, "gl": {"contexts": {"gl_core": {**CONTEXT, "path": "zink"}}}}, gpu_spec.GL)

        self.assertEqual(found["status"], "pass")
        self.assertTrue(any("zink" in line for line in found["evidence"]))

    def test_a_run_that_predates_the_checks_skips(self):
        _, _, report = run(M2_MAX)

        for check_id in gpu_spec.CHECK_IDS:
            self.assertIn("not read in this run", results(report)[check_id]["evidence"][0])


class TablesTest(unittest.TestCase):
    def test_requirements_are_cumulative(self):
        self.assertIn("multiview", gpu_spec.required_for((1, 4)))
        self.assertNotIn("maintenance5", gpu_spec.required_for((1, 3)))
        self.assertEqual(gpu_spec.required_for(None), ())

    def test_every_limit_the_tables_compare_is_one_the_probe_reads(self):
        path = bundled.script_path("gl-spec").replace("run.sh", "glprobe.py")
        with open(path, encoding="utf-8") as f:
            probe = f.read()

        for name in gpu_spec.LIMIT_NAMES:
            self.assertIn(f'"{name}"', probe)


if __name__ == "__main__":
    unittest.main()
