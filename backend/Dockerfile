# CareLoop backend — python:3.12-slim, non-root, uvicorn.
# Prod: `docker compose up` runs the baked CMD.
# Dev:  docker-compose.dev.yml bind-mounts ./backend/app and overrides the
#       command with `uvicorn app.main:app --reload` (deps are already baked).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/careloop

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY app ./app
COPY tests ./tests

RUN useradd --create-home --uid 1000 careloop \
    && chown -R careloop:careloop /srv/careloop
USER careloop

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
