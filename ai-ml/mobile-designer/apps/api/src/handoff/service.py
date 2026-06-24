import asyncio
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Any

import structlog

from src.ai.task_manager import TaskStatus, task_manager
from src.common.db.client import DynamoDBClient
from src.common.db.tables import PROJECTS_TABLE
from src.common.exceptions import NotFoundException, ValidationException
from src.common.s3.client import S3Client
from src.handoff.code_generator.generator import CodeGenerator
from src.handoff.code_generator.theme_generator import ThemeGenerator
from src.handoff.models import ArtifactType, HandoffType
from src.projects.models import StageType
from src.projects.version_service import VersionService

logger = structlog.get_logger()


class HandoffService:
    def __init__(self, db: DynamoDBClient, s3: S3Client) -> None:
        self._db = db
        self._s3 = s3
        self._version_service = VersionService(db, s3)
        self._code_generator = CodeGenerator()
        self._theme_generator = ThemeGenerator()
        self._task_manager = task_manager

    async def generate_artifacts(
        self,
        project_id: str,
        version_id: str | None,
        team_id: str,
        user_id: str,
        handoff_type: HandoffType = HandoffType.FULL_PROJECT,
    ) -> dict[str, Any]:
        design_data, version_id = await self._resolve_design(project_id, version_id)

        if handoff_type == HandoffType.FULL_PROJECT:
            return await self._generate_full_project(project_id, version_id, design_data)
        elif handoff_type == HandoffType.DESIGN_TOKENS:
            return await self._generate_design_tokens(project_id, version_id, design_data)
        elif handoff_type == HandoffType.FIGMA_TOKENS:
            return await self._generate_figma_tokens(project_id, version_id, design_data)
        elif handoff_type == HandoffType.COMPOSE_THEME:
            return await self._generate_compose_theme(project_id, version_id, design_data)
        elif handoff_type == HandoffType.DESIGN_SPEC:
            return await self._generate_design_spec(project_id, version_id, design_data)
        else:
            return await self._generate_full_project(project_id, version_id, design_data)

    async def _resolve_design(self, project_id: str, version_id: str | None) -> tuple[dict[str, Any], str]:
        if version_id:
            snapshot_bytes = await self._version_service.get_snapshot(project_id, version_id)
        else:
            versions = await self._version_service.list_versions(project_id, StageType.DESIGN, limit=1)
            if not versions["items"]:
                raise ValidationException("No design version found. Complete Stage 3 first.")
            version_id = versions["items"][0].version_id
            snapshot_bytes = await self._version_service.get_snapshot(project_id, version_id)

        return json.loads(snapshot_bytes), version_id

    async def _generate_full_project(
        self, project_id: str, version_id: str, design_data: dict[str, Any]
    ) -> dict[str, Any]:
        compose_files = self._code_generator.generate(design_data)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path, content in compose_files.items():
                zf.writestr(file_path, content)

            tokens_json = json.dumps(design_data.get("tokens", {}), indent=2, ensure_ascii=False)
            zf.writestr("design_tokens.json", tokens_json)

            readme = self._generate_readme(design_data)
            zf.writestr("README.md", readme)
            self._write_gradle_wrapper(zf)

        zip_bytes = zip_buffer.getvalue()
        zip_key = f"projects/{project_id}/handoff/{version_id}/project.zip"
        await self._s3.put_object(zip_key, zip_bytes, "application/zip")

        artifacts = [
            {"type": ArtifactType.COMPOSE_PROJECT, "key": zip_key, "size": len(zip_bytes)},
        ]

        logger.info("handoff_full_project", project_id=project_id, version_id=version_id)
        return {"project_id": project_id, "version_id": version_id, "artifacts": artifacts}

    async def _generate_design_tokens(
        self, project_id: str, version_id: str, design_data: dict[str, Any]
    ) -> dict[str, Any]:
        tokens = design_data.get("tokens", {})
        tokens_json = json.dumps(tokens, indent=2, ensure_ascii=False)
        key = f"projects/{project_id}/handoff/{version_id}/design_tokens.json"
        await self._s3.put_object(key, tokens_json.encode(), "application/json")

        artifacts = [{"type": ArtifactType.DESIGN_TOKENS, "key": key, "size": len(tokens_json.encode())}]
        logger.info("handoff_design_tokens", project_id=project_id, version_id=version_id)
        return {"project_id": project_id, "version_id": version_id, "artifacts": artifacts}

    async def _generate_figma_tokens(
        self, project_id: str, version_id: str, design_data: dict[str, Any]
    ) -> dict[str, Any]:
        tokens = design_data.get("tokens", {})
        figma_tokens = self._convert_to_figma_format(tokens)
        figma_json = json.dumps(figma_tokens, indent=2, ensure_ascii=False)
        key = f"projects/{project_id}/handoff/{version_id}/figma_tokens.json"
        await self._s3.put_object(key, figma_json.encode(), "application/json")

        artifacts = [{"type": ArtifactType.FIGMA_TOKENS, "key": key, "size": len(figma_json.encode())}]
        logger.info("handoff_figma_tokens", project_id=project_id, version_id=version_id)
        return {"project_id": project_id, "version_id": version_id, "artifacts": artifacts}

    async def _generate_compose_theme(
        self, project_id: str, version_id: str, design_data: dict[str, Any]
    ) -> dict[str, Any]:
        tokens = design_data.get("tokens", {})
        theme_files = self._theme_generator.generate(tokens)

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path, content in theme_files.items():
                zf.writestr(file_path, content)

        zip_bytes = zip_buffer.getvalue()
        key = f"projects/{project_id}/handoff/{version_id}/compose_theme.zip"
        await self._s3.put_object(key, zip_bytes, "application/zip")

        artifacts = [{"type": ArtifactType.COMPOSE_THEME, "key": key, "size": len(zip_bytes)}]
        logger.info("handoff_compose_theme", project_id=project_id, version_id=version_id)
        return {"project_id": project_id, "version_id": version_id, "artifacts": artifacts}

    async def _generate_design_spec(
        self, project_id: str, version_id: str, design_data: dict[str, Any]
    ) -> dict[str, Any]:
        spec_md = self._build_design_spec(design_data)
        key = f"projects/{project_id}/handoff/{version_id}/design_spec.md"
        await self._s3.put_object(key, spec_md.encode(), "text/markdown")

        artifacts = [{"type": ArtifactType.DESIGN_SPEC, "key": key, "size": len(spec_md.encode())}]
        logger.info("handoff_design_spec", project_id=project_id, version_id=version_id)
        return {"project_id": project_id, "version_id": version_id, "artifacts": artifacts}

    def _convert_to_figma_format(self, tokens: dict[str, Any]) -> dict[str, Any]:
        """Convert design tokens to Figma Token Studio format."""
        figma: dict[str, Any] = {"global": {}}
        colors = tokens.get("colors", {})
        for name, value in colors.items():
            figma["global"][name] = {"value": value, "type": "color"}

        typography = tokens.get("typography", {})
        for name, value in typography.items():
            figma["global"][f"fontSize.{name}"] = {"value": value.get("fontSize", "16"), "type": "fontSizes"}

        spacing = tokens.get("spacing", {})
        for name, value in spacing.items():
            figma["global"][f"spacing.{name}"] = {"value": value, "type": "spacing"}

        return figma

    def _build_design_spec(self, design_data: dict[str, Any]) -> str:
        """Build a developer-facing design specification document."""
        tokens = design_data.get("tokens", {})
        screens = design_data.get("screens", [])

        lines = ["# Design Specification", ""]

        lines.append("## Color Palette")
        lines.append("| Token | Value |")
        lines.append("|-------|-------|")
        for name, value in tokens.get("colors", {}).items():
            lines.append(f"| {name} | `{value}` |")
        lines.append("")

        lines.append("## Typography")
        lines.append("| Style | Size | Weight |")
        lines.append("|-------|------|--------|")
        for name, value in tokens.get("typography", {}).items():
            size = value.get("fontSize", "-")
            weight = value.get("fontWeight", "-")
            lines.append(f"| {name} | {size} | {weight} |")
        lines.append("")

        lines.append("## Spacing")
        lines.append("| Token | Value |")
        lines.append("|-------|-------|")
        for name, value in tokens.get("spacing", {}).items():
            lines.append(f"| {name} | `{value}` |")
        lines.append("")

        lines.append("## Screens")
        for screen in screens:
            lines.append(f"### {screen.get('name', 'Screen')}")
            lines.append(f"**Purpose:** {screen.get('purpose', '-')}")
            components = screen.get("components", [])
            if components:
                lines.append("")
                lines.append("**Component Tree:**")
                lines.append("```")
                self._render_tree(components, lines, depth=0)
                lines.append("```")
            lines.append("")

        return "\n".join(lines)

    def _render_tree(self, components: list[dict[str, Any]], lines: list[str], depth: int) -> None:
        for comp in components:
            indent = "  " * depth
            comp_type = comp.get("type", "?")
            props = comp.get("props", {})
            detail_parts = []
            for key in ("text", "title", "label", "headlineText", "placeholder", "icon", "navigate_to"):
                if props.get(key):
                    val = str(props[key])[:30]
                    detail_parts.append(f'{key}="{val}"')
            detail = f" ({', '.join(detail_parts)})" if detail_parts else ""
            lines.append(f"{indent}├─ {comp_type}{detail}")
            children = comp.get("children", [])
            if children and depth < 4:
                self._render_tree(children, lines, depth + 1)

    def start_generate_project(self, project_id: str, version_id: str | None, team_id: str = "") -> str:
        """Start async LLM-based project generation. Returns task_id for polling."""
        task = self._task_manager.create_task(project_id, "handoff")
        asyncio.create_task(self._run_llm_generate(task.task_id, project_id, version_id, team_id))
        return task.task_id

    async def _run_llm_generate(self, task_id: str, project_id: str, version_id: str | None, team_id: str = "") -> None:
        tm = self._task_manager
        tm.update_task(task_id, status=TaskStatus.RUNNING, progress=5, current_step="디자인 데이터 로드 중",
                       log_step="start", log_detail="핸드오프 코드 생성을 시작합니다")

        try:
            design_data, version_id = await self._resolve_design(project_id, version_id)
            screens = design_data.get("screens", [])
            total_screens = len(screens)

            tm.update_task(task_id, progress=10, current_step=f"LLM 코드 생성 시작 ({total_screens}개 화면)",
                           log_step="screens_found", log_detail=f"{total_screens}개 화면 발견")

            def progress_callback(screen_name: str, index: int, total: int, status: str) -> None:
                if status == "generating":
                    pct = 10 + int((index / total) * 70)
                    tm.update_task(task_id, progress=pct,
                                   current_step=f"화면 생성 중: {screen_name} ({index + 1}/{total})",
                                   log_step=f"screen_{index}", log_detail=f"{screen_name} 코드 생성 중")
                elif status == "done":
                    pct = 10 + int(((index + 1) / total) * 70)
                    tm.update_task(task_id, progress=pct,
                                   log_step=f"screen_{index}_done", log_detail=f"{screen_name} 완료")

            compose_files = await self._code_generator.generate_with_llm(design_data, progress_callback)

            tm.update_task(task_id, progress=85, current_step="ZIP 패키징 중",
                           log_step="packaging", log_detail="프로젝트를 ZIP으로 패키징합니다")

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path, content in compose_files.items():
                    zf.writestr(file_path, content)
                tokens_json = json.dumps(design_data.get("tokens", {}), indent=2, ensure_ascii=False)
                zf.writestr("design_tokens.json", tokens_json)
                readme = self._generate_readme(design_data)
                zf.writestr("README.md", readme)
                self._write_gradle_wrapper(zf)

            zip_bytes = zip_buffer.getvalue()
            zip_key = f"projects/{project_id}/handoff/{version_id}/project.zip"

            tm.update_task(task_id, progress=92, current_step="S3 업로드 중",
                           log_step="uploading", log_detail="S3에 프로젝트를 업로드합니다")

            await self._s3.put_object(zip_key, zip_bytes, "application/zip")

            artifacts = [
                {"type": ArtifactType.COMPOSE_PROJECT, "key": zip_key, "size": len(zip_bytes)},
            ]

            result = {"project_id": project_id, "version_id": version_id, "artifacts": artifacts}

            # Save handoff result metadata to S3 for persistence across page reloads
            handoff_meta_key = f"projects/{project_id}/handoff/latest.json"
            handoff_meta = json.dumps(result, ensure_ascii=False).encode()
            await self._s3.put_object(handoff_meta_key, handoff_meta, "application/json")

            # Update project stageStatus.handoff = completed
            if team_id:
                await self._update_stage_status(team_id, project_id, "handoff")

            tm.update_task(
                task_id, status=TaskStatus.COMPLETED, progress=100, current_step="완료",
                result=result, log_step="done",
                log_detail=f"LLM 코드 생성 완료 ({total_screens}개 화면, {len(compose_files)}개 파일)",
            )

            logger.info("handoff_llm_project_complete", project_id=project_id, version_id=version_id,
                        files=len(compose_files))

        except Exception as e:
            logger.error("handoff_llm_error", error=str(e), project_id=project_id)
            tm.update_task(task_id, status=TaskStatus.FAILED, current_step="오류 발생",
                           error=str(e), log_step="error", log_detail=str(e))

    async def get_llm_project_download_url(self, project_id: str) -> str:
        """Get download URL for the LLM-generated project ZIP."""
        meta_key = f"projects/{project_id}/handoff/latest.json"
        # A missing metadata object means "not generated yet" → 404. Any other S3
        # error (permissions, throttling, outage) is transient and must surface as
        # 503 rather than a misleading 404.
        head = await self._s3.head_object(meta_key)
        if not head:
            raise NotFoundException("LLM-generated project", project_id)
        try:
            meta_bytes = await self._s3.get_object(meta_key)
            meta = json.loads(meta_bytes)
            version_id = meta.get("version_id", "")
            artifacts = meta.get("artifacts", [])
            if artifacts:
                artifact_key = artifacts[0].get("key", "")
                if artifact_key:
                    return await self._s3.generate_presigned_download_url(artifact_key, "mdesigner-project.zip")
            # Fallback to version-based key
            zip_key = f"projects/{project_id}/handoff/{version_id}/project.zip"
            return await self._s3.generate_presigned_download_url(zip_key, "mdesigner-project.zip")
        except (json.JSONDecodeError, KeyError) as e:
            logger.error("handoff_metadata_corrupt", project_id=project_id, error=str(e))
            raise NotFoundException("LLM-generated project", project_id) from e

    async def get_download_url(self, project_id: str, version_id: str, artifact_key: str | None = None) -> str:
        if artifact_key:
            head = await self._s3.head_object(artifact_key)
            if not head:
                raise NotFoundException("Handoff artifact", artifact_key)
            filename = artifact_key.rsplit("/", 1)[-1]
            return await self._s3.generate_presigned_download_url(artifact_key, filename)

        # Fallback: try project.zip
        zip_key = f"projects/{project_id}/handoff/{version_id}/project.zip"
        head = await self._s3.head_object(zip_key)
        if not head:
            raise NotFoundException("Handoff artifact", version_id)
        return await self._s3.generate_presigned_download_url(zip_key, "mdesigner-project.zip")

    async def build_verify(self, project_id: str, version_id: str) -> dict[str, Any]:
        zip_key = f"projects/{project_id}/handoff/{version_id}/project.zip"
        head = await self._s3.head_object(zip_key)
        if not head:
            raise NotFoundException("Handoff artifact", version_id)

        errors = self._code_generator.verify_structure(project_id, version_id)

        if errors:
            return {"status": "failed", "message": "Build verification failed", "errors": errors}
        return {"status": "passed", "message": "Project structure is valid and ready for Android Studio"}

    def _write_gradle_wrapper(self, zf: zipfile.ZipFile) -> None:
        """Add the Gradle wrapper (jar + gradlew scripts) to the project ZIP.

        gradlew must carry the Unix executable bit (0755) so it runs after unzip;
        the ZIP stores it in the high 16 bits of external_attr.
        """
        for path, data, is_executable in self._code_generator.gradle_wrapper_files():
            info = zipfile.ZipInfo(path)
            info.compress_type = zipfile.ZIP_DEFLATED
            # rw-r--r-- normally, rwxr-xr-x for executables.
            mode = 0o755 if is_executable else 0o644
            info.external_attr = mode << 16
            zf.writestr(info, data)

    async def _update_stage_status(self, team_id: str, project_id: str, stage: str) -> None:
        now = datetime.now(UTC).isoformat()
        try:
            await self._db.update_item(
                table_name=PROJECTS_TABLE,
                key={"teamId": team_id, "sk": f"PROJECT#{project_id}"},
                update_expression="SET stageStatus.#stage = :status, updatedAt = :now",
                expression_values={":status": "completed", ":now": now},
                expression_names={"#stage": stage},
            )
        except Exception as e:
            logger.warning("handoff_stage_status_update_failed", error=str(e))

    def _generate_readme(self, design_data: dict[str, Any]) -> str:
        screens = design_data.get("screens", [])
        screen_list = "\n".join(f"- {s.get('name', 'Screen')}" for s in screens) if screens else "- Main Screen"

        return f"""# Mobile Designer - Generated Project

## Screens
{screen_list}

## Setup
1. Open in Android Studio (Arctic Fox or later)
2. Sync Gradle dependencies
3. Run on emulator or device (API 26+)

## Structure
- `app/src/main/java/` - Compose UI code
- `app/src/main/res/` - Resources
- `design_tokens.json` - Design token reference

## Generated by Mobile Designer
This project follows the configured mobile design guidelines.
"""
