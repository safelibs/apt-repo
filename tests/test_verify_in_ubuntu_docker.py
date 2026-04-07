from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "verify-in-ubuntu-docker.sh"
VERIFY_SITE_PATH = REPO_ROOT / "scripts" / "verify-site.sh"


def write_verify_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "archive": {
                    "suite": "noble",
                    "component": "main",
                    "key_name": "safelibs",
                },
                "repositories": [
                    {
                        "name": "demo",
                        "verify_packages": ["libjson-c5"],
                    },
                    {
                        "name": "extra",
                        "verify_packages": ["libpng16-16t64"],
                    }
                ],
            }
        )
    )


def write_verify_config_without_packages(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "archive": {
                    "suite": "noble",
                    "component": "main",
                    "key_name": "safelibs",
                },
                "repositories": [
                    {
                        "name": "demo",
                    }
                ],
            }
        )
    )


def write_fake_docker(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ["DOCKER_ARGS_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))
"""
    )
    path.chmod(0o755)


class VerifyInUbuntuDockerTests(unittest.TestCase):
    def test_remote_mode_defaults_to_all_repository_under_site_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "repositories.yml"
            capture_path = tmp_path / "docker-args.json"
            injected_path = tmp_path / "should-not-exist"
            bin_dir = tmp_path / "bin"
            docker_path = bin_dir / "docker"

            write_verify_config(config_path)
            bin_dir.mkdir()
            write_fake_docker(docker_path)

            repo_target = f"https://example.invalid/$(touch {injected_path})"
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["DOCKER_ARGS_CAPTURE"] = str(capture_path)

            subprocess.run(
                ["bash", str(SCRIPT_PATH), repo_target, str(config_path)],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            self.assertFalse(
                injected_path.exists(),
                "malicious shell content in the remote URL must not execute on the host",
            )

            docker_args = json.loads(capture_path.read_text())
            docker_env: dict[str, str] = {}
            idx = 0
            while idx < len(docker_args):
                if docker_args[idx] == "-e":
                    name, value = docker_args[idx + 1].split("=", 1)
                    docker_env[name] = value
                    idx += 2
                    continue
                idx += 1

            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_MODE"], "remote")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_PACKAGES"], "libjson-c5,libpng16-16t64")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_REPO_URI"], f"{repo_target}/all")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_KEY_NAME"], "safelibs")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_PREFERENCE_FILE"], "safelibs-all.pref")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_SUITE"], "noble")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_COMPONENT"], "main")
            self.assertNotIn("SAFEAPTREPO_VERIFY_SETUP", docker_env)
            self.assertNotIn("SAFEDEBREPO_VERIFY_SETUP", docker_env)

    def test_remote_mode_selects_single_repository_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "repositories.yml"
            capture_path = tmp_path / "docker-args.json"
            bin_dir = tmp_path / "bin"
            docker_path = bin_dir / "docker"

            write_verify_config(config_path)
            bin_dir.mkdir()
            write_fake_docker(docker_path)

            repo_target = "https://example.invalid/releases"
            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["DOCKER_ARGS_CAPTURE"] = str(capture_path)

            subprocess.run(
                ["bash", str(SCRIPT_PATH), repo_target, str(config_path), "extra"],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            docker_args = json.loads(capture_path.read_text())
            docker_env: dict[str, str] = {}
            idx = 0
            while idx < len(docker_args):
                if docker_args[idx] == "-e":
                    name, value = docker_args[idx + 1].split("=", 1)
                    docker_env[name] = value
                    idx += 2
                    continue
                idx += 1

            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_PACKAGES"], "libpng16-16t64")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_REPO_URI"], f"{repo_target}/extra")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_PREFERENCE_FILE"], "safelibs-extra.pref")

    def test_local_mode_derives_packages_from_packages_index_when_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "repositories.yml"
            capture_path = tmp_path / "docker-args.json"
            bin_dir = tmp_path / "bin"
            docker_path = bin_dir / "docker"
            site_dir = tmp_path / "site" / "demo" / "dists" / "noble" / "main" / "binary-amd64"

            write_verify_config_without_packages(config_path)
            bin_dir.mkdir()
            write_fake_docker(docker_path)
            site_dir.mkdir(parents=True)
            (site_dir / "Packages").write_text(
                "\n".join(
                    [
                        "Package: libcjson1",
                        "Version: 1.0",
                        "",
                        "Package: libcjson-dev",
                        "Version: 1.0",
                        "",
                    ]
                )
            )

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["DOCKER_ARGS_CAPTURE"] = str(capture_path)

            subprocess.run(
                ["bash", str(SCRIPT_PATH), str(tmp_path / "site"), str(config_path), "demo"],
                check=True,
                cwd=REPO_ROOT,
                env=env,
            )

            docker_args = json.loads(capture_path.read_text())
            docker_env: dict[str, str] = {}
            idx = 0
            while idx < len(docker_args):
                if docker_args[idx] == "-e":
                    name, value = docker_args[idx + 1].split("=", 1)
                    docker_env[name] = value
                    idx += 2
                    continue
                idx += 1

            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_MODE"], "local")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_PACKAGES"], "libcjson1,libcjson-dev")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_REPO_URI"], "file:///repo")
            self.assertEqual(docker_env["SAFEAPTREPO_VERIFY_PREFERENCE_FILE"], "safelibs-demo.pref")


class VerifySiteTests(unittest.TestCase):
    def test_verify_site_fails_for_missing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = subprocess.run(
                [
                    "bash",
                    str(VERIFY_SITE_PATH),
                    str(tmp_path / "site"),
                    str(tmp_path / "missing.yml"),
                ],
                check=False,
                cwd=REPO_ROOT,
            )

            self.assertNotEqual(result.returncode, 0)

    def test_verify_site_fails_for_malformed_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "repositories.yml"
            config_path.write_text("repositories: invalid\n")

            result = subprocess.run(
                [
                    "bash",
                    str(VERIFY_SITE_PATH),
                    str(tmp_path / "site"),
                    str(config_path),
                ],
                check=False,
                cwd=REPO_ROOT,
            )

            self.assertNotEqual(result.returncode, 0)
