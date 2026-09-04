import json
import os
from unittest.mock import patch

from engine import Error, folder_name, rename_folder_exclusive
from test_webdav import Base


class FolderTests(Base):
    def create(self, name='Test', path='MiShare'):
        self.engine.mutate('folder/create', dict(path=path, name=name))
        return self.root / path / name

    def rename(self, path, name):
        self.engine.mutate('folder/rename', dict(path=path, name=name))

    def test_create_rename_preserves_files_and_configuration(self):
        before = (self.engine.data / 'config.json').read_bytes()
        policy = json.dumps(self.engine.access.config)
        source = self.create('测试资料')
        (source / 'keep.txt').write_text('keep')
        self.rename('MiShare/测试资料', '归档资料')
        self.assertFalse(source.exists())
        self.assertEqual((self.root / 'MiShare/归档资料/keep.txt').read_text(), 'keep')
        self.assertEqual(before, (self.engine.data / 'config.json').read_bytes())
        self.assertEqual(policy, json.dumps(self.engine.access.config))
        self.assertEqual(self.engine.browse('local', 'MiShare')[0]['renameBlocked'], '')

    def test_names_and_traversal_rejected(self):
        for name in ('', '.', '..', '.hidden', ' tail ', 'tail.', 'a/b', 'a\\b', 'a:b',
                     'a\x00b', 'a\x7fb', 'CON', 'aux.txt', 'x' * 256, '中' * 86, None):
            with self.subTest(name=name), self.assertRaises(Error):
                folder_name(name)
        for path in ('../escape', 'MiShare/../../escape', '.hidden'):
            with self.assertRaises((Error, OSError)):
                self.create(path=path)
        (self.root / 'link').symlink_to(self.path)
        with self.assertRaises(Error):
            self.create(path='link')

    def test_existing_targets_never_overwritten(self):
        source = self.create('Source')
        target = self.create('Target')
        (source / 'file').write_text('safe')
        with self.assertRaises(Error):
            self.create('Target')
        with self.assertRaises(Error):
            self.rename('MiShare/Source', 'Target')
        self.assertTrue(target.is_dir())
        self.assertTrue((source / 'file').exists())
        (source.parent / 'Link').symlink_to(target)
        with self.assertRaises(Error):
            self.rename('MiShare/Source', 'Link')
        with self.assertRaises(Error):
            self.rename('MiShare/Link', 'Elsewhere')

    def test_competing_target_at_atomic_rename_is_preserved(self):
        self.create('Source')
        def race(fd, old, new):
            os.mkdir(new, dir_fd=fd)
            return rename_folder_exclusive(fd, old, new)
        with patch('engine.rename_folder_exclusive', side_effect=race), self.assertRaises(Error):
            self.rename('MiShare/Source', 'Competitor')
        self.assertTrue((self.root / 'MiShare/Source').exists())
        self.assertTrue((self.root / 'MiShare/Competitor').exists())

    def test_root_references_and_running_services_protected(self):
        for path in ('', 'MiShare'):
            with self.assertRaises(Error):
                self.rename(path, 'Other')
        self.create('Parent')
        self.create('Child', 'MiShare/Parent')
        for collection, item in ((self.engine.access.config['folders'], {'path':'MiShare/Parent/Child'}),
                                 (self.engine.config['jobs'], {'local':'MiShare/Parent/Child'})):
            collection.append(item)
            for path in ('MiShare/Parent', 'MiShare/Parent/Child'):
                with self.assertRaises(Error):
                    self.rename(path, 'Other')
            collection.clear()
        self.engine.config['share']['path'] = 'MiShare/Parent'
        with self.assertRaises(Error):
            self.rename('MiShare/Parent', 'Other')
        self.engine.config['share']['path'] = 'MiShare'
        self.engine.access.config['enabled'] = True
        with self.assertRaises(Error):
            self.rename('MiShare/Parent', 'Other')
        self.engine.access.config['enabled'] = False
        self.engine.active_id = 'busy'
        with self.assertRaises(Error):
            self.create('New')
        with self.assertRaises(Error):
            self.rename('MiShare/Parent', 'Other')
        self.engine.active_id = None

    def test_missing_safe_rename_support_fails_closed(self):
        self.create('Source')
        with patch('engine.ctypes.CDLL', return_value=object()), self.assertRaises(Error):
            self.rename('MiShare/Source', 'Other')
        self.assertTrue((self.root / 'MiShare/Source').exists())
