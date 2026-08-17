from .base import ProviderConfigurationError


def _provider_classes():
    from .rfg import ResearchForGoodProvider
    from .toluna import TolunaProvider

    return {
        ResearchForGoodProvider.code: ResearchForGoodProvider,
        TolunaProvider.code: TolunaProvider,
    }


def provider_catalog() -> list[dict]:
    installed = [
        {
            "code": provider.code,
            "label": provider.label,
            "default_base_url": provider.default_base_url,
            "minimum_sync_interval_seconds": provider.minimum_sync_interval_seconds,
            "credential_fields": [
                {"key": key, "label": label} for key, label in provider.credential_fields
            ],
        }
        for provider in _provider_classes().values()
    ]
    generic = [
        {
            "code": "innovatemr",
            "label": "InnovateMR",
            "default_base_url": "https://supplier.innovatemr.net/api/v2",
            "minimum_sync_interval_seconds": 60,
            "credential_fields": [{"key": "token", "label": "API token"}],
        },
        {
            "code": "biobrain",
            "label": "BioBrain / Voqall",
            "default_base_url": "https://partner-api.voqall.com/api/v1/surveys",
            "minimum_sync_interval_seconds": 60,
            "credential_fields": [{"key": "token", "label": "Partner access key"}],
        },
        {
            "code": "custom",
            "label": "Custom REST API",
            "default_base_url": "",
            "minimum_sync_interval_seconds": 60,
            "credential_fields": [{"key": "token", "label": "API token"}],
        },
    ]
    return installed + generic


def get_provider(integration, *, session=None):
    provider_class = _provider_classes().get(integration.provider_code)
    if not provider_class:
        raise ProviderConfigurationError(
            f"Provider adapter '{integration.provider_code}' is not installed."
        )
    return provider_class(integration, session=session)
