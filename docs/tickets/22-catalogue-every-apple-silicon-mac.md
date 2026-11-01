# 22: Catalogue board list for every Apple silicon Mac

**What to build:** The catalogue names every Apple silicon Mac, not only the boards in Asahi's tables. Each board carries its id, marketing name, chip, SoC and hardware facts (fans, notch, camera, lid, battery). Boards Asahi doesn't list (M4 and later) get their features as not yet supported, so a run on one is explained instead of "unknown hardware".

**Blocked by:** none

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [x] Board list covering M1 through the current M-series families and the A18 Pro MacBook Neo, with sources cited in `catalogue.sources`
- [x] Hardware facts per board: fans, notch, camera, lid, battery
- [x] Missing chips and SoCs added to `chips`; boards absent from Asahi's tables classified as not yet supported
- [x] `--explain` and the site's model names read the board list; J616s explains instead of "unknown hardware"
- [x] Catalogue version bumped; golden reports and Seam A/B tests updated
