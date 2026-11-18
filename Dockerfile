# Fresh wheel build, then a clean runtime install. No source tree shadows the wheel.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS builder
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml constraints-docker.txt LICENSE COPYING.GPLv3 NOTICE ./
COPY avionmesh_pi ./avionmesh_pi
RUN pip wheel --no-cache-dir --wheel-dir /wheels --constraint constraints-docker.txt .

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-index --no-cache-dir --find-links /wheels avionmesh-pi && rm -rf /wheels
COPY LICENSE COPYING.GPLv3 NOTICE ./
COPY fixtures ./fixtures
COPY tests ./tests
RUN mkdir /data && chown 65532:65532 /data && chmod 0700 /data
# Optional dedicated-host Bluetooth service; never the default build target.
FROM runtime-base AS bluetooth
RUN apt-get update && apt-get install -y --no-install-recommends bluez dbus \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 halo \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin halo \
    && mkdir -p /run/halo-bluetooth /var/lib/bluetooth \
    && chmod 0700 /var/lib/bluetooth \
    && rm -f /etc/machine-id /var/lib/dbus/machine-id \
    && ln -s /run/halo-bluetooth/machine-id /etc/machine-id
COPY docker/bluetooth/ /etc/halo-bluetooth/
ENTRYPOINT ["python", "-m", "avionmesh_pi.bluetooth_stack"]
CMD ["run", "--allow-adapter-control"]

# Keep plain docker build/run as the unprivileged offline-replay image.
FROM runtime-base AS runtime
USER 65532:65532
ENTRYPOINT ["python", "-m", "avionmesh_pi"]
CMD ["replay", "fixtures/session.jsonl"]
