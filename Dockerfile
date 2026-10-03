# Use Python 3.12 to ensure maximum compatibility with pre-compiled wheels (like saxonche, docling)
FROM python:3.12-slim

# Prevent Python from writing pyc files to disc and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

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

# Copy the entire project into the container
COPY . .

# First install CPU-only PyTorch to skip downloading gigabytes of useless NVIDIA CUDA binaries
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu

# Install the Python package and all dependencies (with increased timeout)
RUN pip install --default-timeout=1000 --no-cache-dir -e ".[ml]"

# Create an output directory for the API to use temporarily
RUN mkdir -p out

# Expose the port the app runs on
EXPOSE 8087

# Command to run the application
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8087"]
