#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
SITE_DIR=${1:-"$ROOT_DIR/site"}
CONFIG_PATH=${2:-"$ROOT_DIR/repositories.yml"}
IMAGE=${SAFEAPTREPO_VERIFY_IMAGE:-${SAFEDEBREPO_VERIFY_IMAGE:-ubuntu:24.04}}

if [[ ! -d "$SITE_DIR" ]]; then
  printf 'site directory does not exist: %s\n' "$SITE_DIR" >&2
  exit 1
fi

packages_csv=$(python3 - "$CONFIG_PATH" <<'PY'
from pathlib import Path
import sys
import yaml

config = yaml.safe_load(Path(sys.argv[1]).read_text())
packages = []
for entry in config["repositories"]:
    packages.extend(entry.get("verify_packages", []))
print(",".join(dict.fromkeys(packages)))
PY
)

if [[ -z "$packages_csv" ]]; then
  printf 'no verify_packages found in %s\n' "$CONFIG_PATH" >&2
  exit 1
fi

docker run --rm \
  --mount "type=bind,src=$(cd "$SITE_DIR" && pwd),dst=/repo,readonly" \
  -e SAFEAPTREPO_VERIFY_PACKAGES="$packages_csv" \
  -e SAFEDEBREPO_VERIFY_PACKAGES="$packages_csv" \
  "$IMAGE" \
  bash -lc '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates
    install -D -m 0644 /repo/safelibs.gpg /usr/share/keyrings/safelibs.gpg
    install -D -m 0644 /repo/safelibs.pref /etc/apt/preferences.d/safelibs.pref
    cat >/etc/apt/sources.list.d/safelibs.list <<EOF
deb [signed-by=/usr/share/keyrings/safelibs.gpg] file:///repo noble main
EOF
    apt-get update
    IFS=, read -r -a packages <<<"${SAFEAPTREPO_VERIFY_PACKAGES:-${SAFEDEBREPO_VERIFY_PACKAGES:-}}"
    apt-get install -y --no-install-recommends --allow-downgrades "${packages[@]}"
    for package in "${packages[@]}"; do
      version="$(dpkg-query -W -f='\''${Version}'\'' "$package")"
      printf "%s\t%s\n" "$package" "$version"
      apt-cache madison "$package" | grep -F "| $version | file:/repo " >/dev/null
    done
    if command -v zstd >/dev/null 2>&1; then
      zstd --version | head -n 1
    fi
  '
