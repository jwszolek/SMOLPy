# The ns-3 base image: ghcr.io/jwszolek/smolpy-ns3-base:<ns-3 version>
# (ADR-0004, Container images). Built by .github/workflows/ns3-base.yml.
#
# It holds the pinned ns-3 release, built once: the release profile, without
# examples, tests or Python bindings, and only the modules SMOLPy's scenarios
# need. Not the optimized profile: it compiles with -march=native, for the CPU
# of the machine that builds the image, which another CPU may not run.
#
# The toolchain stays in the image, because the ns-3 backend builds each
# generated scenario as a scratch program against this build. The SMOLPy
# image with ns-3, <version>-ns3, is built on top of it at each release.
#
# ns-3 is licensed under GPLv2, so this image is distributed under its terms.

FROM python:3.12-slim-bookworm

# Changing the pinned release: a new version, its tag's commit
# (git ls-remote https://gitlab.com/nsnam/ns-3-dev.git refs/tags/ns-3.<n>^{})
# and the image tag in ns3-base.yml follow from these.
ARG NS3_VERSION=3.49
ARG NS3_COMMIT=418210e8914e9f6bcb5cddbcc94a2cecb7853b68
ARG NS3_MODULES="core;network;internet;applications;csma;bridge;traffic-control;stats"

LABEL org.opencontainers.image.title="ns-3 ${NS3_VERSION} for SMOLPy" \
      org.opencontainers.image.description="The pinned ns-3 release, built for SMOLPy's ns-3 backend" \
      org.opencontainers.image.source="https://github.com/jwszolek/SMOLPy" \
      org.opencontainers.image.licenses="GPL-2.0-only" \
      org.opencontainers.image.version="${NS3_VERSION}" \
      org.nsnam.ns3.source="https://gitlab.com/nsnam/ns-3-dev/-/tree/ns-${NS3_VERSION}" \
      org.nsnam.ns3.commit="${NS3_COMMIT}"

# g++ 12 and CMake 3.25 of Debian 12 meet ns-3.49's minimums (g++ 11.1, CMake 3.25)
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ cmake ninja-build git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --quiet --depth 1 --branch "ns-${NS3_VERSION}" \
        https://gitlab.com/nsnam/ns-3-dev.git /opt/ns-3 \
    && test "$(git -C /opt/ns-3 rev-parse HEAD)" = "${NS3_COMMIT}" \
    && rm -rf /opt/ns-3/.git

WORKDIR /opt/ns-3
RUN ./ns3 configure --build-profile=release -G Ninja \
        --disable-examples --disable-tests --disable-python-bindings --disable-werror \
        --enable-modules="${NS3_MODULES}" \
    && ./ns3 build

ENV NS3_HOME=/opt/ns-3 \
    NS3_VERSION=${NS3_VERSION}
