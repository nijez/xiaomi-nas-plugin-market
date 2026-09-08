import csv
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
import offline_dependencies as deps


def make_wheel(directory, name="demo", version="1.0", requires=(), tag="py3-none-any"):
    directory.mkdir(exist_ok=True)
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
    metadata += "".join(f"Requires-Dist: {value}\n" for value in requires)
    prefix = f"{name}-{version}.dist-info/"
    files = {name + ".py": "VALUE = 42\n", prefix + "METADATA": metadata + "\n",
             prefix + "WHEEL": f"Wheel-Version: 1.0\nGenerator: local-test\nRoot-Is-Purelib: true\nTag: {tag}\n"}
    record = io.StringIO()
    csv.writer(record).writerows((name, "", "") for name in [*files, prefix + "RECORD"])
    files[prefix + "RECORD"] = record.getvalue()
    path = directory / f"{name}-{version}-{tag}.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


class OfflineDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.requirements = self.root / "requirements.txt"
        self.requirements.write_text("demo==1.0\n")
        self.wheel = make_wheel(self.root / "wheelhouse")
        deps.create_lock(self.requirements)

    def test_reviewed_lock_is_exclusive_and_complete(self):
        self.assertIn("demo==1.0 --hash=sha256:", deps.verify_bundle(self.requirements))
        with self.assertRaises(FileExistsError):
            deps.create_lock(self.requirements)

    def test_pip_bootstrap_requires_pinned_digest(self):
        bootstrap = self.root / 'pip-bootstrap'
        bootstrap.mkdir()
        (bootstrap / deps.PIP_WHEEL).write_bytes(b'not the reviewed pip')
        with self.assertRaisesRegex(deps.DependencyError, 'digest mismatch'):
            deps.pip_command(self.requirements, self.root, sys.executable)

    def test_pip_bootstrap_rejects_symlink_and_extra_files(self):
        bootstrap = self.root / 'pip-bootstrap'
        bootstrap.mkdir()
        wheel = bootstrap / deps.PIP_WHEEL
        wheel.symlink_to(self.wheel)
        with self.assertRaisesRegex(deps.DependencyError, 'Invalid offline pip'):
            deps.pip_command(self.requirements, self.root, sys.executable)
        wheel.unlink()
        wheel.write_bytes(b'fixture')
        (bootstrap / 'extra.py').write_text('')
        with self.assertRaisesRegex(deps.DependencyError, 'Invalid offline pip'):
            deps.pip_command(self.requirements, self.root, sys.executable)

    def test_pip_bootstrap_verified_bytes_are_staged(self):
        bootstrap = self.root / 'pip-bootstrap'
        bootstrap.mkdir()
        body = b'controlled fixture bytes'
        (bootstrap / deps.PIP_WHEEL).write_bytes(body)
        import hashlib
        with patch.object(deps, 'PIP_SHA256', hashlib.sha256(body).hexdigest()):
            command = deps.pip_command(self.requirements, self.root, sys.executable)
        self.assertEqual(command, [sys.executable, '-I', str(self.root / deps.PIP_WHEEL / 'pip')])
        self.assertEqual((self.root / deps.PIP_WHEEL).read_bytes(), body)

    def test_changed_wheel_is_rejected(self):
        with zipfile.ZipFile(self.wheel, "a") as archive:
            archive.writestr("unexpected.txt", "changed bytes")
        with self.assertRaisesRegex(deps.DependencyError, "SHA-256 mismatch"):
            deps.verify_bundle(self.requirements)

    def test_missing_lock_and_missing_wheel_fail_closed(self):
        lock = self.root / "requirements.lock"
        lock.rename(self.root / "saved.lock")
        with self.assertRaisesRegex(deps.DependencyError, "Missing regular"):
            deps.verify_bundle(self.requirements)
        (self.root / "saved.lock").rename(lock)
        self.wheel.unlink()
        with self.assertRaisesRegex(deps.DependencyError, "Missing reviewed wheels"):
            deps.verify_bundle(self.requirements)

    def test_changed_pins_rejected(self):
        self.requirements.write_text("demo==2.0\n")
        with self.assertRaisesRegex(deps.DependencyError, "does not match"):
            deps.verify_bundle(self.requirements)

    def test_requirement_options_urls_duplicates_are_rejected(self):
        for text in ("--index-url https://example.invalid", "demo @ https://example.invalid/a.whl",
                     "demo>=1.0", "demo==1.0 --extra-index-url https://example.invalid",
                     "demo==1.0\ndemo==1.0", "-r other.txt"):
            with self.subTest(text=text):
                self.requirements.write_text(text)
                with self.assertRaises(deps.DependencyError):
                    deps.verify_bundle(self.requirements)

    def test_unlocked_wheels_and_source_archives_rejected(self):
        extra = make_wheel(self.root / "wheelhouse", name="other")
        with self.assertRaisesRegex(deps.DependencyError, "Unexpected wheel"):
            deps.verify_bundle(self.requirements)
        extra.unlink()
        (self.root / "wheelhouse/demo-1.0.tar.gz").write_bytes(b"source")
        with self.assertRaisesRegex(deps.DependencyError, "Only regular wheel"):
            deps.verify_bundle(self.requirements)

    def test_symlinks_rejected(self):
        saved = self.root / self.wheel.name
        self.wheel.rename(saved)
        self.wheel.symlink_to(saved)
        with self.assertRaisesRegex(deps.DependencyError, "Only regular wheel"):
            deps.verify_bundle(self.requirements)

    def test_metadata_direct_url_cannot_bypass_no_index(self):
        make_wheel(self.root / "wheelhouse", requires=("other @ https://example.invalid/other.whl",))
        with self.assertRaisesRegex(deps.DependencyError, "Direct URL dependency forbidden"):
            deps.verify_bundle(self.requirements)

    def test_wheel_metadata_must_match_filename(self):
        self.wheel.unlink()
        fake = make_wheel(self.root / "wheelhouse", name="other")
        fake.rename(self.wheel)
        with self.assertRaisesRegex(deps.DependencyError, "identity mismatch"):
            deps.verify_bundle(self.requirements)

    def test_install_invocation_disables_network_sources_and_config(self):
        def run(command, **kwargs):
            for flag in ("-I", "--isolated", "--no-index", "--no-cache-dir", "--require-hashes", "--only-binary=:all:", "--ignore-installed"):
                self.assertIn(flag, command)
            self.assertNotIn("--no-deps", command)
            self.assertEqual(kwargs["env"]["PIP_CONFIG_FILE"], os.devnull)
            self.assertNotIn("PIP_EXTRA_INDEX_URL", kwargs["env"])
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            Path(command[command.index("--target") + 1]).mkdir()
        with patch.dict(os.environ, {"PIP_EXTRA_INDEX_URL": "https://example.invalid", "PYTHONPATH": "/untrusted"}), patch.object(deps.subprocess, "run", side_effect=run):
            deps.install_bundle(self.requirements, self.root / "lib")
        self.assertTrue((self.root / "lib").is_dir())

    def test_pip_failure_leaves_no_partial_target(self):
        with patch.object(deps.subprocess, "run", side_effect=subprocess.CalledProcessError(1, ["pip"])):
            with self.assertRaisesRegex(deps.DependencyError, "No online fallback"):
                deps.install_bundle(self.requirements, self.root / "lib")
        self.assertFalse((self.root / "lib").exists())
        self.assertFalse(list(self.root.glob(".offline-deps-*")))

    def test_existing_target_not_modified(self):
        target = self.root / "lib"
        target.mkdir()
        (target / "keep").write_text("existing")
        with patch.object(deps.subprocess, "run") as run:
            with self.assertRaisesRegex(deps.DependencyError, "already exists"):
                deps.install_bundle(self.requirements, target)
            run.assert_not_called()
        self.assertEqual((target / "keep").read_text(), "existing")

    def test_real_pip_installs_only_fixture_wheel(self):
        deps.install_bundle(self.requirements, self.root / "lib")
        self.assertEqual((self.root / "lib/demo.py").read_text(), "VALUE = 42\n")

    def test_real_pip_missing_transitive_dependency_fails_without_target(self):
        make_wheel(self.root / "wheelhouse", requires=("missing_fixture_package_xyz==1.0",))
        (self.root / "requirements.lock").unlink()
        deps.create_lock(self.requirements)
        with self.assertRaisesRegex(deps.DependencyError, "Offline dependency install failed"):
            deps.install_bundle(self.requirements, self.root / "lib")
        self.assertFalse((self.root / "lib").exists())

    def test_real_pip_rejects_wrong_platform(self):
        self.wheel.unlink()
        make_wheel(self.root / "wheelhouse", tag="cp399-cp399-win_amd64")
        (self.root / "requirements.lock").unlink()
        deps.create_lock(self.requirements)
        with self.assertRaisesRegex(deps.DependencyError, "Offline dependency install failed"):
            deps.install_bundle(self.requirements, self.root / "lib")
        self.assertFalse((self.root / "lib").exists())


if __name__ == "__main__":
    unittest.main()
