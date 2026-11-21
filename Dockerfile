FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
# RepoHarbor and its runtime dependencies use no SQLite. dpkg refuses
# removal if a future system package requires it.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && dpkg --purge libsqlite3-0 \
    && rm -f /usr/local/lib/python3.12/lib-dynload/_sqlite3*.so \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 repoharbor && useradd -u 10001 -g 10001 -M repoharbor
WORKDIR /app
COPY pyproject.toml requirements-build.lock ./
COPY src ./src
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
    && pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-build.lock \
    && pip install --no-cache-dir --no-deps --no-build-isolation . \
    && chmod +x /usr/local/lib/python3.12/site-packages/repoharbor/askpass.py \
    && pip uninstall --yes pip setuptools \
    && rm -rf /usr/local/lib/python3.12/ensurepip
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["repoharbor"]
