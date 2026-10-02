FROM python:3.12-slim

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Built from the repository root, so paths are prefixed. The Common Event Model
# is copied in first and placed where requirements.txt expects to find it, which
# is the reason the context is the root rather than this directory.
COPY packages/common-event-model ./packages/common-event-model
COPY notification-service/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY notification-service/app ./app

RUN useradd --create-home --uid 10001 appuser
USER appuser

# No inbound HTTP traffic. The service consumes notification-events from
# Service Bus and delivers notifications downstream.
CMD ["python", "-m", "app.notification_main"]