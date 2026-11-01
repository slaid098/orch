FROM python:3.14-slim

ARG DEBIAN_MIRROR=mirror.yandex.ru

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
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code \
    && pip install --no-cache-dir uv

RUN useradd -m -u 1000 agent \
    && mkdir -p /work /out \
    && chown -R agent:agent /work /out

USER agent
WORKDIR /work

CMD ["sleep", "infinity"]
