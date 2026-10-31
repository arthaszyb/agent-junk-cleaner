#!/usr/bin/env python3
"""Build dist/disk-steward-<version>.pyz: a zipapp of src/disk_steward that runs disk_steward.cli.main.

    python3 scripts/build_pyz.py

Standard library only. The package is copied without caches first, so the archive holds exactly the files a
wheel holds."""
from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

from disk_steward import __version__  # noqa: E402

MAIN = 'import sys\nfrom disk_steward.cli import main\n\nsys.exit(main())\n'


def build() -> Path:
    target = ROOT / 'dist' / f'disk-steward-{__version__}.pyz'
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ds-pyz-') as tmp:
        stage = Path(tmp)
        shutil.copytree(ROOT / 'src' / 'disk_steward', stage / 'disk_steward',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
        (stage / '__main__.py').write_text(MAIN, encoding='utf-8')
        zipapp.create_archive(stage, target, interpreter='/usr/bin/env python3', compressed=True)
    return target


if __name__ == '__main__':
    print(build())
