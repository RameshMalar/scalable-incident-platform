FROM python:3.12-slim

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 10001 appuser
USER appuser

# No inbound HTTP traffic. The service consumes notification-events from
# Service Bus and delivers notifications downstream.
CMD ["python", "-m", "app.notification_main"]