FROM python:3.13-slim-bookworm 

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --no-create-home --shell /usr/sbin/nologin gateway \
    && mkdir /data \
    && chown gateway:gateway /data \
    && chmod 700 /data

COPY gateway/ ./gateway/

USER 10001:10001
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import json, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2); assert r.status == 200 and json.load(r)['status'] == 'healthy'"]

CMD ["python", "-m", "gateway.server", "--host", "0.0.0.0", "--port", "8000", "--usage-db", "/data/usage.sqlite3"]
