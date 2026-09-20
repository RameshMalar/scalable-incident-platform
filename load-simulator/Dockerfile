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

# /app belongs to root and the run writes its results as appuser, so results
# go somewhere writable by default. The summary is on stdout regardless, which
# is what the job's log stream carries.
ENV OUT_DIR=/tmp/results

# Nothing in the simulator needs root at runtime.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# No EXPOSE and no ingress: this drives traffic at the platform, it does not
# serve any. It runs to completion and exits, which is why it is deployed as a
# Container Apps job rather than as an app.
CMD ["python", "-m", "app.main"]
