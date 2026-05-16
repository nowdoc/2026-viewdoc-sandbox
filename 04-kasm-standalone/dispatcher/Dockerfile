FROM python:3.12-slim

# The dispatcher is a tiny stdlib-ish Python service. It needs:
#  - PyYAML  (read mapping.yaml)
#  - the docker CLI binary (to `docker run` viewer containers via the
#    mounted /var/run/docker.sock).
#
# Debian 13 (trixie) ships only docker-init in its docker.io package and
# moves the CLI elsewhere, so we download the official static docker CLI
# binary directly — one self-contained file, no apt extras. Mirrors the
# pattern used by the xpra prototype.
ARG DOCKER_CLI_VERSION=27.3.1
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
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

RUN pip install --no-cache-dir "PyYAML==6.0.2"

WORKDIR /app
COPY dispatcher.py /app/dispatcher.py
COPY mapping.yaml  /app/mapping.yaml

EXPOSE 6081
ENV DISPATCHER_PORT=6081 \
    KASM_CONTAINER_PORT=6901 \
    VIEWER_PORT_RANGE_START=6082 \
    VIEWER_PORT_RANGE_END=6099 \
    KASM_SVC_HTTPS_VALUE=disabled \
    MAPPING_PATH=/app/mapping.yaml

ENTRYPOINT ["python3", "/app/dispatcher.py"]
