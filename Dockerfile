# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337
FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY deploy/requirements.runtime.lock deploy/requirements.build.lock ./
RUN python -m pip install --no-cache-dir -r requirements.build.lock
COPY pyproject.toml README.md ./
COPY radar ./radar
COPY web ./web
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.runtime.lock \
    && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels .

FROM ${PYTHON_IMAGE} AS app
ENV PATH=/opt/radar/.venv/bin:$PATH \
    RADAR_HOME=/workspace TZ=Asia/Seoul RADAR_CONTAINER=1 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RADAR_BIN=/opt/radar/.venv/bin/radar
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 radar && useradd --uid 1000 --gid radar --create-home radar \
    && mkdir -p /workspace /opt/radar/bin \
    && chown radar:radar /workspace \
    && python -m venv /opt/radar/.venv
COPY --from=build /wheels /wheels
COPY deploy/requirements.runtime.lock /opt/radar/requirements.runtime.lock
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r /opt/radar/requirements.runtime.lock \
    && pip install --no-cache-dir --no-index --find-links=/wheels martech-radar \
    && rm -rf /wheels
COPY deploy/entrypoint.py deploy/healthcheck.py deploy/smoke.py deploy/check_dependencies.py deploy/worker_healthcheck.py deploy/maintenance.py deploy/hosting_preflight.py deploy/init_workspace.py /opt/radar/
COPY bin/weekly-scan /opt/radar/bin/weekly-scan
RUN chmod 755 /opt/radar/bin/weekly-scan
WORKDIR /workspace
USER radar
ENTRYPOINT ["python", "/opt/radar/entrypoint.py"]

FROM app AS web
EXPOSE 8848
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["python", "/opt/radar/healthcheck.py"]
CMD ["radar", "serve", "--host", "0.0.0.0", "--port", "8848"]

FROM app AS collector
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    RADAR_REQUIRE_SANDBOX=1
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright && rm -rf /var/lib/apt/lists/*
USER radar
CMD ["radar", "worker", "--watch", "--name", "container-staging"]
