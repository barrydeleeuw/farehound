FROM python:3.11-slim AS builder

WORKDIR /app

COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /install /usr/local

COPY src/ ./src/
COPY app-config.yaml ./config.yaml

VOLUME /data

COPY run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/run.sh"]
