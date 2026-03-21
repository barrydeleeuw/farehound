ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install Python and build tools needed for duckdb compilation on Alpine/aarch64
RUN apk add --no-cache \
    python3 python3-dev py3-pip \
    gcc g++ cmake make musl-dev \
    jq bash

COPY pyproject.toml .
COPY src/ ./src/

# Install Python dependencies (duckdb compiles from source ~20min on Pi)
RUN pip install --no-cache-dir --break-system-packages .

# Clean up build tools to reduce image size
RUN apk del gcc g++ cmake make musl-dev python3-dev

COPY app-config.yaml ./config.yaml
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
