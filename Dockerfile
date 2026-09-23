# ---- build stage -----------------------------------------------------------
FROM python:3.12-slim AS build

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY wa_kit ./wa_kit
COPY examples ./examples

RUN python -m pip install --upgrade pip \
 && pip wheel --no-deps --wheel-dir /wheels .

# ---- runtime stage ---------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WA_DB_PATH=/data/wa_kit.db

RUN groupadd --system app && useradd --system --gid app --home /app app \
 && mkdir -p /app /data && chown -R app:app /app /data

WORKDIR /app
COPY --from=build /wheels /wheels
COPY --chown=app:app examples ./examples

RUN pip install --no-cache-dir /wheels/*.whl "uvicorn>=0.30" \
 && rm -rf /wheels

USER app
EXPOSE 8000
VOLUME ["/data"]

CMD ["uvicorn", "examples.run_server:app", "--host", "0.0.0.0", "--port", "8000"]
