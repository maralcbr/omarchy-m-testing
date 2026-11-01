# Build tickets

Tracer-bullet tickets for two builds: 01–21 for the spec maralcbr/omarchy-m-testing#1, and 22–30 for the hardware coverage spec maralcbr/omarchy-m-testing#63. One file per ticket, numbered in dependency order (blockers first). Each file names its blockers; a ticket can start when every blocker is done.

Working rules:
- Tick acceptance criteria in the ticket file as they're met.
- A ticket is done when all its criteria are ticked and its work is merged to `main`.
- When a build's last ticket is done, delete its ticket files and its table in one commit; when both builds are done, delete this whole folder. The tickets are build scaffolding, and their history stays in git.

## Spec #1: test CLI and compatibility site

| # | Ticket | Blocked by |
|---|---|---|
| 01 | Walking skeleton: one check end to end | none |
| 02 | Live on omarchy-m-testing.org with signed release and one-line installer | 01 |
| 03 | Recorded-Mac corpus and privacy scrubber | 01 |
| 04 | Inventory and gap map | 01, 03 |
| 05 | Feature catalogue v1 and classification | 01 |
| 06 | Core automatic checks | 03, 05 |
| 07 | Omarchy-vibes terminal UI | 01 |
| 08 | Site v0.1 pages | 01, 05 |
| 09 | Machine signing keys | 01 |
| 10 | v0.1 dogfood on the M1 and M2 | 02, 04, 06, 07, 08, 09 |
| 11 | Interactive framework and safety | 07 |
| 12 | Audio and display checks | 06, 11 |
| 13 | On-screen video check | 11 |
| 14 | Wi-Fi first join and Bluetooth | 11 |
| 15 | Sleep, lid and clamshell | 11 |
| 16 | Camera, input and ports | 11 |
| 17 | Graphics and video benchmarks | 08, 11 |
| 18 | Power | 11, 15 |
| 19 | Tester sign-in and candidate gating | 08, 09 |
| 20 | Regressions, Aurora support table and one-click issues | 05, 19 |
| 21 | Upstream packaging and move to omacom | 10, quattro-upstream landing upstream |

## Spec #63: hardware coverage, storage capacity and macOS reference numbers

| # | Ticket | Blocked by |
|---|---|---|
| 22 | Catalogue board list for every Apple silicon Mac | none |
| 23 | Deferred-probe reasons and the new check ids | 22 |
| 24 | Thermal | 22, 23 |
| 25 | Sensors and the lid-angle observation | 22, 23 |
| 26 | NVMe health, capacity and the storage benchmark | 23 |
| 27 | Vulkan and GL spec checks | 23 |
| 28 | macOS reference numbers and the DVFS ladder check | 22, 23, 26 |
| 29 | Neural Engine status and benchmark, H.264 encode | 23 |
| 30 | Site: macOS column and capacity grouping | 26, 28 |
