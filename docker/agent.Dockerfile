FROM python:3.14-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV HOME=/home/agent
ENV ANTHROPIC_BASE_URL=http://host.docker.internal:11434
ENV ANTHROPIC_AUTH_TOKEN=ollama
ENV ANTHROPIC_API_KEY=
ENV ANTHROPIC_MODEL=deepseek-v4.1-flash:cloud

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code \
    && pip install --no-cache-dir uv

RUN useradd -m -u 1000 agent \
    && mkdir -p /work /out \
    && chown -R agent:agent /work /out

USER agent
WORKDIR /work

CMD ["sleep", "infinity"]
