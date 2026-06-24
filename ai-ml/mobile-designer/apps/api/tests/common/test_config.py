import pytest

from src.common.config import Settings


class TestSettings:
    def test_default_values(self) -> None:
        settings = Settings(
            _env_file=None,
            environment="test",
            aws_region="us-east-1",
            s3_bucket_name="test-bucket",
            jwt_secret_name="test/jwt-keys",
        )
        assert settings.environment == "test"
        assert settings.jwt_access_token_expire_minutes == 60
        assert settings.jwt_refresh_token_expire_days == 30
        assert settings.max_file_size_mb == 20
        assert "pdf" in settings.allowed_file_types

    def test_cors_origins_list(self) -> None:
        settings = Settings(
            _env_file=None,
            cors_origins=["http://localhost:3000", "https://app.example.com"],
        )
        assert len(settings.cors_origins) == 2
        assert "http://localhost:3000" in settings.cors_origins

    def test_bedrock_region_separate(self) -> None:
        settings = Settings(
            _env_file=None,
            aws_region="ap-northeast-2",
            aws_bedrock_region="us-west-2",
        )
        assert settings.aws_region == "ap-northeast-2"
        assert settings.aws_bedrock_region == "us-west-2"
