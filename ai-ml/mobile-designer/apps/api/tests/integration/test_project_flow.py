import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestProjectFlow:
    async def test_create_project(self, client: AsyncClient, auth_headers: dict) -> None:
        resp = await client.post("/projects", json={"name": "Shopping App"}, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Shopping App"
        assert data["current_stage"] == "requirements"
        assert data["project_id"] != ""

    async def test_list_projects(self, client: AsyncClient, auth_headers: dict) -> None:
        await client.post("/projects", json={"name": "Project A"}, headers=auth_headers)
        await client.post("/projects", json={"name": "Project B"}, headers=auth_headers)

        resp = await client.get("/projects", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["items"]) >= 2

    async def test_get_project_by_id(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "Detail Test"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.get(f"/projects/{project_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Detail Test"

    async def test_update_project_name(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "Original"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.patch(f"/projects/{project_id}", json={"name": "Updated"}, headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"

    async def test_delete_project(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "To Delete"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.delete(f"/projects/{project_id}", headers=auth_headers)
        assert resp.status_code == 204

    async def test_advance_stage_requires_completion(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "Stage Test"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.post(f"/projects/{project_id}/advance-stage", headers=auth_headers)
        assert resp.status_code == 422

    async def test_list_versions_empty_initially(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "Version Test"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.get(f"/projects/{project_id}/versions", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    async def test_unauthenticated_access_rejected(self, client: AsyncClient) -> None:
        resp = await client.post("/projects", json={"name": "Unauth"})
        assert resp.status_code == 403

    async def test_get_nonexistent_project_returns_404(self, client: AsyncClient, auth_headers: dict) -> None:
        resp = await client.get("/projects/nonexistent-id", headers=auth_headers)
        assert resp.status_code == 404
