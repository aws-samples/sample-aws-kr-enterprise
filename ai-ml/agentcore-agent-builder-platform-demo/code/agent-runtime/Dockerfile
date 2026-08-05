# code/agent/Dockerfile
# Base image pulled from Amazon ECR Public (Docker Official mirror).
# Multi-arch manifest (amd64 + arm64/v8) — CodeBuild selects the host arch (arm64 for AgentCore Runtime).
# For reproducible builds pin the base by digest (L11): resolve the current
# multi-arch manifest digest and append it, e.g.
#   FROM ...python:3.12-slim@sha256:<digest>
# obtained via `docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim`.
# The digest is intentionally left as a build/CI-injected value rather than a
# hardcoded (and quickly stale/incorrect) literal here.
FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_runner.py .
COPY config_loader.py .
COPY mcp_connector.py .
COPY side_channel.py .
COPY harness.py .
COPY observability_hook.py .
COPY span_filter.py .
COPY internal_tools/ ./internal_tools/

ENV AGENT_ID="" \
    DYNAMODB_TABLE="aiops-platform" \
    AWS_REGION="ap-northeast-2" \
    OTEL_PYTHON_DISABLED_INSTRUMENTATIONS="urllib3,requests,httpx"

# Run as a non-root user — any code/dep compromise then lacks in-container root (L11).
RUN groupadd --system --gid 1001 appgroup \
    && useradd --system --uid 1001 --gid appgroup appuser \
    && chown -R appuser:appgroup /app
USER appuser

EXPOSE 8080

# Container-level liveness probe hitting the app's own /ping endpoint (L11).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/ping', timeout=3).status==200 else 1)"]

CMD ["opentelemetry-instrument", "python", "agent_runner.py"]
