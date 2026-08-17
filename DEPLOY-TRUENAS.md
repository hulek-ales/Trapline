# Nasazení na TrueNAS + self-update z Gitu

Stejný mechanismus jako u Kuchařky: image nese jen toolchain (Python + git),
vlastní kód si kontejner **naklonuje z Gitu do volume** `/app` a dál se
aktualizuje tlačítkem ve webovém GUI. Nová verze aplikace tedy nevyžaduje
nový image ani přenasazení appky.

## 1. Image

O build se stará `.github/workflows/docker-image.yml` — po pushi do `main`
(když se změní `Dockerfile`, `docker/**` nebo `pyproject.toml`) publikuje
`ghcr.io/hulek-ales/trapline:latest`. U privátního repa je balíček v GHCR
taky privátní; buď ho v nastavení GitHub Packages přepni na public, nebo
na TrueNAS přidej registry credentials.

Ruční build bez GH Actions:

```bash
docker build -t ghcr.io/hulek-ales/trapline:latest .
docker push ghcr.io/hulek-ales/trapline:latest
```

## 2. Custom App na TrueNAS

Apps → Discover Apps → Custom App → **Install via YAML** a vlož obsah
`TrueNasAPP.yaml`. Proměnné se dají přepsat, jinak platí defaulty za `:-`.

Za pozornost stojí:

| Proměnná | Default | K čemu |
|---|---|---|
| `APP_PORT` | `38081` | port na NASu |
| `UPDATE_ENABLED` | `true` | zpřístupní tlačítko „Aktualizovat z Gitu" |
| `REPO_URL` / `REPO_BRANCH` | `…/Trapline.git`, `main` | co a odkud se klonuje |
| `DB_PASSWORD` | `traplineVychoziHeslo` | **změň** |
| `DB_ROOT_PASSWORD` | `traplineVychoziHesloRoot` | **změň** |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | LLM klasifikace inzerátů |

Ollama běží mimo kontejner; `host.docker.internal` míří na hostitele díky
`extra_hosts: host-gateway`. Když Ollamu provozuješ jako jinou TrueNAS appku
na interní síti (např. open-webui), přepiš `OLLAMA_URL` na její adresu a
připoj `app` do příslušné externí sítě:

```yaml
    networks: [default, openwebui]
networks:
  openwebui:
    external: true
    name: ix-internal-open-webui-open-webui-net
```

**První start trvá pár minut** — klonuje repo a instaluje závislosti. Proto
má healthcheck `start_period: 300s`. Průběh: `docker compose logs -f app`.

Databáze `db` (MariaDB) je připravená dopředu; API ji zatím nepoužívá,
protože doménové endpointy ještě nejsou hotové (viz „Stav" v README).

## 3. Aktualizace z webu

1. Na PC commitneš a pushneš do `main`.
2. V GUI → **Verze a aktualizace** → *Zkontrolovat aktualizace* ukáže, o kolik
   commitů jsi pozadu.
3. *Aktualizovat z Gitu* položí značku `.needs-build` a ukončí uvicorn.
   Supervisor smyčka v `docker/entrypoint.sh` udělá `git pull --ff-only`,
   doinstaluje závislosti z `pyproject.toml` a nastartuje novou verzi.
   GUI mezitím poll‑uje `/api/system/version` a počká, až se změní hash
   commitu — pak ukáže „Aktualizováno na `<hash>` ✓".

> `UPDATE_ENABLED=true` zpřístupní tlačítko. Endpoint umí spustit `git pull`
> a restart procesu — drž appku za reverzní proxy / Cloudflare Zero Trust,
> ne na veřejné IP.

## Mimo Docker

Bez supervisor smyčky (`SUPERVISED` není `1`) tlačítko jen provede
`git pull --ff-only` a vypíše, že je potřeba API restartovat ručně. GUI na to
upozorní poznámkou pod tlačítky.

```bash
pip install -e .
UPDATE_ENABLED=true uvicorn trapline.api.main:app --host 0.0.0.0 --port 8000
```
