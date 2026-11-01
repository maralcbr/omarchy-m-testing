#!/usr/bin/env bash
# Build the CLI release tarball: release/build.sh OUTDIR
#
# OUTDIR/omarchy-m-test.tar.gz unpacks to omarchy-m-test/ with bin/, the
# omarchy_m_test package (the feature catalogue bundled as
# omarchy_m_test/catalogue.json, Asahi's reference kernel config as
# omarchy_m_test/asahi-kernel/, omarchy-mac's check scripts under
# omarchy_m_test/vendor/, the on-screen video check under omarchy_m_test/testcard/) and VERSION. release/check.sh checks a built one. OUTDIR/VERSION is the version
# on its own, which the CLI's newer-release check reads. Signing happens
# separately (release.yml), so this runs anywhere with GNU tar.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
out="$(mkdir -p "$1" && cd "$1" && pwd)"
version="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); import omarchy_m_test as t; print(t.TOOL_VERSION)' "$repo/cli")"

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
root="$stage/omarchy-m-test"
mkdir -p "$root/bin" "$root/omarchy_m_test"
install -m 0755 "$repo/cli/bin/omarchy-m-test" "$root/bin/omarchy-m-test"
install -m 0644 "$repo"/cli/omarchy_m_test/*.py "$root/omarchy_m_test/"
install -m 0644 "$repo/catalogue/catalogue.json" "$root/omarchy_m_test/catalogue.json"
mkdir -p "$root/omarchy_m_test/asahi-kernel"
install -m 0644 "$repo/catalogue/asahi-kernel/source.json" "$repo/catalogue/asahi-kernel/config" "$root/omarchy_m_test/asahi-kernel/"
mkdir -p "$root/omarchy_m_test/macos-reference"
install -m 0644 "$repo"/catalogue/macos-reference/*.json "$root/omarchy_m_test/macos-reference/"
# Vendored check scripts and the video test card script (omarchy_m_test/bundled.py runs them), modes kept.
(cd "$repo/cli/omarchy_m_test" && find vendor testcard gpu_native gl_spec -type f ! -name '*.pyc' ! -path '*/__pycache__/*' ! -path '*/bin/*' | sort | while read -r f; do
  install -D -m "$([[ -x $f ]] && echo 0755 || echo 0644)" "$f" "$root/omarchy_m_test/$f"
done)
# Native helper inclusion is explicit; source-only archives report it unsupported.
# Release CI builds one AArch64 helper, not a compiler/install step on users' Macs.
if [[ -n ${OMARCHY_GPU_BINARY_DIR:-} ]]; then
  python3 - "$OMARCHY_GPU_BINARY_DIR" "$repo/cli/omarchy_m_test/gpu_native" <<'PY'
import hashlib,json,pathlib,struct,sys
base=pathlib.Path(sys.argv[1]);p=base/'gpu-benchmark-vulkan';b=p.read_bytes();j=json.loads((base/'gpu-benchmark-build.json').read_text())
assert b[:6]==b'\x7fELF\x02\x01' and struct.unpack_from('<H',b,18)[0]==183,'release helper must be AArch64'
assert hashlib.sha256(b).hexdigest()==j['executable_sha256'] and j['suite']==1
src=pathlib.Path(sys.argv[2]);names=['benchmark.c','compute.comp','fullscreen.vert','raster.frag','texture.frag']
sources={name:hashlib.sha256((src/name).read_bytes()).hexdigest() for name in names}
assert sources==j['sources'],'helper was built from different packaged sources'
assert hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(',',':')).encode()).hexdigest()==j['source_sha256']
PY
  install -Dm755 "$OMARCHY_GPU_BINARY_DIR/gpu-benchmark-vulkan" "$root/omarchy_m_test/gpu_native/bin/gpu-benchmark-vulkan"
  install -Dm644 "$OMARCHY_GPU_BINARY_DIR/gpu-benchmark-build.json" "$root/omarchy_m_test/gpu_native/bin/gpu-benchmark-build.json"
fi
printf '%s\n' "$version" > "$root/VERSION"

# Reproducible: fixed order, owners and times.
epoch="${SOURCE_DATE_EPOCH:-$(git -C "$repo" log -1 --format=%ct 2>/dev/null || echo 0)}"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" -C "$stage" -cf - omarchy-m-test \
  | gzip -9n > "$out/omarchy-m-test.tar.gz"
printf '%s\n' "$version" > "$out/VERSION"
echo "built omarchy-m-test $version: $out/omarchy-m-test.tar.gz"
