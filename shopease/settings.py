"""Environment-driven settings for ShopEase."""
import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

BASE_DIR = Path(__file__).resolve().parent.parent
if load_dotenv:
    load_dotenv(BASE_DIR / ".env")


def _env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name, default=""):
    return [item.strip().rstrip("/") for item in os.environ.get(name, default).split(",") if item.strip()]


def _build_host_security_settings():
    """Build host and CSRF allowlists from the public callback URL too."""
    # Hosts come exclusively from the environment (ALLOWED_HOSTS plus the
    # APP_BASE_URL / PAYMENT_CALLBACK_BASE_URL hostnames). Production must be
    # configured explicitly; development relies on Django's DEBUG-time
    # localhost allowance, and the test runner always appends "testserver".
    allowed_hosts = _env_list("ALLOWED_HOSTS", "")
    trusted_origins = _env_list("CSRF_TRUSTED_ORIGINS", "")
    for raw_url in (os.environ.get("APP_BASE_URL", ""), os.environ.get("PAYMENT_CALLBACK_BASE_URL", "")):
        parsed = urlparse(raw_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            continue
        if parsed.hostname not in allowed_hosts:
            allowed_hosts.append(parsed.hostname)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in trusted_origins:
            trusted_origins.append(origin)
    return allowed_hosts, trusted_origins


DEBUG = _env_bool("DEBUG", False)

# Production is enabled explicitly.
IS_PRODUCTION = (
    _env_bool("IS_PRODUCTION", False)
    or os.environ.get("DJANGO_ENV", "").lower() == "production"
)
if IS_PRODUCTION and DEBUG:
    raise ImproperlyConfigured("DEBUG must be false when IS_PRODUCTION=true.")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if IS_PRODUCTION:
        raise ImproperlyConfigured("SECRET_KEY must be set when IS_PRODUCTION=true.")
    # A local developer who has not created .env still gets a safe ephemeral
    # key, instead of a secret that is published in the source tree.
    SECRET_KEY = get_random_secret_key()

APP_BASE_URL = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")
ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS = _build_host_security_settings()
_default_trusted_origins = APP_BASE_URL if APP_BASE_URL.startswith("https://") else ""
if _default_trusted_origins and _default_trusted_origins not in CSRF_TRUSTED_ORIGINS:
    CSRF_TRUSTED_ORIGINS.append(_default_trusted_origins)
# Same-origin by default; cross-origin access must be explicitly allowed.
CORS_ALLOWED_ORIGINS = _env_list("CORS_ALLOWED_ORIGINS", _default_trusted_origins)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "social_django",
    "store",
    "django_extensions"
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "store.middleware.RestrictedCorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "store.middleware.ProductionSecurityHeadersMiddleware",
]
ROOT_URLCONF = "shopease.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.debug",
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "store.context_processors.user_profile",
    ]},
}]
WSGI_APPLICATION = "shopease.wsgi.application"

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
# Serverless containers can scale horizontally.  Closing a connection at the
# end of a request is the safe default for Supabase's shared pooler; a
# persistent host may opt in to a longer lifetime explicitly.
DB_CONN_MAX_AGE = int(os.environ.get("DB_CONN_MAX_AGE", "0" if IS_PRODUCTION else "600"))
DATABASE_SSL_REQUIRE = _env_bool("DATABASE_SSL_REQUIRE", IS_PRODUCTION)

if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=DB_CONN_MAX_AGE,
            ssl_require=DATABASE_SSL_REQUIRE,
        )
    }
    DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
    if IS_PRODUCTION and DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
        raise ImproperlyConfigured("IS_PRODUCTION requires a PostgreSQL DATABASE_URL.")
elif any(os.environ.get(name, "").strip() for name in ("POSTGRES_DB", "DB_NAME")):
    # Discrete variables are useful for Docker Compose and providers that do
    # not expose a DATABASE_URL. POSTGRES_* takes precedence over DB_*.
    DATABASES = {
        "default": {
            "ENGINE": os.environ.get("DB_ENGINE", "django.db.backends.postgresql"),
            "NAME": os.environ.get("POSTGRES_DB", os.environ.get("DB_NAME", "")),
            "USER": os.environ.get("POSTGRES_USER", os.environ.get("DB_USER", "")),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", os.environ.get("DB_PASSWORD", "")),
            "HOST": os.environ.get("POSTGRES_HOST", os.environ.get("DB_HOST", "")),
            "PORT": os.environ.get("POSTGRES_PORT", os.environ.get("DB_PORT", "5432")),
            "CONN_MAX_AGE": DB_CONN_MAX_AGE,
            "CONN_HEALTH_CHECKS": True,
        }
    }
    if IS_PRODUCTION and DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
        raise ImproperlyConfigured("IS_PRODUCTION requires the PostgreSQL database backend.")
    if IS_PRODUCTION and any(
        not DATABASES["default"][setting]
        for setting in ("NAME", "USER", "PASSWORD", "HOST")
    ):
        raise ImproperlyConfigured(
            "Production PostgreSQL configuration requires database name, user, password, and host."
        )
    if DATABASE_SSL_REQUIRE:
        DATABASES["default"]["OPTIONS"] = {"sslmode": "require"}
else:
    if IS_PRODUCTION:
        raise ImproperlyConfigured(
            "Production requires DATABASE_URL or POSTGRES_DB/DB_NAME and PostgreSQL credentials."
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

AUTHENTICATION_BACKENDS = ["social_core.backends.google.GoogleOAuth2", "django.contrib.auth.backends.ModelBackend"]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
SOCIAL_AUTH_GOOGLE_OAUTH2_KEY = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
SOCIAL_AUTH_GOOGLE_OAUTH2_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
SOCIAL_AUTH_PIPELINE = [
    "social_core.pipeline.social_auth.social_details",
    "social_core.pipeline.social_auth.social_uid",
    "social_core.pipeline.social_auth.auth_allowed",
    "social_core.pipeline.social_auth.social_user",
    "social_core.pipeline.user.get_username",
    "social_core.pipeline.user.create_user",
    "social_core.pipeline.social_auth.associate_user",
    "social_core.pipeline.social_auth.load_extra_data",
    "social_core.pipeline.user.user_details",
    "store.social_pipeline.sync_google_login_session",
]
SOCIAL_AUTH_LOGIN_ERROR_URL = "/login/"
SOCIAL_AUTH_ASSOCIATE_BY_EMAIL = True

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
# A manifest is essential in production, where WhiteNoise serves immutable,
# fingerprinted assets.  Local runserver and Django's test client should use
# source static files instead: they must not depend on a previously collected
# manifest that may be stale while an asset is being edited.
STORAGES = {
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if IS_PRODUCTION
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        )
    }
}
WHITENOISE_MAX_AGE = 31536000
WHITENOISE_USE_FINDERS = not IS_PRODUCTION
# The existing app stores new uploads in UploadedImage (PostgreSQL); MEDIA_ROOT
# exists only for legacy local uploads and is not relied upon in production.
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
MAX_UPLOAD_IMAGE_BYTES = int(os.environ.get("MAX_UPLOAD_IMAGE_BYTES", str(10 * 1024 * 1024)))

TRUST_X_FORWARDED_PROTO = _env_bool("TRUST_X_FORWARDED_PROTO", IS_PRODUCTION)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if TRUST_X_FORWARDED_PROTO else None

USE_X_FORWARDED_HOST = _env_bool("USE_X_FORWARDED_HOST", IS_PRODUCTION)

# Local Django runserver is HTTP-only.
# Production hosting can use HTTPS.
SECURE_SSL_REDIRECT = _env_bool(
    "SECURE_SSL_REDIRECT",
    IS_PRODUCTION,
)

SESSION_COOKIE_SECURE = _env_bool(
    "SESSION_COOKIE_SECURE",
    IS_PRODUCTION,
)

CSRF_COOKIE_SECURE = _env_bool(
    "CSRF_COOKIE_SECURE",
    IS_PRODUCTION,
)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "31536000" if IS_PRODUCTION else "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", IS_PRODUCTION)
SECURE_HSTS_PRELOAD = _env_bool("SECURE_HSTS_PRELOAD", False)

RAZORPAY_MODE = os.environ.get("RAZORPAY_MODE", "test").strip().lower()
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
PAYMENT_CALLBACK_BASE_URL = os.environ.get("PAYMENT_CALLBACK_BASE_URL", APP_BASE_URL).strip().rstrip("/")
RAZORPAY_PAYMENT_LINK_OVERRIDE_URL = os.environ.get("RAZORPAY_PAYMENT_LINK_OVERRIDE_URL", "")
RAZORPAY_PAYMENT_HANDLE_URL = os.environ.get("RAZORPAY_PAYMENT_HANDLE_URL", "")

EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = _env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@shopease.example.com")

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO" if IS_PRODUCTION else "DEBUG")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"verbose": {"format": "{asctime} {levelname} {name} {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "verbose"}},
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
