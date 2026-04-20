FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && apt-get clean

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir -e .

RUN mkdir -p overleaf_cache

ENV PYTHONUNBUFFERED=1

CMD ["overleaf-mcp"]
