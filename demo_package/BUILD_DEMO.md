# Building the Windows Demo Installer

This folder packages the whole Django project into a **self-contained Windows
demo** the buyer installs like normal software — **no Python or pip needed**.
It runs as a local server, is limited to a 6-month trial, and ships with a
bilingual (Hindi-English) guide. It never touches real production data.

## What's inside
- `demo_launcher.py` — entry point frozen into `A1RepairDemo.exe`. Sets
  `DEMO_MODE=true`, scrubs any production env vars, runs first-run
  migrate/createcachetable/collectstatic/demo_seed, then serves on
  `http://127.0.0.1:8777/`. Flags: `--setup` (init only, used by installer),
  `--guide` (open the tutorial), default (start + open app).
- `A1RepairDemo.spec` — PyInstaller spec (onedir, windowed, app icon).
- `make_icon.py` — generates the neutral `a1demo.ico` (no real branding).
- `installer.iss` — Inno Setup script (desktop shortcut with icon, uninstall,
  post-install `--setup`).
- `deadline.txt` — build-time hard trial deadline (build date + 200 days).

## Steps (on Windows, from the project root)
```bat
:: 1. one-off tools
venv\Scripts\pip install pyinstaller

:: 2. icon (only if a1demo.ico is missing)
venv\Scripts\python demo_package\make_icon.py

:: 3. fresh trial deadline baked into this build
venv\Scripts\python -c "from datetime import datetime,timedelta; open('demo_package/deadline.txt','w').write((datetime.now()+timedelta(days=200)).isoformat())"

:: 4. freeze the app (takes several minutes; produces demo_package\dist\A1RepairDemo\)
venv\Scripts\pyinstaller --noconfirm --clean --distpath demo_package\dist --workpath demo_package\build demo_package\A1RepairDemo.spec

:: 5. make the installer (produces demo_package\output\RepairShopDemo-Setup.exe)
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" demo_package\installer.iss
```

## Safety guarantees (why no secret data leaks into the demo)
- `DEMO_MODE` forces local SQLite under `%LOCALAPPDATA%\A1RepairDemo`, console
  email, FileSystem storage (no Cloudinary), and only `127.0.0.1` CSRF origins.
- The launcher deletes `DATABASE_URL`, `DJANGO_SECRET_KEY`, Cloudinary/SMTP/
  Brevo env vars before Django starts, so it can never point at production.
- `demo_seed` refuses to run unless `DEMO_MODE=true` and writes only invented
  fixture data ("Demo Computer Care", example.com emails, 98000000xx phones).
- `.env` / `db.sqlite3` / `media/` are **not** added to the bundle (only
  `templates/` + `static/` are).

## Trial rules (accounting/demo_trial.py + middleware)
- 180-day window stamped on first launch into both a state file and the DB
  cache (earliest wins → clock-rollback / file-delete resistant).
- Hard deadline (deadline.txt) expires stale builds regardless of install date.
- Banner warning appears ≤30 days left; a contact page replaces the app after
  expiry — both show the developer's phone + WhatsApp.

## Do NOT ship: dist/, build/, output/, *.ico should be regenerated, and never
commit real credentials. Everything here is demo-only.
