ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install Python and jq on Debian base
RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv jq && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ ./src/

# Install dependencies (duckdb has pre-built aarch64 wheels for Debian/glibc)
RUN pip install --no-cache-dir --break-system-packages .

COPY app-config.yaml ./config.yaml
COPY run.sh /run.sh
RUN chmod +x /run.sh

ENTRYPOINT []
CMD ["/run.sh"]
