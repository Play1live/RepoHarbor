FROM python:3.12.14-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 repoharbor && useradd -u 10001 -g 10001 -M repoharbor
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi \
    && pip install --no-cache-dir --no-build-isolation pip==26.2 setuptools==84.1.0 \
    && pip install --no-cache-dir --no-build-isolation . \
    && chmod +x /usr/local/lib/python3.12/site-packages/repoharbor/askpass.py
USER 10001:10001
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ENTRYPOINT ["repoharbor"]
