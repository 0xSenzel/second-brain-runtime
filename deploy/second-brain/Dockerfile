# syntax=docker/dockerfile:1.7
# Second-brain writer: the single process that writes the vault (schema 4).
# Installs a TAGGED runtime as a regular (non-editable) wheel.
#
# Build from GitHub (default):
#   docker build -t second-brain-runtime:4.0.0 deploy/second-brain
# Build from a local tag export (no network, no push needed):
#   git archive v4.0.0 --prefix=rt/ | tar -x -C "$TMP"        # creates $TMP/rt
#   docker build --build-context repo="$TMP/rt" -t second-brain-runtime:4.0.0 deploy/second-brain
# A --build-context named "repo" replaces the stage "repo" below.

ARG PYTHON_VERSION=3.13.3

FROM python:${PYTHON_VERSION}-slim-bookworm AS clone
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG RUNTIME_REPO=https://github.com/0xSenzel/second-brain-runtime.git
ARG RUNTIME_REF=v4.0.1
RUN git clone --depth 1 --branch "${RUNTIME_REF}" "${RUNTIME_REPO}" /src

FROM scratch AS repo
COPY --from=clone /src /

FROM python:${PYTHON_VERSION}-slim-bookworm AS build
RUN pip install --no-cache-dir build
COPY --from=repo / /src
RUN python -m build --wheel --outdir /wheels /src

FROM python:${PYTHON_VERSION}-slim-bookworm
ARG RUNTIME_REF=v4.0.1
LABEL org.opencontainers.image.title="second-brain-runtime" \
      org.opencontainers.image.version="${RUNTIME_REF}"

# git: the runtime journals every vault write as a git commit.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/brain --shell /usr/sbin/nologin brain

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

ENV HOME=/home/brain \
    PYTHONUNBUFFERED=1 \
    SECOND_BRAIN_VAULT=/vault \
    OLLAMA_URL=http://ollama:11434 \
    SECOND_BRAIN_EMBED_MODEL=bge-m3

USER brain
WORKDIR /home/brain
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8765), 3).close()"]

ENTRYPOINT ["second-brain-mcp", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8765"]
