# syntax=docker/dockerfile:1
ARG DEBIAN_IMAGE=debian:trixie-slim
FROM ${DEBIAN_IMAGE} AS build
ARG ATS_VERSION=10.2.1
ARG ATS_SHA512=dbb532fa5714540524853ddeb709871de3f7ab5b363b13fbb16ccb23863fd379e691f3689bb7279aaff6dd5250bf5bfe9a37fc7c748276d57485104414919822
ARG BUILD_JOBS=8
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl bzip2 build-essential cmake ninja-build pkg-config patch \
       libssl-dev libpcre2-dev zlib1g-dev libfmt-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl --fail --location --retry 3 \
        "https://archive.apache.org/dist/trafficserver/trafficserver-${ATS_VERSION}.tar.bz2" \
        -o trafficserver.tar.bz2 \
    && echo "${ATS_SHA512}  trafficserver.tar.bz2" | sha512sum --check - \
    && tar -xjf trafficserver.tar.bz2 --strip-components=1 \
    && rm trafficserver.tar.bz2
COPY patches/ /patches/
RUN patch -p1 < /patches/redirect-reuse-cache-writer.patch
RUN cmake -S . -B /build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/ats \
        -DBUILD_TESTING=OFF -DBUILD_REGRESSION_TESTING=OFF \
        -DENABLE_VERIFY_PLUGINS=OFF -DENABLE_DOCS=OFF \
        -DENABLE_LUAJIT=OFF -DENABLE_HWLOC=OFF -DENABLE_UNWIND=OFF \
        -DENABLE_POSIX_CAP=OFF \
    && cmake --build /build --parallel "${BUILD_JOBS}" \
    && cmake --install /build --strip

FROM build AS plugin-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcurl4-openssl-dev \
    && rm -rf /var/lib/apt/lists/*
COPY plugins/redirect_guard.cc /src/redirect_guard.cc
RUN c++ -std=c++17 -O2 -Wall -Wextra -Werror -shared -fPIC \
    -I/opt/ats/include /src/redirect_guard.cc -lcurl \
    -o /opt/ats/libexec/trafficserver/redirect_guard.so

FROM ${DEBIAN_IMAGE} AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl python3 libssl3t64 libpcre2-8-0 zlib1g libfmt10 \
    && rm -rf /var/lib/apt/lists/*

FROM base AS runtime
COPY --from=plugin-build /opt/ats /opt/ats
ENV PATH="/opt/ats/bin:${PATH}"
RUN groupadd --system trafficserver \
    && useradd --system --gid trafficserver --home-dir /opt/ats trafficserver \
    && mkdir -p /opt/ats/var/trafficserver /opt/ats/var/log/trafficserver /var/lib/lancache \
    && chown -R trafficserver:trafficserver /opt/ats/var
COPY config/ /etc/lancache/
COPY entrypoint.py /usr/local/bin/lancache-entrypoint
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
    CMD curl --fail --silent http://127.0.0.1:8080/_health || exit 1
ENTRYPOINT ["python3", "/usr/local/bin/lancache-entrypoint"]

FROM base AS test-certs
RUN mkdir /certs \
    && openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
       -subj /CN=origin2 -addext subjectAltName=DNS:origin2 \
       -keyout /certs/origin.key -out /certs/origin.crt

FROM runtime AS ats-test
COPY --from=test-certs /certs/origin.crt /usr/local/share/ca-certificates/test-origin.crt
RUN update-ca-certificates

FROM base AS tests
COPY --from=test-certs /certs/ /certs/
COPY tests/ /tests/
WORKDIR /tests
ENTRYPOINT ["python3"]
CMD ["integration.py"]

FROM runtime AS ats
