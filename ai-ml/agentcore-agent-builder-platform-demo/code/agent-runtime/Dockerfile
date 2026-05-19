# code/agent/Dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_runner.py .
COPY config_loader.py .
COPY mcp_connector.py .
COPY side_channel.py .
COPY harness.py .
COPY observability_hook.py .
COPY internal_tools/ ./internal_tools/

ENV AGENT_ID="" \
    DYNAMODB_TABLE="aiops-platform" \
    AWS_REGION="ap-northeast-2"

EXPOSE 8080
CMD ["opentelemetry-instrument", "python", "agent_runner.py"]
