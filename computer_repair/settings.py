import os
from pathlib import Path
from django.contrib.messages import constants as messages
from dotenv import load_dotenv
load_dotenv()

# =============================================
# BASE DIRECTORY
# =============================================
BASE_DIR = Path(__file__).resolve().parent.parent

# =============================================
# SECURITY & DEBUG (Environment based)
# =============================================
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
DEBUG = os.getenv('DJANGO_DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')

# =============================================
# INSTALLED APPS
# =============================================
INSTALLED_APPS = [
    'django.contrib.admin',
    'honeypot',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'channels',
    'django_eventstream',
    'django_htmx',
    'django_cleanup.apps.CleanupConfig',
    'accounting.apps.AccountingConfig',
]

if not DEBUG:
    INSTALLED_APPS += ['cloudinary_storage', 'cloudinary']

# =============================================
# MIDDLEWARE
# =============================================
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
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
    'django.contrib.auth.backends.ModelBackend',
]

# =============================================
# SESSION & CSRF SECURITY (Conditional)
# =============================================
SESSION_COOKIE_AGE = 1209600
SESSION_COOKIE_HTTPONLY = True
SESSION_SAVE_EVERY_REQUEST = True

SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    }
}

# =============================================
# URLS & TEMPLATES
# =============================================
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
# ASGI_APPLICATION = 'computer_repair.asgi.application'  # ← ASGI बंद है

# =============================================
# CHANNELS & EVENTSTREAM
# =============================================
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer"
    }
}
EVENTSTREAM_CHANNEL_LAYER = "default"
EVENTSTREAM_MAX_CONNECTIONS = 100
EVENTSTREAM_STORAGE_CLASS = 'django_eventstream.storage.MemoryStorage'

# =============================================
# DATABASE (Production: PostgreSQL via DATABASE_URL, Development: SQLite)
# =============================================
import dj_database_url

DATABASES = {
    'default': dj_database_url.config(
        default=f'sqlite:///{BASE_DIR / "db.sqlite3"}',
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# =============================================
# PASSWORD VALIDATION
# =============================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# =============================================
# INTERNATIONALIZATION
# =============================================
LANGUAGE_CODE = 'en-in'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# =============================================
# STATIC & MEDIA FILES
# =============================================
STATIC_URL = 'static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# =============================================
# CLOUDINARY STORAGE (Production Media Files)
# =============================================
if not DEBUG:
    import cloudinary
    import cloudinary.uploader
    import cloudinary.api

    cloudinary.config(
        cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
        api_key=os.getenv('CLOUDINARY_API_KEY'),
        api_secret=os.getenv('CLOUDINARY_API_SECRET'),
        timeout=60 
    )
    
    DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'
    MEDIA_URL = f'https://res.cloudinary.com/{os.getenv("CLOUDINARY_CLOUD_NAME")}/image/upload/'
else:
    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

# =============================================
# AUTHENTICATION
# =============================================
LOGIN_URL = 'accounting:login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'home'

# =============================================
# EMAIL (Production overrides via Environment Variables)
# =============================================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@yourdomain.com')

# =============================================
# MESSAGE TAGS
# =============================================
MESSAGE_TAGS = {
    messages.DEBUG: 'secondary',
    messages.INFO: 'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR: 'danger',
}

# =============================================
# LOGGING
# =============================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}', 'style': '{'},
        'simple': {'format': '{levelname} {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'level': 'DEBUG' if DEBUG else 'INFO', 'class': 'logging.StreamHandler', 'formatter': 'simple'},
        'file': {'level': 'ERROR', 'class': 'logging.FileHandler', 'filename': BASE_DIR / 'logs/django_errors.log', 'formatter': 'verbose'},
    },
    'loggers': {
        'django': {'handlers': ['console', 'file'], 'level': 'INFO', 'propagate': True},
        'django.request': {'handlers': ['file'], 'level': 'ERROR', 'propagate': False},
        'accounting': {'handlers': ['console', 'file'], 'level': 'DEBUG' if DEBUG else 'INFO', 'propagate': True},
        'django_eventstream': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.server': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
    },
}

# =============================================
# PRODUCTION SECURITY SETTINGS (Conditional)
# =============================================
if not DEBUG:
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = True
    
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    ADMINS = [('Admin', os.getenv('ADMIN_EMAIL', 'admin@example.com'))]
    SERVER_EMAIL = os.getenv('SERVER_EMAIL', 'server@example.com')

# =============================================
# ENSURE LOGS DIRECTORY EXISTS
# =============================================
LOGS_DIR = BASE_DIR / 'logs'
if not LOGS_DIR.exists():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================
# HONEYPOT SETTINGS
# =============================================
HONEYPOT_FIELD_NAME = 'phone'
HONEYPOT_VALUE = ''
HONEYPOT_VERIFY = True

