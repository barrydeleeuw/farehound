ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install Python on Debian base
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ ./src/

# Install dependencies (duckdb has pre-built aarch64 wheels for Debian/glibc)
RUN pip install --no-cache-dir --break-system-packages .

COPY app-config.yaml ./config.yaml

# s6-overlay service directory — this is how HA add-ons run long-lived services
COPY run.sh /etc/services.d/farehound/run
RUN chmod +x /etc/services.d/farehound/run
