# Obraz nese jen toolchain (Python + git). Vlastní kód se za běhu naklonuje
# z Gitu do volume /app a aktualizuje přes `git pull` (self-update), takže
# nová verze aplikace nevyžaduje nový image.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY docker/entrypoint.sh /entrypoint.sh
COPY docker/pydeps.py /usr/local/bin/pydeps.py
RUN chmod +x /entrypoint.sh

# Zapeč závislosti do image, aby uvicorn nikdy nechyběl – ani po znovuvytvoření
# kontejneru, kdy se vrstva resetuje a /app volume zůstane. Runtime install
# v entrypointu pak jen dotahuje změny.
COPY pyproject.toml /tmp/pyproject.toml
RUN python /usr/local/bin/pydeps.py /tmp/pyproject.toml > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt

ENV REPO_URL=https://github.com/hulek-ales/Trapline.git \
    REPO_BRANCH=main \
    REPO_DIR=/app \
    PIP_BREAK_SYSTEM_PACKAGES=1

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
