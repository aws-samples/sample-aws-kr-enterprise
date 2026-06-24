import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestFileUploadFlow:
    async def test_presign_valid_pdf(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "File Project"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.post("/files/presign", json={
            "project_id": project_id, "filename": "requirements.pdf",
            "content_type": "application/pdf", "size": 5000,
        }, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["file_id"] != ""
        assert "upload_url" in data
        assert data["max_size_bytes"] == 20 * 1024 * 1024

    async def test_presign_rejects_oversized_file(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "File Big"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.post("/files/presign", json={
            "project_id": project_id, "filename": "huge.pdf",
            "content_type": "application/pdf", "size": 100 * 1024 * 1024,
        }, headers=auth_headers)
        assert resp.status_code == 422

    async def test_presign_rejects_unsupported_type(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "File Type"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.post("/files/presign", json={
            "project_id": project_id, "filename": "script.sh",
            "content_type": "application/x-sh", "size": 100,
        }, headers=auth_headers)
        assert resp.status_code == 422

    async def test_presign_accepts_images(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "File Image"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        for ct in ["image/png", "image/jpeg", "image/webp"]:
            resp = await client.post("/files/presign", json={
                "project_id": project_id, "filename": f"screenshot.{ct.split('/')[1]}",
                "content_type": ct, "size": 2048,
            }, headers=auth_headers)
            assert resp.status_code == 201

    async def test_complete_nonexistent_file_returns_404(self, client: AsyncClient, auth_headers: dict) -> None:
        resp = await client.post("/files/complete", json={
            "project_id": "p-fake", "file_id": "f-fake",
        }, headers=auth_headers)
        assert resp.status_code == 404

    async def test_list_files_empty(self, client: AsyncClient, auth_headers: dict) -> None:
        create_resp = await client.post("/projects", json={"name": "File List"}, headers=auth_headers)
        project_id = create_resp.json()["project_id"]

        resp = await client.get(f"/files/{project_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == []
