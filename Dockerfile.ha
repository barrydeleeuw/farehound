FROM debian:bookworm-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pip jq && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir --break-system-packages .

COPY farehound/app-config.yaml ./config.yaml
COPY config/ ./config/

COPY farehound/rootfs /
