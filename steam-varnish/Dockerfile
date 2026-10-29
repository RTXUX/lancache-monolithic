# syntax=docker/dockerfile:1
ARG DEBIAN_IMAGE=debian:trixie-slim
FROM ${DEBIAN_IMAGE} AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl python3 \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.py /usr/local/bin/lancache-entrypoint

FROM base AS nginx
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx libnginx-mod-http-lua lua-socket \
    && rm -rf /var/lib/apt/lists/*
COPY nginx/ /etc/lancache/nginx/
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s \
    CMD curl --fail --silent http://127.0.0.1:8080/_health || exit 1
ENTRYPOINT ["python3", "/usr/local/bin/lancache-entrypoint", "nginx"]

FROM base AS varnish
RUN apt-get update \
    && apt-get install -y --no-install-recommends varnish \
    && rm -rf /var/lib/apt/lists/*
COPY varnish/default.vcl /etc/lancache/default.vcl
EXPOSE 80
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
    CMD curl --fail --silent http://127.0.0.1/_health || exit 1
ENTRYPOINT ["python3", "/usr/local/bin/lancache-entrypoint", "varnish"]

# Disposable certificates and origins for the integration test only.
FROM base AS test-certs
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /certs \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
       -subj /CN=origin2 -addext subjectAltName=DNS:origin2 \
       -keyout /certs/origin.key -out /certs/origin.crt

FROM nginx AS nginx-test
COPY --from=test-certs /certs/origin.crt /usr/local/share/ca-certificates/test-origin.crt
RUN update-ca-certificates

FROM base AS tests
COPY --from=test-certs /certs/ /certs/
COPY tests/ /tests/
WORKDIR /tests
ENTRYPOINT ["python3"]
CMD ["integration.py"]
