# 23: Deferred-probe reasons and the new check ids

**What to build:** Every unbound node in the Hardware section says why it didn't bind, from the kernel's deferred-probe list. The catalogue gains every check id the spec adds, mapped to features, so the site's `Regressions` covers them with no new site code.

**Blocked by:** 22

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [ ] `devices_deferred` read through the host via `sudo -n` (skipped without it) and recorded
- [ ] Each unbound node in `hardware.drivers` evidence and the `inventory` block carries its deferral reason, scrubbed by the privacy allowlist
- [ ] Catalogue `checks` lists `thermal.*`, `sensors.*`, `storage.health`, `benchmark.storage`, `graphics.vulkan-spec`, `graphics.gl-spec`, `cpu.dvfs-ladder`, `engines.ane`, `benchmark.ane`, `benchmark.h264-encode`, each mapped to a feature
- [ ] Recording with a deferred device; golden reports updated
