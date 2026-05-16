FROM python:3.12-alpine

RUN apk add --no-cache docker-cli openssl

WORKDIR /app
COPY server.py /app/server.py
COPY static    /app/static
ENV STATIC_DIR=/app/static

EXPOSE 5087
CMD ["python3", "-u", "/app/server.py"]
