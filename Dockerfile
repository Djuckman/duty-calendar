FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY duty ./duty
RUN pip install ".[postgres]"

RUN useradd --create-home --uid 1000 duty && mkdir -p /data && chown duty /data
USER duty

ENV DUTY_DATABASE_URL=sqlite:////data/duty.db
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["python", "-m", "duty"]
