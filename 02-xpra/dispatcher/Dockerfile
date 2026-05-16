FROM python:3.12-slim

# The dispatcher is a tiny Python service. It needs the docker CLI to spawn
# viewer containers (it talks to the host engine via the mounted
# /var/run/docker.sock).
#
# Debian 13 (trixie) ships only `docker-init` in its `docker.io` package and
# moves the CLI elsewhere, so instead of relying on apt we download the
# official static docker CLI binary, which is just one self-contained file.
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

WORKDIR /app
COPY dispatcher.py /app/dispatcher.py

EXPOSE 9081
ENV DISPATCHER_PORT=9081 \
    VIEWER_IMAGE=xpra-viewer:latest \
    XPRA_CONTAINER_PORT=14500

ENTRYPOINT ["python3", "/app/dispatcher.py"]
