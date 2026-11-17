FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Install phys-ai-dataconverter
RUN pip install --no-cache-dir phys-ai-dataconverter boto3

WORKDIR /app

# Copy worker script
COPY aws_batch_worker.py .

# Set default command
CMD ["python", "aws_batch_worker.py"]
