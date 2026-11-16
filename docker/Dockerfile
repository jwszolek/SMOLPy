# SMOLPy with the Rust engine, without ns-3: ghcr.io/jwszolek/smolpy
# (ADR-0004, Container images). Built by .github/workflows/docker-image.yml.
#
# The build context holds only dist/, with the wheels of one version of
# smolpy and smolpy-engine-rs: a release's own files, or a pull request's.
# pip picks the engine wheel of the platform being built.
#
#     docker run --rm -v "$PWD:/work" ghcr.io/jwszolek/smolpy run plant.smol --backend rust

FROM python:3.12-slim-bookworm

ARG VERSION

LABEL org.opencontainers.image.title="SMOLPy" \
      org.opencontainers.image.description="Network simulation DSL and discrete-event simulator, with the Rust engine (without ns-3)" \
      org.opencontainers.image.source="https://github.com/jwszolek/SMOLPy" \
      org.opencontainers.image.documentation="https://jwszolek.github.io/SMOLPy/" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

# both packages pinned to the version, so pip takes them from dist/; their
# dependencies come from PyPI
RUN --mount=type=bind,source=dist,target=/dist \
    test -n "${VERSION}" \
    && pip install --no-cache-dir --find-links /dist \
        "smolpy[rust]==${VERSION}" "smolpy-engine-rs==${VERSION}" \
    && python -c "import smolpy_engine_rs; assert smolpy_engine_rs.__version__ == '${VERSION}'"

# not root: files written to the mounted directory belong to uid 1000, or to
# whoever `docker run --user` names
RUN useradd --create-home --uid 1000 smolpy
USER 1000
WORKDIR /work

ENTRYPOINT ["smolpy"]
CMD ["--help"]
