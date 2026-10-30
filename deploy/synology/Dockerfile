FROM python:3.12-slim-bookworm

WORKDIR /opt/blotter
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir .

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Compose chooses an unprivileged host UID/GID that owns the persistent data.
# The server binds loopback in the NAS host network namespace; TLS is supplied
# by an explicitly configured private reverse proxy, never by this image.
ENTRYPOINT ["blotter-serve"]
