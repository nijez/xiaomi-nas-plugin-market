import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
