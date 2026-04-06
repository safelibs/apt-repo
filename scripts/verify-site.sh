#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SITE_TARGET=${1:-"$ROOT_DIR/site"}
CONFIG_PATH=${2:-"$ROOT_DIR/repositories.yml"}

repository_names_output=$(
  python3 - "$CONFIG_PATH" <<'PY'
from pathlib import Path
import sys
import yaml

config_path = Path(sys.argv[1])
try:
    config_text = config_path.read_text()
except OSError as exc:
    raise SystemExit(f"failed to read {config_path}: {exc}") from exc

try:
    config = yaml.safe_load(config_text)
except yaml.YAMLError as exc:
    raise SystemExit(f"failed to parse {config_path}: {exc}") from exc

if not isinstance(config, dict):
    raise SystemExit(f"{sys.argv[1]} must contain a YAML mapping")
repositories = config.get("repositories")
if not isinstance(repositories, list) or not repositories:
    raise SystemExit(f"{sys.argv[1]} must define a non-empty repositories list")
print("all")
for entry in repositories:
    print(str(entry["name"]))
PY
)
mapfile -t repository_names <<<"$repository_names_output"
if [[ ${#repository_names[@]} -eq 0 ]] || [[ -z ${repository_names[0]} ]]; then
  printf 'failed to resolve repositories from %s\n' "$CONFIG_PATH" >&2
  exit 1
fi

for repository_name in "${repository_names[@]}"; do
  bash "$ROOT_DIR/scripts/verify-in-ubuntu-docker.sh" "$SITE_TARGET" "$CONFIG_PATH" "$repository_name"
done
