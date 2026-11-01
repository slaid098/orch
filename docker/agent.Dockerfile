FROM python:3.14-slim

ARG DEBIAN_MIRROR=mirror.yandex.ru
ARG CLAUDE_VERSION=2.1.316

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/home/agent
ENV ANTHROPIC_BASE_URL=http://host.docker.internal:11434
ENV ANTHROPIC_AUTH_TOKEN=ollama
ENV ANTHROPIC_API_KEY=
ENV ANTHROPIC_MODEL=deepseek-v4.1-flash:cloud

RUN sed -i "s|deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gzip \
        tar \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-${CLAUDE_VERSION}.tgz" -o /tmp/claude.tgz \
    && mkdir -p /opt/claude \
    && tar -xzf /tmp/claude.tgz -C /opt/claude --strip-components=1 \
    && BIN="$(find /opt/claude -type f -size +10M | head -n1)" \
    && test -n "$BIN" \
    && chmod +x "$BIN" \
    && ln -sf "$BIN" /usr/local/bin/claude \
    && rm -f /tmp/claude.tgz

RUN pip install --no-cache-dir uv

RUN useradd -m -u 1000 agent \
    && mkdir -p /work /out /home/agent \
    && chown -R agent:agent /home/agent /work /out

USER agent
WORKDIR /work

CMD ["sleep", "infinity"]
