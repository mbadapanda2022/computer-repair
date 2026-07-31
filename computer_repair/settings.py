
# computer_repair/settings.py 
import os
import sys
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
    # 'jazzmin',
    'django.contrib.admin',
    'honeypot',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Third-party
    'channels',
    'django_eventstream',
    'django_htmx',
    'django_cleanup.apps.CleanupConfig',
    
    # Custom
    'accounting.apps.AccountingConfig',
]

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
    'django.contrib.auth.backends.ModelBackend',  # fallback
]




# =============================================
# SESSION & CSRF SECURITY (Conditional)
# =============================================
SESSION_COOKIE_AGE = 1209600  # 2 weeks
SESSION_COOKIE_HTTPONLY = True
SESSION_SAVE_EVERY_REQUEST = True

# Secure cookies only in production (HTTPS)
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Cache for rate limiting
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    }
}

# =============================================
# CSRF FAILURE VIEW (only if you have a custom view)
# =============================================
# Django uses its own default CSRF error page when this is not set.
# Uncomment and set only if you have created 'accounting.views.error_handlers.csrf_failure'
# if not DEBUG:
#     CSRF_FAILURE_VIEW = 'accounting.views.error_handlers.csrf_failure'

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
ASGI_APPLICATION = 'computer_repair.asgi.application'

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
# DATABASE
# =============================================
DATABASES = {
    'default': {
        'ENGINE': os.getenv('DB_ENGINE', 'django.db.backends.sqlite3'),
        'NAME': os.getenv('DB_NAME', BASE_DIR / 'db.sqlite3'),
        'USER': os.getenv('DB_USER', ''),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST': os.getenv('DB_HOST', ''),
        'PORT': os.getenv('DB_PORT', ''),
    }
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
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, 'static'),
]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

# =============================================
# AUTHENTICATION
# =============================================
LOGIN_URL = 'accounting:login' 
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'home'

# =============================================
# EMAIL (Development default, Production overrides)
# =============================================
if DEBUG:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = 'smtp.gmail.com'
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER')
    EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD')
    DEFAULT_FROM_EMAIL = os.getenv('EMAIL_HOST_USER')
else:
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
    EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
    EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'
    EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
    DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@yourdomain.com')

# =============================================
# MESSAGE TAGS (Bootstrap alert classes)
# =============================================
MESSAGE_TAGS = {
    messages.DEBUG: 'secondary',
    messages.INFO: 'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR: 'danger',
}

# =============================================
# LOGGING (Development / Production)
# =============================================
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'level': 'DEBUG' if DEBUG else 'INFO',
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
        'file': {
            'level': 'ERROR',
            'class': 'logging.FileHandler',
            'filename': BASE_DIR / 'logs/django_errors.log',
            'formatter': 'verbose',
        },
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': True,
        },
        'django.request': {
            'handlers': ['file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'accounting': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG' if DEBUG else 'INFO',
            'propagate': True,
        },
        # =============================================
        # Real-time Notifications (EventStream) ke liye Logger
        # Ye console ko Spam (GET /events/ ... 200) hone se rokti hai
        # =============================================
        'django_eventstream': {
            'handlers': ['console'],
            'level': 'WARNING',  # INFO aur DEBUG level ki saari lines dab jayengi
            'propagate': False,
        },
        # =============================================
        # Static files (CSS/JS) ki har request console me na dikhe
        # =============================================
        'django.server': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

# =============================================
# PRODUCTION SECURITY SETTINGS (Conditional)
# =============================================
if not DEBUG:
    # HSTS
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = True
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'

    # Admin email for error notifications (set env vars)
    ADMINS = [('Admin', os.getenv('ADMIN_EMAIL', 'admin@example.com'))]
    SERVER_EMAIL = os.getenv('SERVER_EMAIL', 'server@example.com')

# =============================================
# ENSURE LOGS DIRECTORY EXISTS
# =============================================
LOGS_DIR = BASE_DIR / 'logs'
if not LOGS_DIR.exists():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    
    

# JAZZMIN_SETTINGS = {
#     "site_title": "A1 Computer Solutions",
#     "site_header": "ACS Admin",
#     "welcome_sign": "Welcome to the ACS Admin Panel.",
#     "copyright": "A1 Computer Solutions",
#     "show_ui_builder": True,
# }


# Honeypot Settings
HONEYPOT_FIELD_NAME = 'phone' 
HONEYPOT_VALUE = ''  
HONEYPOT_VERIFY = True 

    
