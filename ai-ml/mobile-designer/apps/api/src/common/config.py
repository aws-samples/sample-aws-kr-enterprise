from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: str = "development"
    debug: bool = False

    aws_region: str = "ap-northeast-2"
    aws_bedrock_region: str = "us-west-2"

    dynamodb_endpoint_url: str | None = None
    s3_endpoint_url: str | None = None

    s3_bucket_name: str = "mdesigner-files"
    s3_presigned_url_expiry: int = 3600

    jwt_secret_name: str = "mdesigner/jwt-keys"
    jwt_secret_source: str = "env"  # "env" or "secretsmanager"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 30
    jwt_algorithm: str = "HS256"

    cors_origins: list[str] = ["http://localhost:5173"]

    ses_sender_email: str = "noreply@example.com"
    ses_region: str = "ap-northeast-2"

    # Public base URL of the web app, used to build password-reset links in emails.
    frontend_url: str = "http://localhost:3000"

    bedrock_agent_id: str = ""
    bedrock_agent_alias_id: str = ""
    bedrock_chatbot_agent_id: str = ""
    bedrock_chatbot_agent_alias_id: str = ""

    # Max seconds to wait for a single Bedrock agent invocation before timing out.
    bedrock_invocation_timeout_seconds: float = 300.0

    max_file_size_mb: int = 20
    allowed_file_types: list[str] = ["pdf", "docx", "md", "txt"]

    model_config = {"env_prefix": "MDESIGNER_", "env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
