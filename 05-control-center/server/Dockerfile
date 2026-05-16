FROM python:3.12-slim

# The control-center server is a tiny stdlib HTTP server that needs:
#  - the docker CLI (to `docker exec` into spawned kasm containers and
#    rewrite /tmp/kasm_query.{json,env}).
# Same static-CLI download pattern as the 04 dispatcher's Dockerfile.
ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl openssl \
    && rm -rf /var/lib/apt/lists/* \
    && arch="$(uname -m)" \
    && case "$arch" in \
         x86_64)  dockerarch=x86_64 ;; \
         aarch64) dockerarch=aarch64 ;; \
         *) echo "unsupported arch $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${dockerarch}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /usr/local/bin --strip-components=1 docker/docker \
    && docker --version

WORKDIR /app
COPY server.py /app/server.py
COPY static    /app/static

EXPOSE 5081
ENV CONTROL_CENTER_PORT=5081 \
    KASM_DISPATCHER_HOST=kasm-dispatcher \
    KASM_DISPATCHER_PORT=6081 \
    STATIC_DIR=/app/static

ENTRYPOINT ["python3", "/app/server.py"]
