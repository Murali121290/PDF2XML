# Runtime-only application: model weights are supplied in this folder.
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PDF2XML_DATA=/app/out
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libxcb1 libxcb-cursor0 libxrender1 libxext6 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.lock.txt ./
RUN pip install --no-cache-dir --no-deps -c requirements.lock.txt torch torchvision --index-url https://download.pytorch.org/whl/cpu
COPY pyproject.toml requirements.txt api.py run.py ./
COPY src ./src
RUN pip install --default-timeout=1000 --no-cache-dir -r requirements.txt
COPY models ./models
COPY tools ./tools
RUN python tools/prepare_models.py --check
ENV PDF2XML_MODELS_DIR=/app/models HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/out && chown appuser:appuser /app/out
USER appuser
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8087/health/ready', timeout=3)"
EXPOSE 8087
CMD ["python", "run.py", "api", "--host", "0.0.0.0", "--port", "8087"]
