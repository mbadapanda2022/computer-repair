# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows demo launcher.
# Build:  pyinstaller --noconfirm --clean demo_package/A1RepairDemo.spec
# The whole Django project is frozen so the buyer needs NO Python/packages.

import os
import sys
import tempfile
from pathlib import Path

# --- Configure Django so app submodules/migrations collect cleanly -------
ROOT = Path(SPECPATH).parent          # project root (…/computer_repair)
sys.path.insert(0, str(ROOT))
os.environ.setdefault('DEMO_MODE', 'true')            # avoid prod DB guard
os.environ['DEMO_DATA_DIR'] = tempfile.mkdtemp(prefix='pyi_demo_')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'computer_repair.settings')
import django
django.setup()
os.environ.pop('DJANGO_DEBUG', None)

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

DJANGO_APPS = [
    'accounting', 'computer_repair',
    'allauth', 'allauth.account', 'allauth.socialaccount',
    'allauth.usersessions',
    'django_htmx', 'honeypot', 'django_eventstream',
    'django_ckeditor_5', 'django_cleanup',
    'cloudinary_storage', 'cloudinary', 'anymail',
    'whitenoise',
    'django.contrib.admin', 'django.contrib.auth',
    'django.contrib.contenttypes', 'django.contrib.sessions',
    'django.contrib.staticfiles', 'django.contrib.sites',
]

hiddenimports = ['django', 'sqlite3', 'ssl', 'encodings.cp1252']
datas = []
for app in DJANGO_APPS:
    try:
        hiddenimports += collect_submodules(app)
    except Exception:
        pass
    try:
        datas += collect_data_files(app)
    except Exception:
        pass

# Project templates + static (read from sys._MEIPASS at runtime).
datas += [(str(ROOT / 'templates'), 'templates')]
datas += [(str(ROOT / 'static'), 'static')]

if os.path.exists(os.path.join(SPECPATH, 'a1demo.ico')):
    datas += [(os.path.join(SPECPATH, 'a1demo.ico'), '.')]
if os.path.exists(os.path.join(SPECPATH, 'deadline.txt')):
    datas += [(os.path.join(SPECPATH, 'deadline.txt'), '.')]

block_cipher = None

a = Analysis(
    [os.path.join(SPECPATH, 'demo_launcher.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports + [
        'accounting.management.commands.demo_seed',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Unused native/heavy deps kept OUT of the demo build.
    excludes=['weasyprint', 'reportlab', 'psycopg2', 'psycopg', 'gunicorn',
              'django_q', 'pytest', 'IPython', 'tkinter',
              'django.contrib.gis'],
    win_no_prefer_redirects=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='A1RepairDemo',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # windowed: no console box
    icon=os.path.join(SPECPATH, 'a1demo.ico'),
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, name='A1RepairDemo',
)
