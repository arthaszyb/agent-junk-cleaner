#!/bin/sh
# Builds the sdist, the wheel and the zipapp into dist/, copies install.sh next to them and writes dist/SHA256SUMS.
# Nothing is published.
# `build` is installed into a throwaway venv in /tmp (this downloads from PyPI), never into your own Python.
set -eu
cd "$(dirname "$0")/.."

venv=/tmp/ds-build-venv
python3 -m venv "$venv"
"$venv/bin/python" -m pip install --quiet --disable-pip-version-check build

rm -rf build dist
"$venv/bin/python" -m build --outdir dist/
python3 scripts/build_pyz.py
cp scripts/install.sh dist/install.sh
rm -rf build src/*.egg-info

cd dist
for artifact in disk-steward-* disk_steward-* install.sh; do
  [ -f "$artifact" ] && shasum -a 256 "$artifact"
done > SHA256SUMS
cat SHA256SUMS
