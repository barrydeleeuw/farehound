ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install Python and jq
RUN apk add --no-cache python3 py3-pip jq

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir --break-system-packages .

COPY app-config.yaml ./config.yaml

# s6-overlay: copy rootfs overlay (services.d structure)
COPY rootfs /
