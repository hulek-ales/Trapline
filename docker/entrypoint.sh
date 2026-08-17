#!/usr/bin/env bash
# Supervisor smyčka: klon → (pull → install je-li potřeba) → spustí API.
# Self-update: endpoint /api/system/update ukončí uvicorn, smyčka pullne a
# restartuje. Závislosti se přeinstalují jen když se kód změnil.
set -uo pipefail

REPO_URL="${REPO_URL:-https://github.com/hulek-ales/Trapline.git}"
BRANCH="${REPO_BRANCH:-main}"
cd /app

if [ ! -e src/trapline/api/main.py ]; then
  echo "[init] klonuji ${REPO_URL} (${BRANCH})"
  git clone --branch "${BRANCH}" "${REPO_URL}" /tmp/repo \
    && cp -a /tmp/repo/. /app/ && rm -rf /tmp/repo \
    || { echo "[init] klon selhal"; sleep 10; exit 1; }
fi
git config --global --add safe.directory /app

install_deps() {
  echo "[build] pip install závislostí z pyproject.toml"
  if python /usr/local/bin/pydeps.py /app/pyproject.toml > /tmp/req.txt; then
    pip install -q -r /tmp/req.txt || echo "[build] pip varování"
  else
    echo "[build] pyproject.toml nečitelný – přeskakuji install"
  fi
  # Frontend s build krokem tu zatím není (GUI je statické HTML). Až přibude
  # frontend/package.json, sestav ho sem — smyčka na to je připravená.
  if [ -f frontend/package.json ] && command -v npm >/dev/null 2>&1; then
    echo "[build] frontend (npm)"
    ( cd frontend && { npm ci --silent || npm install --silent; } && npm run build ) \
      && rm -rf src/trapline/api/static && cp -r frontend/dist src/trapline/api/static \
      || echo "[build] frontend selhal – ponechávám stávající static"
  fi
}

while true; do
  before="$(git rev-parse HEAD 2>/dev/null || echo none)"
  git pull --ff-only origin "${BRANCH}" 2>&1 || echo "[git] pull přeskočen"
  after="$(git rev-parse HEAD 2>/dev/null || echo none)"

  if [ -f .needs-build ] || [ "${before}" != "${after}" ]; then
    install_deps
    rm -f .needs-build
  fi

  # Pojistka: po znovuvytvoření kontejneru zmizí pip balíčky z vrstvy, ale
  # volume /app zůstane (install se přeskočí). Doinstaluj, když chybí uvicorn.
  if ! command -v uvicorn >/dev/null 2>&1; then
    echo "[deps] uvicorn chybí – doinstalovávám závislosti"
    install_deps
  fi

  echo "[run] start API na :8000 (commit ${after:0:7})"
  SUPERVISED=1 REPO_DIR=/app PYTHONPATH=/app/src \
    uvicorn trapline.api.main:app --host 0.0.0.0 --port 8000
  echo "[run] API skončilo (kód $?) – restart za 3 s"
  sleep 3
done
