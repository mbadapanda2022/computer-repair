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
# 4. INSTALLED APPS – CLEAN + ALLAUTH
# =============================================
INSTALLED_APPS = [
    # Django Core
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Allauth (Sites Framework + Social Login)
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',          # Social Login के लिए (Google/Facebook)
    # 'allauth.socialaccount.providers.google',   # Uncomment if needed
    # 'allauth.socialaccount.providers.facebook', # Uncomment if needed

    # Third Party
    'honeypot',
    'channels',
    'django_eventstream',
    'django_htmx',
    'django_cleanup.apps.CleanupConfig',

    # Cloudinary (Media Storage)
    'cloudinary_storage',
    'cloudinary',

    # My Apps
    'accounting.apps.AccountingConfig',
]

# Sites Framework – Required by Allauth
SITE_ID = 1

# =============================================
# 5. MIDDLEWARE – AccountMiddleware जोड़ा गया
# =============================================
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

# =============================================
# AUTHENTICATION BACKENDS – Custom Phone/Email + Allauth + Default
# =============================================
AUTHENTICATION_BACKENDS = [
    'accounting.auth_backends.EmailOrPhoneBackend',      
    'allauth.account.auth_backends.AuthenticationBackend',  
    'django.contrib.auth.backends.ModelBackend',        
]

# =============================================
# 6. SESSION & CSRF
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
# ASGI_APPLICATION = 'computer_repair.asgi.application'  # Disabled

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
# 12. STATIC FILES (Whitenoise – LOCAL)
# =============================================
STATIC_URL = 'static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
STATICFILES_STORAGE = 'whitenoise.storage.WhiteNoiseStaticFilesStorage'

# =============================================
# 13. MEDIA FILES (Cloudinary – PRODUCTION ONLY)
# =============================================
if not DEBUG:
    print("🔥 DEBUG is False – Configuring Cloudinary for media files")
    CLOUDINARY_STORAGE = {
        'CLOUD_NAME': os.getenv('CLOUDINARY_CLOUD_NAME'),
        'API_KEY': os.getenv('CLOUDINARY_API_KEY'),
        'API_SECRET': os.getenv('CLOUDINARY_API_SECRET'),
        'SECURE': True,
        'TIMEOUT': 120,
        'CHUNK_SIZE': 20 * 1024 * 1024,
        'PREFIX': 'media/',
    }

    import cloudinary
    cloudinary.config(
        cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
        api_key=os.getenv('CLOUDINARY_API_KEY'),
        api_secret=os.getenv('CLOUDINARY_API_SECRET'),
        secure=True,
        timeout=120,
        chunk_size=20 * 1024 * 1024,
    )

    DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'
    MEDIA_URL = f'https://res.cloudinary.com/{os.getenv("CLOUDINARY_CLOUD_NAME")}/image/upload/'
    print(f"🔥 CLOUDINARY_STORAGE configured with cloud name: {CLOUDINARY_STORAGE['CLOUD_NAME']}")
    print(f"🔥 DEFAULT_FILE_STORAGE = {DEFAULT_FILE_STORAGE}")
    print(f"🔥 MEDIA_URL = {MEDIA_URL}")
else:
    print("🔥 DEBUG is True – Using local media storage")
    MEDIA_URL = '/media/'
    MEDIA_ROOT = BASE_DIR / 'media'

# =============================================
# 14. AUTHENTICATION URLs (DEFAULT + ALLAUTH)
# =============================================
LOGIN_URL = 'accounting:login'        
LOGIN_REDIRECT_URL = 'home'             
LOGOUT_REDIRECT_URL = 'home'           

# =============================================
# 15. ALLAUTH SETTINGS – MODERN (बिना Deprecation Warnings के)
# =============================================
# Login Methods: Email, Username (Phone Custom Backend से हैंडल होगा)
ACCOUNT_LOGIN_METHODS = {'username', 'email'}

# Signup Fields: Email और Password (Username हटा दिया – आपके Custom Form में Username है, पर Allauth इससे प्रभावित नहीं)
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']

# OTP Verification ही Primary है – Allauth Email Verification बंद
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_LOGOUT_ON_GET = True
ACCOUNT_SIGNUP_VIEW = 'accounting.views.auth.CustomSignupView'

# (Optional) अगर Social Login चाहिए तो यहाँ Credentials डालें
# SOCIALACCOUNT_PROVIDERS = {
#     'google': {
#         'APP': {
#             'client_id': 'your-client-id',
#             'secret': 'your-secret',
#         }
#     }
# }

# =============================================
# 16. EMAIL (SMTP)
# =============================================
if not DEBUG:  
    # Production (DEBUG=False) – Mailgun
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = 'smtp.mailgun.org'
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = 'postmaster@sandboxe60a463cdea84f9781d4f115b0915307.mailgun.org'
    EMAIL_HOST_PASSWORD = os.getenv('MAILGUN_SMTP_PASSWORD', '')
    DEFAULT_FROM_EMAIL = 'Mailgun Sandbox <postmaster@sandboxe60a463cdea84f9781d4f115b0915307.mailgun.org>'
    ACCOUNT_EMAIL_SUBJECT_PREFIX = ''
else:  
    # Development (DEBUG=True) – Gmail
    EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
    EMAIL_HOST = 'smtp.gmail.com'
    EMAIL_PORT = 587
    EMAIL_USE_TLS = True
    EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
    EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
    DEFAULT_FROM_EMAIL = 'solutionsmanojtech@gmail.com'
    ACCOUNT_EMAIL_SUBJECT_PREFIX = ''

# =============================================
# 17. MESSAGE TAGS (FOR BOOTSTRAP TOASTS)
# =============================================
MESSAGE_TAGS = {
    messages.DEBUG: 'secondary',
    messages.INFO: 'info',
    messages.SUCCESS: 'success',
    messages.WARNING: 'warning',
    messages.ERROR: 'danger',
}

# =============================================
# 18. LOGGING
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
        'cloudinary': {
            'handlers': ['console', 'file'],
            'level': 'ERROR',
            'propagate': True,
        },
    },
}

# =============================================
# 19. PRODUCTION SECURITY SETTINGS
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
    REFERRER_POLICY = 'same-origin'
    ADMINS = [('Admin', os.getenv('ADMIN_EMAIL', 'admin@example.com'))]
    SERVER_EMAIL = os.getenv('SERVER_EMAIL', 'server@example.com')
else:
    X_FRAME_OPTIONS = 'SAMEORIGIN'

# =============================================
# 20. ENSURE LOGS DIRECTORY EXISTS
# =============================================
LOGS_DIR = BASE_DIR / 'logs'
if not LOGS_DIR.exists():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)

# =============================================
# 21. HONEYPOT SETTINGS
# =============================================
HONEYPOT_FIELD_NAME = 'phone'
HONEYPOT_VALUE = ''
HONEYPOT_VERIFY = True

# =============================================
# 22. ADDITIONAL SETTINGS
# =============================================
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10 MB
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

CSRF_TRUSTED_ORIGINS = [
    'https://a1computersolutions.onrender.com',
    'https://*.onrender.com',
]

# Force Cloudinary storage (if needed)
if not DEBUG:
    DEFAULT_FILE_STORAGE = 'cloudinary_storage.storage.MediaCloudinaryStorage'
    print("🔥 FORCE: DEFAULT_FILE_STORAGE =", DEFAULT_FILE_STORAGE)

# =============================================
# END OF SETTINGS
# =============================================