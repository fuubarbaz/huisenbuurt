# Single-stage image built with uv.
#
# The two-step dependency install is deliberate: dependencies change rarely and
# application code changes constantly, so installing them from the lockfile
# BEFORE copying the source keeps the expensive layer cached across deploys.
#
# Note what stays in the image and what does not. `data/schools.db` and
# `data/renovation_costs.json` are read from a path relative to the working
# directory, so they must ship here at /app/data. The writable SQLite database
# does NOT: it lives on a mounted volume at /data, because a volume mounted
# over /app/data would hide the school index and the education layer would go
# permanently MISSING with no error that says why.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Bytecode compilation trades a slower build for a faster cold start, which is
# the right way round for a machine that scales to zero. copy link-mode avoids
# hardlink warnings when the cache and the target are on different filesystems.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Where the writable database lives. Four slashes after the scheme is an
# absolute path; three would make it relative and put it back inside the image.
ENV WOONAGENT_DATABASE_URL="sqlite+aiosqlite:////data/woonagent.db"

EXPOSE 8000

# One worker on purpose. The rate limiter and the robots cache are per-process
# in-memory structures, so a second worker would silently double the request
# rate every upstream sees and halve the cache hit rate.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
