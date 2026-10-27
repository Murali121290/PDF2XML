# syntax=docker/dockerfile:1
# This stage depends only on the model manifest/helper, not application code. Code-only
# updates reuse the verified model layer instead of downloading the weights again.
FROM python:3.12-slim AS prepared-models
WORKDIR /app
RUN pip install --no-cache-dir huggingface-hub==1.33.0
COPY models ./models
COPY tools/prepare_models.py ./tools/prepare_models.py
RUN python tools/prepare_models.py

# Use Python 3.12 for compatibility with the conversion dependencies.
FROM python:3.12-slim

# Prevent Python from writing pyc files to disc and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PDF2XML_DATA=/app/out

WORKDIR /app

# Install necessary system packages (optional depending on docling/pdfplumber C dependencies)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    libxcb1 \
    libxcb-cursor0 \
    libxrender1 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# First install CPU-only PyTorch to skip downloading gigabytes of useless NVIDIA CUDA binaries
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Copy the application and installation metadata into the container
COPY pyproject.toml requirements.txt api.py ./
COPY src ./src

# Install the Python package and all dependencies (with increased timeout)
RUN pip install --default-timeout=1000 --no-cache-dir -r requirements.txt

# The finished image reads its packaged weights locally, including the first request.
COPY --from=prepared-models /app/models ./models

ENV PDF2XML_MODELS_DIR=/app/models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    DO_NOT_TRACK=1

# Fail the build if the image cannot convert using only its packaged models.
COPY tests/fixtures/pdfs/simple/simple.pdf /tmp/pdf2xml-selftest.pdf
RUN --network=none python -c "from pathlib import Path; from pdf2xml.config import Config; from pdf2xml.pipeline import convert; r = convert(Path('/tmp/pdf2xml-selftest.pdf'), Path('/tmp/pdf2xml-selftest'), Config(targets=['json','xml','bits','jats'], overlays=False)); assert r.coverage.ok and not r.xml_errors; assert all(e.valid for e in r.exports.values()); assert 'completeness' in r.files" \
    && rm -rf /tmp/pdf2xml-selftest /tmp/pdf2xml-selftest.pdf

# Create an output directory for the API to use temporarily
RUN mkdir -p out

# Expose the port the app runs on
EXPOSE 8087

# Command to run the application
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8087"]
