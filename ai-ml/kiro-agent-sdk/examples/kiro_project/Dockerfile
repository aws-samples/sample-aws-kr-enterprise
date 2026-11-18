# AgentCore image for the example project. Build the SDK base image first, from the SDK repository root:
#   docker buildx build --platform linux/arm64 --load -f Dockerfile.agentcore -t kiro-agent-sdk:agentcore .
# Then build this image from examples/kiro_project:
#   docker buildx build --platform linux/arm64 --load -t release-writer:latest .
FROM kiro-agent-sdk:agentcore

WORKDIR /opt/app
COPY project ./project
COPY agentcore_app.py ./
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "agentcore_app:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
