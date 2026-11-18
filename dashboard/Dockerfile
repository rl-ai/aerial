# Operator console + ops agent share this one image.
# Build context is the REPO ROOT (see docker-compose.yml: dockerfile: dashboard/Dockerfile).
FROM python:3.12-slim

ENV PYTHONPATH=/app:/app/dashboard

WORKDIR /app

# Whole repo (same layout as running from a clone, PYTHONPATH set to the
# repo root plus dashboard/ per GETTING-STARTED.md sections 5 and 6).
COPY . .

# Non-root runtime user. Pre-create the state dirs the deployment profile
# expects under ~/.aerial (parents are NOT created on demand by the code),
# so a named volume mounted there inherits the right ownership.
RUN useradd --create-home --uid 10001 aerial \
    && mkdir -p /home/aerial/.aerial/logs /home/aerial/.aerial/secrets /home/aerial/.aerial/data \
    && chown -R aerial:aerial /home/aerial/.aerial /app

USER aerial
