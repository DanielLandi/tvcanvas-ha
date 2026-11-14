#!/usr/bin/env bash
# THE gate for tvcanvas-ha. CI runs exactly this.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
"$PY" -m ruff check custom_components tests scripts
"$PY" -m ruff format --check custom_components tests scripts
"$PY" -m pytest -q
