ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install Python and jq (sqlite3 is built into Python — no compilation needed)
RUN apk add --no-cache python3 py3-pip jq bash

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir --break-system-packages .

COPY app-config.yaml ./config.yaml
COPY run.sh /run.sh
RUN chmod +x /run.sh

CMD ["/run.sh"]
