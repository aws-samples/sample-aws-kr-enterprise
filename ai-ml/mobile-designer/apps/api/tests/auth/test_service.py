import pytest
from unittest.mock import AsyncMock, patch

from src.auth.jwt import JWTService
from src.auth.models import RegisterRequest
from src.auth.service import AuthService
from src.common.config import Settings
from src.common.exceptions import ConflictException, UnauthorizedException


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, jwt_secret_name="test-secret", jwt_access_token_expire_minutes=60, jwt_refresh_token_expire_days=30)


@pytest.fixture
def jwt_service(settings: Settings) -> JWTService:
    return JWTService(settings)


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.query = AsyncMock(return_value={"Items": []})
    db.put_item = AsyncMock(return_value={})
    db.get_item = AsyncMock(return_value=None)
    db.batch_write = AsyncMock(return_value=None)
    db.delete_item = AsyncMock(return_value={})
    db.update_item = AsyncMock(return_value={})
    return db


@pytest.fixture
def service(mock_db: AsyncMock, jwt_service: JWTService, settings: Settings) -> AuthService:
    return AuthService(mock_db, jwt_service, settings)


class TestRegister:
    @pytest.mark.asyncio
    async def test_register_creates_user_and_team(self, service: AuthService, mock_db: AsyncMock) -> None:
        req = RegisterRequest(email="Test@Example.COM", name="Alice", password="securepass1")
        result = await service.register(req)

        assert result.email == "test@example.com"
        assert result.name == "Alice"
        assert result.user_id != ""
        assert result.personal_team_id != ""
        mock_db.put_item.assert_called_once()
        mock_db.batch_write.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_normalizes_email_to_lowercase(self, service: AuthService) -> None:
        req = RegisterRequest(email="USER@Gmail.COM", name="Bob", password="securepass1")
        result = await service.register(req)
        assert result.email == "user@gmail.com"

    @pytest.mark.asyncio
    async def test_register_duplicate_email_raises_conflict(self, service: AuthService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": [{"userId": "existing"}]}
        req = RegisterRequest(email="dup@test.com", name="Dup", password="securepass1")
        with pytest.raises(ConflictException):
            await service.register(req)


class TestLogin:
    @pytest.mark.asyncio
    async def test_login_success_returns_tokens(self, service: AuthService, mock_db: AsyncMock) -> None:
        import bcrypt
        hashed = bcrypt.hashpw(b"mypassword", bcrypt.gensalt()).decode()
        mock_db.query.return_value = {"Items": [{"userId": "u-1", "email": "a@b.com", "passwordHash": hashed}]}

        result = await service.login("a@b.com", "mypassword")
        assert result.access_token != ""
        assert result.refresh_token != ""
        assert result.expires_in == 3600

    @pytest.mark.asyncio
    async def test_login_wrong_password_raises(self, service: AuthService, mock_db: AsyncMock) -> None:
        import bcrypt
        hashed = bcrypt.hashpw(b"correct", bcrypt.gensalt()).decode()
        mock_db.query.return_value = {"Items": [{"userId": "u-1", "email": "a@b.com", "passwordHash": hashed}]}

        with pytest.raises(UnauthorizedException):
            await service.login("a@b.com", "wrong")

    @pytest.mark.asyncio
    async def test_login_nonexistent_email_raises(self, service: AuthService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": []}
        with pytest.raises(UnauthorizedException):
            await service.login("nobody@x.com", "pass")


class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_refresh_valid_token(self, service: AuthService, mock_db: AsyncMock, jwt_service: JWTService) -> None:
        from src.auth.service import _hash_token

        token, token_id = jwt_service.create_refresh_token("u-1")
        mock_db.get_item.side_effect = [
            {"userId": "u-1", "tokenId": token_id, "tokenHash": _hash_token(token)},  # RefreshTokens lookup
            {"userId": "u-1", "email": "a@b.com", "personalTeamId": "t-1"},  # User lookup
        ]

        result = await service.refresh_token(token)
        assert result.access_token != ""
        assert result.refresh_token != token  # rotated
        mock_db.delete_item.assert_called_once()

    @pytest.mark.asyncio
    async def test_refresh_invalid_token_raises(self, service: AuthService) -> None:
        with pytest.raises(UnauthorizedException):
            await service.refresh_token("invalid.jwt.token")

    @pytest.mark.asyncio
    async def test_refresh_revoked_token_raises(self, service: AuthService, mock_db: AsyncMock, jwt_service: JWTService) -> None:
        token, _ = jwt_service.create_refresh_token("u-1")
        mock_db.get_item.return_value = None  # token not found (revoked)
        with pytest.raises(UnauthorizedException):
            await service.refresh_token(token)


class TestPasswordReset:
    @pytest.mark.asyncio
    async def test_request_reset_nonexistent_email_no_error(self, service: AuthService, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": []}
        result = await service.request_password_reset("nobody@test.com")
        # Should not raise, returns None silently

    @pytest.mark.asyncio
    async def test_confirm_reset_updates_password(self, service: AuthService, mock_db: AsyncMock, jwt_service: JWTService) -> None:
        reset_token = jwt_service.create_reset_token("u-1")
        await service.confirm_password_reset(reset_token, "newsecure123")
        mock_db.update_item.assert_called_once()
        call_kwargs = mock_db.update_item.call_args
        assert "passwordHash" in str(call_kwargs) or ":ph" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_confirm_reset_invalid_token_raises(self, service: AuthService) -> None:
        with pytest.raises(UnauthorizedException):
            await service.confirm_password_reset("bad-token", "newpass123")
