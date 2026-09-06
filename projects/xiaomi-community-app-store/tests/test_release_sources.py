import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fixture_build_repository", PROJECT / "scripts/build_repository.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleaseSourceTests(unittest.TestCase):
    def test_missing_wheels_block_before_key_access_or_catalog_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "requirements.txt").write_text("demo==1.0\n")
            specification = {"id": "fixture", "version": "1.0.0", "project": root,
                             "requirements": "runtime/requirements.txt"}
            output = root / "catalog"
            with patch.object(builder, "PACKAGE_SPECS", [specification]), \
                 patch.object(builder, "CATALOG", output), patch.object(builder, "ensure_key") as key, \
                 patch("sys.argv", ["build_repository.py", "--signing-key", str(root / "unused.pem")]):
                with self.assertRaisesRegex(SystemExit, "Dependency release gate"):
                    builder.main()
                key.assert_not_called()
            self.assertFalse(output.exists())

    def test_matching_payload_passes_and_changed_source_blocks_refresh(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("server.py", "index.html", "icon.png", "service", "nginx"):
                (source / name).write_text(name)
            specification = {"id": "fixture", "version": "1.0.0", "project": source,
                             "runtime": {"server.py": "server.py"}, "ui": "index.html", "iconSource": "icon.png",
                             "serviceSource": "service", "service": "fixture.service", "nginxSource": "nginx",
                             "nginx": "fixture.conf", "healthPath": "/healthz"}
            stage = root / "stage"
            builder.populate_payload(specification, stage)
            (stage / "manifest.json").write_text(json.dumps({"healthPath": "/healthz"}))
            catalog = root / "catalog"
            builder.write_deterministic_zip(stage, catalog / "fixture.zip")
            (catalog / "catalog.json").write_text(json.dumps({"packages": [{"id": "fixture", "version": "1.0.0", "bundle": "fixture.zip"}]}))
            with patch.object(builder, "PACKAGE_SPECS", [specification]):
                builder.assert_catalog_matches_sources(catalog)
                (source / "server.py").write_text("fixed source, old bundle")
                with self.assertRaisesRegex(SystemExit, "Stale signed bundle"):
                    builder.assert_catalog_matches_sources(catalog)
