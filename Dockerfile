# Use official Python 3.12 slim image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy requirements first (leverages Docker layer caching)
COPY requirements.txt /app/requirements.txt

# Install required Python packages
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . /app

# Expose the port Cloud Run expects
EXPOSE 8080

# Run with uvicorn (ASGI) + gunicorn as process manager
# --workers 1  : Cloud Run is single-instance per container; scale via replicas
# --threads 8  : handles concurrent requests within the worker
CMD ["gunicorn", "main:app", "--bind", "0.0.0.0:8080", "--workers", "1", "--worker-class", "uvicorn.workers.UvicornWorker","--timeout", "200"]
