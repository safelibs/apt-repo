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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


ALL_REPOSITORY_NAME = "all"
STABLE_CHANNEL_NAME = "stable"
TESTING_CHANNEL_NAME = "testing"
UBUNTU_24_04_RUST_VERSION = "1.75"
MAX_FAILURE_OUTPUT_CHARS = 12000
RELEASE_TAG_PREFIX = "build-"
RELEASE_TAG_COMMIT_CHARS = 12
DEFAULT_VALIDATOR_SITE_URL = "https://safelibs.github.io/validator/site-data.json"
DEFAULT_VALIDATOR_MODE = "port"
RUNTIME_PACKAGE_SUFFIX_EXCLUDES = (
    "-dev",
    "-doc",
    "-tools",
    "-progs",
    "-utils",
    "-tests",
    "-java",
    "-perl",
    "-ruby",
    "-tcl",
    "-ocaml",
)
RUNTIME_PACKAGE_PREFIX_EXCLUDES = ("gir1.2-", "python3-")
SOURCE_ARTIFACT_PATTERNS = ("*.dsc", "*.orig.tar.*", "*.debian.tar.*")
SOURCE_DSC_SUFFIX = ".dsc"


def runtime_packages_from_apt_packages(apt_packages: list[str]) -> list[str]:
    """Mirror the validator runtime_packages heuristic.

    Used as a fallback when the validator output predates the
    runtime_packages field, so a freshly pushed validator does not have to
    finish republishing before apt rebuilds succeed.
    """

    runtime: list[str] = []
    for package in apt_packages:
        if not isinstance(package, str) or not package:
            continue
        if not package.startswith("lib"):
            continue
        if any(package.endswith(suffix) for suffix in RUNTIME_PACKAGE_SUFFIX_EXCLUDES):
            continue
        if any(package.startswith(prefix) for prefix in RUNTIME_PACKAGE_PREFIX_EXCLUDES):
            continue
        runtime.append(package)
    return runtime


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
class SourceInfo:
    source: str
    version: str
    dsc_path: Path
    pool_path: Path
    files: tuple[Path, ...]


@dataclass(frozen=True)
class PublishedRepository:
    channel: str
    name: str
    path: str
    repository_id: str
    url: str
    package_infos: tuple[PackageInfo, ...]
    verify_packages: tuple[str, ...] = ()
    verify_all_packages: tuple[str, ...] = ()
    source_infos: tuple[SourceInfo, ...] = ()


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
        def output_part(label: str, text: str | None) -> str:
            if not text or not text.strip():
                return ""
            stripped = text.strip()
            if len(stripped) <= MAX_FAILURE_OUTPUT_CHARS:
                return stripped
            return (
                f"[{label} truncated to last {MAX_FAILURE_OUTPUT_CHARS} chars]\n"
                f"{stripped[-MAX_FAILURE_OUTPUT_CHARS:]}"
            )

        details = "\n".join(
            part
            for part in (
                output_part("stdout", exc.stdout),
                output_part("stderr", exc.stderr),
            )
            if part
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
    if "archive" not in data:
        raise BuildError(f"{path} must define archive")
    archive = data["archive"]
    if not isinstance(archive, dict):
        raise BuildError(f"{path} archive must be a YAML mapping")
    repositories = data.get("repositories")
    if repositories is not None and (not isinstance(repositories, list) or not repositories):
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

    if repositories is not None:
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
    validator = data.get("validator")
    if validator is not None:
        if not isinstance(validator, dict):
            raise BuildError(f"{path} validator must be a YAML mapping")
        site_url = validator.get("site_url")
        if site_url is not None and not str(site_url).strip():
            raise BuildError(f"{path} validator site_url must be non-empty when provided")
        mode = validator.get("mode")
        if mode is not None and not str(mode).strip():
            raise BuildError(f"{path} validator mode must be non-empty when provided")
    port_build_overrides = data.get("port_build_overrides")
    if port_build_overrides is not None:
        if not isinstance(port_build_overrides, list):
            raise BuildError(f"{path} port_build_overrides must be a YAML list")
        seen_override_names: set[str] = set()
        for index, override in enumerate(port_build_overrides, start=1):
            if not isinstance(override, dict):
                raise BuildError(
                    f"{path} port_build_overrides #{index} must be a YAML mapping"
                )
            override_name = str(override.get("name") or "").strip()
            if not override_name:
                raise BuildError(
                    f"{path} port_build_overrides #{index} must define name"
                )
            if override_name in seen_override_names:
                raise BuildError(
                    f"{path} port_build_overrides defines duplicate name: {override_name}"
                )
            seen_override_names.add(override_name)
            if "build" in override:
                validate_build_config(
                    path,
                    override["build"],
                    f"port_build_overrides #{index}",
                )
    return data


def fetch_validator_site_data(site_url: str) -> dict[str, Any]:
    fixture_path = os.environ.get("SAFEAPTREPO_VALIDATOR_FIXTURE")
    if fixture_path:
        try:
            return json.loads(Path(fixture_path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BuildError(f"failed to load validator fixture {fixture_path}: {exc}") from exc

    request = urllib.request.Request(site_url, headers={"User-Agent": "safelibs-apt"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise BuildError(f"failed to fetch validator site data from {site_url}: {exc}") from exc


def _validating_proof(site_data: dict[str, Any], mode: str) -> dict[str, Any]:
    if not isinstance(site_data, dict):
        raise BuildError("validator site data must be a JSON object")
    if site_data.get("schema_version") != 2:
        raise BuildError(
            f"unsupported validator schema_version {site_data.get('schema_version')!r}; expected 2"
        )
    proofs = site_data.get("proofs") or []
    for proof in proofs:
        if isinstance(proof, dict) and proof.get("mode") == mode:
            return proof
    raise BuildError(f"validator proof for mode {mode!r} not found in site data")


def _is_fully_passing(library_entry: dict[str, Any]) -> bool:
    totals = library_entry.get("totals") or {}
    return (
        totals.get("failed") == 0
        and totals.get("passed") == totals.get("cases")
        and bool(totals.get("cases"))
    )


def synthesize_repository_entries(
    site_data: dict[str, Any],
    mode: str,
    *,
    overrides: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build apt repository entries directly from validator site-data.

    Each fully-passing library in the requested validator proof produces a
    repository entry shaped like the legacy hand-maintained ``repositories[]``
    blocks: name, github_repo, ref, verify_packages, verify_all_packages, and
    a default checkout-artifacts ``build`` block. ``overrides`` (matched by
    library name) layer optional per-port build overrides on top so the
    ``checkout-artifacts`` default does not have to win every time.
    """

    proof = _validating_proof(site_data, mode)
    overrides_by_name: dict[str, dict[str, Any]] = {}
    if overrides:
        for override in overrides:
            if not isinstance(override, dict):
                continue
            name = str(override.get("name") or "").strip()
            if name:
                overrides_by_name[name] = override

    entries: list[dict[str, Any]] = []
    for library_entry in proof.get("libraries") or []:
        if not isinstance(library_entry, dict):
            continue
        if not _is_fully_passing(library_entry):
            continue
        name = str(library_entry.get("library") or "").strip()
        if not name:
            continue
        port_repository = str(library_entry.get("port_repository") or "").strip()
        port_tag_ref = str(library_entry.get("port_tag_ref") or "").strip()
        port_release_tag = str(library_entry.get("port_release_tag") or "").strip()
        if not port_repository or not (port_tag_ref or port_release_tag):
            raise BuildError(
                f"validator entry for {name!r} is missing port repository metadata"
            )
        ref = (
            port_tag_ref
            if port_tag_ref
            else f"refs/tags/{port_release_tag}"
        )

        port_debs = library_entry.get("port_debs") or []
        ported_packages: list[str] = []
        for deb in port_debs:
            if not isinstance(deb, dict):
                continue
            package = str(deb.get("package") or "").strip()
            if package and package not in ported_packages:
                ported_packages.append(package)
        if not ported_packages:
            raise BuildError(
                f"validator entry for {name!r} has no port_debs; "
                "every validating port must publish at least one .deb"
            )
        verify_packages = ported_packages
        validator_runtime = library_entry.get("runtime_packages") or []
        if validator_runtime:
            verify_all_packages = [
                package for package in validator_runtime if package in verify_packages
            ]
        else:
            verify_all_packages = []
        if not verify_all_packages:
            verify_all_packages = runtime_packages_from_apt_packages(verify_packages)
        if not verify_all_packages:
            raise BuildError(
                f"validator entry for {name!r} has no runtime library packages; "
                "neither validator runtime_packages nor port_debs yields a non-empty "
                "runtime subset"
            )

        entry: dict[str, Any] = {
            "name": name,
            "github_repo": port_repository,
            "ref": ref,
            "verify_packages": verify_packages,
            "verify_all_packages": verify_all_packages,
            "build": {
                "mode": "checkout-artifacts",
                "workdir": ".",
                "artifact_globs": ["*.deb"],
            },
        }
        override = overrides_by_name.get(name)
        if override and "build" in override:
            entry["build"] = copy_build_config(override["build"])
        entries.append(entry)

    return entries


def copy_build_config(build: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(build))


def _dedupe_package_names(package_infos: tuple["PackageInfo", ...]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for info in package_infos:
        if info.name and info.name not in seen:
            seen[info.name] = None
    return tuple(seen)


def _runtime_package_infos(
    package_infos: tuple["PackageInfo", ...],
) -> tuple["PackageInfo", ...]:
    return tuple(info for info in package_infos if _is_runtime_package_name(info.name))


def _is_runtime_package_name(name: str) -> bool:
    if not isinstance(name, str) or not name.startswith("lib"):
        return False
    if any(name.endswith(suffix) for suffix in RUNTIME_PACKAGE_SUFFIX_EXCLUDES):
        return False
    if any(name.startswith(prefix) for prefix in RUNTIME_PACKAGE_PREFIX_EXCLUDES):
        return False
    return True


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


def require_gh() -> None:
    if not shutil.which("gh"):
        raise BuildError("gh is required to download SafeLibs release artifacts")


def gh_api_json(endpoint: str) -> Any:
    result = run(["gh", "api", endpoint], capture_output=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise BuildError(f"failed to parse GitHub API response for {endpoint}") from exc


def release_tag_for_commit(commit_sha: str) -> str:
    commit = commit_sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BuildError(f"invalid commit SHA for release lookup: {commit_sha}")
    return f"{RELEASE_TAG_PREFIX}{commit[:RELEASE_TAG_COMMIT_CHARS]}"


def resolve_git_object_to_commit(github_repo: str, object_sha: str, object_type: str) -> str:
    seen_tags: set[str] = set()
    resolved_sha = object_sha
    resolved_type = object_type
    while resolved_type == "tag":
        if resolved_sha in seen_tags:
            raise BuildError(f"cycle while resolving tag object {resolved_sha} in {github_repo}")
        seen_tags.add(resolved_sha)
        tag_data = gh_api_json(f"repos/{github_repo}/git/tags/{resolved_sha}")
        tag_object = tag_data.get("object") if isinstance(tag_data, dict) else None
        if not isinstance(tag_object, dict):
            raise BuildError(f"failed to resolve tag object {resolved_sha} in {github_repo}")
        resolved_sha = str(tag_object.get("sha") or "").strip()
        resolved_type = str(tag_object.get("type") or "").strip()

    if resolved_type != "commit" or not resolved_sha:
        raise BuildError(
            f"GitHub ref in {github_repo} resolved to unsupported object type: {resolved_type}"
        )
    return resolved_sha


def resolve_ref_commit(github_repo: str, ref: str) -> str:
    require_gh()
    ref_name = ref.strip()
    if re.fullmatch(r"[0-9a-fA-F]{40}", ref_name):
        return ref_name.lower()
    if ref_name.startswith("refs/"):
        ref_name = ref_name.removeprefix("refs/")
    if not ref_name:
        raise BuildError(f"empty ref for {github_repo}")

    ref_data = gh_api_json(f"repos/{github_repo}/git/ref/{ref_name}")
    ref_object = ref_data.get("object") if isinstance(ref_data, dict) else None
    if not isinstance(ref_object, dict):
        raise BuildError(f"failed to resolve ref {ref} in {github_repo}")
    object_sha = str(ref_object.get("sha") or "").strip()
    object_type = str(ref_object.get("type") or "").strip()
    return resolve_git_object_to_commit(github_repo, object_sha, object_type)


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
            # Rewrite debian/changelog so the deb version becomes
            # <upstream>+safelibs<commit-epoch>. Using the commit's committer
            # date as the suffix keeps the version deterministic per commit
            # and monotonic across history, so apt clients reliably treat new
            # commits as upgrades without the port maintainer having to bump
            # the changelog.
            'upstream_version=$(dpkg-parsechangelog -S Version | sed -E "s/\\+safelibs[0-9]+$//")',
            'package_name=$(dpkg-parsechangelog -S Source)',
            'distribution=$(dpkg-parsechangelog -S Distribution)',
            'commit_epoch=$(git -C "$SAFEAPTREPO_SOURCE" log -1 --format=%ct HEAD)',
            'new_version="${upstream_version}+safelibs${commit_epoch}"',
            'release_date=$(date -u -R -d "@${commit_epoch}")',
            '{',
            '  printf "%s (%s) %s; urgency=medium\\n\\n  * Automated SafeLibs rebuild.\\n\\n -- SafeLibs CI <ci@safelibs.org>  %s\\n\\n" \\',
            '    "$package_name" "$new_version" "$distribution" "$release_date"',
            '  cat debian/changelog',
            '} > debian/changelog.new',
            'mv debian/changelog.new debian/changelog',
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
        capture_output=True,
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


def is_source_artifact(path: Path) -> bool:
    name = path.name
    if name.endswith(SOURCE_DSC_SUFFIX):
        return True
    return ".orig.tar." in name or ".debian.tar." in name


def parse_dsc(dsc_path: Path) -> tuple[str, str, list[str]]:
    """Return (Source, Version, file_basenames) extracted from a .dsc file.

    Handles both clearsigned and plain .dsc inputs and dedupes basenames
    referenced from any of Files / Checksums-Sha1 / Checksums-Sha256.
    """

    text = dsc_path.read_text()
    if text.startswith("-----BEGIN PGP SIGNED MESSAGE-----"):
        _, _, rest = text.partition("\n\n")
        signature_marker = rest.find("\n-----BEGIN PGP SIGNATURE-----")
        if signature_marker >= 0:
            rest = rest[:signature_marker]
        text = rest

    multiline_fields = {"Files", "Checksums-Sha1", "Checksums-Sha256"}
    source = ""
    version = ""
    seen_files: dict[str, None] = {}
    current_field: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip():
            current_field = None
            continue
        if raw_line[0] in (" ", "\t"):
            if current_field in multiline_fields:
                parts = raw_line.split()
                if len(parts) >= 3:
                    seen_files.setdefault(parts[-1], None)
            continue
        current_field = None
        if ":" not in raw_line:
            continue
        name, _, value = raw_line.partition(":")
        name = name.strip()
        value = value.strip()
        if name in multiline_fields:
            current_field = name
        elif name == "Source" and not source:
            source = value
        elif name == "Version" and not version:
            version = value
    if not source:
        raise BuildError(f"missing Source field in {dsc_path}")
    return source, version, list(seen_files)


def stage_source_packages(
    site_root: Path,
    component: str,
    source_paths: list[Path],
) -> list[SourceInfo]:
    if not source_paths:
        return []
    by_basename: dict[str, Path] = {}
    for path in source_paths:
        existing = by_basename.get(path.name)
        if existing is not None and existing != path:
            raise BuildError(
                f"duplicate source artifact basename {path.name!r}: {existing} vs {path}"
            )
        by_basename[path.name] = path

    dsc_paths = sorted(
        (path for path in source_paths if path.suffix == SOURCE_DSC_SUFFIX),
        key=lambda item: item.name,
    )
    if not dsc_paths:
        return []

    infos: list[SourceInfo] = []
    for dsc_path in dsc_paths:
        source, version, file_names = parse_dsc(dsc_path)
        pool_rel = Path("pool") / component / source[0] / source
        dest_dir = site_root / pool_rel
        dest_dir.mkdir(parents=True, exist_ok=True)
        dsc_dest = dest_dir / dsc_path.name
        shutil.copy2(dsc_path, dsc_dest)
        copied: list[Path] = [dsc_dest]
        for file_name in file_names:
            sibling = by_basename.get(file_name)
            if sibling is None:
                raise BuildError(
                    f"{dsc_path.name} references {file_name}, but it was not present "
                    "in the downloaded source artifacts"
                )
            file_dest = dest_dir / file_name
            if not file_dest.exists():
                shutil.copy2(sibling, file_dest)
            copied.append(file_dest)
        infos.append(
            SourceInfo(
                source=source,
                version=version,
                dsc_path=dsc_dest,
                pool_path=pool_rel / dsc_path.name,
                files=tuple(copied),
            )
        )
    return infos


def write_source_indexes(site_root: Path, suite: str, component: str) -> bool:
    sources_output = run(
        ["apt-ftparchive", "sources", "pool"],
        cwd=site_root,
        capture_output=True,
    ).stdout
    if not sources_output.strip():
        return False
    source_dir = site_root / "dists" / suite / component / "source"
    source_dir.mkdir(parents=True, exist_ok=True)
    sources_path = source_dir / "Sources"
    sources_path.write_text(sources_output)
    run(["gzip", "-9", "-kf", str(sources_path)])
    return True


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
    *,
    has_sources: bool = False,
) -> None:
    release_path = site_root / "dists" / suite / "Release"
    release_architectures = list(architectures)
    if has_sources and "source" not in release_architectures:
        release_architectures.append("source")
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
            f"APT::FTPArchive::Release::Architectures={' '.join(release_architectures)}",
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


def channel_label(channel_name: str) -> str:
    if channel_name == STABLE_CHANNEL_NAME:
        return "Stable"
    if channel_name == TESTING_CHANNEL_NAME:
        return "Testing"
    return channel_name.replace("-", " ").title()


def channel_sort_key(channel_name: str) -> tuple[int, str]:
    order = {
        STABLE_CHANNEL_NAME: 0,
        TESTING_CHANNEL_NAME: 1,
    }
    return (order.get(channel_name, len(order)), channel_name)


def repository_sort_key(repository: PublishedRepository) -> tuple[int, str, str]:
    channel_order, channel_name = channel_sort_key(repository.channel)
    return (channel_order, repository.name, channel_name)


def channel_class(channel_name: str) -> str:
    token = re.sub(r"[^a-z0-9_-]+", "-", channel_name.lower()).strip("-")
    return f"is-{token or 'channel'}"


def package_count_text(package_infos: tuple[PackageInfo, ...]) -> str:
    count = len(package_infos)
    suffix = "" if count == 1 else "s"
    return f"{count} published package{suffix}"


def package_summary(package_infos: tuple[PackageInfo, ...]) -> str:
    sorted_infos = sorted(package_infos, key=lambda item: item.name)
    return ", ".join(info.name for info in sorted_infos)


def aggregate_repository_description(repository: PublishedRepository) -> str:
    if repository.channel == TESTING_CHANNEL_NAME:
        return "Latest buildable default-branch packages across the SafeLibs ports."
    if repository.channel == STABLE_CHANNEL_NAME:
        return "Pinned SafeLibs releases across the full package set."
    return f"{channel_label(repository.channel)} aggregate repository for the full package set."


def render_featured_repository_card(repository: PublishedRepository) -> str:
    escaped_url = html.escape(repository.url)
    escaped_path = html.escape(repository.path)
    return "\n".join(
        [
            f'      <article class="primary-repo {channel_class(repository.channel)}">',
            '        <div class="repo-topline">',
            "          <span class=\"channel-pill\">"
            + html.escape(channel_label(repository.channel))
            + "</span>",
            "          <span>"
            + html.escape(package_count_text(repository.package_infos))
            + "</span>",
            "        </div>",
            f'        <h3><a href="{escaped_url}/"><code>/{escaped_path}/</code></a></h3>',
            f"        <p>{html.escape(aggregate_repository_description(repository))}</p>",
            "      </article>",
        ]
    )


def render_featured_repository_cards(repositories: list[PublishedRepository]) -> str:
    aggregate_repositories = sorted(
        (repo for repo in repositories if repo.name == ALL_REPOSITORY_NAME),
        key=repository_sort_key,
    )
    return "\n".join(
        render_featured_repository_card(repo) for repo in aggregate_repositories
    )


def render_repository_row(repository: PublishedRepository) -> str:
    escaped_url = html.escape(repository.url)
    escaped_name = html.escape(repository.name)
    escaped_path = html.escape(repository.path)
    return "\n".join(
        [
            "      <li class=\"repo-row\">",
            "        <span class=\"channel-pill "
            + html.escape(channel_class(repository.channel))
            + "\">"
            + html.escape(channel_label(repository.channel))
            + "</span>",
            f'        <a class="repo-name" href="{escaped_url}/">{escaped_name}</a>',
            f'        <span class="repo-path"><code>/{escaped_path}/</code></span>',
            "        <span class=\"repo-count\">"
            + html.escape(package_count_text(repository.package_infos))
            + "</span>",
            "        <span class=\"repo-packages\"><code>"
            + html.escape(package_summary(repository.package_infos))
            + "</code></span>",
            "      </li>",
        ]
    )


def render_repository_rows(repositories: list[PublishedRepository]) -> str:
    per_library_repositories = sorted(
        (repo for repo in repositories if repo.name != ALL_REPOSITORY_NAME),
        key=repository_sort_key,
    )
    return "\n".join(
        render_repository_row(repo) for repo in per_library_repositories
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
        heading=f"/{repository_name}/",
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
    all_repo_file_stem = repository_file_stem(key_name, all_repo.repository_id)
    if all_repo.channel == STABLE_CHANNEL_NAME:
        default_install_heading = "Default Stable Install"
        default_install_text = (
            "Installs the signed key, package pinning, and the stable "
            "<code>/all/</code> source."
        )
    else:
        default_install_heading = f"{channel_label(all_repo.channel)} Aggregate Install"
        default_install_text = (
            "Installs the signed key, package pinning, and the "
            f"<code>/{html.escape(all_repo.path)}/</code> source."
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
        default_install_heading=default_install_heading,
        default_install_text=default_install_text,
        preferences_download=f"{all_repo_file_stem}.pref",
        preferences_file=f"{all_repo_file_stem}.pref",
        list_file=f"{all_repo_file_stem}.list",
        featured_repository_cards=render_featured_repository_cards(repositories),
        repository_rows=render_repository_rows(repositories),
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
                "source_packages": [
                    {
                        "source": info.source,
                        "version": info.version,
                        "dsc": info.pool_path.as_posix(),
                    }
                    for info in sorted(repo.source_infos, key=lambda item: item.source)
                ],
                "verify_packages": list(repo.verify_packages),
                "verify_all_packages": list(repo.verify_all_packages),
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
    source_paths: list[Path] | None = None,
) -> tuple[list[PackageInfo], list[SourceInfo]]:
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

    source_infos = stage_source_packages(output_dir, component, list(source_paths or []))
    has_sources = write_source_indexes(output_dir, suite, component) if source_infos else False

    homedir, fingerprint, passphrase = signing_key or prepare_signing_key()
    export_public_key_binary(homedir, fingerprint, output_dir, str(archive["key_name"]))
    write_release_file(
        output_dir,
        suite,
        component,
        architectures,
        archive,
        has_sources=has_sources,
    )
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
    return infos, source_infos


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
    repository_source_artifacts: dict[str, list[Path]] | None = None,
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
    source_artifacts = dict(repository_source_artifacts or {})
    if ALL_REPOSITORY_NAME in source_artifacts:
        raise BuildError(f"repository name '{ALL_REPOSITORY_NAME}' is reserved")
    unexpected_source_names = sorted(
        name for name in source_artifacts if name not in configured_names
    )
    if unexpected_source_names:
        raise BuildError(
            "unexpected source artifacts for unknown repositories: "
            + ", ".join(unexpected_source_names)
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
    all_source_paths = dedupe_paths(
        [path for name in repository_names for path in source_artifacts.get(name, [])]
    )

    if clean_output and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    resolved_signing_key = signing_key or prepare_signing_key()
    _, fingerprint, _ = resolved_signing_key

    aggregate_path = repository_path(path_prefix, ALL_REPOSITORY_NAME)
    aggregate_id = repository_id(path_prefix, ALL_REPOSITORY_NAME)

    aggregate_package_infos_list, aggregate_source_infos_list = generate_site_from_artifacts(
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
        source_paths=all_source_paths,
    )
    aggregate_package_infos = tuple(aggregate_package_infos_list)
    aggregate_source_infos = tuple(aggregate_source_infos_list)

    published_repositories = [
        PublishedRepository(
            channel=channel_name,
            name=ALL_REPOSITORY_NAME,
            path=aggregate_path,
            repository_id=aggregate_id,
            url=join_url(base_url, aggregate_path),
            package_infos=aggregate_package_infos,
            verify_packages=_dedupe_package_names(aggregate_package_infos),
            verify_all_packages=_dedupe_package_names(
                _runtime_package_infos(aggregate_package_infos)
            ),
            source_infos=aggregate_source_infos,
        )
    ]

    for repository_name in repository_names:
        repo_path = repository_path(path_prefix, repository_name)
        repo_id = repository_id(path_prefix, repository_name)
        repo_package_infos_list, repo_source_infos_list = generate_site_from_artifacts(
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
            source_paths=source_artifacts.get(repository_name, []),
        )
        repo_package_infos = tuple(repo_package_infos_list)
        repo_source_infos = tuple(repo_source_infos_list)
        published_repositories.append(
            PublishedRepository(
                channel=channel_name,
                name=repository_name,
                path=repo_path,
                repository_id=repo_id,
                url=join_url(base_url, repo_path),
                package_infos=repo_package_infos,
                verify_packages=_dedupe_package_names(repo_package_infos),
                verify_all_packages=_dedupe_package_names(
                    _runtime_package_infos(repo_package_infos)
                ),
                source_infos=repo_source_infos,
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
) -> tuple[dict[str, list[Path]], dict[str, list[Path]]]:
    repository_artifacts: dict[str, list[Path]] = {}
    repository_source_artifacts: dict[str, list[Path]] = {}
    for entry in entries:
        repo_dir = artifact_root / str(entry["name"])
        artifacts = dedupe_paths(sorted(repo_dir.glob("*.deb")))
        if artifacts:
            repository_artifacts[repo_dir.name] = artifacts
        sources: list[Path] = []
        for pattern in SOURCE_ARTIFACT_PATTERNS:
            sources.extend(sorted(repo_dir.glob(pattern)))
        sources = dedupe_paths(sources)
        if sources:
            repository_source_artifacts[repo_dir.name] = sources
    return repository_artifacts, repository_source_artifacts


def download_release_artifacts(
    entry: dict[str, Any],
    artifact_root: Path,
) -> tuple[list[Path], list[Path]]:
    require_gh()
    build = dict(entry["build"])
    output_dir = artifact_root / str(entry["name"])
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    github_repo = str(entry["github_repo"])
    commit_sha = resolve_ref_commit(github_repo, str(entry["ref"]))
    release_tag = release_tag_for_commit(commit_sha)

    base_args = [
        "gh",
        "release",
        "download",
        release_tag,
        "--repo",
        github_repo,
        "--dir",
        str(output_dir),
    ]
    binary_args = list(base_args)
    for pattern in build["artifact_globs"]:
        binary_args.extend(["--pattern", str(pattern)])
    run(binary_args, capture_output=True)

    binaries: list[Path] = []
    for pattern in build["artifact_globs"]:
        binaries.extend(sorted(output_dir.glob(str(pattern))))
    binaries = dedupe_paths(binaries)
    if not binaries:
        raise BuildError(
            f"no release artifacts found for {entry['name']} in "
            f"{github_repo} release {release_tag} ({commit_sha})"
        )

    source_args = list(base_args)
    for pattern in SOURCE_ARTIFACT_PATTERNS:
        source_args.extend(["--pattern", pattern])
    try:
        run(source_args, capture_output=True)
    except BuildError as exc:
        # Older port releases may not yet publish source artifacts. Treat
        # "no source assets found" as soft-missing so the binary publish
        # still succeeds; surface anything else.
        if "no assets" not in str(exc).lower() and "no asset" not in str(exc).lower():
            print(
                f"warning: source artifact download failed for {entry['name']}: {exc}",
                file=sys.stderr,
            )

    sources: list[Path] = []
    for pattern in SOURCE_ARTIFACT_PATTERNS:
        sources.extend(sorted(output_dir.glob(pattern)))
    sources = dedupe_paths(sources)

    return binaries, sources


def download_repository_entries(
    entries: list[dict[str, Any]],
    *,
    artifact_root: Path,
    allow_failures: bool,
    channel_name: str,
) -> tuple[dict[str, list[Path]], dict[str, list[Path]], list[dict[str, Any]]]:
    repository_artifacts: dict[str, list[Path]] = {}
    repository_source_artifacts: dict[str, list[Path]] = {}
    downloaded_entries: list[dict[str, Any]] = []
    for entry in entries:
        print(f"downloading {channel_name}/{entry['name']}", file=sys.stderr)
        try:
            binaries, sources = download_release_artifacts(entry, artifact_root)
        except BuildError as exc:
            if not allow_failures:
                raise
            print(
                f"skipping {channel_name}/{entry['name']}: {exc}",
                file=sys.stderr,
            )
            continue
        name = str(entry["name"])
        repository_artifacts[name] = binaries
        if sources:
            repository_source_artifacts[name] = sources
        downloaded_entries.append(entry)
        binary_word = "binary" if len(binaries) == 1 else "binaries"
        message = f"downloaded {channel_name}/{entry['name']}: {len(binaries)} {binary_word}"
        if sources:
            source_word = "artifact" if len(sources) == 1 else "artifacts"
            message += f", {len(sources)} source {source_word}"
        print(message, file=sys.stderr)
    return repository_artifacts, repository_source_artifacts, downloaded_entries


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the SafeLibs GitHub Pages apt repository")
    parser.add_argument("--config", type=Path, default=Path("repositories.yml"))
    parser.add_argument("--output", type=Path, default=Path("site"))
    parser.add_argument("--workspace", type=Path, default=Path(".work"))
    parser.add_argument("--base-url", default="")
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument(
        "--validator-url",
        default=None,
        help="Override the validator site-data URL used to filter the stable channel.",
    )
    parser.add_argument(
        "--validator-mode",
        default=None,
        help="Override the validator proof mode whose passing libraries become the stable selection.",
    )
    parser.add_argument(
        "--skip-validator-filter",
        action="store_true",
        help="Skip dynamic validator filtering and publish every entry from repositories.yml.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    archive = config["archive"]
    base_url = args.base_url or str(archive["base_url"])
    artifact_root = args.workspace / "artifacts"
    artifact_root.mkdir(parents=True, exist_ok=True)
    repository_template_path = Path(__file__).resolve().parent.parent / "templates" / "index.html"
    landing_template_path = Path(__file__).resolve().parent.parent / "templates" / "landing.html"

    validator_config = config.get("validator") if isinstance(config.get("validator"), dict) else {}
    validator_url = (
        args.validator_url
        or str(validator_config.get("site_url") or DEFAULT_VALIDATOR_SITE_URL)
    )
    validator_mode = (
        args.validator_mode
        or str(validator_config.get("mode") or DEFAULT_VALIDATOR_MODE)
    )

    if args.skip_validator_filter:
        if not config.get("repositories"):
            raise BuildError(
                "skip_validator_filter requires repositories: to be defined in the config"
            )
        stable_entries = list(config["repositories"])
    else:
        site_data = fetch_validator_site_data(validator_url)
        stable_entries = synthesize_repository_entries(
            site_data,
            validator_mode,
            overrides=config.get("port_build_overrides") or [],
        )
        if not stable_entries:
            raise BuildError(
                f"validator at {validator_url} mode {validator_mode!r} reports no validating "
                f"libraries; refusing to publish an empty stable channel"
            )
        print(
            f"validator at {validator_url} mode {validator_mode!r} synthesized "
            f"{len(stable_entries)} stable channel entry/entries from validator data: "
            f"{', '.join(entry['name'] for entry in stable_entries)}"
        )
    testing_entries = resolve_testing_repositories(config)
    testing_config = config.get("testing") if isinstance(config.get("testing"), dict) else {}
    testing_allow_failures = bool(testing_config.get("allow_build_failures", False))

    if args.skip_build:
        stable_artifacts, stable_source_artifacts = collect_cached_artifacts(
            stable_entries, artifact_root
        )
        testing_artifacts, testing_source_artifacts = collect_cached_artifacts(
            testing_entries,
            artifact_root / TESTING_CHANNEL_NAME,
        )
        downloaded_testing_entries = [
            entry for entry in testing_entries if str(entry["name"]) in testing_artifacts
        ]
    else:
        stable_artifacts, stable_source_artifacts, _ = download_repository_entries(
            stable_entries,
            artifact_root=artifact_root,
            allow_failures=False,
            channel_name=STABLE_CHANNEL_NAME,
        )
        (
            testing_artifacts,
            testing_source_artifacts,
            downloaded_testing_entries,
        ) = download_repository_entries(
            testing_entries,
            artifact_root=artifact_root / TESTING_CHANNEL_NAME,
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
            repository_source_artifacts=stable_source_artifacts,
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
                repository_source_artifacts=stable_source_artifacts,
            )
        )
        if testing_artifacts:
            published_repositories.extend(
                generate_split_site(
                    config_with_repositories(config, downloaded_testing_entries),
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
                    repository_source_artifacts=testing_source_artifacts,
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
