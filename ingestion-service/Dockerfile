FROM python:3.12-slim

# The base image lags behind Debian's security updates, so pull the patched
# OS packages at build time. Without this the image ships known-fixable CVEs
# and the pipeline's image scan fails the build.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Built from the repository root, so paths are prefixed. The Common Event Model
# is copied in first and placed where requirements.txt expects to find it, which
# is the reason the context is the root rather than this directory.
COPY packages/common-event-model ./packages/common-event-model
COPY ingestion-service/requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY ingestion-service/app ./app

# Nothing in this service needs root at runtime.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
