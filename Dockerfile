FROM docker.m.daocloud.io/library/python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BTF_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r /app/requirements.txt

COPY app /app/app

# Create a fixed-uid unprivileged user and the writable runtime data directory.
# A fixed uid (10001) keeps host bind-mount ownership predictable; the host
# data directory must be owned by this uid, e.g.:
#   chown -R 10001:10001 /opt/btf-device-gateway/data
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R 10001:10001 /data

USER 10001:10001

EXPOSE 8080

# Liveness probe against the application's own /health endpoint. Uses the
# stdlib so no extra packages (curl) are needed in the slim base image.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
