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

COPY app.py evidence.py dashboard_security.py account_setup.py access_roles.py access_api.py admin_access.py auth_preflight.py local_auth.py ./
COPY access-policy.json ./
COPY .streamlit/config.toml ./.streamlit/config.toml

# Nothing in this dashboard needs root at runtime.
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8501

# The preflight stops a revision with unusable sign-in configuration from ever
# becoming healthy, so it cannot replace a working one (see auth_preflight.py).
CMD ["sh", "-c", "python auth_preflight.py && exec streamlit run app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true"]
