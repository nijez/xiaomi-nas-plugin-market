"""Bake local PNG icons into the single Mac MicroApp script."""
import base64
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
source = root / 'web/app.js'
icons = {p.stem: 'data:image/png;base64,' + base64.b64encode(p.read_bytes()).decode()
         for p in sorted((root / 'web/assets').glob('*.png'))}
template = source.read_text()
if '__ICON_ASSETS__' not in template:
    raise SystemExit('Expected source icon placeholder')
(root / 'web/app.bundle.js').write_text(template.replace('__ICON_ASSETS__', json.dumps(icons)))
print('qB single-script UI built')
