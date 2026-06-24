import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.common.config import get_settings
from src.common.dependencies import get_db, get_s3
from src.common.exceptions import AppException, app_exception_handler
from src.common.middleware import RequestIdMiddleware, SecurityHeadersMiddleware, StructuredLoggingMiddleware
from src.common.secrets import resolve_jwt_secret
from src.prompts.loader import PromptLoader, set_prompt_loader

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    logger.info("application_startup", environment=settings.environment)

    # Resolve JWT secret (from env or Secrets Manager)
    await resolve_jwt_secret(settings)

    # Initialize PromptLoader (reads from DB on each call, no caching)
    prompt_loader = PromptLoader(get_db(), get_s3())
    set_prompt_loader(prompt_loader)

    # Wire the AI task store to DynamoDB so progress polling survives
    # load-balancing across API instances and restarts.
    from src.ai.task_manager import task_manager

    task_manager.set_db(get_db())

    yield

    logger.info("application_shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    root_path = os.environ.get("MDESIGNER_ROOT_PATH", "")

    is_development = settings.environment == "development"

    app = FastAPI(
        title="Mobile Designer API",
        version="0.1.0",
        root_path=root_path,
        docs_url="/docs" if is_development else None,
        redoc_url="/redoc" if is_development else None,
        lifespan=lifespan,
    )

    # A wildcard origin cannot be combined with credentialed requests (the browser
    # rejects it and Starlette disables credentials). Only enable credentials when
    # an explicit origin allowlist is configured.
    allow_wildcard = "*" in settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=not allow_wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(SecurityHeadersMiddleware, production=settings.environment == "production")
    app.add_middleware(StructuredLoggingMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # Starlette types the handler against the base Exception; our handler narrows to
    # AppException, which is a known FastAPI typing quirk.
    app.add_exception_handler(AppException, app_exception_handler)  # type: ignore[arg-type]


    from src.admin.router import router as admin_router
    from src.ai.router import router as ai_router
    from src.auth.router import router as auth_router
    from src.collaboration.router import router as collaboration_router
    from src.files.router import router as files_router
    from src.handoff.router import router as handoff_router
    from src.projects.router import router as projects_router

    api_prefix = "/api" if settings.environment == "production" else ""

    app.include_router(auth_router, prefix=f"{api_prefix}/auth", tags=["Auth"])
    app.include_router(admin_router, prefix=f"{api_prefix}/admin", tags=["Admin"])
    app.include_router(projects_router, prefix=f"{api_prefix}/projects", tags=["Projects"])
    app.include_router(files_router, prefix=f"{api_prefix}/files", tags=["Files"])
    app.include_router(ai_router, prefix=f"{api_prefix}/ai", tags=["AI"])
    app.include_router(handoff_router, prefix=f"{api_prefix}/handoff", tags=["Handoff"])
    app.include_router(collaboration_router, prefix=f"{api_prefix}/collaboration", tags=["Collaboration"])

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {"status": "healthy"}

    @app.get(f"{api_prefix}/health")
    async def api_health_check() -> dict[str, str]:
        return {"status": "healthy"}

    return app


app = create_app()
