ARG RUST_IMAGE=rust@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2
ARG PYTHON_IMAGE=python@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267
ARG NODE_IMAGE=node@sha256:9d266751ca3d7fdbe651c878ffd5ad0a42fed49a18c283509267c97f025b7496
ARG JAVA_IMAGE=eclipse-temurin@sha256:432e54724c3e57d1846aeb886f4d339f3b0522931c4e64119c2797013b7cfc3c
ARG DOTNET_IMAGE=mcr.microsoft.com/dotnet/sdk@sha256:1b955f179b02df4f0eb5fadedaa898cbe44d620a64aab52eec72c8e91d1355b5
FROM ${RUST_IMAGE} AS rust
FROM ${NODE_IMAGE} AS node
RUN npm install --prefix /opt/typescript --ignore-scripts --no-audit --no-fund typescript@5.9.3
FROM ${JAVA_IMAGE} AS java
FROM ${DOTNET_IMAGE} AS dotnet
FROM ${PYTHON_IMAGE}
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /opt/typescript/node_modules/typescript /opt/typescript
COPY --from=java /opt/java/openjdk /opt/java/openjdk
COPY --from=dotnet /usr/share/dotnet /opt/dotnet
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=rust /usr/local/cargo/bin /usr/local/cargo/bin
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/tmp PATH=/usr/local/cargo/bin:/opt/java/openjdk/bin:/opt/dotnet:$PATH
RUN ln -s /opt/typescript/bin/tsc /usr/local/bin/tsc
RUN python -m pip install --no-cache-dir httpx==0.28.1 anyio==4.16.0 certifi==2026.10.23 \
    httpcore==1.0.9 h11==0.16.0 idna==3.20 typing-extensions==4.16.0
WORKDIR /opt/recheck
COPY recheck/__init__.py recheck/paths.py recheck/process.py recheck/porting_manifest.py \
     recheck/porting_worker.py recheck/porting_drivers.py recheck/porting_browser.py \
     recheck/porting_desktop.py recheck/porting_library.py recheck/porting_library_child.py ./recheck/
USER 65532:65532
ENTRYPOINT ["python", "-m", "recheck.porting_worker"]
