import json
import pytest
from unittest.mock import AsyncMock, patch

from src.ai.models import AgentInput
from src.ai.orchestrator import AIOrchestrator
from src.ai.task_manager import TaskManager, TaskStatus
from src.common.config import Settings
from src.common.exceptions import ServiceUnavailableException


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        aws_region="us-east-1",
        bedrock_agent_id="agent-123",
        bedrock_agent_alias_id="alias-456",
    )


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    db.put_item = AsyncMock(return_value={})
    db.get_item = AsyncMock(return_value=None)
    db.query = AsyncMock(return_value={"Items": []})
    db.update_item = AsyncMock(return_value={})
    return db


@pytest.fixture
def mock_s3() -> AsyncMock:
    s3 = AsyncMock()
    s3.put_object = AsyncMock(return_value=None)
    s3.get_object = AsyncMock(return_value=b'{"screens": []}')
    return s3


@pytest.fixture
def orchestrator(settings: Settings, mock_db: AsyncMock, mock_s3: AsyncMock) -> AIOrchestrator:
    orch = AIOrchestrator(settings, mock_db, mock_s3)
    # Isolate task state per test so background tasks don't leak across cases.
    orch._task_manager = TaskManager()
    return orch


def _make_agent_input(stage: str = "wireframe", command: str = "Generate design") -> AgentInput:
    return AgentInput(
        session_id="test-session",
        project_id="p-1",
        command=command,
        stage=stage,
        context={},
    )


class TestStartGenerate:
    @pytest.mark.asyncio
    async def test_returns_task_id_and_creates_task(self, orchestrator: AIOrchestrator) -> None:
        with patch("src.ai.orchestrator.bedrock_circuit") as mock_cb:
            mock_cb.is_call_permitted.return_value = True
            with patch.object(orchestrator, "_run_generate", new_callable=AsyncMock):
                task_id = orchestrator.start_generate(_make_agent_input(), "u-1", "t-1")

        task = orchestrator._task_manager.get_task(task_id)
        assert task is not None
        assert task.project_id == "p-1"

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_raises_service_unavailable(self, orchestrator: AIOrchestrator) -> None:
        with patch("src.ai.orchestrator.bedrock_circuit") as mock_cb:
            mock_cb.is_call_permitted.return_value = False
            with pytest.raises(ServiceUnavailableException):
                orchestrator.start_generate(_make_agent_input(), "u-1", "t-1")


class TestStartModify:
    @pytest.mark.asyncio
    async def test_returns_task_id(self, orchestrator: AIOrchestrator) -> None:
        with patch("src.ai.orchestrator.bedrock_circuit") as mock_cb:
            mock_cb.is_call_permitted.return_value = True
            with patch.object(orchestrator, "_run_modify", new_callable=AsyncMock):
                task_id = orchestrator.start_modify(_make_agent_input(), "u-1", "t-1")

        assert orchestrator._task_manager.get_task(task_id) is not None

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_raises(self, orchestrator: AIOrchestrator) -> None:
        with patch("src.ai.orchestrator.bedrock_circuit") as mock_cb:
            mock_cb.is_call_permitted.return_value = False
            with pytest.raises(ServiceUnavailableException):
                orchestrator.start_modify(_make_agent_input(), "u-1", "t-1")


class TestStartChat:
    def test_circuit_breaker_open_raises(self, orchestrator: AIOrchestrator) -> None:
        from src.ai.models import ChatRequest

        request = ChatRequest(project_id="p-1", session_id="s-1", message="hi", stage="requirements")
        with patch("src.ai.orchestrator.bedrock_circuit") as mock_cb:
            mock_cb.is_call_permitted.return_value = False
            with pytest.raises(ServiceUnavailableException):
                orchestrator.start_chat(request)


class TestRunGenerate:
    @pytest.mark.asyncio
    async def test_successful_generation_completes_task_and_saves_version(
        self, orchestrator: AIOrchestrator, mock_db: AsyncMock, mock_s3: AsyncMock
    ) -> None:
        valid_design = {"components": [{"id": "c1", "type": "Button", "props": {}, "style": {}}]}
        task = orchestrator._task_manager.create_task("p-1", "wireframe")

        with patch.object(orchestrator, "_invoke_design_agent", new_callable=AsyncMock, return_value=valid_design):
            with patch("src.ai.orchestrator.bedrock_circuit") as mock_cb:
                await orchestrator._run_generate(task.task_id, _make_agent_input(), "u-1", "t-1")

        updated = orchestrator._task_manager.get_task(task.task_id)
        assert updated.status == TaskStatus.COMPLETED
        assert updated.result == {"design": valid_design}
        mock_s3.put_object.assert_called()
        mock_cb.record_success.assert_called_once()

    @pytest.mark.asyncio
    async def test_generation_retries_on_validation_failure(self, orchestrator: AIOrchestrator) -> None:
        invalid_design = {"components": [{"id": "c1", "type": "InvalidWidget", "props": {}, "style": {}}]}
        valid_design = {"components": [{"id": "c1", "type": "Button", "props": {}, "style": {}}]}

        call_count = 0

        async def mock_invoke(agent_input, task_id=None):
            nonlocal call_count
            call_count += 1
            return invalid_design if call_count == 1 else valid_design

        task = orchestrator._task_manager.create_task("p-1", "wireframe")
        with patch.object(orchestrator, "_invoke_design_agent", side_effect=mock_invoke):
            with patch("src.ai.orchestrator.bedrock_circuit"):
                await orchestrator._run_generate(task.task_id, _make_agent_input(), "u-1", "t-1")

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_generation_error_records_circuit_failure_and_fails_task(
        self, orchestrator: AIOrchestrator
    ) -> None:
        task = orchestrator._task_manager.create_task("p-1", "wireframe")
        with patch.object(
            orchestrator, "_invoke_design_agent", new_callable=AsyncMock, side_effect=RuntimeError("Bedrock timeout")
        ):
            with patch("src.ai.orchestrator.bedrock_circuit") as mock_cb:
                await orchestrator._run_generate(task.task_id, _make_agent_input(), "u-1", "t-1")

        mock_cb.record_failure.assert_called_once()
        updated = orchestrator._task_manager.get_task(task.task_id)
        assert updated.status == TaskStatus.FAILED
        assert "Bedrock timeout" in updated.error


class TestRunModify:
    @pytest.mark.asyncio
    async def test_modify_with_no_existing_design_fails(self, orchestrator: AIOrchestrator) -> None:
        task = orchestrator._task_manager.create_task("p-1", "wireframe")
        with patch.object(orchestrator, "_get_latest_snapshot", new_callable=AsyncMock, return_value={}):
            await orchestrator._run_modify(task.task_id, _make_agent_input(), "u-1", "t-1")

        updated = orchestrator._task_manager.get_task(task.task_id)
        assert updated.status == TaskStatus.FAILED

    @pytest.mark.asyncio
    async def test_modify_applies_patches_and_saves(
        self, orchestrator: AIOrchestrator, mock_db: AsyncMock
    ) -> None:
        existing = {"screens": [{"name": "Home", "components": [{"id": "c1", "type": "Card", "props": {}}]}]}
        patches = {"patches": [{"action": "update", "screen": "Home", "id": "c1", "changes": {"props": {"text": "Hi"}}}]}

        with patch.object(orchestrator, "_get_latest_snapshot", new_callable=AsyncMock, return_value=existing):
            with patch.object(orchestrator, "_invoke_agent", new_callable=AsyncMock, return_value=json.dumps(patches)):
                with patch("src.ai.orchestrator.bedrock_circuit") as mock_cb:
                    task = orchestrator._task_manager.create_task("p-1", "wireframe")
                    await orchestrator._run_modify(task.task_id, _make_agent_input(), "u-1", "t-1")

        updated = orchestrator._task_manager.get_task(task.task_id)
        assert updated.status == TaskStatus.COMPLETED
        mock_cb.record_success.assert_called_once()


class TestInvokeDesignAgent:
    @pytest.mark.asyncio
    async def test_parses_json_from_agent_output(self, orchestrator: AIOrchestrator) -> None:
        design = {"screens": [{"name": "Home"}]}
        text = f"Here is the design:\n{json.dumps(design)}\nDone."

        with patch.object(orchestrator, "_invoke_agent", new_callable=AsyncMock, return_value=text):
            result = await orchestrator._invoke_design_agent(_make_agent_input())

        assert result == design

    @pytest.mark.asyncio
    async def test_retries_once_on_invalid_json(self, orchestrator: AIOrchestrator) -> None:
        design = {"screens": []}
        outputs = ["not json at all", json.dumps(design)]

        async def fake_invoke(agent, text):
            return outputs.pop(0)

        with patch.object(orchestrator, "_invoke_agent", side_effect=fake_invoke):
            result = await orchestrator._invoke_design_agent(_make_agent_input())

        assert result == design


class TestGetLatestSnapshot:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_versions(self, orchestrator: AIOrchestrator, mock_db: AsyncMock) -> None:
        mock_db.query.return_value = {"Items": []}
        result = await orchestrator._get_latest_snapshot("p-1", "wireframe")
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_parsed_snapshot(
        self, orchestrator: AIOrchestrator, mock_db: AsyncMock, mock_s3: AsyncMock
    ) -> None:
        version_item = {
            "versionId": "v-1", "projectId": "p-1", "stageId": "wireframe",
            "snapshotKey": "key/snap.json", "action": "initial", "command": "gen",
            "createdAt": "2026-01-01", "createdBy": "u-1",
        }
        mock_db.query.return_value = {"Items": [version_item]}
        mock_db.get_item.return_value = version_item
        mock_s3.get_object.return_value = b'{"screens": [{"name": "Home"}]}'

        result = await orchestrator._get_latest_snapshot("p-1", "wireframe")
        assert result == {"screens": [{"name": "Home"}]}


class TestInvokeAgentTimeout:
    @pytest.mark.asyncio
    async def test_timeout_raises_service_unavailable(self, orchestrator: AIOrchestrator) -> None:
        import asyncio

        orchestrator._settings.bedrock_invocation_timeout_seconds = 0.01

        def slow_agent(text):
            import time

            time.sleep(1)
            return "done"

        with pytest.raises(ServiceUnavailableException):
            await orchestrator._invoke_agent(slow_agent, "prompt")
