import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestAuthFlow:
    async def test_register_creates_user_and_team(self, client: AsyncClient) -> None:
        resp = await client.post("/auth/register", json={
            "email": "newuser@test.com", "name": "New User", "password": "SecurePass1",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "newuser@test.com"
        assert data["user_id"] != ""
        assert data["personal_team_id"] != ""

    async def test_register_duplicate_email_returns_409(self, client: AsyncClient) -> None:
        await client.post("/auth/register", json={"email": "dup@test.com", "name": "Alice", "password": "SecurePass1"})
        resp = await client.post("/auth/register", json={"email": "dup@test.com", "name": "Bob", "password": "SecurePass2"})
        assert resp.status_code == 409

    async def test_login_returns_tokens(self, client: AsyncClient) -> None:
        await client.post("/auth/register", json={"email": "login@test.com", "name": "Login", "password": "SecurePass1"})
        resp = await client.post("/auth/login", json={"email": "login@test.com", "password": "SecurePass1"})
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "Bearer"

    async def test_login_wrong_password_returns_401(self, client: AsyncClient) -> None:
        await client.post("/auth/register", json={"email": "wrongpw@test.com", "name": "WP", "password": "SecurePass1"})
        resp = await client.post("/auth/login", json={"email": "wrongpw@test.com", "password": "WrongPassword"})
        assert resp.status_code == 401

    async def test_login_nonexistent_returns_401(self, client: AsyncClient) -> None:
        resp = await client.post("/auth/login", json={"email": "nobody@test.com", "password": "AnyPass1"})
        assert resp.status_code == 401

    async def test_refresh_token_rotation(self, client: AsyncClient) -> None:
        await client.post("/auth/register", json={"email": "refresh@test.com", "name": "Ref", "password": "SecurePass1"})
        login_resp = await client.post("/auth/login", json={"email": "refresh@test.com", "password": "SecurePass1"})
        refresh_token = login_resp.json()["refresh_token"]

        resp = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 200
        new_data = resp.json()
        assert new_data["access_token"] != login_resp.json()["access_token"]
        assert new_data["refresh_token"] != refresh_token

    async def test_refresh_used_token_returns_401(self, client: AsyncClient) -> None:
        await client.post("/auth/register", json={"email": "reuse@test.com", "name": "Reuse", "password": "SecurePass1"})
        login_resp = await client.post("/auth/login", json={"email": "reuse@test.com", "password": "SecurePass1"})
        refresh_token = login_resp.json()["refresh_token"]

        await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        resp = await client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert resp.status_code == 401

    async def test_me_with_valid_token(self, client: AsyncClient, auth_headers: dict) -> None:
        resp = await client.get("/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["email"] == "inttest@example.com"

    async def test_me_without_token_returns_403(self, client: AsyncClient) -> None:
        resp = await client.get("/auth/me")
        assert resp.status_code == 403

    async def test_password_reset_request_always_202(self, client: AsyncClient) -> None:
        resp = await client.post("/auth/password-reset/request", json={"email": "nonexistent@test.com"})
        assert resp.status_code == 202
