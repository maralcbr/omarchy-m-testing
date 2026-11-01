#!/usr/bin/env bash
# Build the release tarball and check what it ships: release/check.sh
#
# Fails unless the tarball holds the launcher, every module of the package,
# the bundled feature catalogue, Asahi's reference kernel config, every
# vendored omarchy-mac check script and the video test card script
# byte for byte (executable where the source is), and unless the unpacked
# CLI finds its catalogue and scripts from where it was unpacked.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
t="$(mktemp -d)"
trap 'rm -rf "$t"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

"$repo/release/build.sh" "$t/dist" >/dev/null
mkdir "$t/x"
tar -xzf "$t/dist/omarchy-m-test.tar.gz" -C "$t/x"
root="$t/x/omarchy-m-test"
pkg="$root/omarchy_m_test"

[[ -x $root/bin/omarchy-m-test ]] || fail "no executable bin/omarchy-m-test"
cmp -s "$repo/catalogue/catalogue.json" "$pkg/catalogue.json" || fail "catalogue.json missing or different"
for f in source.json config; do
  cmp -s "$repo/catalogue/asahi-kernel/$f" "$pkg/asahi-kernel/$f" || fail "asahi-kernel/$f missing or different"
done
for f in "$repo"/cli/omarchy_m_test/*.py; do
  cmp -s "$f" "$pkg/$(basename "$f")" || fail "module $(basename "$f") missing or different"
done
for f in "$repo"/catalogue/macos-reference/*.json; do
  cmp -s "$f" "$pkg/macos-reference/$(basename "$f")" || fail "macos-reference/$(basename "$f") missing or different"
done
count=0
while read -r f; do
  cmp -s "$repo/cli/omarchy_m_test/$f" "$pkg/$f" || fail "$f missing or different"
  if [[ -x $repo/cli/omarchy_m_test/$f && ! -x $pkg/$f ]]; then fail "$f lost its executable bit"; fi
  count=$((count + 1))
done < <(cd "$repo/cli/omarchy_m_test" && find vendor testcard gpu_native gl_spec -type f ! -name '*.pyc' ! -path '*/__pycache__/*' ! -path '*/bin/*' | sort)
(( count > 0 )) || fail "no vendored files found in the source tree"

# The unpacked CLI resolves its own data, not the repository's.
python3 - "$root" <<'PY' || fail "the unpacked CLI can't find its bundled files"
import os, sys
sys.path.insert(0, sys.argv[1])
from omarchy_m_test import bundled
assert bundled.catalogue_text()
assert all(bundled.asahi_kernel_config())
assert bundled.ASAHI_KERNEL_DIRS[0].startswith(sys.argv[1]) and os.path.isdir(bundled.ASAHI_KERNEL_DIRS[0])
for name in bundled.SCRIPTS:
    path = bundled.script_path(name)
    assert path.startswith(os.path.realpath(sys.argv[1])) or path.startswith(sys.argv[1]), path
assert os.path.isfile(os.path.join(os.path.dirname(bundled.script_path("video-card")), "testcard.lua"))
assert bundled.gpu_document_schema()
PY
echo "release ok: launcher, $(ls "$pkg"/*.py | wc -l | tr -d ' ') modules, catalogue, Asahi kernel config and $count vendored, test card and GPU source/data files"
