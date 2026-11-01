# 28: macOS reference numbers and the DVFS ladder check

**What to build:** A standard-library script run on macOS produces a reviewed reference file per board. The CLI shows each compared result's delta as evidence, and `cpu.dvfs-ladder` compares Linux's advertised CPU ladder with macOS's.

**Blocked by:** 22, 23, 26

**Status:** ready-for-human (needs the lab Macs booted into macOS)

Parent spec: maralcbr/omarchy-m-testing#63

- [ ] `scripts/macos-reference` captures, on AC with brightness recorded: CPU DVFS ladders, H.264/HEVC decode fps on the CLI's clip (VideoToolbox), idle power (sudo), the ANE MobileNet result when available, and the `benchmark.storage` fio job on APFS
- [ ] Writes `catalogue/macos-reference/<board>.json` (schema, version, macOS version and build, date, conditions, per-board metrics, storage keyed by capacity) after printing what it will save; no serial, hostname, user or volume names
- [ ] Unit tests over recorded macOS output; CI validates every committed reference file
- [ ] References committed for the lab M1 Pro and M2 Max, and J616s imported from aurora-diag (credited)
- [ ] `cpu.dvfs-ladder` compares cpufreq/OPP ladders with the reference; skips without one
- [ ] Each compared check gets a descriptive "vs macOS" evidence line, never a verdict; storage only at a matching capacity
