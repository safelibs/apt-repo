from __future__ import annotations

import argparse
import copy
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
        "base_url": "https://example.invalid/apt-repo/",
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

    def test_load_config_accepts_checked_in_repositories_file(self) -> None:
        config_path = Path(__file__).resolve().parent.parent / "repositories.yml"
        loaded = build_site.load_config(config_path)

        self.assertEqual(loaded["archive"]["suite"], "noble")
        self.assertEqual(loaded["archive"]["key_name"], "safelibs")
        self.assertEqual(
            [entry["name"] for entry in loaded["repositories"]],
            ["libjson", "libpng", "libzstd"],
        )
        self.assertEqual(loaded["repositories"][1]["build"]["mode"], "checkout-artifacts")
        self.assertNotIn("command", loaded["repositories"][1]["build"])

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
            self.assertTrue((output_dir / "dists/noble/InRelease").exists())
            packages_text = (output_dir / "dists/noble/main/binary-amd64/Packages").read_text()
            self.assertIn("Package: libalpha1", packages_text)
            self.assertIn("Package: libbeta1", packages_text)
            self.assertIn("\n\nPackage: libbeta1", packages_text)
            index_text = (output_dir / "index.html").read_text()
            self.assertIn("https://example.invalid/repo/safelibs.gpg", index_text)
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
                base_url="https://example.invalid/apt-repo/",
            )

            self.assertEqual([repo.name for repo in published], ["all", "alpha", "beta"])
            self.assertTrue((output_dir / "dists/noble/InRelease").exists())
            self.assertTrue((output_dir / "all/dists/noble/InRelease").exists())
            self.assertTrue((output_dir / "alpha/dists/noble/InRelease").exists())
            self.assertTrue((output_dir / "beta/dists/noble/InRelease").exists())
            root_index = (output_dir / "index.html").read_text()
            self.assertIn("https://example.invalid/apt-repo/all", root_index)
            self.assertIn('href="https://example.invalid/apt-repo/alpha/"', root_index)
            self.assertIn('href="https://example.invalid/apt-repo/beta/"', root_index)
            all_packages = (output_dir / "all/dists/noble/main/binary-amd64/Packages").read_text()
            self.assertIn("Package: libalpha1", all_packages)
            self.assertIn("Package: libbeta1", all_packages)
            alpha_packages = (output_dir / "alpha/dists/noble/main/binary-amd64/Packages").read_text()
            self.assertIn("Package: libalpha1", alpha_packages)
            self.assertNotIn("Package: libbeta1", alpha_packages)

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
                    base_url="https://example.invalid/apt-repo/",
                )

    def test_split_stanzas_discards_empty_chunks(self) -> None:
        raw = "Package: a\nArchitecture: amd64\n\nPackage: b\nArchitecture: amd64\n\n"
        stanzas = build_site.split_stanzas(raw)
        self.assertEqual(len(stanzas), 2)
        self.assertTrue(stanzas[0].startswith("Package: a"))

    def test_main_skip_build_uses_cached_artifacts(self) -> None:
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
                mock.patch("tools.build_site.sync_repo") as sync_mock,
                mock.patch("tools.build_site.build_repo") as build_mock,
            ):
                result = build_site.main()

        self.assertEqual(result, 0)
        sync_mock.assert_not_called()
        build_mock.assert_not_called()
        self.assertEqual(
            generate_mock.call_args.args[1],
            {"alpha": [artifact_a], "zeta": [artifact_b]},
        )
        self.assertEqual(
            generate_mock.call_args.kwargs["base_url"], "https://example.invalid/apt-repo/"
        )

    def test_main_build_flow_syncs_and_builds_repositories(self) -> None:
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
            source_dirs = [workspace / "sources" / "alpha", workspace / "sources" / "beta"]
            built_artifacts = [
                [workspace / "artifacts" / "alpha" / "alpha.deb"],
                [
                    workspace / "artifacts" / "beta" / "beta-a.deb",
                    workspace / "artifacts" / "beta" / "beta-b.deb",
                ],
            ]

            with (
                mock.patch("tools.build_site.parse_args", return_value=args),
                mock.patch("tools.build_site.load_config", return_value=config),
                mock.patch("tools.build_site.sync_repo", side_effect=source_dirs) as sync_mock,
                mock.patch("tools.build_site.build_repo", side_effect=built_artifacts) as build_mock,
                mock.patch("tools.build_site.generate_split_site") as generate_mock,
            ):
                result = build_site.main()

        self.assertEqual(result, 0)
        self.assertEqual(sync_mock.call_count, 2)
        self.assertEqual(build_mock.call_count, 2)
        self.assertEqual(
            [call.args[0]["name"] for call in build_mock.call_args_list],
            ["alpha", "beta"],
        )
        self.assertEqual(
            build_mock.call_args_list[0].args[3:],
            ("ubuntu:24.04", ["ca-certificates", "git"]),
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
