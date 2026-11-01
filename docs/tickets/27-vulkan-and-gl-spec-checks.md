# 27: Vulkan and GL spec checks

**What to build:** The Graphics section reports the Vulkan conformance version and any features the spec makes mandatory for the claimed API version that the device doesn't support. It also compares GL 4.6 and GLES 3.2 limits with the spec minimums. These are spot checks, not a conformance suite run.

**Blocked by:** 23

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [ ] `graphics.vulkan-spec`: conformance version and missing mandatory features (1.1–1.4 tables, spec sections cited in the code) from `vulkaninfo --json`
- [ ] `graphics.gl-spec`: GL 4.6 core/compat and GLES 3.2 limits vs spec minimums through EGL surfaceless; other versions reported as not checked; path recorded as native, Zink or llvmpipe
- [ ] Tables credit aurora-diag's `compliance.py` (MIT)
- [ ] Recordings with a passing and a missing-feature device; golden reports updated
