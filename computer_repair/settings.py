# computer_repair/settings.py
import os
from pathlib import Path
from django.contrib.messages import constants as messages
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("DJANGO_SECRET_KEY environment variable is missing!")

DEBUG = os.getenv('DJANGO_DEBUG', 'False').strip().lower() == 'true'

ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
    if h.strip()
]

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'cloudinary_storage',
    'cloudinary',
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'honeypot',
    'django_htmx',
    'django_eventstream',
    'django_cleanup.apps.CleanupConfig',
    'anymail',
    'accounting.apps.AccountingConfig',
]

SITE_ID = 1

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'allauth.account.middleware.AccountMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django_htmx.middleware.HtmxMiddleware',
    'accounting.middleware.AccessControlMiddleware',
]

AUTHENTICATION_BACKENDS = [
    'accounting.auth_backends.EmailOrPhoneBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
    'django.contrib.auth.backends.ModelBackend',
]

SESSION_COOKIE_AGE = 1209600
SESSION_COOKIE_HTTPONLY = True
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# ============================================================
# CACHES — Database-backed (Render free tier + multi-worker safe)
# ============================================================
# LocMemCache is per-process. Gunicorn 2 workers on the Render free tier.
# ...runs it, so to share login rate limiting across workers...
# We are using DatabaseCache (on Supabase Postgres).
# Running `createcachetable` in Build.sh is mandatory.
# ============================================================
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
        'LOCATION': 'django_cache_table',
        'TIMEOUT': 300,
        'OPTIONS': {
            'MAX_ENTRIES': 5000,   # Free tier: bounded rows
            'CULL_FREQUENCY': 3,   # Delete 1/3 rows when full
        },
    }
}

ROOT_URLCONF = 'computer_repair.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'accounting.context_processors.company_profile',
            ],
        },
    },
]

WSGI_APPLICATION = 'computer_repair.wsgi.application'


# ============================================================
# DATABASE CONFIGURATION (Secure – Using Environment Variables)
# ============================================================

import dj_database_url

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    _db_config = dj_database_url.parse(
        DATABASE_URL,
        conn_max_age=600,
        conn_health_checks=True,
        ssl_require=True,
    )

    # Supabase shared transaction pooler (port 6543) does not support
    # prepared statements and cannot safely use PostgreSQL server-side
    # cursors across pooled transactions.
    if str(_db_config.get("PORT")) == "6543":
        _db_config.setdefault("OPTIONS", {})
        _db_config["OPTIONS"]["prepare_threshold"] = None
        _db_config["DISABLE_SERVER_SIDE_CURSORS"] = True

    DATABASES = {"default": _db_config}
else:
    # Local development
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            # DatabaseCache (CACHES) + concurrent requests ke saath
            # journal_mode=delete transient "unable to open database file"
            # deta hai (Windows journal-file race). WAL + IMMEDIATE isse
            # eliminate karta hai — Django 5.1 documented config.
            "OPTIONS": {
                "transaction_mode": "IMMEDIATE",
                # temp_store=MEMORY: jab TEMP dir unusable ho (git-bash ka
                # POSIX-style TEMP Windows SQLite me CANTOPEN deta hai),
                # statement-journal/temp-file wale statements deterministically
                # "unable to open database file" fail hote hain. In-memory temp
                # isse eliminate karta hai (app ka data volume chhota hai).
                "init_command": (
                    "PRAGMA journal_mode=WAL;"
                    "PRAGMA synchronous=NORMAL;"
                    "PRAGMA temp_store=MEMORY;"
                ),
            },
        }
    }


AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-in'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'

STATIC_URL = 'static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

import cloudinary
cloudinary.config(
    cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
    api_key=os.getenv('CLOUDINARY_API_KEY'),
    api_secret=os.getenv('CLOUDINARY_API_SECRET'),
    secure=True,
)

# ============================================================
# CLOUDINARY STORAGE — same filename pe overwrite allow karo
# ============================================================
CLOUDINARY_STORAGE = {
    'CLOUD_NAME': os.getenv('CLOUDINARY_CLOUD_NAME'),
    'API_KEY': os.getenv('CLOUDINARY_API_KEY'),
    'API_SECRET': os.getenv('CLOUDINARY_API_SECRET'),
    'SECURE': True,
    'OVERWRITE': True,
}

if not DEBUG:
    STORAGES = {
        "default": {
            "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
        },
        "staticfiles": {
            "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"
        },
    }
    MEDIA_URL = ''
else:
    # Development: use Cloudinary when credentials are available so that
    # images restored from production work immediately (production images
    # are stored on Cloudinary, not in the local media/ folder).
    _has_cloudinary = bool(
        os.getenv('CLOUDINARY_CLOUD_NAME')
        and os.getenv('CLOUDINARY_API_KEY')
        and os.getenv('CLOUDINARY_API_SECRET')
    )
    if _has_cloudinary:
        STORAGES = {
            "default": {
                "BACKEND": "cloudinary_storage.storage.MediaCloudinaryStorage",
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
            },
        }
        MEDIA_URL = ''
    else:
        STORAGES = {
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
            },
        }
        MEDIA_URL = '/media/'
        MEDIA_ROOT = BASE_DIR / 'media'

WHITENOISE_MANIFEST_STRICT = False

LOGIN_URL = 'accounting:login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'home'

ACCOUNT_LOGIN_METHODS = {'username', 'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_LOGOUT_ON_GET = False


if not DEBUG:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = 'smtp-relay.brevo.com'
    EMAIL_PORT = 2525
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = os.getenv('BREVO_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.getenv('BREVO_SMTP_KEY', '')
    DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'solutionsmanojtech@gmail.com')
else:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = 'smtp.gmail.com'
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
    DEFAULT_FROM_EMAIL = os.getenv('EMAIL_HOST_USER', 'solutionsmanojtech@gmail.com')

MESSAGE_TAGS = {
    messages.DEBUG: 'secondary',
    messages.INFO: 'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR: 'danger',
}

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {
            'format': '{levelname} {asctime} {module} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
    'loggers': {
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        'accounting': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'django_eventstream': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.server': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}

if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    REFERRER_POLICY = 'same-origin'
else:
    X_FRAME_OPTIONS = 'SAMEORIGIN'


HONEYPOT_FIELD_NAME = 'website'
HONEYPOT_VALUE = ''
HONEYPOT_VERIFY = True

DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024   # 10 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

CSRF_FAILURE_VIEW = 'accounting.views.error_handlers.csrf_failure'


CSRF_TRUSTED_ORIGINS = [
    'https://a1computersolutions.onrender.com',
]

# ENV se bhi allow karo — future proof
_extra = os.getenv('CSRF_TRUSTED_ORIGINS', '')
if _extra:
    CSRF_TRUSTED_ORIGINS += [o.strip() for o in _extra.split(',') if o.strip()]
