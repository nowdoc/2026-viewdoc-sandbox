# Tiny dispatcher: stdlib only, no pip wheels required.
FROM python:3.12-slim

WORKDIR /app
COPY dispatcher.py /app/dispatcher.py

EXPOSE 8081

CMD ["python", "-u", "/app/dispatcher.py"]
