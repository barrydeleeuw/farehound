ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app

# Install Python and jq
RUN apk add --no-cache python3 py3-pip jq

COPY pyproject.toml .
COPY src/ ./src/

RUN pip install --no-cache-dir --break-system-packages .

COPY app-config.yaml ./config.yaml

# s6-overlay v3 service: place run script in the right location
RUN mkdir -p /etc/s6-overlay/s6-rc.d/farehound /etc/s6-overlay/s6-rc.d/user/contents.d
COPY run.sh /etc/s6-overlay/s6-rc.d/farehound/run
RUN chmod +x /etc/s6-overlay/s6-rc.d/farehound/run && \
    echo "longrun" > /etc/s6-overlay/s6-rc.d/farehound/type && \
    touch /etc/s6-overlay/s6-rc.d/user/contents.d/farehound
