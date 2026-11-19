# Build from the repository root with --platform linux/amd64.
ARG PYTHON_IMAGE=python@sha256:4209fae09a3e20afd863e528bfddc235da5fd5977983e1996cd54cc9a72e6267
FROM ${PYTHON_IMAGE}

ARG ATSPI_VERSION=2.46.0-5
ARG XVFB_VERSION=2:21.1.7-3+deb12u13
ARG PYGOBJECT_VERSION=3.42.2-3+b1
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-venv "python3-gi=${PYGOBJECT_VERSION}" \
         "gir1.2-atspi-2.0=${ATSPI_VERSION}" "at-spi2-core=${ATSPI_VERSION}" \
         "xvfb=${XVFB_VERSION}" dbus-x11 xauth libgl1 libegl1 libxkbcommon-x11-0 \
         libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
         libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-xinerama0 libx11-xcb1 \
    && rm -rf /var/lib/apt/lists/*
RUN /usr/bin/python3 -m venv --system-site-packages /opt/recheck-venv \
    && /opt/recheck-venv/bin/python -m pip install --no-cache-dir \
         PySide6-Essentials==6.12.0 httpx==0.28.1 psutil==7.2.2 anyio==4.16.0 \
         certifi==2026.10.23 httpcore==1.0.9 h11==0.16.0 idna==3.20 typing-extensions==4.16.0 \
    && useradd -m -u 65532 -s /bin/sh recheck

WORKDIR /opt/recheck
COPY recheck/__init__.py recheck/paths.py recheck/process.py recheck/porting_manifest.py \
     recheck/porting_worker.py recheck/porting_drivers.py recheck/porting_browser.py \
     recheck/porting_desktop.py recheck/porting_library.py recheck/porting_library_child.py ./recheck/
COPY --chmod=0755 samples/porting/desktop/worker-python.sh /opt/gui-runner/python
ENV PATH=/opt/gui-runner:$PATH
USER 65532:65532
ENTRYPOINT ["python", "-m", "recheck.porting_worker"]
