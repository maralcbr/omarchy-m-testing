# 29: Neural Engine status and benchmark, H.264 encode

**What to build:** The Benchmarks section reads the Neural Engine's status without starting a workload, runs its benchmark only when the tool is already installed, and measures hardware H.264 encode when the kernel exposes an encoder.

**Blocked by:** 23

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [ ] `engines.ane`: `/sys/class/accel` driver, generation, firmware and source, runtime power state, wedged/error counters; never starts a workload, boots firmware or resets the engine
- [ ] `benchmark.ane`: runs `omarchy-ane-smoke` (MobileNetV1 FP16, the workload aurora-diag documents) only when already installed; never a temporary package; skips with the reason otherwise
- [ ] `benchmark.h264-encode`: ffmpeg hardware encode of the test card when an encoder device exists, fps as the score; skips with the reason otherwise
- [ ] Recordings with and without the ANE tool and encoder; golden reports updated
