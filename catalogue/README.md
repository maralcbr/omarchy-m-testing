# catalogue

`catalogue.json` is the feature catalogue: what each hardware feature is expected to do on each Apple Silicon chip generation. The CLI explains every check result against it and records `catalogue_version` in the report; the site accepts only the check ids it lists and words each outcome the way it says.

## Layers

Each feature names its layer, so a failure goes to the right project:

1. `asahi`: Asahi hardware features, one per row of Asahi's feature tables. Per chip generation: Asahi's state (`upstream` with the kernel version, `linux-asahi`, `wip`, `tba`, `out-of-tree`, `absent` for "-", ...) with the original cell text, and Aurora's state. A few Asahi-layer features are hardware the tables have no row for (DisplayPort audio); they're marked `"asahi_tables": false`, kept by hand with Asahi's state `no`, and left alone by `asahi_tables.py`.
2. `aurora`: what the Aurora kernel (omacom/linux) adds: external displays over USB-C, VRR, the camera ISP, AOP. Where Aurora goes beyond Asahi on an Asahi feature (DP alt mode, Thunderbolt's USB4 host on M2), that feature's Aurora state says so.
3. `omarchy`: Omarchy integration: the notch bar, clamshell, the automatic keyboard light, speaker protection, the first 5 GHz join, first-boot hardware setup, the boot chain, disk encryption, package repositories, vendor firmware, system services, snapshots, the iwd Wi-Fi backend and the microphone mapping.

A chip generation's state can be overridden per board under `models` (e.g. the MacBook Pro 13" has no notch). Aurora and Omarchy features can point at the Asahi feature they depend on with `asahi_feature`; its Asahi state is used for them.

Aurora states: `supported` (with the Aurora kernel version), `asahi` (expected to match linux-asahi, not verified on its own), `unsupported`, `unknown`, `absent`. A state that was corrected from what a real Mac showed cites it in `evidence` (the kernel line and the corpus machine). Omarchy states: `supported`, `unsupported`, `absent`.

`checks` maps each check id to the feature it tests. `hardware` maps device-tree compatible strings (shell-style patterns such as `apple,t*-avd`) to the feature that hardware is: an enabled node no driver claimed counts as that feature failing, and a compatible the map doesn't know is unknown hardware.

## Asahi's reference kernel config

`asahi-kernel/config` is asahi-alarm's `linux-asahi/config`, pinned byte for byte at the commit in `asahi-kernel/source.json` (with its sha256). omarchy-m-test compares the running kernel's build options with it. To move the pin, fetch the file at a newer commit of [asahi-alarm/PKGBUILDs](https://github.com/asahi-alarm/PKGBUILDs), update `source.json` (commit, version from that commit's PKGBUILD, sha256, date) and rerun `cli/scripts/reseed_recordings.py`.

## Classification

A passed check `works`; a skipped one is `not-tested` (a missing human answer is never a failure), or `not-applicable` where the board lacks the hardware. A failed check is matched against Aurora's expected state first, then Asahi's:

- Aurora expects it to work: `fails` (or `not-in-omarchy` when Omarchy's integration isn't there yet). The site turns `fails` into a regression only against a verified earlier pass.
- Asahi has it but Aurora doesn't: `not-in-aurora`. Asahi doesn't have it either: `not-in-asahi`.
- The board lacks the hardware: `not-applicable`. The chip isn't in the catalogue: `unknown-hardware`. Unclaimed hardware the `hardware` map doesn't know is `unknown-hardware` too.

## Editing

Change the catalogue by pull request and bump `catalogue_version` on every change.

The Asahi layer is generated; don't edit it by hand:

    python3 catalogue/asahi_tables.py check           # against the pinned snapshot (CI, every push)
    python3 catalogue/asahi_tables.py check --live    # against Asahi's main branch (CI, weekly)
    python3 catalogue/asahi_tables.py update --live   # rewrite the Asahi layer, keeping Aurora and Omarchy fields

After `update --live`, refresh `asahi-snapshot/` from the same Asahi commit and set `sources.asahi.commit`. A new Mac model or table row stops the parser until it's added to `COLUMNS`, `MODEL_COLUMNS` or `ROWS` in `asahi_tables.py`. Try a draft with `omarchy-m-test --catalogue catalogue.json` or `--explain REPORT`.

## Credit

The Asahi layer and `asahi-snapshot/` come from the [Asahi Linux documentation](https://github.com/AsahiLinux/docs/tree/main/docs/platform/feature-support) feature-support tables, by the Asahi Linux contributors, licensed under [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). The tables were converted to per-chip and per-board states; each state keeps the original cell text in `cell`. Anywhere this data appears, credit Asahi Linux.
