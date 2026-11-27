# The E2B template every trial-build sandbox starts from (docs/TRIAL_BUILDER_SETUP.md, ADR 0027).
# Test sandboxes have no network, so the template's test tools are installed here, pinned.
# Build it once with E2B's CLI (see the setup doc); its name goes in TRIAL_E2B_TEMPLATE.
FROM python:3.12.16-slim
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pytest==8.4.2
RUN useradd -m -s /bin/bash user || true
USER user
WORKDIR /home/user
