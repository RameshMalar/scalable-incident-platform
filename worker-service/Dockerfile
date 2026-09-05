FROM python:3.12-slim

# The base image lags behind Debian's security updates, so pull the patched
# OS packages at build time. Without this the image ships known-fixable CVEs
# and the pipeline's image scan fails the build.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Nothing in this worker needs root at runtime.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# No EXPOSE and no ingress: the worker takes no inbound traffic, it only pulls
# from the queue.
CMD ["python", "-m", "app.main"]
