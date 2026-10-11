FROM python:3.12.15-slim-trixie@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip==26.2 \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && python -m pip check \
    && python -m pip uninstall -y pip \
    && rm -rf /usr/local/lib/python3.12/ensurepip \
    && python -c "import importlib.util, pathlib; assert importlib.util.find_spec('pip') is None; assert not list(pathlib.Path('/usr/local/bin').glob('pip*')); assert not list(pathlib.Path('/usr/local/lib/python3.12/site-packages').glob('pip*')); assert not pathlib.Path('/usr/local/lib/python3.12/ensurepip').exists()"
COPY app ./app
RUN useradd --create-home --uid 10001 appuser
USER appuser
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
