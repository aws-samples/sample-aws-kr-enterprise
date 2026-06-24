import pytest
from unittest.mock import patch


class TestAppCreation:
    def test_create_app_returns_fastapi_instance(self) -> None:
        from src.main import create_app
        app = create_app()
        assert app.title == "Mobile Designer API"

    def test_health_endpoint_registered(self) -> None:
        from src.main import create_app
        app = create_app()
        route_paths = [r.path for r in app.routes]
        assert "/health" in route_paths

    def test_all_routers_registered(self) -> None:
        from src.main import create_app
        app = create_app()
        route_paths = [r.path for r in app.routes]
        assert any("/auth" in p for p in route_paths)
        assert any("/projects" in p for p in route_paths)
        assert any("/ai" in p for p in route_paths)
        assert any("/files" in p for p in route_paths)
        assert any("/handoff" in p for p in route_paths)
        assert any("/collaboration" in p for p in route_paths)
