"""Spot checks of the GPU drivers against the specs they claim, not conformance runs.

  graphics.vulkan-spec  the conformance version the driver reports (0.0.0.0: no
                        conformant submission claimed) and the features the Vulkan
                        spec's Feature Requirements chapter makes mandatory for
                        the API version the device claims. A mandatory feature
                        missing fails.
  graphics.gl-spec      the limits GL and GLES contexts report, through EGL with no
                        surface, against the minimums in the state tables of the
                        version they claim (GL 4.6 core and compatibility, GLES
                        3.2; other versions aren't checked). A limit under its
                        minimum fails. A software rasteriser says nothing about the
                        GPU, so it's skipped.

These lists cover some rows of the specs, and a finding is something to look at;
they don't replace the Khronos conformance test suites.

The tables are from aurora-diag's compliance.py
(https://github.com/aurora-silicon/aurora-diag, MIT, Copyright (c) 2026 Aurora Silicon).
"""

from __future__ import annotations

import re

from . import packages
from .session import Context

VULKAN, GL = "graphics.vulkan-spec", "graphics.gl-spec"
VULKAN_TOOLS = "vulkan-tools"
CHECK_IDS = (VULKAN, GL)
NOT_READ = "not read in this run (it predates this check)"
SPOT = "a spot check of the spec, not a conformance run"
NO_CONFORMANCE = {"0.0.0 patch 0", "0.0.0"}

# Vulkan spec, Feature Requirements chapter: mandatory for a device claiming that version or later.
REQUIRED_VK = {
    (1, 1): ("multiview",),
    (1, 2): ("uniformBufferStandardLayout", "subgroupBroadcastDynamicId", "imagelessFramebuffer",
             "separateDepthStencilLayouts", "hostQueryReset", "timelineSemaphore", "shaderSubgroupExtendedTypes"),
    (1, 3): ("robustImageAccess", "inlineUniformBlock", "pipelineCreationCacheControl", "privateData",
             "shaderDemoteToHelperInvocation", "shaderTerminateInvocation", "subgroupSizeControl",
             "computeFullSubgroups", "synchronization2", "shaderZeroInitializeWorkgroupMemory",
             "dynamicRendering", "shaderIntegerDotProduct", "maintenance4", "bufferDeviceAddress",
             "vulkanMemoryModel", "vulkanMemoryModelDeviceScope"),
    (1, 4): ("dynamicRenderingLocalRead", "maintenance5", "maintenance6", "pushDescriptor", "indexTypeUint8",
             "shaderSubgroupRotate", "shaderFloatControls2", "shaderExpectAssume",
             "vertexAttributeInstanceRateDivisor", "bresenhamLines", "pipelineRobustness",
             "fullDrawIndexUint32", "imageCubeArray", "independentBlend", "sampleRateShading",
             "drawIndirectFirstInstance", "depthClamp", "depthBiasClamp", "samplerAnisotropy",
             "fragmentStoresAndAtomics", "shaderImageGatherExtended", "shaderInt16", "largePoints",
             "samplerYcbcrConversion", "storageBuffer16BitAccess", "variablePointers",
             "variablePointersStorageBuffer", "samplerMirrorClampToEdge", "scalarBlockLayout"),
}

# Minimums from the state tables of OpenGL 4.6 (core) and OpenGL ES 3.2.
GL46_MIN = {"MAX_TEXTURE_SIZE": 16384, "MAX_3D_TEXTURE_SIZE": 2048, "MAX_CUBE_MAP_TEXTURE_SIZE": 16384,
            "MAX_ARRAY_TEXTURE_LAYERS": 2048, "MAX_RENDERBUFFER_SIZE": 16384, "MAX_SAMPLES": 4,
            "MAX_COLOR_ATTACHMENTS": 8, "MAX_DRAW_BUFFERS": 8, "MAX_VERTEX_ATTRIBS": 16,
            "MAX_UNIFORM_BLOCK_SIZE": 16384, "MAX_TEXTURE_IMAGE_UNITS": 16,
            "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 80, "MAX_SHADER_STORAGE_BUFFER_BINDINGS": 8,
            "MAX_SHADER_STORAGE_BLOCK_SIZE": 1 << 27, "MAX_IMAGE_UNITS": 8,
            "MAX_COMPUTE_WORK_GROUP_INVOCATIONS": 1024, "MAX_COMPUTE_SHARED_MEMORY_SIZE": 32768,
            "MAX_GEOMETRY_OUTPUT_VERTICES": 256, "MAX_TESS_GEN_LEVEL": 64,
            "MAX_TRANSFORM_FEEDBACK_BUFFERS": 4, "MAX_TEXTURE_BUFFER_SIZE": 65536,
            "MAX_TEXTURE_LOD_BIAS": 2.0, "MAX_TEXTURE_MAX_ANISOTROPY": 16.0}
GLES32_MIN = {"MAX_TEXTURE_SIZE": 2048, "MAX_3D_TEXTURE_SIZE": 256, "MAX_CUBE_MAP_TEXTURE_SIZE": 2048,
              "MAX_ARRAY_TEXTURE_LAYERS": 256, "MAX_RENDERBUFFER_SIZE": 2048, "MAX_SAMPLES": 4,
              "MAX_COLOR_ATTACHMENTS": 4, "MAX_DRAW_BUFFERS": 4, "MAX_VERTEX_ATTRIBS": 16,
              "MAX_UNIFORM_BLOCK_SIZE": 16384, "MAX_TEXTURE_IMAGE_UNITS": 16,
              "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 96, "MAX_SHADER_STORAGE_BLOCK_SIZE": 1 << 27,
              "MAX_COMPUTE_WORK_GROUP_INVOCATIONS": 128, "MAX_COMPUTE_SHARED_MEMORY_SIZE": 16384,
              "MAX_GEOMETRY_OUTPUT_VERTICES": 256, "MAX_TESS_GEN_LEVEL": 64,
              "MAX_TEXTURE_BUFFER_SIZE": 65536, "MAX_TEXTURE_LOD_BIAS": 2.0}
LIMIT_NAMES = frozenset(GL46_MIN) | frozenset(GLES32_MIN)
CONTEXTS = (("gl_core", "OpenGL core", False), ("gl_compat", "OpenGL compatibility", False), ("gles", "OpenGL ES", True))


def required_for(api: tuple[int, int] | None) -> tuple[str, ...]:
    """The features mandatory for a device claiming this API version."""
    if api is None:
        return ()
    return tuple(name for version, names in sorted(REQUIRED_VK.items()) if version <= api for name in names)


def version_of(text: str | None) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", text or "")
    return (int(match[1]), int(match[2])) if match else None


def run(ctx: Context) -> list[dict]:
    found = ctx.host.hardware_snapshot("gpu-spec")
    if not found.get("observed"):
        return [_result(check_id, "skip", [NOT_READ]) for check_id in CHECK_IDS]
    extra: list[str] = []
    if found["vulkan"].get("not_installed"):
        ready = packages.temporary(ctx, [VULKAN_TOOLS], "The Vulkan spec check")
        extra = ready.evidence()
        if ready.ready:
            found = ctx.host.hardware_snapshot("gpu-spec")
    return [_vulkan(found["vulkan"], extra), _gl(found["gl"])]


def _vulkan(found: dict, extra: list[str] | None = None) -> dict:
    devices = found.get("devices") or []
    if not devices:
        return _result(VULKAN, "skip", [*(extra or []), found.get("missing") or "vulkaninfo found no GPU device"])
    evidence, status = list(extra or []), "pass"
    for device in devices:
        conformance = device["conformance"]
        claim = "no conformant submission claimed" if conformance in NO_CONFORMANCE or conformance is None else f"conformance version {conformance}"
        evidence.append(f"{device['name']}: Vulkan {device['api']}, {claim}, {device['checked']} mandatory features checked, {len(device['missing'])} missing")
        if device["missing"]:
            status = "fail"
            evidence.append(f"missing: {', '.join(device['missing'][:40])}")
    return _result(VULKAN, status, [*evidence, SPOT])


def _gl(found: dict) -> dict:
    contexts = found.get("contexts") or {}
    if not contexts:
        return _result(GL, "skip", [found.get("missing") or "no GL context could be made through EGL"])
    if all(context["path"] == "llvmpipe" for context in contexts.values()):
        return _result(GL, "skip", ["only a software rasteriser answered, which says nothing about the GPU"])
    evidence, status, checked = [], "pass", 0
    for key, words, es in CONTEXTS:
        context = contexts.get(key)
        if not context:
            evidence.append(f"{words}: no context")
            continue
        table, claim = _table(context["version"], es)
        through = f"{context['renderer'] or 'unknown renderer'}, {context['path']}"
        if table is None:
            evidence.append(f"{words}: {context['version']} ({through}); no minimums for this version, not checked")
            continue
        checked += 1
        below = [f"{name} {context['limits'].get(name)} (minimum {need})" for name, need in table.items()
                 if context["limits"].get(name) is None or context["limits"][name] < need]
        evidence.append(f"{words}: {claim} ({through}), {len(table)} limits checked, {len(below)} under the minimum")
        if below:
            status = "fail"
            evidence.append(f"under the minimum: {', '.join(below)}")
    if not checked:
        return _result(GL, "skip", [*evidence, "no context claims a version these minimums cover"])
    return _result(GL, status, [*evidence, SPOT])


def _table(version: str | None, es: bool) -> tuple[dict | None, str]:
    found = version_of(re.sub(r"^OpenGL ES ", "", version or ""))
    if es and found and found >= (3, 2):
        return GLES32_MIN, "ES 3.2"
    if not es and found and found >= (4, 6):
        return GL46_MIN, "4.6"
    return None, ""


def _result(check_id: str, status: str, evidence: list[str]) -> dict:
    return {"id": check_id, "kind": "automatic", "status": status, "evidence": evidence}
