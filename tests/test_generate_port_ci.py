from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from tools import generate_port_ci as gen

REPO_ROOT = Path(__file__).resolve().parent.parent


def sample_archive() -> dict:
    return {
        "image": "ubuntu:24.04",
        "install_packages": [
            "ca-certificates",
            "file",
            "git",
            "jq",
            "python3",
            "rsync",
            "xz-utils",
        ],
    }


def bash_syntax_ok(script: str) -> tuple[bool, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(script)
        path = fh.name
    result = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
    return result.returncode == 0, result.stderr


def inner_script_from(workflow_text: str) -> str:
    doc = yaml.safe_load(workflow_text)
    write_step = next(
        s for s in doc["jobs"]["build"]["steps"]
        if s.get("name") == "Write container build script"
    )
    outer = write_step["run"]
    marker = "<<'SAFELIBS_PORT_CI_SCRIPT'\n"
    start = outer.index(marker) + len(marker)
    end = outer.index("\nSAFELIBS_PORT_CI_SCRIPT\n", start)
    return outer[start:end]


class RenderWorkflowTests(unittest.TestCase):
    def test_safe_debian_default_contents(self):
        entry = {"name": "cjson", "build": {"mode": "safe-debian", "artifact_globs": ["*.deb"]}}
        workflow = gen.render_workflow(entry, sample_archive())

        self.assertIn("name: build-debs", workflow)
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertIn("permissions:\n  contents: write", workflow)
        self.assertIn("IMAGE: ubuntu:24.04", workflow)
        self.assertIn("gh release create", workflow)
        self.assertIn("gh release upload", workflow)

        inner = inner_script_from(workflow)
        self.assertIn("mk-build-deps -i -r", inner)
        self.assertIn("dpkg-buildpackage -us -uc -b", inner)
        self.assertIn("cp -v ../*.deb", inner)
        self.assertIn("cd safe", inner)
        for pkg in ["build-essential", "devscripts", "dpkg-dev", "equivs", "fakeroot"]:
            self.assertIn(pkg, inner)

        ok, err = bash_syntax_ok(inner)
        self.assertTrue(ok, err)

    def test_checkout_artifacts_mode(self):
        entry = {
            "name": "libpng",
            "build": {
                "mode": "checkout-artifacts",
                "workdir": ".",
                "artifact_globs": ["*.deb"],
            },
        }
        workflow = gen.render_workflow(entry, sample_archive())
        inner = inner_script_from(workflow)

        self.assertIn('cp -v *.deb "$SAFEAPTREPO_OUTPUT"/', inner)
        self.assertNotIn("dpkg-buildpackage", inner)
        self.assertNotIn("mk-build-deps", inner)
        ok, err = bash_syntax_ok(inner)
        self.assertTrue(ok, err)

    def test_docker_custom_command_embedded(self):
        entry = {
            "name": "libzstd",
            "build": {
                "mode": "docker",
                "workdir": ".",
                "rustup_toolchain": "1.94.0",
                "packages": ["cmake", "help2man", "zlib1g-dev"],
                "command": (
                    "bash safe/scripts/build-deb.sh\n"
                    "cp -v safe/out/*.deb \"$SAFEAPTREPO_OUTPUT\"/"
                ),
                "artifact_globs": ["*.deb"],
            },
        }
        workflow = gen.render_workflow(entry, sample_archive())
        inner = inner_script_from(workflow)

        self.assertIn("bash safe/scripts/build-deb.sh", inner)
        self.assertIn('cp -v safe/out/*.deb "$SAFEAPTREPO_OUTPUT"/', inner)
        self.assertIn("https://sh.rustup.rs", inner)
        self.assertIn("--default-toolchain 1.94.0", inner)
        for pkg in ["cmake", "help2man", "zlib1g-dev", "curl"]:
            self.assertIn(pkg, inner)
        ok, err = bash_syntax_ok(inner)
        self.assertTrue(ok, err)

    def test_setup_hook_is_embedded_verbatim(self):
        setup = (
            "python3 - <<'PY'\n"
            "from pathlib import Path\n"
            "Path('safe/Cargo.toml').write_text('placeholder')\n"
            "PY"
        )
        entry = {
            "name": "liblzma",
            "build": {
                "mode": "safe-debian",
                "setup": setup,
                "artifact_globs": ["*.deb"],
            },
        }
        workflow = gen.render_workflow(entry, sample_archive())
        inner = inner_script_from(workflow)

        self.assertIn("python3 - <<'PY'", inner)
        self.assertIn("Path('safe/Cargo.toml').write_text('placeholder')", inner)
        self.assertIn("PY\n", inner)

        ok, err = bash_syntax_ok(inner)
        self.assertTrue(ok, err)

    def test_unsupported_mode_raises(self):
        entry = {"name": "weird", "build": {"mode": "nonsense", "artifact_globs": ["*.deb"]}}
        with self.assertRaises(SystemExit):
            gen.render_workflow(entry, sample_archive())

    def test_docker_mode_requires_command(self):
        entry = {"name": "naked", "build": {"mode": "docker", "artifact_globs": ["*.deb"]}}
        with self.assertRaises(SystemExit):
            gen.render_workflow(entry, sample_archive())

    def test_unlisted_port_override_used(self):
        entry = gen.build_entry_for({"repositories": []}, "libjansson")
        self.assertEqual(entry["build"]["mode"], "docker")
        self.assertIn("safe/scripts/build-deb.sh", entry["build"]["command"])

    def test_unknown_port_falls_back_to_safe_debian(self):
        entry = gen.build_entry_for({"repositories": []}, "made-up")
        self.assertEqual(entry["build"]["mode"], "safe-debian")


class AutoToolchainTests(unittest.TestCase):
    def test_modern_cargo_lock_triggers_rustup(self):
        entry = {
            "name": "cjson",
            "build": {"mode": "safe-debian", "artifact_globs": ["*.deb"]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            port = Path(tmp)
            safe = port / "safe"
            safe.mkdir()
            (safe / "Cargo.toml").write_text('[package]\nname = "x"\nedition = "2021"\n')
            (safe / "Cargo.lock").write_text("version = 4\n")
            (safe / "debian").mkdir()
            (safe / "debian" / "control").write_text("Source: x\n")
            workflow = gen.render_workflow(entry, sample_archive(), port)
        inner = inner_script_from(workflow)
        self.assertIn("https://sh.rustup.rs", inner)
        self.assertIn("--default-toolchain stable", inner)
        self.assertIn("curl", inner)

    def test_default_rustc_is_sufficient_no_rustup(self):
        entry = {
            "name": "plain",
            "build": {"mode": "safe-debian", "artifact_globs": ["*.deb"]},
        }
        with tempfile.TemporaryDirectory() as tmp:
            port = Path(tmp)
            safe = port / "safe"
            safe.mkdir()
            (safe / "debian").mkdir()
            (safe / "debian" / "control").write_text("Source: plain\n")
            workflow = gen.render_workflow(entry, sample_archive(), port)
        inner = inner_script_from(workflow)
        self.assertNotIn("https://sh.rustup.rs", inner)

    def test_explicit_toolchain_overrides_autodetect(self):
        entry = {
            "name": "tiff",
            "build": {
                "mode": "safe-debian",
                "rustup_toolchain": "1.80",
                "artifact_globs": ["*.deb"],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            port = Path(tmp)
            safe = port / "safe"
            safe.mkdir()
            (safe / "Cargo.lock").write_text("version = 4\n")
            (safe / "debian").mkdir()
            (safe / "debian" / "control").write_text("Source: tiff\n")
            workflow = gen.render_workflow(entry, sample_archive(), port)
        inner = inner_script_from(workflow)
        self.assertIn("--default-toolchain 1.80", inner)
        self.assertNotIn("--default-toolchain stable", inner)


class WriteWorkflowTests(unittest.TestCase):
    def test_idempotent_write(self):
        entry = {"name": "demo", "build": {"mode": "safe-debian", "artifact_globs": ["*.deb"]}}
        content = gen.render_workflow(entry, sample_archive())
        with tempfile.TemporaryDirectory() as tmp:
            port_dir = Path(tmp)
            changed1, dest = gen.write_workflow(port_dir, content)
            self.assertTrue(changed1)
            self.assertTrue(dest.exists())
            changed2, _ = gen.write_workflow(port_dir, content)
            self.assertFalse(changed2)


class CliTests(unittest.TestCase):
    def test_script_runs_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "repositories.yml"
            ports_root = tmp_path / "ports"
            ports_root.mkdir()
            config_path.write_text(yaml.safe_dump({"archive": sample_archive(), "repositories": []}))

            result = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "tools" / "generate_port_ci.py"),
                    "--config",
                    str(config_path),
                    "--ports-root",
                    str(ports_root),
                    "--dry-run",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
