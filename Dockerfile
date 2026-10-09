FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LUMEN_HOST=0.0.0.0 \
    LUMEN_PORT=5024 \
    LUMEN_DATA=/data

WORKDIR /app
COPY pyproject.toml README.md LICENSE MANIFEST.in ./
COPY lumenfeed ./lumenfeed
RUN pip install . && rm -rf /app

# Run as an unprivileged user; the database lives in the /data volume.
RUN useradd --create-home --uid 1000 lumenfeed && mkdir /data && chown lumenfeed /data
USER lumenfeed
WORKDIR /home/lumenfeed
VOLUME /data

EXPOSE 5024
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5024/api/saved/count', timeout=4)"

CMD ["lumenfeed"]
