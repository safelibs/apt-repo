#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
IMAGE=${SAFEAPTREPO_VERIFY_IMAGE:-${SAFEDEBREPO_VERIFY_IMAGE:-ubuntu:24.04}}
REPO_TARGET=${1:-"$ROOT_DIR/site"}
CONFIG_PATH=${2:-"$ROOT_DIR/repositories.yml"}
REPOSITORY_NAME=${3:-all}
REPOSITORY_PATH=${4:-"$REPOSITORY_NAME"}

IFS=$'\t' read -r suite component key_name packages_csv <<EOF
$(python3 - "$CONFIG_PATH" "$REPOSITORY_NAME" "$REPOSITORY_PATH" <<'PY'
from pathlib import Path
import sys
import yaml

config = yaml.safe_load(Path(sys.argv[1]).read_text())
archive = config["archive"]
repository_name = sys.argv[2]
repository_path = sys.argv[3]
repositories = config["repositories"]
if repository_name == "all":
    packages = []
    packages_complete = True
    for entry in repositories:
        verify_packages = entry.get("verify_all_packages", entry.get("verify_packages", []))
        if verify_packages:
            packages.extend(verify_packages)
            continue
        packages_complete = False
    if not packages_complete:
        packages = []
else:
    entry = next(
        (candidate for candidate in repositories if candidate["name"] == repository_name),
        None,
    )
    if entry is None:
        if not repository_path.startswith("testing/"):
            raise SystemExit(f"unknown repository for verification: {repository_name}")
        packages = []
    else:
        packages = list(entry.get("verify_packages", []))
packages = list(dict.fromkeys(packages))
print(
    "\t".join(
        [
            str(archive["suite"]),
            str(archive["component"]),
            str(archive["key_name"]),
            ",".join(dict.fromkeys(packages)),
        ]
    )
)
PY
)
EOF

repository_id=${REPOSITORY_PATH//\//-}
preference_name="${key_name}-${repository_id}.pref"
repo_uri=
repo_mode=
madison_source=
docker_args=()

if [[ -d "$REPO_TARGET" ]]; then
  site_dir=$(cd "$REPO_TARGET" && pwd)
  if [[ -d "$site_dir/dists" ]]; then
    repo_dir=$site_dir
  else
    repo_dir="$site_dir/$REPOSITORY_PATH"
  fi
  if [[ ! -d "$repo_dir" ]]; then
    printf 'expected repository directory for %s under %s\n' "$REPOSITORY_PATH" "$REPO_TARGET" >&2
    exit 1
  fi
  repo_mode='local'
  repo_uri='file:///repo'
  madison_source='file:/repo'
  docker_args+=(
    --mount
    "type=bind,src=$repo_dir,dst=/repo,readonly"
  )
elif [[ "$REPO_TARGET" =~ ^https?:// ]]; then
  repo_mode='remote'
  case "${REPO_TARGET%/}" in
    */"$REPOSITORY_PATH")
      repo_uri=${REPO_TARGET%/}
      ;;
    *)
      repo_uri="${REPO_TARGET%/}/$REPOSITORY_PATH"
      ;;
  esac
  madison_source=$repo_uri
else
  printf 'expected site directory or http(s) base URL, got: %s\n' "$REPO_TARGET" >&2
  exit 1
fi

if [[ -z "$packages_csv" ]]; then
  packages_csv=$(
    python3 - "$repo_mode" "${repo_dir:-}" "$repo_uri" "$suite" "$component" <<'PY'
import gzip
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
import sys


def package_csv(texts: list[str]) -> str:
    packages: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for line in text.splitlines():
            if not line.startswith("Package: "):
                continue
            package = line.split(":", 1)[1].strip()
            if package and package not in seen:
                seen.add(package)
                packages.append(package)
    return ",".join(packages)


mode = sys.argv[1]
suite = sys.argv[4]
component = sys.argv[5]
packages_rels = [
    f"dists/{suite}/{component}/binary-amd64/Packages",
    f"dists/{suite}/{component}/binary-all/Packages",
]
packages_texts: list[str] = []

if mode == "local":
    repo_root = Path(sys.argv[2])
    for packages_rel in packages_rels:
        packages_path = repo_root / packages_rel
        if packages_path.exists():
            packages_texts.append(packages_path.read_text())
elif mode == "remote":
    base_url = sys.argv[3].rstrip("/")
    for packages_rel in packages_rels:
        packages_url = f"{base_url}/{packages_rel}.gz"
        try:
            with urlopen(packages_url) as response:
                packages_texts.append(gzip.decompress(response.read()).decode())
        except HTTPError as exc:
            if exc.code != 404:
                raise
else:
    raise SystemExit(f"unsupported verify mode: {mode}")

print(package_csv(packages_texts))
PY
  )
fi

if [[ -z "$packages_csv" ]]; then
  printf 'no packages found to verify for %s\n' "$REPOSITORY_PATH" >&2
  exit 1
fi

docker run --rm \
  "${docker_args[@]}" \
  -e SAFEAPTREPO_VERIFY_PACKAGES="$packages_csv" \
  -e SAFEDEBREPO_VERIFY_PACKAGES="$packages_csv" \
  -e SAFEAPTREPO_VERIFY_MODE="$repo_mode" \
  -e SAFEDEBREPO_VERIFY_MODE="$repo_mode" \
  -e SAFEAPTREPO_VERIFY_REPO_URI="$repo_uri" \
  -e SAFEDEBREPO_VERIFY_REPO_URI="$repo_uri" \
  -e SAFEAPTREPO_VERIFY_KEY_NAME="$key_name" \
  -e SAFEDEBREPO_VERIFY_KEY_NAME="$key_name" \
  -e SAFEAPTREPO_VERIFY_PREFERENCE_FILE="$preference_name" \
  -e SAFEDEBREPO_VERIFY_PREFERENCE_FILE="$preference_name" \
  -e SAFEAPTREPO_VERIFY_SUITE="$suite" \
  -e SAFEDEBREPO_VERIFY_SUITE="$suite" \
  -e SAFEAPTREPO_VERIFY_COMPONENT="$component" \
  -e SAFEDEBREPO_VERIFY_COMPONENT="$component" \
  -e SAFEAPTREPO_VERIFY_MADISON_SOURCE="$madison_source" \
  -e SAFEDEBREPO_VERIFY_MADISON_SOURCE="$madison_source" \
  "$IMAGE" \
  bash -lc '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    repo_mode="${SAFEAPTREPO_VERIFY_MODE:-${SAFEDEBREPO_VERIFY_MODE:-}}"
    repo_uri="${SAFEAPTREPO_VERIFY_REPO_URI:-${SAFEDEBREPO_VERIFY_REPO_URI:-}}"
    key_name="${SAFEAPTREPO_VERIFY_KEY_NAME:-${SAFEDEBREPO_VERIFY_KEY_NAME:-}}"
    preference_name="${SAFEAPTREPO_VERIFY_PREFERENCE_FILE:-${SAFEDEBREPO_VERIFY_PREFERENCE_FILE:-${key_name}.pref}}"
    suite="${SAFEAPTREPO_VERIFY_SUITE:-${SAFEDEBREPO_VERIFY_SUITE:-}}"
    component="${SAFEAPTREPO_VERIFY_COMPONENT:-${SAFEDEBREPO_VERIFY_COMPONENT:-}}"
    apt-get update
    apt-get install -y --no-install-recommends ca-certificates curl
    install -d -m 0755 /usr/share/keyrings /etc/apt/preferences.d
    case "$repo_mode" in
      local)
        install -D -m 0644 "/repo/${key_name}.gpg" "/usr/share/keyrings/${key_name}.gpg"
        install -D -m 0644 "/repo/${preference_name}" "/etc/apt/preferences.d/${preference_name}"
        ;;
      remote)
        curl -fsSL "${repo_uri}/${key_name}.gpg" -o "/usr/share/keyrings/${key_name}.gpg"
        curl -fsSL "${repo_uri}/${preference_name}" -o "/etc/apt/preferences.d/${preference_name}"
        ;;
      *)
        printf "unsupported verify mode: %s\n" "$repo_mode" >&2
        exit 1
        ;;
    esac
    printf "deb [signed-by=/usr/share/keyrings/%s.gpg] %s %s %s\n" "$key_name" "$repo_uri" "$suite" "$component" >"/etc/apt/sources.list.d/${key_name}.list"
    apt-get update
    IFS=, read -r -a packages <<<"${SAFEAPTREPO_VERIFY_PACKAGES:-${SAFEDEBREPO_VERIFY_PACKAGES:-}}"
    madison_source="${SAFEAPTREPO_VERIFY_MADISON_SOURCE:-${SAFEDEBREPO_VERIFY_MADISON_SOURCE:-}}"
    apt-get install -y --no-install-recommends --allow-downgrades "${packages[@]}"
    for package in "${packages[@]}"; do
      version="$(dpkg-query -W -f='\''${Version}'\'' "$package")"
      printf "%s\t%s\n" "$package" "$version"
      apt-cache madison "$package" | grep -F "| $version | ${madison_source} " >/dev/null
    done
    if command -v zstd >/dev/null 2>&1; then
      zstd --version | head -n 1
    fi
  '
