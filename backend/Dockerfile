FROM python:3.12-slim

WORKDIR /app

# Install system deps (gcc for some Python packages)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Logs directory
RUN mkdir -p /app/logs

# Single run per day — triggered by cron or EC2 scheduler
CMD ["python", "-u", "strategy_engine.py"]
