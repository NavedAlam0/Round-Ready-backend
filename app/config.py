import json
import os

from pydantic_settings import BaseSettings


def load_aws_secret_env() -> None:
    secret_name = os.getenv("AWS_BACKEND_ENV_SECRET_NAME")
    if not secret_name:
        return

    import boto3

    client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION"))
    response = client.get_secret_value(SecretId=secret_name)
    secret_string = response.get("SecretString")
    if not secret_string:
        return

    secret_values = json.loads(secret_string)
    for key, value in secret_values.items():
        if value is not None and not os.environ.get(key):
            os.environ[key] = json.dumps(value) if isinstance(value, (dict, list)) else str(value)


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


load_aws_secret_env()
settings = Settings()
