import pytest

from src.auth.jwt import JWTService
from src.common.config import Settings
from src.common.exceptions import UnauthorizedException


@pytest.fixture
def jwt_service() -> JWTService:
    settings = Settings(
        _env_file=None,
        jwt_secret_name="test-secret-key-for-jwt",
        jwt_algorithm="HS256",
        jwt_access_token_expire_minutes=60,
        jwt_refresh_token_expire_days=30,
    )
    return JWTService(settings)


class TestJWTService:
    def test_create_and_verify_access_token(self, jwt_service: JWTService) -> None:
        token = jwt_service.create_access_token("user-123", "test@example.com")
        payload = jwt_service.verify_access_token(token)
        assert payload["sub"] == "user-123"
        assert payload["email"] == "test@example.com"
        assert payload["type"] == "access"

    def test_create_and_verify_refresh_token(self, jwt_service: JWTService) -> None:
        token, token_id = jwt_service.create_refresh_token("user-123")
        payload = jwt_service.verify_refresh_token(token)
        assert payload["sub"] == "user-123"
        assert payload["type"] == "refresh"
        assert payload["jti"] == token_id

    def test_create_and_verify_reset_token(self, jwt_service: JWTService) -> None:
        token = jwt_service.create_reset_token("user-123")
        payload = jwt_service.verify_reset_token(token)
        assert payload["sub"] == "user-123"
        assert payload["type"] == "reset"

    def test_access_token_rejected_as_refresh(self, jwt_service: JWTService) -> None:
        token = jwt_service.create_access_token("user-123", "test@example.com")
        with pytest.raises(UnauthorizedException):
            jwt_service.verify_refresh_token(token)

    def test_refresh_token_rejected_as_access(self, jwt_service: JWTService) -> None:
        token, _ = jwt_service.create_refresh_token("user-123")
        with pytest.raises(UnauthorizedException):
            jwt_service.verify_access_token(token)

    def test_invalid_token_raises(self, jwt_service: JWTService) -> None:
        with pytest.raises(UnauthorizedException):
            jwt_service.verify_access_token("invalid.token.here")

    def test_wrong_secret_raises(self) -> None:
        settings1 = Settings(_env_file=None, jwt_secret_name="secret-1")
        settings2 = Settings(_env_file=None, jwt_secret_name="secret-2")
        svc1 = JWTService(settings1)
        svc2 = JWTService(settings2)

        token = svc1.create_access_token("user-123", "test@example.com")
        with pytest.raises(UnauthorizedException):
            svc2.verify_access_token(token)
