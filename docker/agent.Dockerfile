FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

ARG DEBIAN_MIRROR=deb.debian.org
ARG PG_VERSION=17
ARG CLAUDE_VERSION=2.1.316
ARG CODEGRAPH_VERSION=1.6.2
ARG PLAYWRIGHT_MCP_VERSION=0.0.84
ARG PLAYWRIGHT_VERSION=1.65.0-alpha-2026-10-30

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/home/agent
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV CODEGRAPH_TELEMETRY=0
ENV ANTHROPIC_BASE_URL=http://host.docker.internal:11434
ENV ANTHROPIC_AUTH_TOKEN=ollama
ENV ANTHROPIC_API_KEY=
ENV ANTHROPIC_MODEL=deepseek-v4.1-flash:cloud
ENV CLAUDE_CODE_MAX_CONTEXT_TOKENS=200000
ENV CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules

RUN printf '%s\n' \
        'Acquire::Retries "20";' \
        'Acquire::ForceIPv4 "true";' \
        'Acquire::http::Timeout "30";' \
        'Acquire::https::Timeout "30";' \
        > /etc/apt/apt.conf.d/99orch \
    && sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        ffmpeg \
        git \
        gzip \
        jq \
        less \
        libatomic1 \
        procps \
        ripgrep \
        tar \
        unzip \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && node --version \
    && npm --version

RUN curl -fsSL "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-${CLAUDE_VERSION}.tgz" -o /tmp/claude.tgz \
    && mkdir -p /opt/claude \
    && tar -xzf /tmp/claude.tgz -C /opt/claude --strip-components=1 \
    && BIN="$(find /opt/claude -type f -size +10M | head -n1)" \
    && test -n "$BIN" \
    && chmod +x "$BIN" \
    && ln -sf "$BIN" /usr/local/bin/claude \
    && rm -f /tmp/claude.tgz

RUN npm install -g \
        "@colbymchenry/codegraph@${CODEGRAPH_VERSION}" \
        "@playwright/mcp@${PLAYWRIGHT_MCP_VERSION}" \
        "playwright@${PLAYWRIGHT_VERSION}" \
    && npm cache clean --force \
    && codegraph --version \
    && playwright-mcp --version

# Optional host override for mirrors that throttle the large Chromium package.
ARG CHROMIUM_MIRROR=
RUN if [ -n "$CHROMIUM_MIRROR" ]; then \
        sed -i -E "s|URIs: https?://[^/]+|URIs: http://${CHROMIUM_MIRROR}|" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        chromium \
        fonts-liberation \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv playwright==1.64.1 \
    && uv --version

# Postgres 16 matches the CI service container and the production database.
RUN install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && printf '%s\n' \
        'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main' \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-${PG_VERSION} \
        postgresql-client-${PG_VERSION} \
    && rm -rf /var/lib/apt/lists/*

COPY docker/orch-postgres.sh /usr/local/bin/orch-postgres
COPY docker/review-gateway.py /usr/local/bin/orch-review-gateway
RUN chmod +x /usr/local/bin/orch-postgres /usr/local/bin/orch-review-gateway

RUN useradd -m -u 1000 agent \
    && mkdir -p /work /out /home/agent \
    && chown -R agent:agent /home/agent /work /out

RUN mkdir -p /home/agent/.claude \
    && printf '%s\n' '{"autoCompactEnabled":true,"autoCompactWindow":200000,"modelSettings":{"deepseek-v4.1-flash:cloud":{"autoCompactWindow":200000}},"env":{"CLAUDE_CODE_AUTO_COMPACT_WINDOW":"200000","CLAUDE_CODE_MAX_CONTEXT_TOKENS":"200000"}}' \
        > /home/agent/.claude/settings.json \
    && chown -R agent:agent /home/agent/.claude

USER agent
WORKDIR /work

# Keep the image able to run every orchestrated project on its own Python:
# 3.12 is the base, 3.13/3.14 are managed by uv next to the agent home.
ENV UV_PYTHON_DOWNLOADS=automatic
RUN uv python install 3.13 3.14

CMD ["sleep", "infinity"]
