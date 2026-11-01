# 30: Site: macOS column and capacity grouping

**What to build:** The site's benchmark page shows the macOS reference next to each Linux result for the same board, groups storage results by capacity, and shows each run's capacity.

**Blocked by:** 26, 28

**Status:** ready-for-agent

Parent spec: maralcbr/omarchy-m-testing#63

- [ ] Benchmark page gains a macOS column from `catalogue/macos-reference/`, with the conditions shown; GL and Vulkan show none
- [ ] `benchmark.storage` rows split by model, stack and capacity; "no macOS reference at this capacity" when it doesn't match
- [ ] `machine.storage` on run pages; reports without it accepted
- [ ] `BenchmarkScores::NAMES` covers the new benchmarks
- [ ] Seam B tests post the new golden reports, including one without `machine.storage`
