import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestCommentFlow:
    async def test_create_and_list_comments(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "Comment Project"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.post("/collaboration/comments", json={
            "project_id": project_id, "screen_id": "main", "stage_id": "wireframe",
            "content": "버튼 위치 좀 변경해주세요",
        }, headers=auth_headers)
        assert resp.status_code == 201
        comment = resp.json()
        assert comment["content"] == "버튼 위치 좀 변경해주세요"
        assert comment["resolved"] is False
        assert comment["component_id"] is None

        list_resp = await client.get(f"/collaboration/comments?project_id={project_id}&screen_id=main", headers=auth_headers)
        assert list_resp.status_code == 200
        assert len(list_resp.json()) >= 1

    async def test_create_component_level_comment(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "Component Comment"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.post("/collaboration/comments", json={
            "project_id": project_id, "screen_id": "login", "stage_id": "design",
            "content": "색상 더 진하게", "component_id": "login-submit-btn",
        }, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.json()["component_id"] == "login-submit-btn"

    async def test_resolve_comment(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "Resolve Project"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        comment_resp = await client.post("/collaboration/comments", json={
            "project_id": project_id, "screen_id": "home", "stage_id": "wireframe", "content": "수정 필요",
        }, headers=auth_headers)
        comment_id = comment_resp.json()["comment_id"]

        resp = await client.patch(
            f"/collaboration/comments/{comment_id}/resolve?project_id={project_id}&screen_id=home",
            json={"resolved": True}, headers=auth_headers,
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestShareLinkFlow:
    async def test_create_and_verify_share_link(self, client: AsyncClient, auth_headers: dict) -> None:
        me_resp = await client.get("/auth/me", headers=auth_headers)
        team_id = me_resp.json()["personal_team_id"]

        create_resp = await client.post("/projects", json={"name": "Share Project"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        share_resp = await client.post("/collaboration/share", json={
            "project_id": project_id, "team_id": team_id, "permission": "read_only",
        }, headers=auth_headers)
        assert share_resp.status_code == 201
        share_data = share_resp.json()
        assert len(share_data["share_token"]) == 64
        assert share_data["permission"] == "read_only"
        assert share_data["active"] is True

        verify_resp = await client.get(f"/collaboration/share/{share_data['share_token']}")
        assert verify_resp.status_code == 200
        assert verify_resp.json()["project_id"] == project_id

    async def test_deactivate_share_link(self, client: AsyncClient, auth_headers: dict) -> None:
        me_resp = await client.get("/auth/me", headers=auth_headers)
        team_id = me_resp.json()["personal_team_id"]

        create_resp = await client.post("/projects", json={"name": "Deactivate Share"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        share_resp = await client.post("/collaboration/share", json={
            "project_id": project_id, "team_id": team_id, "permission": "edit",
        }, headers=auth_headers)
        token = share_resp.json()["share_token"]

        del_resp = await client.delete(f"/collaboration/share/{token}", headers=auth_headers)
        assert del_resp.status_code == 204

        verify_resp = await client.get(f"/collaboration/share/{token}")
        assert verify_resp.status_code == 403

    async def test_nonexistent_share_link_returns_404(self, client: AsyncClient) -> None:
        resp = await client.get("/collaboration/share/nonexistent_token_abcdef1234567890")
        assert resp.status_code == 404
