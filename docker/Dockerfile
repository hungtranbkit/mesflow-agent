FROM python:3.13-alpine3.22

ARG AGENT_VERSION=2.24.37-docker-runtime
LABEL org.opencontainers.image.title="MESFlow Deploy Agent" \
      org.opencontainers.image.version="$AGENT_VERSION"

RUN apk add --no-cache \
      bash ca-certificates curl docker-cli docker-cli-compose docker-cli-buildx git openssh-client tzdata zip \
    && apk add --no-cache --virtual .build-deps \
      build-base linux-headers python3-dev \
    && addgroup -S mesflow-agent \
    && adduser -S -G mesflow-agent -h /app mesflow-agent

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt \
    && apk del .build-deps

COPY agent.py ota_control.py predictive.py release_gate.py deployment_platform.py VERSION.txt ./
COPY agent_backend ./agent_backend
COPY templates ./templates
COPY static ./static

RUN mkdir -p /data/config /data/data \
    && chown -R mesflow-agent:mesflow-agent /app /data

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    WORKSHOP_AGENT_HOME=/data \
    WORKSHOP_AGENT_HOST=0.0.0.0 \
    WORKSHOP_AGENT_PORT=8090 \
    WORKSHOP_AGENT_URL_PREFIX=/agent \
    MESFLOW_AGENT_RUNTIME=container

EXPOSE 8090
HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=4 \
  CMD wget -qO- http://127.0.0.1:8090/agent/live >/dev/null || exit 1

# Docker socket access often requires root or a host-matched docker group GID.
# Compose files default to root for portability; hardening guidance is in docs/DEPLOY_DOCKER.md.
USER root
CMD ["python", "agent.py"]
