FROM python:3.11-slim AS builder

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir --prefix=/install .

FROM python:3.11-slim

WORKDIR /app

COPY --from=builder /install /usr/local

COPY src/ ./src/
COPY config.yaml .

VOLUME /data

COPY ha-addon/run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT ["/run.sh"]
