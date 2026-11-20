This project moved: Omarchy Mac Lab is at https://lab.omarchy-mac.org, its source at https://github.com/omacom/omarchy-maclab.

# omarchy-m-testing

Test every hardware feature of your Apple Silicon Mac under Omarchy, and share the results at [omarchy-m-testing.org](https://omarchy-m-testing.org).

## Install

On an Apple Silicon Mac running Omarchy (or another Linux):

    curl -fsSL https://omarchy-m-testing.org/install | bash

The installer downloads the latest tagged release, checks its signature and installs `omarchy-m-test` under `~/.local/share/omarchy-m-test`, linked from `~/.local/bin`. Run it again to upgrade. It needs `curl`, `tar`, `python3` and `ssh-keygen` (OpenSSH), all on a stock Omarchy install. The tool tells you before each run when a newer release exists.

Releases are signed with this key (`release/allowed_signers`, also pinned in `installer/install.sh`):

    release@omarchy-m-testing.org namespaces="omarchy-m-test-release" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKHA9ywzf4hxAd5Ohzw8tRg5i2lxHAxp2mDV0PWi5o0u

To check a download by hand:

    ssh-keygen -Y verify -f release/allowed_signers -I release@omarchy-m-testing.org \
      -n omarchy-m-test-release -s omarchy-m-test.tar.gz.sig < omarchy-m-test.tar.gz

## Layout

- `cli/`: the `omarchy-m-test` command.
- `site/`: the omarchy-m-testing.org site and API.
- `schema/`: the versioned report schema and the golden reports both are tested against.
- `installer/`: the one-line installer the site serves at `/install`, and its test.
- `release/`: the release build and the release signing public key.
- `catalogue/`: the versioned feature catalogue every result is explained against, and the tool that keeps its Asahi layer in step with Asahi's feature tables.

## Releasing and deploying

- **CLI**: bump `TOOL_VERSION` in `cli/omarchy_m_test/__init__.py`, merge, then push a tag `v<version>` on `main`. `.github/workflows/release.yml` builds `omarchy-m-test.tar.gz` (`release/build.sh`), signs it with the `RELEASE_SIGNING_KEY` Actions secret and publishes it with its `.sig` and a `VERSION` file as the latest GitHub release.
- **Site**: Railway (project `omarchy-m-testing`, service `site` plus Postgres) deploys every push to `main` from `site/Dockerfile`, running `bin/rails db:prepare` first (settings in `site/README.md`). Secrets (`SECRET_KEY_BASE`) live in Railway variables only.
- **Smoke test** against production: `release/smoke.sh` (install, `--dry-run`, upload a golden report, delete it).

The spec is in this repo's issues.

## GPU benchmarking (unreleased)

The opt-in `--gpu-benchmark` mode runs seven fixed offscreen Vulkan workloads with full CPU correctness controls, repeated raw measurements and a separate local document with optional machine signing. It requires a reviewed AArch64 native helper; a source-only archive reports it unsupported. It never installs packages, uploads results or changes the normal hardware report. See [protocol, limitations and build instructions](docs/benchmarks/gpu-v1.md).
