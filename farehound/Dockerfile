ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install build dependencies for compiled packages
RUN apk add --no-cache gcc musl-dev python3-dev

COPY pyproject.toml .
COPY src/ ./src/
RUN pip install --no-cache-dir .

COPY app-config.yaml ./config.yaml
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
