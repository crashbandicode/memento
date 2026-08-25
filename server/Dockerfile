FROM python:3.12-slim AS base

WORKDIR /app

# Backup uses pure SQL COPY TO STDOUT (no pg_dump binary needed).
# Schema rebuild via _run_migrations on fresh deploy; backup only carries
# data. Avoids the bookworm libldap / pgdg client version mess.

# Optional pip mirror — set PIP_INDEX_URL via docker-compose build args
# when on flaky / slow networks (e.g. .env with
# PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple in China).
# Empty default keeps the standard pypi.org behavior for everyone else.
ARG PIP_INDEX_URL=""
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
ENV PIP_DEFAULT_TIMEOUT=300

# Install server
COPY server/pyproject.toml .
COPY server/server/ server/
# pyproject force-includes the canonical embedding runtime into the installed
# wheel so `python -m server.services.embedding_server` also works outside a
# monorepo checkout.
COPY embedding/embedding_server.py /embedding/embedding_server.py
RUN pip install --no-cache-dir .

# Install MCP server package (for remote /mcp endpoint)
COPY mcp_server/ /tmp/mcp_server/
RUN pip install --no-cache-dir /tmp/mcp_server && rm -rf /tmp/mcp_server

# py-spy for on-demand, no-redeploy profiling of a live process (the API or a
# Celery ingest worker): `docker exec <c> py-spy dump --pid 1` for a stack
# snapshot, or `py-spy record -o /tmp/prof.svg --pid 1 --duration 20` for a
# flame graph. Attaching needs SYS_PTRACE (add `cap_add: [SYS_PTRACE]` to the
# service, or run the exec as root). See docs/PROFILING.md.
RUN pip install --no-cache-dir py-spy

# Bootstrap installer assets served at /install.sh, /install.ps1, /install
COPY deploy/bootstrap/ /app/bootstrap/

# Note: BGE-M3 embedding model runs on host machine (not in container)
# Start with: python -m server.services.embedding_server --port 8002

EXPOSE 8000

CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8000"]

# Reproducible test target for hosts where the application runs entirely in
# Docker and no compatible Python is installed. The production/runtime target
# below remains free of pytest and test sources.
FROM base AS test
COPY server/tests/ tests/
COPY deploy/k8s/ deploy/k8s/
RUN pip install --no-cache-dir ".[dev]"
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q"]

FROM base AS runtime
