# pdf2xml conversion API: PDF -> Docling -> canonical JSON/XML -> BITS -> JATS (CPU, offline).
#
# Built from this folder with no internet access: every Python package is in wheels/ and the
# Docling models are in models/. Only the base image is pulled (or loaded) from a registry.
#
#   docker build -t pdf2xml-api:VERSION .
#   docker compose up -d            (see docker-compose.yml and README.md)

FROM python:3.13-slim-bookworm

# HF_*/TRANSFORMERS_OFFLINE: never reach for a model hub, the models are baked in
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/tmp/huggingface \
    TORCH_HOME=/tmp/torch \
    PDF2XML_MODELS_DIR=/opt/pdf2xml/models \
    PDF2XML_DATA=/data \
    PDF2XML_OUT=/data \
    PYTHONPATH=/opt/pdf2xml/app

# Python packages from the bundled wheels only (--no-index: nothing is downloaded). The wheels
# are bind-mounted, so they do not stay in an image layer. opencv-python needs libGL, which a
# slim image lacks; its headless build provides the same cv2 module without it.
RUN --mount=type=bind,source=wheels,target=/tmp/wheels \
    --mount=type=bind,source=requirements.txt,target=/tmp/requirements.txt \
    pip install --no-index --find-links /tmp/wheels -r /tmp/requirements.txt \
 && pip uninstall -y opencv-python \
 && pip install --no-index --find-links /tmp/wheels --no-deps --force-reinstall \
        opencv-python-headless

COPY models /opt/pdf2xml/models
COPY app /opt/pdf2xml/app
COPY healthcheck.py selftest.pdf /opt/pdf2xml/

# Self-test while building: load the models and convert a small PDF, so a broken image fails
# here and not at the first upload.
RUN python -m pdf2xml /opt/pdf2xml/selftest.pdf -o /tmp/selftest \
 && test -s /tmp/selftest/selftest/40_bits.xml \
 && test -s /tmp/selftest/selftest/41_jats.xml \
 && rm -rf /tmp/selftest /tmp/huggingface /tmp/torch

RUN useradd --create-home --uid 10001 pdf2xml \
 && mkdir -p /data \
 && chown pdf2xml:pdf2xml /data
USER pdf2xml
VOLUME /data
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "/opt/pdf2xml/healthcheck.py"]

CMD ["python", "-m", "pdf2xml.api"]
