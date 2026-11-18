# syntax=docker/dockerfile:1
# Build context: a directory holding app/ (this folder) and kiro-agent-sdk/ (the SDK source).
FROM python:3.12-slim-bookworm

ARG TARGETARCH=arm64
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KIRO_RUNTIME_CACHE=/opt/kiro-prewarm

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Kiro CLI 2.35.1, fixed SHA-256. ARM uses the musl build because the GNU ARM build needs a newer glibc.
RUN case "$TARGETARCH" in \
      amd64) archive=kirocli-x86_64-linux.zip; \
             digest=9896a0849ea6abe259f7b3e742d6637dd413bbd8d6fa0afdf241c6c5614d4366 ;; \
      arm64) archive=kirocli-aarch64-linux-musl.zip; \
             digest=59758f69080a341184c520aa34d2245c24ae4d3859f231f591f477709420851f ;; \
      *) echo "Unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --silent --show-error \
       "https://prod.download.cli.kiro.dev/stable/2.35.1/$archive" -o /tmp/kiro.zip \
    && echo "$digest  /tmp/kiro.zip" | sha256sum -c - \
    && unzip -q /tmp/kiro.zip 'kirocli/bin/kiro-cli' 'kirocli/bin/kiro-cli-chat' 'kirocli/bin/kiro-cli-term' -d /tmp \
    && install -m 755 /tmp/kirocli/bin/kiro-cli /tmp/kirocli/bin/kiro-cli-chat /tmp/kirocli/bin/kiro-cli-term /usr/local/bin/ \
    && rm -rf /tmp/kiro.zip /tmp/kirocli \
    && kiro-cli --version | grep -Fx 'kiro-cli 2.35.1'

COPY kiro-agent-sdk /opt/kiro-agent-sdk
RUN python -m pip install --no-cache-dir '/opt/kiro-agent-sdk[agentcore]' \
    && HOME=/root python /opt/kiro-agent-sdk/scripts/prewarm_kiro.py /opt/kiro-prewarm \
    && chmod -R a+rX /opt/kiro-prewarm \
    && rm -rf /opt/kiro-agent-sdk

# The agent app, its Kiro workspace (.kiro agents, steering, skills) and the incident-tools MCP servers
COPY app /opt/app
RUN python -m pip install --no-cache-dir /opt/app && rm -rf /opt/app/build /opt/app/*.egg-info \
    && groupadd --gid 10001 agent && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agent \
    && rm -rf /tmp/* /root/.cache
USER 10001:10001
# Kiro home, workspace copy and synced knowledge live under /tmp, which is writable in the AgentCore microVM.
ENV INCIDENT_STATE_DIR=/tmp/incident \
    HOME=/tmp/incident/home
WORKDIR /opt/app
EXPOSE 8080
CMD ["python", "main.py"]
