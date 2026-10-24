"""Windows demo launcher (frozen with PyInstaller → A1RepairDemo.exe).

Boots the full Django project in DEMO_MODE against a per-user SQLite DB,
runs first-run setup (migrate / cache table / static / seed), opens the
browser, then serves. No Python or packages are needed on the buyer's PC —
everything is bundled in the installer.

IMPORTANT: never reads or writes any real production config. Any leaked
DATABASE_URL / DJANGO_SECRET_KEY from the ambient environment is scrubbed
before Django starts, and DEMO_MODE forces local SQLite + console email.
"""
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _bundle_root():
    if getattr(sys, 'frozen', False):
        return Path(getattr(sys, '_MEIPASS'))
    return Path(__file__).resolve().parent.parent


BUNDLE = _bundle_root()


def _prepare_env():
    # Windowed (console=False) frozen builds have sys.stdout/stderr = None;
    # Django management output + print() would crash. Redirect to a null sink
    # (real logs still go to launcher.log via _log and the file logging).
    class _Null:
        encoding = 'utf-8'
        def write(self, *a, **k): return 0
        def flush(self): pass
        def isatty(self): return False
        def seek(self, *a, **k): return 0
        def tell(self): return 0
    if sys.stdout is None:
        sys.stdout = _Null()
    if sys.stderr is None:
        sys.stderr = _Null()

    # Add the bundle to import path so 'accounting'/'computer_repair' resolve
    # inside the frozen app (their compiled modules live in the archive, but
    # data files — templates/static — are read from _MEIPASS).
    for p in (str(BUNDLE), os.getcwd()):
        if p not in sys.path:
            sys.path.insert(0, p)

    os.environ['DEMO_MODE'] = 'true'
    os.environ['DJANGO_SETTINGS_MODULE'] = 'computer_repair.settings'
    os.environ['DJANGO_DEBUG'] = 'False'
    # Scrub anything that could point the demo at production.
    for key in ('DATABASE_URL', 'DJANGO_SECRET_KEY', 'CLOUDINARY_CLOUD_NAME',
                'CLOUDINARY_API_KEY', 'CLOUDINARY_API_SECRET', 'BREVO_SMTP_KEY',
                'BREVO_HOST_USER', 'EMAIL_HOST_USER', 'EMAIL_HOST_PASSWORD',
                'DEFAULT_FROM_EMAIL', 'CSRF_TRUSTED_ORIGINS'):
        os.environ.pop(key, None)

    # Build-time hard deadline (ISO datetime) — old demo builds expire even if
    # the clock is rolled back. Written by the build script next to the exe.
    deadline_file = BUNDLE / 'deadline.txt'
    if deadline_file.exists():
        os.environ['DEMO_TRIAL_HARD_DEADLINE'] = deadline_file.read_text(
            encoding='utf-8').strip()

    # Data dir: %LOCALAPPDATA%\A1RepairDemo (settings default) unless overridden.
    if not os.environ.get('DEMO_DATA_DIR'):
        base = Path(os.environ.get('LOCALAPPDATA') or str(Path.home()))
        os.environ['DEMO_DATA_DIR'] = str(base / 'A1RepairDemo')
    Path(os.environ['DEMO_DATA_DIR']).mkdir(parents=True, exist_ok=True)
    return int(os.environ.get('DEMO_PORT', '8777'))


def _log(msg):
    # Shown on the console window (hidden by default; visible if launched
    # from a terminal). Also appended to a log file for support.
    try:
        print(msg, flush=True)
    except Exception:
        pass
    try:
        logf = Path(os.environ['DEMO_DATA_DIR']) / 'launcher.log'
        with open(logf, 'a', encoding='utf-8') as f:
            f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")} {msg}\n')
    except OSError:
        pass


def main():
    port = _prepare_env()

    import django
    django.setup()
    from django.conf import settings
    from django.core.management import call_command

    setup_only = any(a in ('--setup', 'setup') for a in sys.argv[1:])
    open_guide = any(a in ('--guide', 'guide') for a in sys.argv[1:])

    # One-time init. migrate/seed are idempotent; collectstatic is skipped
    # once static is present so repeat launches stay fast.
    _log('step: migrate')
    call_command('migrate', no_input=True, verbosity=0)
    _log('step: createcachetable')
    call_command('createcachetable', verbosity=0)
    static_marker = Path(settings.STATIC_ROOT)
    if setup_only or not (static_marker.exists() and any(static_marker.iterdir())):
        _log('step: collectstatic')
        call_command('collectstatic', interactive=False, clear=True,
                     post_process=False, verbosity=0)
    _log('step: demo_seed')
    call_command('demo_seed', verbosity=0)

    if setup_only:
        _log('Setup complete. Launch the demo from the desktop shortcut.')
        return 0

    url = f'http://127.0.0.1:{port}/'
    open_url = url + ('demo-guide/' if open_guide else '')
    _log(f'Demo is ready: {url}')
    _log('Login → Admin / Demo@12345   (guide: /demo-guide/)')

    def _open():
        time.sleep(2.0)
        try:
            webbrowser.open(open_url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()

    # --noreload: run the WSGI server in THIS process (autoreload would
    # re-exec the frozen binary and lose the environment).
    call_command('runserver', f'127.0.0.1:{port}',
                 skip_checks=True, use_reloader=False, verbosity=1)
    return 0


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # pragma: no cover
        import traceback
        _log('Demo failed to start:\n' + traceback.format_exc())
        if getattr(sys, 'frozen', False):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    'A1 Repair Demo could not start.\n\n' + str(exc),
                    'Demo launch error', 0x10)
            except Exception:
                pass
