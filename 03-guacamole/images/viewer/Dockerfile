FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc \
        xpdf feh vlc \
        ca-certificates curl \
        fonts-dejavu-core \
        dbus-x11 \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DISPLAY=:0
EXPOSE 5900

ENTRYPOINT ["/entrypoint.sh"]
