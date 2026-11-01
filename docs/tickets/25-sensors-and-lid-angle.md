# 25: Sensors and the lid-angle observation

**What to build:** A new Sensors section checks the motion, lid-angle and light sensors automatically, with no tilting. The Sleep section's existing lid close also samples the lid angle, at no extra cost to the tester.

**Blocked by:** 22, 23

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [x] `sensors.motion`: accelerometer magnitude 9.8 m/s² ±15% at rest, gyro near 0; absent sensors say why
- [x] `sensors.lid-angle`: reading within 0–360°; skipped on boards without a lid
- [x] `sensors.light`: ambient light and colour temperature readable
- [x] Sleep's lid poll samples the lid angle; `sensors.lid-angle` records whether it fell while closing
- [x] Golden reports updated; verdicts and the lid watch tested on synthetic readings
- [ ] Real recordings, including a replayed lid close (the lab Macs were offline; the next `--record` run at a Mac captures them)
