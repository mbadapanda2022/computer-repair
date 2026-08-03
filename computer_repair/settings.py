import os
from pathlib import Path
from django.contrib.messages import constants as messages
from dotenv import load_dotenv

# =============================================
# 1. ENVIRONMENT VARIABLES LOAD
# =============================================
load_dotenv()

# =============================================
# 2. BASE DIRECTORY
# =============================================
BASE_DIR = Path(__file__).resolve().parent.parent

# =============================================
# 3. SECURITY & DEBUG (ENVIRONMENT BASED)
# =============================================
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY')
DEBUG = os.getenv('DJANGO_DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.getenv('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')

# =============================================
# 4. INSTALLED APPS
# =============================================
INSTALLED_APPS = [
    # Django Core
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    
    # Third Party
    'honeypot',
    'channels',
    'django_eventstream',
    'django_htmx',
    'django_cleanup.apps.CleanupConfig', 
    
    # Your Apps
    'accounting.apps.AccountingConfig',
]

# Cloudinary Storage – Only in Production
if not DEBUG:
    INSTALLED_APPS += ['cloudinary_storage', 'cloudinary']

# =============================================
# 5. MIDDLEWARE
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
# 6. SESSION & CSRF (CONDITIONAL)
# =============================================
SESSION_COOKIE_AGE = 1209600  # 2 weeks
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
# 7. URLS & TEMPLATES
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
# ASGI_APPLICATION = 'computer_repair.asgi.application'  # ASGI is disabled (using channels only for eventstream)

# =============================================
# 8. CHANNELS & EVENTSTREAM
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
# 9. DATABASE (PRODUCTION: POSTGRES, DEV: SQLITE)
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
# 10. PASSWORD VALIDATION
# =============================================
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# =============================================
# 11. INTERNATIONALIZATION
# =============================================
LANGUAGE_CODE = 'en-in'
TIME_ZONE = 'Asia/Kolkata'
USE_I18N = True
USE_TZ = True

# =============================================
# 12. STATIC & MEDIA FILES
# =============================================
STATIC_URL = 'static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# =============================================
# 13. CLOUDINARY STORAGE (PRODUCTION) - FULLY OPTIMIZED
# =============================================
if not DEBUG:
    print("🔥 DEBUG is False – Cloudinary block is running")
    import cloudinary
    import cloudinary.uploader
    import cloudinary.api
    import logging

    # Get Cloudinary credentials from environment
    CLOUDINARY_CLOUD_NAME = os.getenv('CLOUDINARY_CLOUD_NAME')
    CLOUDINARY_API_KEY = os.getenv('CLOUDINARY_API_KEY')
    CLOUDINARY_API_SECRET = os.getenv('CLOUDINARY_API_SECRET')

    # Configure Cloudinary with all necessary parameters
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,           # Always use HTTPS
        timeout=60,            # 60 seconds timeout for large files
        chunk_size=20 * 1024 * 1024,  # 20MB chunk size for large uploads
        max_file_size=100 * 1024 * 1024  # 100MB max file size
    )

    # Set Cloudinary as the default storage backend for media files
    DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'
    
    # Media URL - always use Cloudinary URL
    MEDIA_URL = f'https://res.cloudinary.com/{CLOUDINARY_CLOUD_NAME}/image/upload/'

    # Optional: Enable Cloudinary's built-in image transformations in templates
    # You can use {% cloudinary_url ... %} tag in templates if needed

    # Log a confirmation (visible in Render logs)
    logger = logging.getLogger(__name__)
    logger.info(f"✅ Cloudinary configured with cloud name: {CLOUDINARY_CLOUD_NAME}")
    print(f"🔥 DEFAULT_FILE_STORAGE = {DEFAULT_FILE_STORAGE}")

else:
    # Development: Use local media storage
    print("🔥 DEBUG is True – Using local storage")
    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

# =============================================
# 14. AUTHENTICATION URLs
# =============================================
LOGIN_URL = 'accounting:login'
LOGIN_REDIRECT_URL = 'home'
LOGOUT_REDIRECT_URL = 'home'

# =============================================
# 15. EMAIL (SMTP WITH GMAIL / ANY PROVIDER)
# =============================================
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@yourdomain.com')

# =============================================
# 16. MESSAGE TAGS (FOR BOOTSTRAP TOASTS)
# =============================================
MESSAGE_TAGS = {
    messages.DEBUG: 'secondary',
    messages.INFO: 'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR: 'danger',
}

# =============================================
# 17. LOGGING (FULLY CONFIGURED FOR PRODUCTION)
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
        'django_eventstream': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.server': {
            'handlers': ['console'],
            'level': 'WARNING',
            'propagate': False,
        },
        # Add Cloudinary logger to capture upload errors
        'cloudinary': {
            'handlers': ['console', 'file'],
            'level': 'ERROR',
            'propagate': True,
        },
    },
}

# =============================================
# 18. PRODUCTION SECURITY SETTINGS (HTTPS, HSTS, ETC.)
# =============================================
if not DEBUG:
    # HSTS (HTTP Strict Transport Security)
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    
    # SSL Redirection
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    
    # Additional Security Headers
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    REFERRER_POLICY = 'same-origin'  # Prevents referrer leakage
    
    # Admin & Server Email
    ADMINS = [('Admin', os.getenv('ADMIN_EMAIL', 'admin@example.com'))]
    SERVER_EMAIL = os.getenv('SERVER_EMAIL', 'server@example.com')
else:
    # In development, allow iframe for local testing
    X_FRAME_OPTIONS = 'SAMEORIGIN'

# =============================================
# 19. ENSURE LOGS DIRECTORY EXISTS
# =============================================
LOGS_DIR = BASE_DIR / 'logs'
if not LOGS_DIR.exists():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================
# 20. HONEYPOT SETTINGS (SPAM PROTECTION)
# =============================================
HONEYPOT_FIELD_NAME = 'phone'
HONEYPOT_VALUE = ''
HONEYPOT_VERIFY = True

# =============================================
# 21. ADDITIONAL PERFORMANCE & CUSTOM SETTINGS
# =============================================

# Maximum file upload size (10MB)
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB

# Session security
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# CSRF trusted origins (for Render)
CSRF_TRUSTED_ORIGINS = [
    'https://a1computersolutions.onrender.com',
    'https://*.onrender.com',
]

# =============================================
# END OF SETTINGS
# =============================================