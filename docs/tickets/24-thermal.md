# 24: Thermal

**What to build:** A new read-only Thermal section reports whether Linux sees the Mac's temperatures and fans. Readings are evidence; presence is judged against the board's facts.

**Blocked by:** 22, 23

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [ ] `thermal.sensors`: hwmon channels (label, unit), thermal zones with trip points and governors, cooling devices; fails when no die temperature is readable
- [ ] `thermal.fans`: speed, target and limits; fails on a board with fans and none readable, skips on a fanless board
- [ ] Never writes to hwmon, thermal or fan attributes
- [ ] Section skippable with `--skip thermal` and checkpointed
- [ ] Recordings from a fanned and a fanless Mac; golden reports updated
