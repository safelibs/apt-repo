#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ALL_REPOSITORY_NAME = "all"
STABLE_CHANNEL_NAME = "stable"
TESTING_CHANNEL_NAME = "testing"
UBUNTU_24_04_RUST_VERSION = "1.75"


class BuildError(RuntimeError):
    """Raised when site generation fails."""


@dataclass(frozen=True)
class PackageInfo:
    path: Path
    name: str
    version: str
    architecture: str
    pool_path: Path


@dataclass(frozen=True)
class PublishedRepository:
    channel: str
    name: str
    path: str
    repository_id: str
    url: str
    package_infos: tuple[PackageInfo, ...]


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            check=True,
            capture_output=capture_output,
        )
    except subprocess.CalledProcessError as exc:
        details = "\n".join(
            part.strip() for part in (exc.stdout or "", exc.stderr or "") if part.strip()
        )
        location = f" (cwd={cwd})" if cwd is not None else ""
        raise BuildError(f"{' '.join(args)} failed{location}: {details or exc}") from exc


def validate_build_config(path: Path, build: Any, context: str) -> None:
    if not isinstance(build, dict):
        raise BuildError(f"{path} {context} must define build")
    artifact_globs = build.get("artifact_globs")
    if not isinstance(artifact_globs, list) or not artifact_globs:
        raise BuildError(f"{path} {context} build must define artifact_globs")
    if not all(str(pattern).strip() for pattern in artifact_globs):
        raise BuildError(f"{path} {context} build artifact_globs must be non-empty")

    mode = str(build.get("mode") or "docker")
    if mode == "docker" and not str(build.get("command") or "").strip():
        raise BuildError(f"{path} {context} docker build must define command")


def load_config(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise BuildError(f"{path} must contain a YAML mapping")
    if "archive" not in data or "repositories" not in data:
        raise BuildError(f"{path} must define archive and repositories")
    archive = data["archive"]
    repositories = data["repositories"]
    if not isinstance(archive, dict):
        raise BuildError(f"{path} archive must be a YAML mapping")
    if not isinstance(repositories, list) or not repositories:
        raise BuildError(f"{path} must define a non-empty repositories list")

    for field in [
        "suite",
        "component",
        "origin",
        "label",
        "description",
        "homepage",
        "base_url",
        "key_name",
        "image",
    ]:
        if not str(archive.get(field) or "").strip():
            raise BuildError(f"{path} archive must define {field}")

    seen_repository_names: set[str] = set()
    for index, entry in enumerate(repositories, start=1):
        if not isinstance(entry, dict):
            raise BuildError(f"{path} repository #{index} must be a YAML mapping")
        for field in ["name", "github_repo", "ref"]:
            if not str(entry.get(field) or "").strip():
                raise BuildError(f"{path} repository #{index} must define {field}")
        repository_name = str(entry["name"]).strip()
        if repository_name == ALL_REPOSITORY_NAME:
            raise BuildError(
                f"{path} repository #{index} name '{ALL_REPOSITORY_NAME}' is reserved"
            )
        if repository_name in seen_repository_names:
            raise BuildError(f"{path} defines duplicate repository name: {repository_name}")
        seen_repository_names.add(repository_name)

        validate_build_config(path, entry.get("build"), f"repository #{index}")

    testing = data.get("testing")
    if testing is not None:
        if not isinstance(testing, dict):
            raise BuildError(f"{path} testing must be a YAML mapping")
        discover = testing.get("discover", {})
        if discover is not None and not isinstance(discover, dict):
            raise BuildError(f"{path} testing discover must be a YAML mapping")
        default_build = testing.get(
            "default_build",
            {"mode": "safe-debian", "artifact_globs": ["*.deb"]},
        )
        validate_build_config(path, default_build, "testing default_build")

        overrides = testing.get("repository_overrides", [])
        if overrides is None:
            overrides = []
        if not isinstance(overrides, list):
            raise BuildError(f"{path} testing repository_overrides must be a YAML list")
        seen_override_names: set[str] = set()
        for index, override in enumerate(overrides, start=1):
            if not isinstance(override, dict):
                raise BuildError(
                    f"{path} testing repository override #{index} must be a YAML mapping"
                )
            override_name = str(override.get("name") or "").strip()
            if not override_name:
                raise BuildError(f"{path} testing repository override #{index} must define name")
            if override_name in seen_override_names:
                raise BuildError(
                    f"{path} testing defines duplicate repository override: {override_name}"
                )
            seen_override_names.add(override_name)
            if "build" in override:
                validate_build_config(
                    path,
                    override["build"],
                    f"testing repository override #{index}",
                )
    return data


def dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def join_url(base_url: str, *parts: str) -> str:
    url = base_url.rstrip("/")
    clean_parts = [part.strip("/") for part in parts if part.strip("/")]
    if not clean_parts:
        return url
    return "/".join([url, *clean_parts])


def first_non_empty_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and value.strip():
            return value
    return ""


def clone_or_update_repo(repo_name: str, target_dir: Path) -> None:
    if target_dir.exists():
        run(["git", "-C", str(target_dir), "reset", "--hard", "HEAD"])
        run(["git", "-C", str(target_dir), "clean", "-fdx"])
        run(["git", "-C", str(target_dir), "fetch", "--tags", "--prune", "origin"])
        return
    if shutil.which("gh"):
        run(["gh", "repo", "clone", repo_name, str(target_dir)])
        return
    raise BuildError("gh is required to clone private safelibs repositories")


def checkout_ref_name(ref: str) -> str:
    if ref.startswith("refs/heads/"):
        return f"origin/{ref.removeprefix('refs/heads/')}"
    return ref


def sync_repo(entry: dict[str, Any], source_root: Path) -> Path:
    repo_name = str(entry["github_repo"])
    ref = str(entry["ref"])
    target_dir = source_root / str(entry["name"])
    clone_or_update_repo(repo_name, target_dir)
    run(["git", "-C", str(target_dir), "checkout", "--detach", checkout_ref_name(ref)])
    return target_dir


def discover_port_repositories(github_org: str, repository_prefix: str) -> list[dict[str, str]]:
    result = run(
        [
            "gh",
            "repo",
            "list",
            github_org,
            "--limit",
            "1000",
            "--json",
            "name,defaultBranchRef,isArchived",
        ],
        capture_output=True,
    )
    try:
        repos = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError("failed to parse GitHub repository discovery output") from exc

    discovered: list[dict[str, str]] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        repo_name = str(repo.get("name") or "")
        if not repo_name.startswith(repository_prefix) or repo.get("isArchived"):
            continue
        default_branch = repo.get("defaultBranchRef") or {}
        if not isinstance(default_branch, dict):
            continue
        branch_name = str(default_branch.get("name") or "").strip()
        if not branch_name:
            continue
        discovered.append(
            {
                "name": repo_name.removeprefix(repository_prefix),
                "github_repo": f"{github_org}/{repo_name}",
                "ref": f"refs/heads/{branch_name}",
            }
        )
    return sorted(discovered, key=lambda item: item["name"])


def merge_build_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    merged.update(override)
    return merged


def resolve_testing_repositories(config: dict[str, Any]) -> list[dict[str, Any]]:
    testing = config.get("testing")
    if not isinstance(testing, dict) or testing.get("enabled") is False:
        return []

    discover = testing.get("discover") or {}
    github_org = str(discover.get("github_org") or "safelibs")
    repository_prefix = str(discover.get("repository_prefix") or "port-")
    default_build = dict(
        testing.get("default_build")
        or {"mode": "safe-debian", "artifact_globs": ["*.deb"]}
    )
    stable_builds = {
        str(entry["name"]): dict(entry["build"])
        for entry in config.get("repositories", [])
        if isinstance(entry, dict) and "name" in entry and "build" in entry
    }

    def base_build_for(name: str) -> dict[str, Any]:
        return dict(stable_builds.get(name) or default_build)

    discovered_entries = discover_port_repositories(github_org, repository_prefix)
    entries_by_name: dict[str, dict[str, Any]] = {
        entry["name"]: {**entry, "build": base_build_for(entry["name"])}
        for entry in discovered_entries
    }

    overrides = testing.get("repository_overrides") or []
    for override in overrides:
        name = str(override["name"])
        base_entry = entries_by_name.get(
            name,
            {
                "name": name,
                "github_repo": str(
                    override.get("github_repo") or f"{github_org}/{repository_prefix}{name}"
                ),
                "ref": str(override.get("ref") or "refs/heads/main"),
                "build": base_build_for(name),
            },
        )
        merged_entry = dict(base_entry)
        for key, value in override.items():
            if key in {"build", "name"}:
                continue
            merged_entry[key] = value
        if "build" in override:
            merged_entry["build"] = merge_build_config(base_entry["build"], override["build"])
        entries_by_name[name] = merged_entry

    return [entries_by_name[name] for name in sorted(entries_by_name)]


_RUST_VERSION_RE = re.compile(r'^\s*rust-version\s*=\s*"([^"]+)"\s*$')
_RUST_EDITION_RE = re.compile(r'^\s*edition\s*=\s*"([^"]+)"\s*$')
_RUST_TOOLCHAIN_CHANNEL_RE = re.compile(r'^\s*channel\s*=\s*"([^"]+)"\s*$')


def version_key(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", value):
        return ()
    return tuple(int(part) for part in value.split("."))


def max_version(candidates: list[str]) -> str:
    numeric_candidates = [candidate for candidate in candidates if version_key(candidate)]
    if not numeric_candidates:
        return ""
    return max(numeric_candidates, key=version_key)


def detect_rust_toolchain(workdir: Path) -> str:
    candidates: list[str] = []
    named_candidates: list[str] = []
    edition_minimums = {
        "2018": "1.31",
        "2021": "1.56",
        "2024": "1.85",
    }
    has_modern_lockfile = False

    for cargo_path in sorted(workdir.rglob("Cargo.toml")):
        for line in cargo_path.read_text(errors="replace").splitlines():
            rust_match = _RUST_VERSION_RE.match(line)
            if rust_match:
                candidates.append(rust_match.group(1))
                continue
            edition_match = _RUST_EDITION_RE.match(line)
            if edition_match:
                minimum = edition_minimums.get(edition_match.group(1))
                if minimum:
                    candidates.append(minimum)

    for lockfile_path in sorted(workdir.rglob("Cargo.lock")):
        for line in lockfile_path.read_text(errors="replace").splitlines()[:5]:
            if line.strip() == "version = 4":
                has_modern_lockfile = True
                break
        if has_modern_lockfile:
            break

    for toolchain_name in ["rust-toolchain.toml", "rust-toolchain"]:
        toolchain_path = workdir / toolchain_name
        if not toolchain_path.exists():
            continue
        for line in toolchain_path.read_text(errors="replace").splitlines():
            channel_match = _RUST_TOOLCHAIN_CHANNEL_RE.match(line)
            if channel_match:
                channel = channel_match.group(1)
                if version_key(channel):
                    candidates.append(channel)
                else:
                    named_candidates.append(channel)
                break
        else:
            channel = toolchain_path.read_text(errors="replace").strip().splitlines()
            if channel:
                named_channel = channel[0].strip()
                if version_key(named_channel):
                    candidates.append(named_channel)
                else:
                    named_candidates.append(named_channel)

    required = max_version(candidates)
    if required and version_key(required) > version_key(UBUNTU_24_04_RUST_VERSION):
        return required
    if has_modern_lockfile:
        return "stable"
    if named_candidates:
        return named_candidates[0]
    return ""


def safe_debian_script() -> str:
    return "\n".join(
        [
            'mk-build-deps -i -r -t "apt-get -y --no-install-recommends" debian/control',
            "dpkg-buildpackage -us -uc -b",
            'cp -v ../*.deb "$SAFEAPTREPO_OUTPUT"/',
        ]
    )


def build_repo(
    entry: dict[str, Any],
    source_dir: Path,
    artifact_root: Path,
    default_image: str,
    default_packages: list[str],
) -> list[Path]:
    build = dict(entry["build"])
    mode = str(build.get("mode") or "docker")
    image = str(build.get("image") or default_image)
    packages = dedupe(default_packages + list(build.get("packages", [])))
    rustup_toolchain = str(build.get("rustup_toolchain") or "").strip()
    if rustup_toolchain and "curl" not in packages:
        packages.append("curl")
    output_dir = artifact_root / str(entry["name"])
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    default_workdir = "safe" if mode == "safe-debian" else "."
    workdir = source_dir / str(build.get("workdir") or default_workdir)
    if not workdir.exists():
        raise BuildError(f"missing workdir for {entry['name']}: {workdir}")

    if mode == "checkout-artifacts":
        artifacts: list[Path] = []
        for pattern in build["artifact_globs"]:
            for source_path in sorted(workdir.glob(str(pattern))):
                dest = output_dir / source_path.name
                shutil.copy2(source_path, dest)
                artifacts.append(dest)
        if not artifacts:
            raise BuildError(f"no checked-in artifacts found for {entry['name']} under {workdir}")
        return dedupe_paths(artifacts)

    if mode == "safe-debian":
        if not (workdir / "debian" / "control").exists():
            raise BuildError(f"missing debian/control for {entry['name']}: {workdir}")
        if "curl" not in packages:
            packages.append("curl")
        packages = dedupe(
            packages
            + [
                "build-essential",
                "devscripts",
                "dpkg-dev",
                "equivs",
                "fakeroot",
            ]
        )
        if not rustup_toolchain:
            rustup_toolchain = detect_rust_toolchain(workdir)
        script = safe_debian_script()
    elif mode == "docker":
        script = str(build["command"]).strip()
    else:
        raise BuildError(f"unsupported build mode for {entry['name']}: {mode}")
    if rustup_toolchain and "curl" not in packages:
        packages.append("curl")
    packages = dedupe(packages)
    env = os.environ.copy()
    env["SAFEAPTREPO_SOURCE"] = "/workspace/source"
    env["SAFEAPTREPO_OUTPUT"] = "/workspace/output"
    env["SAFEDEBREPO_SOURCE"] = env["SAFEAPTREPO_SOURCE"]
    env["SAFEDEBREPO_OUTPUT"] = env["SAFEAPTREPO_OUTPUT"]
    host_uid = os.getuid()
    host_gid = os.getgid()

    install_cmd = " ".join(packages)
    setup_steps: list[str] = []
    if rustup_toolchain:
        setup_steps.extend(
            [
                f"curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain {shell_quote(rustup_toolchain)}",
                'source "$HOME/.cargo/env"',
                "rustc --version",
                "cargo --version",
            ]
        )
    extra_setup = str(build.get("setup") or "").strip()
    if extra_setup:
        setup_steps.append(extra_setup)
    docker_script = "\n".join(
        [
            "set -euo pipefail",
            f"trap 'chown -R {host_uid}:{host_gid} /workspace/source /workspace/output' EXIT",
            "export DEBIAN_FRONTEND=noninteractive",
            "apt-get update",
            f"apt-get install -y --no-install-recommends {install_cmd}",
            *setup_steps,
            "git config --global --add safe.directory /workspace/source",
            f"cd {shell_quote(str(workdir.relative_to(source_dir)) or '.')}",
            script,
        ]
    )

    run(
        [
            "docker",
            "run",
            "--rm",
            "--mount",
            f"type=bind,src={source_dir.resolve()},dst=/workspace/source",
            "--mount",
            f"type=bind,src={output_dir.resolve()},dst=/workspace/output",
            "-w",
            "/workspace/source",
            "-e",
            "SAFEAPTREPO_SOURCE=/workspace/source",
            "-e",
            "SAFEAPTREPO_OUTPUT=/workspace/output",
            "-e",
            "SAFEDEBREPO_SOURCE=/workspace/source",
            "-e",
            "SAFEDEBREPO_OUTPUT=/workspace/output",
            image,
            "bash",
            "-lc",
            docker_script,
        ],
        env=env,
    )

    artifacts: list[Path] = []
    for pattern in build["artifact_globs"]:
        artifacts.extend(sorted(output_dir.glob(str(pattern))))
    artifacts = dedupe_paths(artifacts)
    if not artifacts:
        raise BuildError(f"no artifacts found for {entry['name']} in {output_dir}")
    return artifacts


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: dict[str, Path] = {}
    for path in paths:
        seen[str(path)] = path
    return list(seen.values())


def shell_quote(value: str) -> str:
    return shlex.quote(value)


def read_package_info(package_path: Path, component: str) -> PackageInfo:
    def field(name: str) -> str:
        result = run(
            ["dpkg-deb", "-f", str(package_path), name],
            capture_output=True,
        )
        return result.stdout.strip()

    package_name = field("Package")
    version = field("Version")
    architecture = field("Architecture")
    pool_rel = Path("pool") / component / package_name[0] / package_name / package_path.name
    return PackageInfo(
        path=package_path,
        name=package_name,
        version=version,
        architecture=architecture,
        pool_path=pool_rel,
    )


def stage_packages(site_root: Path, component: str, package_paths: list[Path]) -> list[PackageInfo]:
    infos: list[PackageInfo] = []
    for package_path in package_paths:
        info = read_package_info(package_path, component)
        dest = site_root / info.pool_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(package_path, dest)
        infos.append(
            PackageInfo(
                path=dest,
                name=info.name,
                version=info.version,
                architecture=info.architecture,
                pool_path=info.pool_path,
            )
        )
    return infos


def split_stanzas(raw_text: str) -> list[str]:
    stanzas = [chunk.strip() for chunk in raw_text.split("\n\n")]
    return [f"{chunk}\n" for chunk in stanzas if chunk]


def stanza_field(stanza: str, name: str) -> str:
    prefix = f"{name}: "
    for line in stanza.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise BuildError(f"missing {name} in stanza:\n{stanza}")


def write_package_indexes(site_root: Path, suite: str, component: str) -> list[str]:
    packages_output = run(
        ["apt-ftparchive", "packages", "pool"],
        cwd=site_root,
        capture_output=True,
    ).stdout
    stanzas = split_stanzas(packages_output)
    by_arch: dict[str, list[str]] = {}
    for stanza in stanzas:
        arch = stanza_field(stanza, "Architecture")
        by_arch.setdefault(arch, []).append(stanza)

    architectures = sorted(by_arch)
    for arch, arch_stanzas in by_arch.items():
        binary_dir = site_root / "dists" / suite / component / f"binary-{arch}"
        binary_dir.mkdir(parents=True, exist_ok=True)
        packages_path = binary_dir / "Packages"
        packages_path.write_text("\n".join(arch_stanzas))
        run(["gzip", "-9", "-kf", str(packages_path)])
    return architectures


def export_public_key_binary(homedir: Path, key_id: str, site_root: Path, key_name: str) -> None:
    asc_path = site_root / f"{key_name}.asc"
    gpg_path = site_root / f"{key_name}.gpg"

    with asc_path.open("w", encoding="utf-8") as handle:
        handle.write(
            run(
                ["gpg", "--homedir", str(homedir), "--armor", "--export", key_id],
                capture_output=True,
            ).stdout
        )
    gpg_bytes = subprocess.check_output(
        ["gpg", "--homedir", str(homedir), "--export", key_id],
        text=False,
    )
    gpg_path.write_bytes(gpg_bytes)


def prepare_signing_key() -> tuple[Path, str, str]:
    homedir = Path(tempfile.mkdtemp(prefix="safelibs-gpg-"))
    private_key = first_non_empty_env(
        "SAFEAPTREPO_GPG_PRIVATE_KEY",
        "SAFEDEBREPO_GPG_PRIVATE_KEY",
    ).strip()
    passphrase = first_non_empty_env(
        "SAFEAPTREPO_GPG_PASSPHRASE",
        "SAFEDEBREPO_GPG_PASSPHRASE",
    )

    if private_key:
        run(
            ["gpg", "--batch", "--homedir", str(homedir), "--import"],
            input_text=private_key,
        )
    else:
        params = "\n".join(
            [
                "%no-protection",
                "Key-Type: RSA",
                "Key-Length: 3072",
                "Subkey-Type: RSA",
                "Subkey-Length: 3072",
                "Name-Real: SafeLibs Archive Test Key",
                "Name-Email: noreply@safelibs.invalid",
                "Expire-Date: 0",
                "%commit",
            ]
        )
        run(
            ["gpg", "--batch", "--homedir", str(homedir), "--generate-key"],
            input_text=params,
        )

    listing = run(
        ["gpg", "--homedir", str(homedir), "--list-secret-keys", "--with-colons"],
        capture_output=True,
    ).stdout.splitlines()
    fingerprints = [line.split(":")[9] for line in listing if line.startswith("fpr:")]
    if not fingerprints:
        raise BuildError("failed to discover signing key fingerprint")
    fingerprint = fingerprints[0]
    return homedir, fingerprint, passphrase


def write_release_file(
    site_root: Path,
    suite: str,
    component: str,
    architectures: list[str],
    archive: dict[str, Any],
) -> None:
    release_path = site_root / "dists" / suite / "Release"
    release_text = run(
        [
            "apt-ftparchive",
            "-o",
            f"APT::FTPArchive::Release::Origin={archive['origin']}",
            "-o",
            f"APT::FTPArchive::Release::Label={archive['label']}",
            "-o",
            f"APT::FTPArchive::Release::Suite={suite}",
            "-o",
            f"APT::FTPArchive::Release::Codename={suite}",
            "-o",
            f"APT::FTPArchive::Release::Architectures={' '.join(architectures)}",
            "-o",
            f"APT::FTPArchive::Release::Components={component}",
            "-o",
            f"APT::FTPArchive::Release::Description={archive['description']}",
            "release",
            str(site_root / "dists" / suite),
        ],
        capture_output=True,
    ).stdout
    release_path.write_text(release_text)


def sign_release(site_root: Path, suite: str, homedir: Path, key_id: str, passphrase: str) -> None:
    release_path = site_root / "dists" / suite / "Release"
    inrelease_path = site_root / "dists" / suite / "InRelease"
    detached_path = site_root / "dists" / suite / "Release.gpg"
    base = [
        "gpg",
        "--batch",
        "--yes",
        "--homedir",
        str(homedir),
        "--pinentry-mode",
        "loopback",
        "-u",
        key_id,
    ]
    if passphrase:
        base.extend(["--passphrase", passphrase])

    run(base + ["--clearsign", "--output", str(inrelease_path), str(release_path)])
    run(base + ["--detach-sign", "--output", str(detached_path), str(release_path)])


def fingerprint_display(fingerprint: str) -> str:
    return " ".join(fingerprint[i : i + 4] for i in range(0, len(fingerprint), 4))


def repository_file_stem(key_name: str, repository_name: str) -> str:
    return f"{key_name}-{repository_name}"


def repository_path(path_prefix: str, repository_name: str) -> str:
    clean_prefix = path_prefix.strip("/")
    if not clean_prefix:
        return repository_name
    return f"{clean_prefix}/{repository_name}"


def repository_id(path_prefix: str, repository_name: str) -> str:
    return repository_path(path_prefix, repository_name).replace("/", "-")


def repository_lead_text(
    repository_name: str,
    *,
    channel_name: str,
    is_aggregate: bool,
) -> str:
    if channel_name == TESTING_CHANNEL_NAME:
        target = "the full SafeLibs package set" if is_aggregate else repository_name
        return (
            f"Latest buildable SafeLibs packages for {target}, built from the current "
            "default branch of the matching port repository."
        )
    if is_aggregate:
        return (
            "Memory-safe drop-in packages for the full SafeLibs package set, "
            "published as a signed static apt repository for Ubuntu 24.04."
        )
    return (
        f"Memory-safe drop-in packages for {repository_name}, published as a "
        "dedicated signed apt repository for Ubuntu 24.04."
    )


def write_preferences_file(
    site_root: Path,
    archive: dict[str, Any],
    package_infos: list[PackageInfo],
    *,
    repository_id: str | None = None,
) -> None:
    package_names = " ".join(sorted({info.name for info in package_infos}))
    if not package_names:
        raise BuildError("cannot write apt preferences without published packages")

    priority = int(archive.get("pin_priority", 1001))
    preference_text = "\n".join(
        [
            "# Prefer SafeLibs builds for the published package set.",
            f"Package: {package_names}",
            f"Pin: release o={archive['origin']}",
            f"Pin-Priority: {priority}",
            "",
        ]
    )
    key_name = str(archive["key_name"])
    (site_root / f"{key_name}.pref").write_text(preference_text)
    if repository_id:
        (site_root / f"{repository_file_stem(key_name, repository_id)}.pref").write_text(
            preference_text
        )


def render_index(
    template_path: Path,
    site_root: Path,
    archive: dict[str, Any],
    package_infos: list[PackageInfo],
    fingerprint: str,
    repo_url: str,
    repository_name: str,
    repository_id: str,
    channel_name: str,
    is_aggregate: bool,
) -> None:
    package_items = "\n".join(
        "          <li><code>"
        f"{html.escape(info.name)}</code> <span>{html.escape(info.version)}</span></li>"
        for info in sorted(package_infos, key=lambda item: item.name)
    )
    key_name = str(archive["key_name"])
    repo_url = repo_url.rstrip("/")
    file_stem = repository_file_stem(key_name, repository_id)
    channel_label = channel_name.capitalize()
    html_text = template_path.read_text().format(
        page_title=f"SafeLibs Apt Repository ({repository_name})",
        chip_text=(
            f"{channel_label} aggregate apt repository"
            if is_aggregate
            else f"{channel_label} single-library apt repository: {repository_name}"
        ),
        heading="SafeLibs Apt Repository",
        lead_text=repository_lead_text(
            repository_name,
            channel_name=channel_name,
            is_aggregate=is_aggregate,
        ),
        repository_name=repository_name,
        repo_url=repo_url,
        key_name=key_name,
        preferences_download=f"{file_stem}.pref",
        preferences_file=f"{file_stem}.pref",
        list_file=f"{file_stem}.list",
        suite=archive["suite"],
        component=archive["component"],
        origin=archive["origin"],
        homepage=archive["homepage"],
        description=archive["description"],
        fingerprint=fingerprint_display(fingerprint),
        package_items=package_items,
    )
    (site_root / "index.html").write_text(html_text)
    (site_root / ".nojekyll").write_text("")


def render_root_index(
    template_path: Path,
    site_root: Path,
    archive: dict[str, Any],
    repositories: list[PublishedRepository],
    fingerprint: str,
    base_url: str,
) -> None:
    key_name = str(archive["key_name"])
    all_repo = next(
        (
            repo
            for repo in repositories
            if repo.channel == STABLE_CHANNEL_NAME and repo.name == ALL_REPOSITORY_NAME
        ),
        next(repo for repo in repositories if repo.name == ALL_REPOSITORY_NAME),
    )
    all_repo_url = all_repo.url
    repo_cards = "\n".join(
        "\n".join(
            [
                '      <article class="panel repo-card stack">',
                "        <div class=\"chip\">"
                + html.escape(
                    f"{repo.channel.capitalize()} "
                    + ("aggregate repo" if repo.name == ALL_REPOSITORY_NAME else "per-library repo")
                )
                + "</div>",
                f'        <h3><a href="{html.escape(repo.url)}/">{html.escape(repo.name)}</a></h3>',
                f"        <p><code>/{html.escape(repo.path)}/</code></p>",
                f"        <p>{len(repo.package_infos)} published package{'s' if len(repo.package_infos) != 1 else ''}.</p>",
                "        <p><code>"
                + html.escape(", ".join(info.name for info in sorted(repo.package_infos, key=lambda item: item.name)))
                + "</code></p>",
                "      </article>",
            ]
        )
        for repo in repositories
    )
    package_items = "\n".join(
        "          <li><code>"
        f"{html.escape(info.name)}</code> <span>{html.escape(info.version)}</span></li>"
        for info in sorted(all_repo.package_infos, key=lambda item: item.name)
    )
    html_text = template_path.read_text().format(
        page_title="SafeLibs Apt Repositories",
        key_name=key_name,
        suite=archive["suite"],
        component=archive["component"],
        origin=archive["origin"],
        homepage=archive["homepage"],
        description=archive["description"],
        fingerprint=fingerprint_display(fingerprint),
        default_repo_url=all_repo_url,
        preferences_download=f"{repository_file_stem(key_name, ALL_REPOSITORY_NAME)}.pref",
        preferences_file=f"{repository_file_stem(key_name, ALL_REPOSITORY_NAME)}.pref",
        list_file=f"{repository_file_stem(key_name, ALL_REPOSITORY_NAME)}.list",
        repository_cards=repo_cards,
        package_items=package_items,
    )
    (site_root / "index.html").write_text(html_text)
    (site_root / ".nojekyll").write_text("")


def write_manifest(
    site_root: Path,
    archive: dict[str, Any],
    repositories: list[PublishedRepository],
) -> None:
    channels: dict[str, list[dict[str, Any]]] = {}
    for repo in repositories:
        channels.setdefault(repo.channel, []).append(
            {
                "name": repo.name,
                "path": repo.path,
                "url": repo.url,
                "packages": [
                    {"name": info.name, "version": info.version, "architecture": info.architecture}
                    for info in sorted(repo.package_infos, key=lambda item: item.name)
                ],
            }
        )

    manifest = {
        "archive": {
            "suite": archive["suite"],
            "component": archive["component"],
            "key_name": archive["key_name"],
        },
        "channels": [
            {
                "name": channel_name,
                "repositories": sorted(repos, key=lambda item: item["path"]),
            }
            for channel_name, repos in sorted(channels.items())
        ],
    }
    (site_root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


def generate_site_from_artifacts(
    config: dict[str, Any],
    package_paths: list[Path],
    output_dir: Path,
    *,
    template_path: Path,
    base_url: str,
    repository_name: str = ALL_REPOSITORY_NAME,
    repository_id: str | None = None,
    channel_name: str = STABLE_CHANNEL_NAME,
    is_aggregate: bool | None = None,
    signing_key: tuple[Path, str, str] | None = None,
) -> list[PackageInfo]:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    archive = config["archive"]
    suite = str(archive["suite"])
    component = str(archive["component"])
    infos = stage_packages(output_dir, component, package_paths)
    architectures = write_package_indexes(output_dir, suite, component)
    if not architectures:
        raise BuildError("no package indexes were generated")

    homedir, fingerprint, passphrase = signing_key or prepare_signing_key()
    export_public_key_binary(homedir, fingerprint, output_dir, str(archive["key_name"]))
    write_release_file(output_dir, suite, component, architectures, archive)
    sign_release(output_dir, suite, homedir, fingerprint, passphrase)
    resolved_repository_id = repository_id or repository_name
    resolved_is_aggregate = (
        repository_name == ALL_REPOSITORY_NAME if is_aggregate is None else is_aggregate
    )
    write_preferences_file(output_dir, archive, infos, repository_id=resolved_repository_id)
    render_index(
        template_path,
        output_dir,
        archive,
        infos,
        fingerprint,
        base_url,
        repository_name,
        resolved_repository_id,
        channel_name,
        resolved_is_aggregate,
    )
    return infos


def generate_split_site(
    config: dict[str, Any],
    repository_artifacts: dict[str, list[Path]],
    output_dir: Path,
    *,
    repository_template_path: Path,
    landing_template_path: Path,
    base_url: str,
    channel_name: str = STABLE_CHANNEL_NAME,
    path_prefix: str = "",
    signing_key: tuple[Path, str, str] | None = None,
    clean_output: bool = True,
    render_landing: bool = True,
) -> list[PublishedRepository]:
    configured_names = [str(entry["name"]) for entry in config["repositories"]]
    if ALL_REPOSITORY_NAME in configured_names:
        raise BuildError(f"repository name '{ALL_REPOSITORY_NAME}' is reserved")
    if len(configured_names) != len(set(configured_names)):
        raise BuildError("configured repository names must be unique")
    if ALL_REPOSITORY_NAME in repository_artifacts:
        raise BuildError(f"repository name '{ALL_REPOSITORY_NAME}' is reserved")
    unexpected_names = sorted(name for name in repository_artifacts if name not in configured_names)
    if unexpected_names:
        raise BuildError(
            "unexpected artifacts for unknown repositories: " + ", ".join(unexpected_names)
        )
    missing_names = [name for name in configured_names if not repository_artifacts.get(name)]
    if missing_names:
        raise BuildError(
            "missing artifacts for configured repositories: " + ", ".join(missing_names)
        )
    repository_names = [name for name in configured_names if repository_artifacts.get(name)]
    all_package_paths = dedupe_paths(
        [path for name in repository_names for path in repository_artifacts[name]]
    )
    if not all_package_paths:
        raise BuildError("cannot generate site without package artifacts")

    if clean_output and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    resolved_signing_key = signing_key or prepare_signing_key()
    _, fingerprint, _ = resolved_signing_key

    aggregate_path = repository_path(path_prefix, ALL_REPOSITORY_NAME)
    aggregate_id = repository_id(path_prefix, ALL_REPOSITORY_NAME)

    published_repositories = [
        PublishedRepository(
            channel=channel_name,
            name=ALL_REPOSITORY_NAME,
            path=aggregate_path,
            repository_id=aggregate_id,
            url=join_url(base_url, aggregate_path),
            package_infos=tuple(
                generate_site_from_artifacts(
                    config,
                    all_package_paths,
                    output_dir / aggregate_path,
                    template_path=repository_template_path,
                    base_url=join_url(base_url, aggregate_path),
                    repository_name=aggregate_path,
                    repository_id=aggregate_id,
                    channel_name=channel_name,
                    is_aggregate=True,
                    signing_key=resolved_signing_key,
                )
            ),
        )
    ]

    for repository_name in repository_names:
        repo_path = repository_path(path_prefix, repository_name)
        repo_id = repository_id(path_prefix, repository_name)
        published_repositories.append(
            PublishedRepository(
                channel=channel_name,
                name=repository_name,
                path=repo_path,
                repository_id=repo_id,
                url=join_url(base_url, repo_path),
                package_infos=tuple(
                    generate_site_from_artifacts(
                        config,
                        repository_artifacts[repository_name],
                        output_dir / repo_path,
                        template_path=repository_template_path,
                        base_url=join_url(base_url, repo_path),
                        repository_name=repo_path,
                        repository_id=repo_id,
                        channel_name=channel_name,
                        is_aggregate=False,
                        signing_key=resolved_signing_key,
                    )
                ),
            )
        )

    if render_landing:
        render_root_index(
            landing_template_path,
            output_dir,
            config["archive"],
            published_repositories,
            fingerprint,
            base_url,
        )
        write_manifest(output_dir, config["archive"], published_repositories)
    return published_repositories


def config_with_repositories(config: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    channel_config = dict(config)
    channel_config["repositories"] = entries
    return channel_config


def collect_cached_artifacts(
    entries: list[dict[str, Any]],
    artifact_root: Path,
) -> dict[str, list[Path]]:
    repository_artifacts: dict[str, list[Path]] = {}
    for entry in entries:
        repo_dir = artifact_root / str(entry["name"])
        artifacts = dedupe_paths(sorted(repo_dir.glob("*.deb")))
        if artifacts:
            repository_artifacts[repo_dir.name] = artifacts
    return repository_artifacts


def build_repository_entries(
    entries: list[dict[str, Any]],
    *,
    source_root: Path,
    artifact_root: Path,
    default_image: str,
    default_packages: list[str],
    allow_failures: bool,
    channel_name: str,
) -> tuple[dict[str, list[Path]], list[dict[str, Any]]]:
    repository_artifacts: dict[str, list[Path]] = {}
    built_entries: list[dict[str, Any]] = []
    for entry in entries:
        try:
            source_dir = sync_repo(entry, source_root)
            artifacts = build_repo(
                entry,
                source_dir,
                artifact_root,
                default_image,
                default_packages,
            )
        except BuildError as exc:
            if not allow_failures:
                raise
            print(
                f"skipping {channel_name}/{entry['name']}: {exc}",
                file=sys.stderr,
            )
            continue
        repository_artifacts[str(entry["name"])] = artifacts
        built_entries.append(entry)
    return repository_artifacts, built_entries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the SafeLibs GitHub Pages apt repository")
    parser.add_argument("--config", type=Path, default=Path("repositories.yml"))
    parser.add_argument("--output", type=Path, default=Path("site"))
    parser.add_argument("--workspace", type=Path, default=Path(".work"))
    parser.add_argument("--base-url", default="")
    parser.add_argument("--skip-build", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    archive = config["archive"]
    base_url = args.base_url or str(archive["base_url"])
    source_root = args.workspace / "sources"
    artifact_root = args.workspace / "artifacts"
    source_root.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    repository_template_path = Path(__file__).resolve().parent.parent / "templates" / "index.html"
    landing_template_path = Path(__file__).resolve().parent.parent / "templates" / "landing.html"

    stable_entries = list(config["repositories"])
    testing_entries = resolve_testing_repositories(config)
    testing_config = config.get("testing") if isinstance(config.get("testing"), dict) else {}
    testing_allow_failures = bool(testing_config.get("allow_build_failures", False))

    if args.skip_build:
        stable_artifacts = collect_cached_artifacts(stable_entries, artifact_root)
        testing_artifacts = collect_cached_artifacts(
            testing_entries,
            artifact_root / TESTING_CHANNEL_NAME,
        )
        built_testing_entries = [
            entry for entry in testing_entries if str(entry["name"]) in testing_artifacts
        ]
    else:
        stable_artifacts, _ = build_repository_entries(
            stable_entries,
            source_root=source_root,
            artifact_root=artifact_root,
            default_image=str(archive["image"]),
            default_packages=list(archive.get("install_packages", [])),
            allow_failures=False,
            channel_name=STABLE_CHANNEL_NAME,
        )
        testing_artifacts, built_testing_entries = build_repository_entries(
            testing_entries,
            source_root=source_root,
            artifact_root=artifact_root / TESTING_CHANNEL_NAME,
            default_image=str(archive["image"]),
            default_packages=list(archive.get("install_packages", [])),
            allow_failures=testing_allow_failures,
            channel_name=TESTING_CHANNEL_NAME,
        )

    if not testing_entries:
        generate_split_site(
            config,
            stable_artifacts,
            args.output,
            repository_template_path=repository_template_path,
            landing_template_path=landing_template_path,
            base_url=base_url,
        )
    else:
        if args.output.exists():
            shutil.rmtree(args.output)
        args.output.mkdir(parents=True, exist_ok=True)
        signing_key = prepare_signing_key()
        _, fingerprint, _ = signing_key
        published_repositories: list[PublishedRepository] = []
        published_repositories.extend(
            generate_split_site(
                config_with_repositories(config, stable_entries),
                stable_artifacts,
                args.output,
                repository_template_path=repository_template_path,
                landing_template_path=landing_template_path,
                base_url=base_url,
                channel_name=STABLE_CHANNEL_NAME,
                signing_key=signing_key,
                clean_output=False,
                render_landing=False,
            )
        )
        if testing_artifacts:
            published_repositories.extend(
                generate_split_site(
                    config_with_repositories(config, built_testing_entries),
                    testing_artifacts,
                    args.output,
                    repository_template_path=repository_template_path,
                    landing_template_path=landing_template_path,
                    base_url=base_url,
                    channel_name=TESTING_CHANNEL_NAME,
                    path_prefix=TESTING_CHANNEL_NAME,
                    signing_key=signing_key,
                    clean_output=False,
                    render_landing=False,
                )
            )
        render_root_index(
            landing_template_path,
            args.output,
            archive,
            published_repositories,
            fingerprint,
            base_url,
        )
        write_manifest(args.output, archive, published_repositories)
    print(f"wrote site to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
