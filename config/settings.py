import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "development-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = [value.strip() for value in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if value.strip()]
API_DOCS_BASIC_USERNAME = os.getenv("API_DOCS_BASIC_USERNAME", "").strip()
API_DOCS_BASIC_PASSWORD = os.getenv("API_DOCS_BASIC_PASSWORD", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "django_filters",
    "accounts.apps.AccountsConfig",
    "vendors.apps.VendorsConfig",
    "surveys",
    "prescreener_vault.apps.PrescreenerVaultConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "accounts.request_cache.RequestAccessCacheMiddleware",
    "vendors.middleware.VendorPanelAccessMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": [
            "django.template.context_processors.request",
            "django.contrib.auth.context_processors.auth",
            "django.contrib.messages.context_processors.messages",
            "accounts.context_processors.access_context",
        ]},
    }
]
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DB_ENGINE = os.getenv("DB_ENGINE", "sqlite").lower()
if DB_ENGINE == "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("DB_NAME", "api-tool"),
            "USER": os.getenv("DB_USER", "api-tool"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "3306"),
            "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
                "isolation_level": "read committed",
                "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.getenv("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
        }
    }

# Prescreener profile data is deliberately isolated from the operational database.
# The SQLite fallback keeps local development and tests self-contained; production
# must enable the vault and provide the dedicated MySQL credentials below.
PRESCREENER_VAULT_ENABLED = env_bool("PRESCREENER_VAULT_ENABLED", False)
PRESCREENER_DB_ENGINE = os.getenv("PRESCREENER_DB_ENGINE", "sqlite").lower()
if PRESCREENER_DB_ENGINE == "mysql":
    DATABASES["prescreener_vault"] = {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.getenv("PRESCREENER_DB_NAME", "prescreener-vault"),
        "USER": os.getenv("PRESCREENER_DB_USER", ""),
        "PASSWORD": os.getenv("PRESCREENER_DB_PASSWORD", ""),
        "HOST": os.getenv("PRESCREENER_DB_HOST", "127.0.0.1"),
        "PORT": os.getenv("PRESCREENER_DB_PORT", "3306"),
        "CONN_MAX_AGE": int(os.getenv("PRESCREENER_DB_CONN_MAX_AGE", "60")),
        "OPTIONS": {
            "charset": "utf8mb4",
            "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            "isolation_level": "read committed",
            "connect_timeout": int(os.getenv("PRESCREENER_DB_CONNECT_TIMEOUT", "10")),
        },
    }
else:
    DATABASES["prescreener_vault"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.getenv("PRESCREENER_SQLITE_PATH", BASE_DIR / "prescreener_vault.sqlite3"),
    }

DATABASE_ROUTERS = ["prescreener_vault.router.PrescreenerVaultRouter"]

AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
DEPLOYED_STATIC_ROOT = BASE_DIR.parent / "exchange.api-grid.com" / "static"
STATIC_ROOT = Path(os.getenv(
    "DJANGO_STATIC_ROOT",
    DEPLOYED_STATIC_ROOT if DEPLOYED_STATIC_ROOT.parent.is_dir() else BASE_DIR / "staticfiles",
))
# Export workbooks are private, owner-scoped files and are removed by the
# scheduled cleanup task after their retention window.
EXPORT_JOB_DIR = Path(os.getenv("EXPORT_JOB_DIR", BASE_DIR / "var" / "exports"))
EXPORT_JOB_RETENTION_HOURS = max(1, int(os.getenv("EXPORT_JOB_RETENTION_HOURS", "24")))
STATICFILES_DIRS = [BASE_DIR / "static"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

CACHE_ENABLED = env_bool("CACHE_ENABLED", bool(os.getenv("REDIS_CACHE_URL", "").strip()))
CACHE_DEFAULT_TTL_SECONDS = max(1, int(os.getenv("CACHE_DEFAULT_TTL_SECONDS", "900")))
CACHE_TTL_JITTER_SECONDS = max(0, int(os.getenv("CACHE_TTL_JITTER_SECONDS", "180")))
CACHE_KEY_PREFIX = os.getenv("CACHE_KEY_PREFIX", "quant-tool").strip() or "quant-tool"
PROJECT_CACHE_DEFAULT_TTL_SECONDS = max(1, int(os.getenv("PROJECT_CACHE_DEFAULT_TTL_SECONDS", "300")))
PROJECT_CACHE_TTL_JITTER_SECONDS = max(0, int(os.getenv("PROJECT_CACHE_TTL_JITTER_SECONDS", "60")))
PROJECT_CACHE_FILTERS_TTL_SECONDS = max(1, int(os.getenv("PROJECT_CACHE_FILTERS_TTL_SECONDS", "600")))
PROJECT_CACHE_COUNT_TTL_SECONDS = max(1, int(os.getenv("PROJECT_CACHE_COUNT_TTL_SECONDS", "90")))
REPORT_CACHE_DEFAULT_TTL_SECONDS = max(1, int(os.getenv("REPORT_CACHE_DEFAULT_TTL_SECONDS", "30")))
REPORT_CACHE_RESULT_TTL_SECONDS = max(1, int(os.getenv("REPORT_CACHE_RESULT_TTL_SECONDS", "15")))
REPORT_CACHE_METADATA_TTL_SECONDS = max(1, int(os.getenv("REPORT_CACHE_METADATA_TTL_SECONDS", "600")))
REPORT_CACHE_DYNAMIC_METADATA_TTL_SECONDS = max(1, int(os.getenv("REPORT_CACHE_DYNAMIC_METADATA_TTL_SECONDS", "30")))
REPORT_CACHE_TTL_JITTER_SECONDS = max(0, int(os.getenv("REPORT_CACHE_TTL_JITTER_SECONDS", "3")))
INNOVATEMR_INVENTORY_WRITE_BATCH_SIZE = max(
    100, min(int(os.getenv("INNOVATEMR_INVENTORY_WRITE_BATCH_SIZE", "1000")), 5000)
)
SUPPLIER_CALLBACK_CONNECT_TIMEOUT_SECONDS = max(0.5, float(os.getenv("SUPPLIER_CALLBACK_CONNECT_TIMEOUT_SECONDS", "3")))
SUPPLIER_CALLBACK_READ_TIMEOUT_SECONDS = max(0.5, float(os.getenv("SUPPLIER_CALLBACK_READ_TIMEOUT_SECONDS", "7")))
SUPPLIER_CALLBACK_RECOVERY_INTERVAL_SECONDS = max(60, int(os.getenv("SUPPLIER_CALLBACK_RECOVERY_INTERVAL_SECONDS", "300")))
SUPPLIER_CALLBACK_RECOVERY_LOOKBACK_HOURS = max(1, int(os.getenv("SUPPLIER_CALLBACK_RECOVERY_LOOKBACK_HOURS", "168")))
SUPPLIER_CALLBACK_RECOVERY_BATCH = max(1, min(int(os.getenv("SUPPLIER_CALLBACK_RECOVERY_BATCH", "100")), 1000))

if CACHE_ENABLED:
    def redis_cache(alias_url, db, timeout, prefix):
        return {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": os.getenv(alias_url, f"redis://127.0.0.1:6379/{db}"),
            "TIMEOUT": timeout,
            "KEY_PREFIX": prefix,
            "OPTIONS": {
                "socket_connect_timeout": float(os.getenv("CACHE_CONNECT_TIMEOUT_SECONDS", "0.25")),
                "socket_timeout": float(os.getenv("CACHE_SOCKET_TIMEOUT_SECONDS", "0.25")),
                "max_connections": max(1, int(os.getenv("CACHE_MAX_CONNECTIONS", "100"))),
            },
        }

    CACHES = {
        "default": redis_cache("REDIS_CACHE_URL", 2, CACHE_DEFAULT_TTL_SECONDS, CACHE_KEY_PREFIX),
        "projects": redis_cache("PROJECTS_REDIS_CACHE_URL", 3, PROJECT_CACHE_DEFAULT_TTL_SECONDS, f"{CACHE_KEY_PREFIX}-projects"),
        "reports": redis_cache("REPORTS_REDIS_CACHE_URL", 4, REPORT_CACHE_DEFAULT_TTL_SECONDS, f"{CACHE_KEY_PREFIX}-reports"),
    }
else:
    CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": f"{CACHE_KEY_PREFIX}-local", "TIMEOUT": CACHE_DEFAULT_TTL_SECONDS, "OPTIONS": {"MAX_ENTRIES": 5000}},
        "projects": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": f"{CACHE_KEY_PREFIX}-projects-local", "TIMEOUT": PROJECT_CACHE_DEFAULT_TTL_SECONDS, "OPTIONS": {"MAX_ENTRIES": 5000}},
        "reports": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": f"{CACHE_KEY_PREFIX}-reports-local", "TIMEOUT": REPORT_CACHE_DEFAULT_TTL_SECONDS, "OPTIONS": {"MAX_ENTRIES": 10000}},
    }

SESSION_ENGINE = os.getenv(
    "DJANGO_SESSION_ENGINE",
    "django.contrib.sessions.backends.cached_db" if CACHE_ENABLED else "django.contrib.sessions.backends.db",
)
SESSION_CACHE_ALIAS = os.getenv("DJANGO_SESSION_CACHE_ALIAS", "default")
PERMISSION_CACHE_TTL_SECONDS = max(1, int(os.getenv("PERMISSION_CACHE_TTL_SECONDS", "300")))

REST_FRAMEWORK = {
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "surveys.pagination.SurveyPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "vendors.authentication.VendorAPIKeyAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"] if not DEBUG else [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Survey Workspace API",
    "DESCRIPTION": (
        "Internal multi-provider survey API. Active client-provider sections let authorized "
        "administrators execute allow-listed provider operations while every credential remains "
        "server-side. Authentication requires the current Django admin session and the separate "
        "documentation password."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "PREPROCESSING_HOOKS": [
        "vendors.schema.filter_unconfigured_upstream_provider_endpoints",
    ],
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "vendors.schema.remove_unconfigured_upstream_provider_tags",
    ],
    "TAGS": [
        {"name": "Surveys", "description": "Browse locally synchronized survey inventory."},
        {"name": "Survey details", "description": "Quota and pre-screening targeting captured from InnovateMR."},
        {"name": "Survey attempts", "description": "Staff-only respondent attempt, callback, IP and LOI audit records."},
        {"name": "Dashboard", "description": "Permission-aware operational analytics, client contribution and performance trends."},
        {"name": "User hits", "description": "Date-wise user hits and completes aggregated by respondent device."},
        {"name": "Synchronization", "description": "Trigger and audit upstream inventory synchronization."},
        {"name": "Access control", "description": "Dynamic roles, function assignments and per-user access overrides."},
        {"name": "Vendors & allocations", "description": "UAT client scope, vendor commercial policy and quantity allocation APIs."},
        {"name": "Organization hierarchy", "description": "Branch, Sub-branch, Shift, team assignment and unit-level client visibility APIs."},
        {"name": "Client API catalog", "description": "Search configured clients by stable name/code and inspect their available provider operations without database IDs."},
        {"name": "InnovateMR APIs", "description": "Every documented InnovateMR Supplier API, grouped for direct testing with server-side credentials and guarded live mutations."},
        {"name": "RFG APIs", "description": "Every documented Research For Good LiveAlert command using server-generated HMAC authentication."},
        {"name": "RFG Callbacks", "description": "Research For Good callback contract and safe result-code interpretation tools."},
        {"name": "Toluna APIs", "description": "Toluna External Sample and Reference Data diagnostics; live member registration and invite generation run inside the respondent flow."},
    ],
    "ENUM_NAME_OVERRIDES": {
        "SurveyStatusEnum": "surveys.models.Survey.Status",
        "SyncRunStatusEnum": "surveys.models.SyncRun.Status",
    },
}

INNOVATEMR_API_TOKEN = os.getenv("INNOVATEMR_API_TOKEN", "")
INNOVATEMR_BASE_URL = os.getenv("INNOVATEMR_BASE_URL", "https://supplier.innovatemr.net/api/v2").rstrip("/")
INNOVATEMR_CALLBACK_HASH_KEY = os.getenv("INNOVATEMR_CALLBACK_HASH_KEY", "")
INNOVATEMR_CALLBACK_HASH_ALGORITHM = os.getenv("INNOVATEMR_CALLBACK_HASH_ALGORITHM", "sha256").strip().lower()
INNOVATEMR_CALLBACK_HASH_REQUIRED = env_bool("INNOVATEMR_CALLBACK_HASH_REQUIRED", True)
BIOBRAIN_CALLBACK_HASH_KEY = (
    os.getenv("BIOBRAIN_CALLBACK_HASH_KEY")
    or os.getenv("BIOBRAIN_HASH_KEY")
    or ""
).strip()
BIOBRAIN_CALLBACK_HASH_ALGORITHM = os.getenv(
    "BIOBRAIN_CALLBACK_HASH_ALGORITHM",
    os.getenv("BIOBRAIN_HASH_ALGORITHM", "sha256"),
).strip().lower()
BIOBRAIN_CALLBACK_HASH_REQUIRED = bool(BIOBRAIN_CALLBACK_HASH_KEY) and env_bool(
    "BIOBRAIN_CALLBACK_HASH_REQUIRED", True
)
PUBLIC_SUPPLIER_CODE = os.getenv("PUBLIC_SUPPLIER_CODE", "1000").strip() or "1000"
INTEGRATION_CREDENTIAL_ENCRYPTION_KEY = os.getenv("INTEGRATION_CREDENTIAL_ENCRYPTION_KEY", SECRET_KEY)
INNOVATEMR_TIMEOUT_SECONDS = int(os.getenv("INNOVATEMR_TIMEOUT_SECONDS", "30"))
INNOVATEMR_PAGE_SIZE = int(os.getenv("INNOVATEMR_PAGE_SIZE", "100"))
INNOVATEMR_MAX_PAGES = int(os.getenv("INNOVATEMR_MAX_PAGES", "1000"))
INNOVATEMR_DETAIL_REFRESH_BATCH = int(os.getenv("INNOVATEMR_DETAIL_REFRESH_BATCH", "20"))
DJANGO_BEHIND_HTTPS_PROXY = env_bool("DJANGO_BEHIND_HTTPS_PROXY", False)
TRUST_X_FORWARDED_FOR = env_bool("TRUST_X_FORWARDED_FOR", DJANGO_BEHIND_HTTPS_PROXY)
ENFORCE_PROJECT_UNIQUE_ENTRY_IP = env_bool(
    "ENFORCE_PROJECT_UNIQUE_ENTRY_IP",
    env_bool("ENFORCE_GLOBAL_UNIQUE_ENTRY_IP", True),
)
ENFORCE_SURVEY_TARGET_COUNTRY = env_bool("ENFORCE_SURVEY_TARGET_COUNTRY", True)
GEOIP_CITY_DB_PATH = os.getenv("GEOIP_CITY_DB_PATH", "").strip()
GEOIP_LOOKUP_URL = os.getenv("GEOIP_LOOKUP_URL", "").strip()
GEOIP_LOOKUP_TIMEOUT_SECONDS = max(0.2, float(os.getenv("GEOIP_LOOKUP_TIMEOUT_SECONDS", "2.5")))
GEOIP_CACHE_TTL_SECONDS = max(60, int(os.getenv("GEOIP_CACHE_TTL_SECONDS", "2592000")))

CSRF_TRUSTED_ORIGINS = [
    value.strip() for value in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if value.strip()
]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if DJANGO_BEHIND_HTTPS_PROXY else None
USE_X_FORWARDED_HOST = DJANGO_BEHIND_HTTPS_PROXY
SESSION_COOKIE_SECURE = env_bool("DJANGO_SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_CSRF_COOKIE_SECURE", not DEBUG)
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_SECURE_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_HSTS_PRELOAD = SECURE_HSTS_SECONDS > 0

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300
ENABLE_SCHEDULED_JOBS = env_bool("ENABLE_SCHEDULED_JOBS", True)
INNOVATEMR_INVENTORY_SYNC_INTERVAL_SECONDS = int(os.getenv("INNOVATEMR_INVENTORY_SYNC_INTERVAL_SECONDS", "60"))
CLIENT_INTEGRATION_DISPATCH_INTERVAL_SECONDS = int(os.getenv("CLIENT_INTEGRATION_DISPATCH_INTERVAL_SECONDS", "30"))
CLIENT_INTEGRATION_INNOVATEMR_SYNC_INTERVAL_SECONDS = int(os.getenv("CLIENT_INTEGRATION_INNOVATEMR_SYNC_INTERVAL_SECONDS", "150"))
CLIENT_INTEGRATION_RFG_SYNC_INTERVAL_SECONDS = max(
    600, int(os.getenv("CLIENT_INTEGRATION_RFG_SYNC_INTERVAL_SECONDS", "600"))
)
CLIENT_INTEGRATION_TOLUNA_SYNC_INTERVAL_SECONDS = int(os.getenv("CLIENT_INTEGRATION_TOLUNA_SYNC_INTERVAL_SECONDS", "60"))
TOLUNA_NOTIFICATION_IP_ALLOWLIST = tuple(
    value.strip()
    for value in os.getenv(
        "TOLUNA_NOTIFICATION_IP_ALLOWLIST",
        "54.211.6.36/32,34.199.200.42/32,3.228.103.61/32",
    ).split(",")
    if value.strip()
)
TOLUNA_NOTIFICATION_TRUSTED_PROXY_IPS = tuple(
    value.strip()
    for value in os.getenv(
        "TOLUNA_NOTIFICATION_TRUSTED_PROXY_IPS",
        "127.0.0.1/32,::1/128",
    ).split(",")
    if value.strip()
)
TOLUNA_NOTIFICATION_HMAC_KEY = (
    os.getenv("TOLUNA_NOTIFICATION_HMAC_KEY", "").strip()
    or os.getenv("TOLUNA_HMAC_KEY", "").strip()
)
TOLUNA_NOTIFICATION_REQUIRE_HMAC = env_bool(
    "TOLUNA_NOTIFICATION_REQUIRE_HMAC", False
)
INNOVATEMR_DETAIL_SYNC_INTERVAL_SECONDS = int(os.getenv("INNOVATEMR_DETAIL_SYNC_INTERVAL_SECONDS", "60"))
INNOVATEMR_ATTEMPT_RECONCILE_INTERVAL_SECONDS = int(os.getenv("INNOVATEMR_ATTEMPT_RECONCILE_INTERVAL_SECONDS", "60"))
INNOVATEMR_ATTEMPT_RECONCILE_BATCH = int(os.getenv("INNOVATEMR_ATTEMPT_RECONCILE_BATCH", "20"))
INNOVATEMR_ATTEMPT_RECONCILE_LOOKBACK_HOURS = int(os.getenv("INNOVATEMR_ATTEMPT_RECONCILE_LOOKBACK_HOURS", "168"))
VENDOR_RESERVATION_TTL_MINUTES = int(os.getenv("VENDOR_RESERVATION_TTL_MINUTES", "180"))
VENDOR_RESERVATION_CLEANUP_INTERVAL_SECONDS = int(os.getenv("VENDOR_RESERVATION_CLEANUP_INTERVAL_SECONDS", "60"))
CELERY_BEAT_SCHEDULE = {
    "cleanup-expired-export-jobs": {
        "task": "surveys.cleanup_expired_export_jobs",
        "schedule": 3600.0,
    },
    "dispatch-client-integration-syncs": {
        "task": "surveys.dispatch_due_integrations",
        "schedule": float(CLIENT_INTEGRATION_DISPATCH_INTERVAL_SECONDS),
    },
    "reconcile-legacy-redirect-attempts": {
        "task": "surveys.reconcile_pending_attempts",
        "schedule": float(INNOVATEMR_ATTEMPT_RECONCILE_INTERVAL_SECONDS),
    },
    "expire-vendor-allocation-reservations": {
        "task": "vendors.expire_allocation_reservations",
        "schedule": float(VENDOR_RESERVATION_CLEANUP_INTERVAL_SECONDS),
    },
    "recover-pending-supplier-callbacks": {
        "task": "surveys.dispatch_pending_supplier_callbacks",
        "schedule": float(SUPPLIER_CALLBACK_RECOVERY_INTERVAL_SECONDS),
    },
} if ENABLE_SCHEDULED_JOBS else {}
