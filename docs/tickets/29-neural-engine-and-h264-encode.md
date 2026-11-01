# 29: Neural Engine status and benchmark, H.264 encode

**What to build:** The Benchmarks section reads the Neural Engine's status without starting a workload, runs its benchmark only when the tool is already installed, and measures hardware H.264 encode when the kernel exposes an encoder.

**Blocked by:** 23

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [x] `engines.ane`: `/sys/class/accel` driver, generation, firmware and source, runtime power state, wedged/error counters; never starts a workload, boots firmware or resets the engine
- [x] `benchmark.ane`: runs `omarchy-ane-smoke` only when already installed (its workload is a small add program, not MobileNet, so a macOS number sits beside it only if a result names the same model); never a temporary package; skips with the reason otherwise
- [x] `benchmark.h264-encode`: ffmpeg hardware encode of the test card when an encoder device exists, fps as the score; skips with the reason otherwise
- [x] Golden reports updated; verdicts tested on synthetic results with and without the tool and encoder
- [ ] Real recordings (the lab Macs were offline; whether ffmpeg can drive the AVE through V4L2 M2M is unverified)
