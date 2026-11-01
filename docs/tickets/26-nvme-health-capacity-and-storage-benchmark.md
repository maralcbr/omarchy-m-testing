# 26: NVMe health, capacity and the storage benchmark

**What to build:** A new Storage section reports SSD health without any disk identifier, every report records the SSD's capacity tier, and a short fio benchmark measures it on a temporary file.

**Blocked by:** 23

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [x] `storage.health`: model, firmware, available spare, percentage used, data read/written (TB), power cycles, power-on hours, unsafe shutdowns, media errors, critical warnings, temperature; fails on media errors above 0 or a critical warning
- [x] Serial, EUI-64, NGUID, namespace ids and exact byte capacity never reach the report; NVMe corpus case lists them as `forbidden`
- [x] `machine.storage` records the sales tier (`256GB`…`8TB`) rounded from the namespace size; report v1 schema, privacy allowlist and golden reports extended in place
- [x] `benchmark.storage`: fio as a temporary package, 1 GiB sequential write and read, 4K random read QD1 and QD32, direct I/O, file under `$XDG_CACHE_HOME/omarchy-m-test/`
- [x] The file's restorer is registered before it's created and runs on success, error and interruption; skipped under 4 GiB free; safety guard test shows no block device is opened
- [x] Evidence records root encryption and filesystem type
