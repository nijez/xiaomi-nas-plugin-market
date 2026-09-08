import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
import io
import json
import hashlib
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('staged_privacy_fixture', ROOT / 'scripts/verify_release.py')
scanner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scanner)


class StagedPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git('init', '-q')
        scanner.findings.clear()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.root)

    def stage(self, text, name='source file.txt'):
        file = self.root / name
        file.write_text(text)
        self.git('add', '--', name)
        return file

    def test_clean_index_and_untracked_private_file(self):
        self.stage('public source')
        (self.root / '.env').write_text('local-only fixture')
        self.assertEqual(1, scanner.inspect_staged(self.root))
        self.assertEqual([], scanner.findings)

    def test_index_is_scanned_even_when_working_tree_is_cleaned(self):
        marker = '-----BEGIN ' + 'OPENSSH PRIVATE KEY-----'
        file = self.stage(marker)
        file.write_text('clean unstaged replacement')
        scanner.inspect_staged(self.root)
        self.assertIn(('source file.txt', 'private key'), scanner.findings)
        self.assertNotIn(marker, repr(scanner.findings))

    def test_unstaged_changes_are_not_reported_as_committed(self):
        file = self.stage('public source')
        file.write_text('-----BEGIN ' + 'OPENSSH PRIVATE KEY-----')
        scanner.inspect_staged(self.root)
        self.assertEqual([], scanner.findings)

    def test_sensitive_path_is_blocked(self):
        self.stage('fixture', '.env')
        scanner.inspect_staged(self.root)
        self.assertIn(('.env', 'forbidden distribution path'), scanner.findings)

    def test_symlink_is_blocked_without_following_it(self):
        (self.root / 'link').symlink_to('/outside-fixture')
        self.git('add', 'link')
        scanner.inspect_staged(self.root)
        self.assertIn(('link', 'non-regular staged file'), scanner.findings)

    def wheel_fixture(self, member='demo.py', content=b'public fixture'):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr(member, content)
        body = stream.getvalue()
        report = self.root / 'projects/xiaomi-115-sync-plugin/build-report.json'
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({'wheels': {'demo-1.0-py3-none-any.whl': hashlib.sha256(body).hexdigest()}}))
        return body

    def test_wheel_requires_reviewed_digest(self):
        body = self.wheel_fixture()
        with patch.object(scanner, 'ROOT', self.root):
            scanner.inspect('demo-1.0-py3-none-any.whl', body)
            with self.assertRaisesRegex(ValueError, 'Unreviewed'):
                scanner.inspect('demo-1.0-py3-none-any.whl', body + b'changed')

    def test_reviewed_wheel_still_rejects_path_traversal(self):
        body = self.wheel_fixture('../outside.py')
        with patch.object(scanner, 'ROOT', self.root), self.assertRaisesRegex(ValueError, 'Unsafe'):
            scanner.inspect('demo-1.0-py3-none-any.whl', body)

    def test_reviewed_wheel_rejects_wrong_native_architecture(self):
        body = self.wheel_fixture('native.so', b'not-an-arm64-elf')
        with patch.object(scanner, 'ROOT', self.root), self.assertRaisesRegex(ValueError, 'Linux ARM64'):
            scanner.inspect('demo-1.0-py3-none-any.whl', body)


if __name__ == '__main__':
    unittest.main()
