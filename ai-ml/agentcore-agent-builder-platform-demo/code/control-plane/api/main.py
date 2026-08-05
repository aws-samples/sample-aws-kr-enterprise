"""Platform API — FastAPI app. Spec Section 3.2."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager

import boto3
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers import agents, auth, builder, chat, events, gateways, health, obs, sessions
from services.agentcore_client import AgentCoreClient
from services.auth_middleware import CognitoAuthMiddleware
from services.builder_service import BuilderService
from services.dynamodb_service import DynamoDBService
from services.harness import Tier1Harness
from services.healthcheck import start_reconciler
from services.otel_middleware import OtelMiddleware, setup_tracing

logging.basicConfig(level=logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-2")
TABLE_NAME = os.environ.get("DYNAMODB_TABLE")

# Comma-separated exact origins allowed to make credentialed cross-origin
# requests (e.g. "https://aiops-v2.example.com"). When unset we fall back to a
# wildcard WITHOUT credentials, which is the only safe way to use "*" (Starlette
# refuses to reflect an arbitrary origin with Allow-Credentials in that mode).
_allowed_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
ALLOWED_ORIGINS = (
    [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
    if _allowed_origins_env
    else ["*"]
)
ALLOW_CREDENTIALS = ALLOWED_ORIGINS != ["*"]

# Install an OTEL SDK TracerProvider so OtelMiddleware spans are actually
# recorded/exported (opentelemetry-api alone provides only a no-op provider).
setup_tracing("platform-api")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)

db_service = DynamoDBService(table)
agentcore_client = AgentCoreClient()
builder_service = BuilderService(db_service)
harness_service = Tier1Harness(db_service)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(start_reconciler(agentcore_client, db_service))
    yield
    task.cancel()


app = FastAPI(
    title="AIOps Multi Agent Platform API", version="2.0.0", lifespan=lifespan
)

app.state.db_service = db_service
app.state.agentcore_client = agentcore_client
app.state.builder_service = builder_service
app.state.harness = harness_service

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(CognitoAuthMiddleware)
app.add_middleware(OtelMiddleware, service_name="platform-api")

app.include_router(auth.router)
app.include_router(health.router)
app.include_router(agents.router)
app.include_router(builder.router)
app.include_router(chat.router)
app.include_router(events.router)
app.include_router(gateways.router)
app.include_router(obs.router)
app.include_router(sessions.router)
