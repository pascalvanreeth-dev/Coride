from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    app_name: str = "Veloverhaal"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    user_agent: str = "Veloverhaal/0.1 (fietsgids voor Belgie; lokaal prototype)"
    wikipedia_user_agent: str = (
        "Veloverhaal/0.1 (https://www.openstreetmap.org/; veloverhaal-local@example.com)"
    )
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    overpass_urls: str = (
        "https://overpass-api.de/api/interpreter,"
        "https://lz4.overpass-api.de/api/interpreter"
    )
    osrm_bike_url: str = "https://routing.openstreetmap.de/routed-bike"
    wikipedia_langs: str = "nl,fr,de,en"


settings = Settings()
