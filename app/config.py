from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://user:pass@localhost:5432/roundready"
    secret_key: str = "dev-secret-key-change-me-in-production"
    claude_api_key: str = ""
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    daily_api_key: str = ""
    upload_dir: str = "./app/uploads"
    firebase_credentials_path: str = "./roundready-5fc54-firebase-adminsdk-fbsvc-2e9825edf9.json"
    firebase_credentials_json: str | None = None
    firebase_http_timeout: float = 10.0
    force_ipv4_http: bool = True
    cors_origins: str = "http://localhost:3000,https://roundready.in,https://www.roundready.in"
    voice_agent_url: str = "http://localhost:8001"

    model_config = {"env_file": ".env"}


settings = Settings()
