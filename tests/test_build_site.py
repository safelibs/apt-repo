from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from tools import build_site


def make_deb(root: Path, package: str, version: str) -> Path:
    pkg_root = root / package
    (pkg_root / "DEBIAN").mkdir(parents=True)
    (pkg_root / "usr/share/doc" / package).mkdir(parents=True)
    (pkg_root / "usr/share/doc" / package / "README").write_text("ok\n")
    (pkg_root / "DEBIAN" / "control").write_text(
        "\n".join(
            [
                f"Package: {package}",
                f"Version: {version}",
                "Section: libs",
                "Priority: optional",
                "Architecture: amd64",
                "Maintainer: SafeLibs <test@safelibs.invalid>",
                "Description: test package",
                "",
            ]
        )
    )
    deb_path = root / f"{package}_{version}_amd64.deb"
    subprocess.run(["dpkg-deb", "--build", str(pkg_root), str(deb_path)], check=True)
    return deb_path


def completed(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def archive_config() -> dict[str, str]:
    return {
        "suite": "noble",
        "component": "main",
        "origin": "SafeLibs",
        "label": "SafeLibs",
        "description": "Test repo for Ubuntu 24.04",
        "homepage": "https://example.invalid/project",
        "base_url": "https://example.invalid/apt/",
        "key_name": "safelibs",
        "image": "ubuntu:24.04",
    }


def repo_config(name: str = "demo") -> dict[str, object]:
    return {
        "name": name,
        "github_repo": f"safelibs/port-{name}",
        "ref": f"refs/tags/{name}/04-test",
        "build": {
            "workdir": ".",
            "command": "./build.sh",
            "artifact_globs": ["*.deb"],
        },
    }


def config_with_repo(
    *,
    archive: dict[str, object] | None = None,
    repository: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "archive": copy.deepcopy(archive if archive is not None else archive_config()),
        "repositories": [copy.deepcopy(repository if repository is not None else repo_config())],
    }


def write_config(path: Path, data: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(data))


class BuildSiteTests(unittest.TestCase):
    def test_run_wraps_subprocess_failures(self) -> None:
        error = subprocess.CalledProcessError(
            returncode=2,
            cmd=["git", "status"],
            output="stdout line\n",
            stderr="stderr line\n",
        )
        with mock.patch("tools.build_site.subprocess.run", side_effect=error):
            with self.assertRaises(build_site.BuildError) as ctx:
                build_site.run(["git", "status"], cwd=Path("/tmp/test-run"))

        message = str(ctx.exception)
        self.assertIn("git status failed", message)
        self.assertIn("cwd=/tmp/test-run", message)
        self.assertIn("stdout line", message)
        self.assertIn("stderr line", message)

    def test_run_truncates_large_captured_failures(self) -> None:
        error = subprocess.CalledProcessError(
            returncode=2,
            cmd=["docker", "run"],
            output="early stdout\n" + ("x" * (build_site.MAX_FAILURE_OUTPUT_CHARS + 20)),
            stderr="early stderr\n" + ("y" * (build_site.MAX_FAILURE_OUTPUT_CHARS + 20)),
        )
        with mock.patch("tools.build_site.subprocess.run", side_effect=error):
            with self.assertRaises(build_site.BuildError) as ctx:
                build_site.run(["docker", "run"], capture_output=True)

        message = str(ctx.exception)
        self.assertIn("[stdout truncated", message)
        self.assertIn("[stderr truncated", message)
        self.assertNotIn("early stdout", message)
        self.assertNotIn("early stderr", message)

    def test_load_config_rejects_non_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            config_path.write_text("- not-a-mapping\n")
            with self.assertRaisesRegex(build_site.BuildError, "must contain a YAML mapping"):
                build_site.load_config(config_path)

    def test_load_config_requires_archive_and_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            write_config(config_path, {"archive": archive_config()})
            with self.assertRaisesRegex(
                build_site.BuildError, "must define archive and repositories"
            ):
                build_site.load_config(config_path)

    def test_load_config_requires_archive_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            write_config(config_path, {"archive": [], "repositories": [repo_config()]})
            with self.assertRaisesRegex(build_site.BuildError, "archive must be a YAML mapping"):
                build_site.load_config(config_path)

    def test_load_config_requires_non_empty_repository_list(self) -> None:
        for repositories in ({}, []):
            with self.subTest(repositories=repositories):
                with tempfile.TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "repositories.yml"
                    write_config(
                        config_path,
                        {"archive": archive_config(), "repositories": repositories},
                    )
                    with self.assertRaisesRegex(
                        build_site.BuildError, "must define a non-empty repositories list"
                    ):
                        build_site.load_config(config_path)

    def test_load_config_requires_each_archive_field(self) -> None:
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
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "repositories.yml"
                    config = config_with_repo()
                    config["archive"][field] = "   "
                    write_config(config_path, config)
                    with self.assertRaisesRegex(
                        build_site.BuildError, rf"archive must define {field}"
                    ):
                        build_site.load_config(config_path)

    def test_load_config_requires_repository_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            write_config(config_path, {"archive": archive_config(), "repositories": ["broken"]})
            with self.assertRaisesRegex(
                build_site.BuildError, r"repository #1 must be a YAML mapping"
            ):
                build_site.load_config(config_path)

    def test_load_config_requires_each_repository_field(self) -> None:
        for field in ["name", "github_repo", "ref"]:
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "repositories.yml"
                    config = config_with_repo()
                    config["repositories"][0][field] = "  "
                    write_config(config_path, config)
                    with self.assertRaisesRegex(
                        build_site.BuildError, rf"repository #1 must define {field}"
                    ):
                        build_site.load_config(config_path)

    def test_load_config_rejects_reserved_all_repository_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            config = config_with_repo(repository=repo_config(build_site.ALL_REPOSITORY_NAME))
            write_config(config_path, config)
            with self.assertRaisesRegex(build_site.BuildError, r"name 'all' is reserved"):
                build_site.load_config(config_path)

    def test_load_config_rejects_duplicate_repository_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            config = {
                "archive": archive_config(),
                "repositories": [repo_config("alpha"), repo_config("alpha")],
            }
            write_config(config_path, config)
            with self.assertRaisesRegex(
                build_site.BuildError, r"defines duplicate repository name: alpha"
            ):
                build_site.load_config(config_path)

    def test_load_config_requires_build_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            config = config_with_repo()
            config["repositories"][0]["build"] = None
            write_config(config_path, config)
            with self.assertRaisesRegex(build_site.BuildError, r"repository #1 must define build"):
                build_site.load_config(config_path)

    def test_load_config_requires_artifact_globs_list(self) -> None:
        for artifact_globs in (None, [], "*.deb"):
            with self.subTest(artifact_globs=artifact_globs):
                with tempfile.TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "repositories.yml"
                    config = config_with_repo()
                    config["repositories"][0]["build"]["artifact_globs"] = artifact_globs
                    write_config(config_path, config)
                    with self.assertRaisesRegex(
                        build_site.BuildError, r"repository #1 build must define artifact_globs"
                    ):
                        build_site.load_config(config_path)

    def test_load_config_requires_non_empty_artifact_globs(self) -> None:
        for artifact_globs in ([""], ["*.deb", "   "]):
            with self.subTest(artifact_globs=artifact_globs):
                with tempfile.TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "repositories.yml"
                    config = config_with_repo()
                    config["repositories"][0]["build"]["artifact_globs"] = artifact_globs
                    write_config(config_path, config)
                    with self.assertRaisesRegex(
                        build_site.BuildError,
                        r"repository #1 build artifact_globs must be non-empty",
                    ):
                        build_site.load_config(config_path)

    def test_load_config_validates_repository_build_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            write_config(
                config_path,
                config_with_repo(
                    repository={
                        "name": "broken",
                        "github_repo": "safelibs/port-broken",
                        "ref": "refs/tags/broken/04-test",
                        "build": {"artifact_globs": ["*.deb"]},
                    }
                ),
            )
            with self.assertRaisesRegex(
                build_site.BuildError, "docker build must define command"
            ):
                build_site.load_config(config_path)

    def test_load_config_allows_checkout_artifacts_without_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            write_config(
                config_path,
                config_with_repo(
                    repository={
                        "name": "artifacts-only",
                        "github_repo": "safelibs/port-artifacts-only",
                        "ref": "refs/tags/artifacts-only/04-test",
                        "build": {
                            "mode": "checkout-artifacts",
                            "workdir": ".",
                            "artifact_globs": ["*.deb"],
                        },
                    }
                ),
            )
            loaded = build_site.load_config(config_path)

        self.assertEqual(loaded["repositories"][0]["build"]["mode"], "checkout-artifacts")
        self.assertNotIn("command", loaded["repositories"][0]["build"])

    def test_load_config_allows_safe_debian_without_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "repositories.yml"
            write_config(
                config_path,
                config_with_repo(
                    repository={
                        "name": "safe-debian",
                        "github_repo": "safelibs/port-safe-debian",
                        "ref": "refs/tags/safe-debian/04-test",
                        "build": {
                            "mode": "safe-debian",
                            "artifact_globs": ["*.deb"],
                        },
                    }
                ),
            )
            loaded = build_site.load_config(config_path)

        self.assertEqual(loaded["repositories"][0]["build"]["mode"], "safe-debian")
        self.assertNotIn("command", loaded["repositories"][0]["build"])

    def test_load_config_accepts_checked_in_repositories_file(self) -> None:
        config_path = Path(__file__).resolve().parent.parent / "repositories.yml"
        loaded = build_site.load_config(config_path)

        self.assertEqual(loaded["archive"]["suite"], "noble")
        self.assertEqual(loaded["archive"]["key_name"], "safelibs")
        self.assertEqual(
            [entry["name"] for entry in loaded["repositories"]],
            [
                "cjson",
                "giflib",
                "libarchive",
                "libbz2",
                "libcsv",
                "libexif",
                "libjansson",
                "libjpeg-turbo",
                "libjson",
                "liblzma",
                "libpng",
                "libsdl",
                "libsodium",
                "libtiff",
                "libuv",
                "libvips",
                "libwebp",
                "libxml",
                "libyaml",
                "libzstd",
            ],
        )
        repositories_by_name = {entry["name"]: entry for entry in loaded["repositories"]}
        self.assertEqual(repositories_by_name["cjson"]["ref"], "refs/tags/build-de29489668c1")
        self.assertEqual(repositories_by_name["cjson"]["build"]["mode"], "safe-debian")
        verify_packages_by_name = {
            entry["name"]: entry["verify_packages"]
            for entry in loaded["repositories"]
            if "verify_packages" in entry
        }
        self.assertEqual(
            verify_packages_by_name,
            {
                "cjson": ["libcjson-dev", "libcjson1"],
                "giflib": ["libgif-dev", "libgif7"],
                "libarchive": [
                    "libarchive-dev",
                    "libarchive-tools",
                    "libarchive13t64",
                ],
                "libbz2": ["bzip2-doc", "bzip2", "libbz2-1.0", "libbz2-dev"],
                "libcsv": ["libcsv-dev", "libcsv3"],
                "libexif": ["libexif-dev", "libexif-doc", "libexif12"],
                "libjansson": ["libjansson-dev", "libjansson4"],
                "libjpeg-turbo": [
                    "libjpeg-turbo-progs",
                    "libjpeg-turbo8-dev",
                    "libjpeg-turbo8",
                    "libturbojpeg-java",
                    "libturbojpeg0-dev",
                    "libturbojpeg",
                ],
                "libjson": ["libjson-c-dev", "libjson-c5"],
                "liblzma": ["liblzma-dev", "liblzma5"],
                "libpng": ["libpng-dev", "libpng-tools", "libpng16-16t64"],
                "libsdl": ["libsdl2-2.0-0", "libsdl2-dev", "libsdl2-tests"],
                "libsodium": ["libsodium-dev", "libsodium23"],
                "libtiff": ["libtiff-dev", "libtiff-tools", "libtiff6", "libtiffxx6"],
                "libuv": ["libuv1-dev", "libuv1t64"],
                "libvips": [
                    "gir1.2-vips-8.0",
                    "libvips-dev",
                    "libvips-doc",
                    "libvips-tools",
                    "libvips42t64",
                ],
                "libwebp": [
                    "libsharpyuv-dev",
                    "libsharpyuv0",
                    "libwebp-dev",
                    "libwebp7",
                    "libwebpdecoder3",
                    "libwebpdemux2",
                    "libwebpmux3",
                    "webp",
                ],
                "libxml": ["libxml2-dev", "libxml2-utils", "libxml2", "python3-libxml2"],
                "libyaml": ["libyaml-0-2", "libyaml-dev", "libyaml-doc"],
                "libzstd": ["libzstd-dev", "libzstd1", "zstd"],
            },
        )
        self.assertEqual(
            {
                entry["name"]: entry["verify_all_packages"]
                for entry in loaded["repositories"]
                if "verify_all_packages" in entry
            },
            {
                "cjson": ["libcjson1"],
                "giflib": ["libgif7"],
                "libarchive": ["libarchive13t64"],
                "libbz2": ["libbz2-1.0"],
                "libcsv": ["libcsv3"],
                "libexif": ["libexif12"],
                "libjansson": ["libjansson4"],
                "libjpeg-turbo": ["libjpeg-turbo8", "libturbojpeg"],
                "libjson": ["libjson-c5"],
                "liblzma": ["liblzma5"],
                "libpng": ["libpng16-16t64"],
                "libsdl": ["libsdl2-2.0-0"],
                "libsodium": ["libsodium23"],
                "libtiff": ["libtiff6"],
                "libuv": ["libuv1t64"],
                "libvips": ["libvips-doc"],
                "libwebp": ["libwebp7"],
                "libxml": ["libxml2"],
                "libyaml": ["libyaml-0-2"],
                "libzstd": ["libzstd1"],
            },
        )
        self.assertEqual(repositories_by_name["libcsv"]["build"]["mode"], "checkout-artifacts")
        self.assertNotIn("command", repositories_by_name["libcsv"]["build"])
        self.assertEqual(repositories_by_name["libpng"]["build"]["mode"], "checkout-artifacts")
        self.assertNotIn("command", repositories_by_name["libpng"]["build"])
        self.assertEqual(repositories_by_name["libuv"]["ref"], "refs/tags/build-a2d0955c60f5")
        self.assertEqual(repositories_by_name["libuv"]["build"]["mode"], "safe-debian")
        self.assertEqual(repositories_by_name["libvips"]["build"]["mode"], "docker")
        self.assertIn("build-check-install", repositories_by_name["libvips"]["build"]["command"])
        self.assertIn(
            "dpkg-architecture -qDEB_HOST_MULTIARCH",
            repositories_by_name["libvips"]["build"]["command"],
        )
        self.assertIn(
            'cp -a build-check-install/lib/"$(dpkg-architecture -qDEB_HOST_MULTIARCH)"/libvips*.so*',
            repositories_by_name["libvips"]["build"]["command"],
        )
        self.assertIn("DEB_BUILD_OPTIONS", repositories_by_name["libvips"]["build"]["command"])
        self.assertIn("nocheck", repositories_by_name["libvips"]["build"]["command"])
        self.assertEqual(loaded["testing"]["discover"]["github_org"], "safelibs")
        self.assertTrue(loaded["testing"]["allow_build_failures"])
        self.assertEqual(loaded["testing"]["default_build"]["mode"], "safe-debian")
        self.assertEqual(
            [entry["name"] for entry in loaded["testing"]["repository_overrides"]],
            ["libc6", "libjansson"],
        )
        self.assertIn(
            'package-deb --out "$SAFEAPTREPO_OUTPUT/debs"',
            loaded["testing"]["repository_overrides"][0]["build"]["command"],
        )
        self.assertIn(
            'cp -v "$SAFEAPTREPO_OUTPUT"/debs/*.deb "$SAFEAPTREPO_OUTPUT"/',
            loaded["testing"]["repository_overrides"][0]["build"]["command"],
        )

    def test_clone_or_update_repo_refreshes_existing_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp) / "repo"
            target_dir.mkdir()
            with mock.patch("tools.build_site.run") as run_mock:
                build_site.clone_or_update_repo("safelibs/port-demo", target_dir)

        self.assertEqual(
            [call.args[0] for call in run_mock.call_args_list],
            [
                ["git", "-C", str(target_dir), "reset", "--hard", "HEAD"],
                ["git", "-C", str(target_dir), "clean", "-fdx"],
                ["git", "-C", str(target_dir), "fetch", "--tags", "--prune", "origin"],
            ],
        )

    def test_clone_or_update_repo_clones_with_gh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp) / "repo"
            with (
                mock.patch("tools.build_site.shutil.which", return_value="/usr/bin/gh"),
                mock.patch("tools.build_site.run") as run_mock,
            ):
                build_site.clone_or_update_repo("safelibs/port-demo", target_dir)

        run_mock.assert_called_once_with(["gh", "repo", "clone", "safelibs/port-demo", str(target_dir)])

    def test_clone_or_update_repo_requires_gh_for_new_repos(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target_dir = Path(tmp) / "repo"
            with mock.patch("tools.build_site.shutil.which", return_value=None):
                with self.assertRaisesRegex(
                    build_site.BuildError, "gh is required to clone private safelibs repositories"
                ):
                    build_site.clone_or_update_repo("safelibs/port-demo", target_dir)

    def test_sync_repo_checks_out_requested_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            entry = repo_config("alpha")
            target_dir = source_root / "alpha"
            with (
                mock.patch("tools.build_site.clone_or_update_repo") as clone_mock,
                mock.patch("tools.build_site.run") as run_mock,
            ):
                result = build_site.sync_repo(entry, source_root)

        clone_mock.assert_called_once_with("safelibs/port-alpha", target_dir)
        run_mock.assert_called_once_with(
            ["git", "-C", str(target_dir), "checkout", "--detach", "refs/tags/alpha/04-test"]
        )
        self.assertEqual(result, target_dir)

    def test_sync_repo_checks_out_branch_refs_from_origin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source_root = Path(tmp)
            entry = {**repo_config("alpha"), "ref": "refs/heads/main"}
            target_dir = source_root / "alpha"
            with (
                mock.patch("tools.build_site.clone_or_update_repo") as clone_mock,
                mock.patch("tools.build_site.run") as run_mock,
            ):
                result = build_site.sync_repo(entry, source_root)

        clone_mock.assert_called_once_with("safelibs/port-alpha", target_dir)
        run_mock.assert_called_once_with(
            ["git", "-C", str(target_dir), "checkout", "--detach", "origin/main"]
        )
        self.assertEqual(result, target_dir)

    def test_release_tag_for_commit_uses_short_build_sha(self) -> None:
        self.assertEqual(
            build_site.release_tag_for_commit("0123456789abcdef0123456789abcdef01234567"),
            "build-0123456789ab",
        )

    def test_resolve_ref_commit_resolves_lightweight_tag(self) -> None:
        commit_sha = "0123456789abcdef0123456789abcdef01234567"
        github_output = json.dumps({"object": {"sha": commit_sha, "type": "commit"}})

        with (
            mock.patch("tools.build_site.shutil.which", return_value="/usr/bin/gh"),
            mock.patch("tools.build_site.run", return_value=completed(stdout=github_output)) as run_mock,
        ):
            resolved = build_site.resolve_ref_commit(
                "safelibs/port-demo",
                "refs/tags/demo/04-test",
            )

        self.assertEqual(resolved, commit_sha)
        run_mock.assert_called_once_with(
            ["gh", "api", "repos/safelibs/port-demo/git/ref/tags/demo/04-test"],
            capture_output=True,
        )

    def test_resolve_ref_commit_dereferences_annotated_tag(self) -> None:
        tag_sha = "abcdef0123456789abcdef0123456789abcdef01"
        commit_sha = "fedcba9876543210fedcba9876543210fedcba98"

        with (
            mock.patch("tools.build_site.shutil.which", return_value="/usr/bin/gh"),
            mock.patch("tools.build_site.run") as run_mock,
        ):
            run_mock.side_effect = [
                completed(stdout=json.dumps({"object": {"sha": tag_sha, "type": "tag"}})),
                completed(stdout=json.dumps({"object": {"sha": commit_sha, "type": "commit"}})),
            ]
            resolved = build_site.resolve_ref_commit(
                "safelibs/port-demo",
                "refs/tags/demo/04-test",
            )

        self.assertEqual(resolved, commit_sha)
        self.assertEqual(
            [call.args[0] for call in run_mock.call_args_list],
            [
                ["gh", "api", "repos/safelibs/port-demo/git/ref/tags/demo/04-test"],
                ["gh", "api", f"repos/safelibs/port-demo/git/tags/{tag_sha}"],
            ],
        )

    def test_download_release_artifacts_uses_ref_commit_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp) / "artifacts"
            artifact_name = "demo_1.0_amd64.deb"

            def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(args[:3], ["gh", "release", "download"])
                self.assertIn("build-0123456789ab", args)
                self.assertIn("--pattern", args)
                self.assertIn("*.deb", args)
                output_dir = Path(args[args.index("--dir") + 1])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / artifact_name).write_text("deb")
                return completed()

            with (
                mock.patch("tools.build_site.shutil.which", return_value="/usr/bin/gh"),
                mock.patch(
                    "tools.build_site.resolve_ref_commit",
                    return_value="0123456789abcdef0123456789abcdef01234567",
                ) as resolve_mock,
                mock.patch("tools.build_site.run", side_effect=fake_run) as run_mock,
            ):
                artifacts = build_site.download_release_artifacts(
                    repo_config("demo"),
                    artifact_root,
                )

            resolve_mock.assert_called_once_with("safelibs/port-demo", "refs/tags/demo/04-test")
            run_mock.assert_called_once()
            self.assertEqual([path.name for path in artifacts], [artifact_name])
            self.assertTrue((artifact_root / "demo" / artifact_name).exists())

    def test_download_release_artifacts_requires_downloaded_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_root = Path(tmp) / "artifacts"

            with (
                mock.patch("tools.build_site.shutil.which", return_value="/usr/bin/gh"),
                mock.patch(
                    "tools.build_site.resolve_ref_commit",
                    return_value="0123456789abcdef0123456789abcdef01234567",
                ),
                mock.patch("tools.build_site.run", return_value=completed()),
            ):
                with self.assertRaisesRegex(build_site.BuildError, "no release artifacts found"):
                    build_site.download_release_artifacts(repo_config("demo"), artifact_root)

    def test_resolve_testing_repositories_discovers_ports_and_applies_overrides(self) -> None:
        github_output = json.dumps(
            [
                {
                    "name": "port-beta",
                    "defaultBranchRef": {"name": "develop"},
                    "isArchived": False,
                },
                {
                    "name": "port-alpha",
                    "defaultBranchRef": {"name": "main"},
                    "isArchived": False,
                },
                {
                    "name": "not-a-port",
                    "defaultBranchRef": {"name": "main"},
                    "isArchived": False,
                },
                {
                    "name": "port-old",
                    "defaultBranchRef": {"name": "main"},
                    "isArchived": True,
                },
            ]
        )
        config = {
            "repositories": [
                {
                    "name": "alpha",
                    "github_repo": "safelibs/port-alpha",
                    "ref": "refs/tags/alpha/04-test",
                    "build": {
                        "mode": "checkout-artifacts",
                        "workdir": ".",
                        "artifact_globs": ["*.deb"],
                    },
                }
            ],
            "testing": {
                "discover": {"github_org": "safelibs", "repository_prefix": "port-"},
                "default_build": {"mode": "safe-debian", "artifact_globs": ["*.deb"]},
                "repository_overrides": [
                    {
                        "name": "beta",
                        "build": {"packages": ["pkg-config"]},
                    },
                    {
                        "name": "gamma",
                        "github_repo": "safelibs/port-gamma",
                        "ref": "refs/heads/main",
                    },
                ],
            }
        }

        with mock.patch("tools.build_site.run", return_value=completed(stdout=github_output)):
            entries = build_site.resolve_testing_repositories(config)

        self.assertEqual([entry["name"] for entry in entries], ["alpha", "beta", "gamma"])
        self.assertEqual(entries[0]["ref"], "refs/heads/main")
        self.assertEqual(entries[0]["build"]["mode"], "checkout-artifacts")
        self.assertEqual(entries[0]["build"]["workdir"], ".")
        self.assertEqual(entries[1]["ref"], "refs/heads/develop")
        self.assertEqual(entries[1]["build"]["mode"], "safe-debian")
        self.assertEqual(entries[1]["build"]["packages"], ["pkg-config"])
        self.assertEqual(entries[2]["github_repo"], "safelibs/port-gamma")

    def test_build_repo_checkout_artifacts_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            artifact_root = tmp_path / "artifacts"
            source_dir.mkdir()
            artifact_root.mkdir()
            deb_path = make_deb(source_dir, "libgamma1", "3.0+safelibs1")

            artifacts = build_site.build_repo(
                {
                    "name": "libgamma",
                    "build": {
                        "mode": "checkout-artifacts",
                        "workdir": ".",
                        "artifact_globs": ["*.deb"],
                    },
                },
                source_dir,
                artifact_root,
                "ubuntu:24.04",
                [],
            )

            self.assertEqual([path.name for path in artifacts], [deb_path.name])
            self.assertTrue((artifact_root / "libgamma" / deb_path.name).exists())

    def test_build_repo_checkout_artifacts_mode_requires_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            artifact_root = tmp_path / "artifacts"
            source_dir.mkdir()
            artifact_root.mkdir()

            with self.assertRaisesRegex(build_site.BuildError, "no checked-in artifacts found"):
                build_site.build_repo(
                    {
                        "name": "libgamma",
                        "build": {
                            "mode": "checkout-artifacts",
                            "workdir": ".",
                            "artifact_globs": ["*.deb"],
                        },
                    },
                    source_dir,
                    artifact_root,
                    "ubuntu:24.04",
                    [],
                )

    def test_build_repo_rejects_missing_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            artifact_root = tmp_path / "artifacts"
            source_dir.mkdir()
            artifact_root.mkdir()

            with self.assertRaisesRegex(build_site.BuildError, "missing workdir"):
                build_site.build_repo(
                    {
                        "name": "demo",
                        "build": {
                            "workdir": "missing",
                            "command": "./build.sh",
                            "artifact_globs": ["*.deb"],
                        },
                    },
                    source_dir,
                    artifact_root,
                    "ubuntu:24.04",
                    [],
                )

    def test_build_repo_safe_debian_requires_debian_control(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            safe_dir = source_dir / "safe"
            artifact_root = tmp_path / "artifacts"
            safe_dir.mkdir(parents=True)
            artifact_root.mkdir()

            with self.assertRaisesRegex(build_site.BuildError, "missing debian/control"):
                build_site.build_repo(
                    {
                        "name": "demo",
                        "build": {
                            "mode": "safe-debian",
                            "artifact_globs": ["*.deb"],
                        },
                    },
                    source_dir,
                    artifact_root,
                    "ubuntu:24.04",
                    [],
                )

    def test_build_repo_rejects_unsupported_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            artifact_root = tmp_path / "artifacts"
            source_dir.mkdir()
            artifact_root.mkdir()

            with self.assertRaisesRegex(build_site.BuildError, "unsupported build mode"):
                build_site.build_repo(
                    {
                        "name": "demo",
                        "build": {
                            "mode": "host-shell",
                            "workdir": ".",
                            "command": "./build.sh",
                            "artifact_globs": ["*.deb"],
                        },
                    },
                    source_dir,
                    artifact_root,
                    "ubuntu:24.04",
                    [],
                )

    def test_build_repo_docker_mode_invokes_docker_and_collects_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            workdir = source_dir / "pkg"
            artifact_root = tmp_path / "artifacts"
            workdir.mkdir(parents=True)
            artifact_root.mkdir()
            output_dir = artifact_root / "demo"
            artifact_name = "demo_1.0_amd64.deb"

            entry = {
                "name": "demo",
                "build": {
                    "workdir": "pkg",
                    "packages": ["git", "make"],
                    "rustup_toolchain": "1.94.0",
                    "setup": "echo extra-setup",
                    "command": "./build.sh",
                    "artifact_globs": ["*.deb"],
                },
            }

            def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(args[:2], ["docker", "run"])
                self.assertTrue(kwargs["capture_output"])
                docker_script = args[-1]
                self.assertIn("apt-get install -y --no-install-recommends ca-certificates git make curl", docker_script)
                self.assertIn("rustc --version", docker_script)
                self.assertIn("cargo --version", docker_script)
                self.assertIn("echo extra-setup", docker_script)
                self.assertIn("cd pkg", docker_script)
                env = kwargs["env"]
                self.assertEqual(env["SAFEAPTREPO_SOURCE"], "/workspace/source")
                self.assertEqual(env["SAFEAPTREPO_OUTPUT"], "/workspace/output")
                self.assertEqual(env["SAFEDEBREPO_SOURCE"], "/workspace/source")
                self.assertEqual(env["SAFEDEBREPO_OUTPUT"], "/workspace/output")
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / artifact_name).write_text("deb")
                return completed()

            with mock.patch("tools.build_site.run", side_effect=fake_run) as run_mock:
                artifacts = build_site.build_repo(
                    entry,
                    source_dir,
                    artifact_root,
                    "ubuntu:24.04",
                    ["ca-certificates", "git"],
                )

        run_mock.assert_called_once()
        self.assertEqual([path.name for path in artifacts], [artifact_name])

    def test_detect_rust_toolchain_prefers_highest_manifest_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "Cargo.toml").write_text('\n'.join(['edition = "2021"', '']) )
            (workdir / "vendor").mkdir()
            (workdir / "vendor" / "Cargo.toml").write_text(
                '\n'.join(['rust-version = "1.92"', 'edition = "2024"', ''])
            )
            (workdir / "rust-toolchain.toml").write_text(
                '\n'.join(['[toolchain]', 'channel = "stable"', ''])
            )

            self.assertEqual(build_site.detect_rust_toolchain(workdir), "1.92")

    def test_detect_rust_toolchain_skips_ubuntu_default_or_older(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "Cargo.toml").write_text('edition = "2021"\n')

            self.assertEqual(build_site.detect_rust_toolchain(workdir), "")

    def test_detect_rust_toolchain_uses_named_channel_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "Cargo.toml").write_text('edition = "2021"\n')
            (workdir / "rust-toolchain.toml").write_text(
                '\n'.join(['[toolchain]', 'channel = "stable"', ''])
            )

            self.assertEqual(build_site.detect_rust_toolchain(workdir), "stable")

    def test_detect_rust_toolchain_uses_stable_for_modern_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "Cargo.toml").write_text('edition = "2021"\n')
            (workdir / "Cargo.lock").write_text(
                '\n'.join(
                    [
                        "# This file is automatically @generated by Cargo.",
                        "# It is not intended for manual editing.",
                        "version = 4",
                        "",
                    ]
                )
            )

            self.assertEqual(build_site.detect_rust_toolchain(workdir), "stable")

    def test_build_repo_safe_debian_mode_invokes_docker_and_collects_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            safe_dir = source_dir / "safe"
            artifact_root = tmp_path / "artifacts"
            safe_dir.mkdir(parents=True)
            (safe_dir / "debian").mkdir()
            (safe_dir / "debian" / "control").write_text("Source: demo\n")
            (safe_dir / "Cargo.toml").write_text('edition = "2024"\n')
            artifact_root.mkdir()
            output_dir = artifact_root / "demo"
            artifact_name = "demo_1.0_amd64.deb"

            def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(args[:2], ["docker", "run"])
                self.assertTrue(kwargs["capture_output"])
                docker_script = args[-1]
                self.assertIn(
                    "apt-get install -y --no-install-recommends ca-certificates file git jq python3 rsync xz-utils curl build-essential devscripts dpkg-dev equivs fakeroot",
                    docker_script,
                )
                self.assertIn("mk-build-deps -i -r -t", docker_script)
                self.assertIn("dpkg-buildpackage -us -uc -b", docker_script)
                self.assertIn('cp -v ../*.deb "$SAFEAPTREPO_OUTPUT"/', docker_script)
                self.assertIn("--default-toolchain 1.85", docker_script)
                self.assertIn("cd safe", docker_script)
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / artifact_name).write_text("deb")
                return completed()

            with mock.patch("tools.build_site.run", side_effect=fake_run) as run_mock:
                artifacts = build_site.build_repo(
                    {
                        "name": "demo",
                        "build": {
                            "mode": "safe-debian",
                            "artifact_globs": ["*.deb"],
                        },
                    },
                    source_dir,
                    artifact_root,
                    "ubuntu:24.04",
                    ["ca-certificates", "file", "git", "jq", "python3", "rsync", "xz-utils"],
                )

        run_mock.assert_called_once()
        self.assertEqual([path.name for path in artifacts], [artifact_name])

    def test_build_repo_safe_debian_uses_stable_for_modern_lockfile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            safe_dir = source_dir / "safe"
            artifact_root = tmp_path / "artifacts"
            safe_dir.mkdir(parents=True)
            (safe_dir / "debian").mkdir()
            (safe_dir / "debian" / "control").write_text("Source: demo\n")
            (safe_dir / "Cargo.toml").write_text('edition = "2021"\n')
            (safe_dir / "Cargo.lock").write_text(
                '\n'.join(
                    [
                        "# This file is automatically @generated by Cargo.",
                        "# It is not intended for manual editing.",
                        "version = 4",
                        "",
                    ]
                )
            )
            artifact_root.mkdir()
            output_dir = artifact_root / "demo"
            artifact_name = "demo_1.0_amd64.deb"

            def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
                self.assertEqual(args[:2], ["docker", "run"])
                self.assertTrue(kwargs["capture_output"])
                docker_script = args[-1]
                self.assertIn("--default-toolchain stable", docker_script)
                self.assertIn("rustc --version", docker_script)
                self.assertIn("cargo --version", docker_script)
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / artifact_name).write_text("deb")
                return completed()

            with mock.patch("tools.build_site.run", side_effect=fake_run) as run_mock:
                artifacts = build_site.build_repo(
                    {
                        "name": "demo",
                        "build": {
                            "mode": "safe-debian",
                            "artifact_globs": ["*.deb"],
                        },
                    },
                    source_dir,
                    artifact_root,
                    "ubuntu:24.04",
                    ["ca-certificates", "file", "git", "jq", "python3", "rsync", "xz-utils"],
                )

        run_mock.assert_called_once()
        self.assertEqual([path.name for path in artifacts], [artifact_name])

    def test_build_repo_docker_mode_requires_output_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_dir = tmp_path / "source"
            artifact_root = tmp_path / "artifacts"
            source_dir.mkdir()
            artifact_root.mkdir()

            with mock.patch("tools.build_site.run", return_value=completed()):
                with self.assertRaisesRegex(build_site.BuildError, "no artifacts found"):
                    build_site.build_repo(
                        {
                            "name": "demo",
                            "build": {
                                "workdir": ".",
                                "command": "./build.sh",
                                "artifact_globs": ["*.deb"],
                            },
                        },
                        source_dir,
                        artifact_root,
                        "ubuntu:24.04",
                        [],
                    )

    def test_prepare_signing_key_imports_private_key_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "SAFEAPTREPO_GPG_PRIVATE_KEY": "PRIVATE KEY",
                        "SAFEAPTREPO_GPG_PASSPHRASE": "secret",
                    },
                    clear=False,
                ),
                mock.patch("tools.build_site.tempfile.mkdtemp", return_value=tmp),
                mock.patch("tools.build_site.run") as run_mock,
            ):
                run_mock.side_effect = [
                    completed(),
                    completed(stdout="fpr:::::::::ABCDEF1234567890:\n"),
                ]
                homedir, fingerprint, passphrase = build_site.prepare_signing_key()

        self.assertEqual(homedir, Path(tmp))
        self.assertEqual(fingerprint, "ABCDEF1234567890")
        self.assertEqual(passphrase, "secret")
        self.assertEqual(run_mock.call_args_list[0].kwargs["input_text"], "PRIVATE KEY")
        self.assertEqual(run_mock.call_args_list[0].args[0][:4], ["gpg", "--batch", "--homedir", tmp])

    def test_prepare_signing_key_falls_back_to_legacy_environment_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "SAFEDEBREPO_GPG_PRIVATE_KEY": "LEGACY PRIVATE KEY",
                        "SAFEDEBREPO_GPG_PASSPHRASE": "legacy-secret",
                    },
                    clear=True,
                ),
                mock.patch("tools.build_site.tempfile.mkdtemp", return_value=tmp),
                mock.patch("tools.build_site.run") as run_mock,
            ):
                run_mock.side_effect = [
                    completed(),
                    completed(stdout="fpr:::::::::ABCDEF1234567890:\n"),
                ]
                homedir, fingerprint, passphrase = build_site.prepare_signing_key()

        self.assertEqual(homedir, Path(tmp))
        self.assertEqual(fingerprint, "ABCDEF1234567890")
        self.assertEqual(passphrase, "legacy-secret")
        self.assertEqual(run_mock.call_args_list[0].kwargs["input_text"], "LEGACY PRIVATE KEY")

    def test_prepare_signing_key_requires_discoverable_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("tools.build_site.tempfile.mkdtemp", return_value=tmp),
                mock.patch("tools.build_site.run") as run_mock,
            ):
                run_mock.side_effect = [completed(), completed(stdout="sec:::::::::\n")]
                with self.assertRaisesRegex(
                    build_site.BuildError, "failed to discover signing key fingerprint"
                ):
                    build_site.prepare_signing_key()

    def test_sign_release_passes_through_passphrase(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            site_root = Path(tmp)
            release_dir = site_root / "dists" / "noble"
            release_dir.mkdir(parents=True)
            (release_dir / "Release").write_text("Origin: SafeLibs\n")
            with mock.patch("tools.build_site.run") as run_mock:
                build_site.sign_release(site_root, "noble", Path("/tmp/keyring"), "ABC123", "secret")

        clearsign_args = run_mock.call_args_list[0].args[0]
        detach_args = run_mock.call_args_list[1].args[0]
        self.assertIn("--passphrase", clearsign_args)
        self.assertIn("secret", clearsign_args)
        self.assertIn("--passphrase", detach_args)
        self.assertIn("secret", detach_args)

    def test_write_preferences_file_requires_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            site_root = Path(tmp)
            with self.assertRaisesRegex(
                build_site.BuildError, "cannot write apt preferences without published packages"
            ):
                build_site.write_preferences_file(site_root, archive_config(), [])

    def test_generate_site_from_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deb_a = make_deb(tmp_path, "libalpha1", "1.0+safelibs1")
            deb_b = make_deb(tmp_path, "libbeta1", "2.0+safelibs1")
            output_dir = tmp_path / "site"
            template_path = Path(__file__).resolve().parent.parent / "templates" / "index.html"
            config = {"archive": archive_config()}

            infos = build_site.generate_site_from_artifacts(
                config,
                [deb_a, deb_b],
                output_dir,
                template_path=template_path,
                base_url="https://example.invalid/repo/",
            )

            self.assertEqual([info.name for info in infos], ["libalpha1", "libbeta1"])
            self.assertTrue((output_dir / "index.html").exists())
            self.assertTrue((output_dir / "safelibs.asc").exists())
            self.assertTrue((output_dir / "safelibs.gpg").exists())
            self.assertTrue((output_dir / "safelibs.pref").exists())
            self.assertTrue((output_dir / "safelibs-all.pref").exists())
            self.assertTrue((output_dir / "dists/noble/InRelease").exists())
            packages_text = (output_dir / "dists/noble/main/binary-amd64/Packages").read_text()
            self.assertIn("Package: libalpha1", packages_text)
            self.assertIn("Package: libbeta1", packages_text)
            self.assertIn("\n\nPackage: libbeta1", packages_text)
            index_text = (output_dir / "index.html").read_text()
            self.assertIn("https://example.invalid/repo/safelibs.gpg", index_text)
            self.assertIn("https://example.invalid/repo/safelibs-all.pref", index_text)
            self.assertIn("safelibs-all.pref", index_text)
            self.assertIn("safelibs-all.list", index_text)
            release_text = (output_dir / "dists/noble/Release").read_text()
            self.assertIn("Origin: SafeLibs", release_text)
            pref_text = (output_dir / "safelibs.pref").read_text()
            self.assertIn("Package: libalpha1 libbeta1", pref_text)
            self.assertIn("Pin: release o=SafeLibs", pref_text)
            self.assertIn("Pin-Priority: 1001", pref_text)

    def test_generate_split_site_creates_all_and_per_library_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deb_a = make_deb(tmp_path, "libalpha1", "1.0+safelibs1")
            deb_b = make_deb(tmp_path, "libbeta1", "2.0+safelibs1")
            output_dir = tmp_path / "site"
            template_root = Path(__file__).resolve().parent.parent / "templates"
            config = {
                "archive": archive_config(),
                "repositories": [repo_config("alpha"), repo_config("beta")],
            }

            published = build_site.generate_split_site(
                config,
                {"alpha": [deb_a], "beta": [deb_b]},
                output_dir,
                repository_template_path=template_root / "index.html",
                landing_template_path=template_root / "landing.html",
                base_url="https://example.invalid/apt/",
            )

            self.assertEqual([repo.name for repo in published], ["all", "alpha", "beta"])
            self.assertFalse((output_dir / "dists").exists())
            self.assertTrue((output_dir / "all/dists/noble/InRelease").exists())
            self.assertTrue((output_dir / "alpha/dists/noble/InRelease").exists())
            self.assertTrue((output_dir / "beta/dists/noble/InRelease").exists())
            self.assertTrue((output_dir / "all/safelibs-all.pref").exists())
            self.assertTrue((output_dir / "alpha/safelibs-alpha.pref").exists())
            self.assertTrue((output_dir / "beta/safelibs-beta.pref").exists())
            root_index = (output_dir / "index.html").read_text()
            self.assertIn("https://example.invalid/apt/all", root_index)
            self.assertIn('href="https://example.invalid/apt/alpha/"', root_index)
            self.assertIn('href="https://example.invalid/apt/beta/"', root_index)
            all_packages = (output_dir / "all/dists/noble/main/binary-amd64/Packages").read_text()
            self.assertIn("Package: libalpha1", all_packages)
            self.assertIn("Package: libbeta1", all_packages)
            alpha_packages = (output_dir / "alpha/dists/noble/main/binary-amd64/Packages").read_text()
            self.assertIn("Package: libalpha1", alpha_packages)
            self.assertNotIn("Package: libbeta1", alpha_packages)
            manifest = json.loads((output_dir / "manifest.json").read_text())
            self.assertEqual(manifest["channels"][0]["name"], "stable")
            self.assertEqual(
                [repo["path"] for repo in manifest["channels"][0]["repositories"]],
                ["all", "alpha", "beta"],
            )

    def test_generate_split_site_can_publish_testing_under_path_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deb_a = make_deb(tmp_path, "libalpha1", "1.0+safelibs1")
            output_dir = tmp_path / "site"
            template_root = Path(__file__).resolve().parent.parent / "templates"
            config = {
                "archive": archive_config(),
                "repositories": [repo_config("alpha")],
            }

            published = build_site.generate_split_site(
                config,
                {"alpha": [deb_a]},
                output_dir,
                repository_template_path=template_root / "index.html",
                landing_template_path=template_root / "landing.html",
                base_url="https://example.invalid/apt/",
                channel_name="testing",
                path_prefix="testing",
            )

            self.assertEqual([(repo.channel, repo.name, repo.path) for repo in published], [
                ("testing", "all", "testing/all"),
                ("testing", "alpha", "testing/alpha"),
            ])
            self.assertTrue((output_dir / "testing/all/dists/noble/InRelease").exists())
            self.assertTrue((output_dir / "testing/alpha/dists/noble/InRelease").exists())
            self.assertTrue((output_dir / "testing/all/safelibs-testing-all.pref").exists())
            self.assertTrue((output_dir / "testing/alpha/safelibs-testing-alpha.pref").exists())
            root_index = (output_dir / "index.html").read_text()
            self.assertIn("https://example.invalid/apt/testing/all", root_index)
            alpha_index = (output_dir / "testing/alpha/index.html").read_text()
            self.assertIn("safelibs-testing-alpha.pref", alpha_index)
            self.assertIn("testing/alpha", alpha_index)

    def test_render_root_index_features_aggregate_repositories_first(self) -> None:
        def package_info(name: str) -> build_site.PackageInfo:
            return build_site.PackageInfo(
                path=Path(f"{name}.deb"),
                name=name,
                version="1.0+safelibs1",
                architecture="amd64",
                pool_path=Path("pool/main/l") / name / f"{name}.deb",
            )

        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            template_path = (
                Path(__file__).resolve().parent.parent / "templates" / "landing.html"
            )
            repositories = [
                build_site.PublishedRepository(
                    channel="stable",
                    name="alpha",
                    path="alpha",
                    repository_id="alpha",
                    url="https://example.invalid/apt/alpha",
                    package_infos=(package_info("libalpha1"),),
                ),
                build_site.PublishedRepository(
                    channel="testing",
                    name="all",
                    path="testing/all",
                    repository_id="testing-all",
                    url="https://example.invalid/apt/testing/all",
                    package_infos=(package_info("libalpha1"), package_info("libbeta1")),
                ),
                build_site.PublishedRepository(
                    channel="stable",
                    name="all",
                    path="all",
                    repository_id="all",
                    url="https://example.invalid/apt/all",
                    package_infos=(package_info("libalpha1"),),
                ),
                build_site.PublishedRepository(
                    channel="testing",
                    name="alpha",
                    path="testing/alpha",
                    repository_id="testing-alpha",
                    url="https://example.invalid/apt/testing/alpha",
                    package_infos=(package_info("libalpha1"),),
                ),
            ]

            build_site.render_root_index(
                template_path,
                output_dir,
                archive_config(),
                repositories,
                "A" * 40,
                "https://example.invalid/apt/",
            )

            root_index = (output_dir / "index.html").read_text()

        stable_all_pos = root_index.index('href="https://example.invalid/apt/all/"')
        testing_all_pos = root_index.index(
            'href="https://example.invalid/apt/testing/all/"'
        )
        directory_pos = root_index.index("Repository Directory")
        stable_alpha_pos = root_index.index('href="https://example.invalid/apt/alpha/"')

        self.assertLess(stable_all_pos, testing_all_pos)
        self.assertLess(testing_all_pos, directory_pos)
        self.assertLess(directory_pos, stable_alpha_pos)

    def test_generate_split_site_rejects_reserved_all_repository_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deb_a = make_deb(tmp_path, "libalpha1", "1.0+safelibs1")
            deb_b = make_deb(tmp_path, "libbeta1", "2.0+safelibs1")
            template_root = Path(__file__).resolve().parent.parent / "templates"
            config = {
                "archive": archive_config(),
                "repositories": [repo_config("beta")],
            }

            with self.assertRaisesRegex(build_site.BuildError, r"name 'all' is reserved"):
                build_site.generate_split_site(
                    config,
                    {"all": [deb_a], "beta": [deb_b]},
                    tmp_path / "site",
                    repository_template_path=template_root / "index.html",
                    landing_template_path=template_root / "landing.html",
                    base_url="https://example.invalid/apt/",
                )

    def test_generate_split_site_rejects_unconfigured_repository_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deb_a = make_deb(tmp_path, "libalpha1", "1.0+safelibs1")
            deb_b = make_deb(tmp_path, "libbeta1", "2.0+safelibs1")
            template_root = Path(__file__).resolve().parent.parent / "templates"
            config = {
                "archive": archive_config(),
                "repositories": [repo_config("alpha")],
            }

            with self.assertRaisesRegex(
                build_site.BuildError, r"unexpected artifacts for unknown repositories: zeta"
            ):
                build_site.generate_split_site(
                    config,
                    {"alpha": [deb_a], "zeta": [deb_b]},
                    tmp_path / "site",
                    repository_template_path=template_root / "index.html",
                    landing_template_path=template_root / "landing.html",
                    base_url="https://example.invalid/apt/",
                )

    def test_generate_split_site_requires_artifacts_for_all_configured_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            deb_a = make_deb(tmp_path, "libalpha1", "1.0+safelibs1")
            template_root = Path(__file__).resolve().parent.parent / "templates"
            config = {
                "archive": archive_config(),
                "repositories": [repo_config("alpha"), repo_config("beta")],
            }

            with self.assertRaisesRegex(
                build_site.BuildError, r"missing artifacts for configured repositories: beta"
            ):
                build_site.generate_split_site(
                    config,
                    {"alpha": [deb_a]},
                    tmp_path / "site",
                    repository_template_path=template_root / "index.html",
                    landing_template_path=template_root / "landing.html",
                    base_url="https://example.invalid/apt/",
                )

    def test_split_stanzas_discards_empty_chunks(self) -> None:
        raw = "Package: a\nArchitecture: amd64\n\nPackage: b\nArchitecture: amd64\n\n"
        stanzas = build_site.split_stanzas(raw)
        self.assertEqual(len(stanzas), 2)
        self.assertTrue(stanzas[0].startswith("Package: a"))

    def test_main_skip_build_ignores_stale_artifact_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workspace = tmp_path / "workspace"
            output = tmp_path / "site"
            artifact_a = workspace / "artifacts" / "alpha" / "a.deb"
            artifact_b = workspace / "artifacts" / "zeta" / "b.deb"
            artifact_a.parent.mkdir(parents=True)
            artifact_b.parent.mkdir(parents=True)
            artifact_a.write_text("a")
            artifact_b.write_text("b")
            args = argparse.Namespace(
                config=tmp_path / "repositories.yml",
                output=output,
                workspace=workspace,
                base_url="",
                skip_build=True,
            )
            config = {"archive": archive_config(), "repositories": [repo_config("alpha")]}

            with (
                mock.patch("tools.build_site.parse_args", return_value=args),
                mock.patch("tools.build_site.load_config", return_value=config),
                mock.patch("tools.build_site.generate_split_site") as generate_mock,
                mock.patch("tools.build_site.download_release_artifacts") as download_mock,
            ):
                result = build_site.main()

        self.assertEqual(result, 0)
        download_mock.assert_not_called()
        self.assertEqual(
            generate_mock.call_args.args[1],
            {"alpha": [artifact_a]},
        )
        self.assertEqual(
            generate_mock.call_args.kwargs["base_url"], "https://example.invalid/apt/"
        )

    def test_main_skip_build_requires_cached_artifacts_for_all_configured_repositories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workspace = tmp_path / "workspace"
            output = tmp_path / "site"
            artifact_a = workspace / "artifacts" / "alpha" / "a.deb"
            artifact_a.parent.mkdir(parents=True)
            artifact_a.write_text("a")
            args = argparse.Namespace(
                config=tmp_path / "repositories.yml",
                output=output,
                workspace=workspace,
                base_url="",
                skip_build=True,
            )
            config = {
                "archive": archive_config(),
                "repositories": [repo_config("alpha"), repo_config("beta")],
            }

            with (
                mock.patch("tools.build_site.parse_args", return_value=args),
                mock.patch("tools.build_site.load_config", return_value=config),
                mock.patch(
                    "tools.build_site.generate_split_site",
                    side_effect=build_site.BuildError(
                        "missing artifacts for configured repositories: beta"
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    build_site.BuildError, r"missing artifacts for configured repositories: beta"
                ):
                    build_site.main()

    def test_main_skip_build_generates_stable_and_testing_channels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workspace = tmp_path / "workspace"
            output = tmp_path / "site"
            stable_artifact_dir = workspace / "artifacts" / "alpha"
            testing_artifact_dir = workspace / "artifacts" / "testing" / "beta"
            stable_artifact_dir.mkdir(parents=True)
            testing_artifact_dir.mkdir(parents=True)
            make_deb(stable_artifact_dir, "libalpha1", "1.0+safelibs1")
            make_deb(testing_artifact_dir, "libbeta1", "2.0+safelibs1")
            config_path = tmp_path / "repositories.yml"
            write_config(
                config_path,
                {
                    "archive": archive_config(),
                    "repositories": [repo_config("alpha")],
                    "testing": {
                        "discover": {
                            "github_org": "safelibs",
                            "repository_prefix": "port-",
                        },
                        "default_build": {
                            "mode": "safe-debian",
                            "artifact_globs": ["*.deb"],
                        },
                        "allow_build_failures": True,
                    },
                },
            )
            args = argparse.Namespace(
                config=config_path,
                output=output,
                workspace=workspace,
                base_url="https://example.invalid/apt/",
                skip_build=True,
            )

            with (
                mock.patch("tools.build_site.parse_args", return_value=args),
                mock.patch(
                    "tools.build_site.discover_port_repositories",
                    return_value=[
                        {
                            "name": "beta",
                            "github_repo": "safelibs/port-beta",
                            "ref": "refs/heads/main",
                        }
                    ],
                ),
            ):
                result = build_site.main()

            self.assertEqual(result, 0)
            self.assertTrue((output / "all/dists/noble/InRelease").exists())
            self.assertTrue((output / "testing/all/dists/noble/InRelease").exists())
            manifest = json.loads((output / "manifest.json").read_text())
            channel_paths = [
                (channel["name"], [repo["path"] for repo in channel["repositories"]])
                for channel in manifest["channels"]
            ]
            self.assertEqual(
                channel_paths,
                [
                    ("stable", ["all", "alpha"]),
                    ("testing", ["testing/all", "testing/beta"]),
                ],
            )

    def test_main_build_flow_downloads_release_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            workspace = tmp_path / "workspace"
            output = tmp_path / "site"
            args = argparse.Namespace(
                config=tmp_path / "repositories.yml",
                output=output,
                workspace=workspace,
                base_url="https://override.invalid/repo/",
                skip_build=False,
            )
            config = {
                "archive": {
                    **archive_config(),
                    "install_packages": ["ca-certificates", "git"],
                },
                "repositories": [repo_config("alpha"), repo_config("beta")],
            }
            downloaded_artifacts = [
                [workspace / "artifacts" / "alpha" / "alpha.deb"],
                [
                    workspace / "artifacts" / "beta" / "beta-a.deb",
                    workspace / "artifacts" / "beta" / "beta-b.deb",
                ],
            ]

            with (
                mock.patch("tools.build_site.parse_args", return_value=args),
                mock.patch("tools.build_site.load_config", return_value=config),
                mock.patch(
                    "tools.build_site.download_release_artifacts",
                    side_effect=downloaded_artifacts,
                ) as download_mock,
                mock.patch("tools.build_site.generate_split_site") as generate_mock,
            ):
                result = build_site.main()

        self.assertEqual(result, 0)
        self.assertEqual(download_mock.call_count, 2)
        self.assertEqual(
            [call.args[0]["name"] for call in download_mock.call_args_list],
            ["alpha", "beta"],
        )
        self.assertEqual(
            [call.args[1] for call in download_mock.call_args_list],
            [workspace / "artifacts", workspace / "artifacts"],
        )
        self.assertEqual(
            generate_mock.call_args.args[1],
            {
                "alpha": [workspace / "artifacts" / "alpha" / "alpha.deb"],
                "beta": [
                    workspace / "artifacts" / "beta" / "beta-a.deb",
                    workspace / "artifacts" / "beta" / "beta-b.deb",
                ],
            },
        )
        self.assertEqual(
            generate_mock.call_args.kwargs["base_url"], "https://override.invalid/repo/"
        )


if __name__ == "__main__":
    unittest.main()
