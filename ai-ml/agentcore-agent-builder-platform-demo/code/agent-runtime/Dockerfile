# code/agent/Dockerfile
# Base image pulled from Amazon ECR Public (Docker Official mirror).
# Multi-arch manifest (amd64 + arm64/v8) — CodeBuild selects the host arch (arm64 for AgentCore Runtime).
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

EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "agent_runner.py"]
