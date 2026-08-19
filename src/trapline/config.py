"""Konfigurace přes proměnné prostředí.

Každý klíč se hledá nejdřív s prefixem ``TRAPLINE_`` (tak je psaný
``.env.example``), pak bez něj. Nepefixovaná varianta je tu kvůli Dockeru —
``REPO_DIR`` / ``REPO_BRANCH`` nastavuje entrypoint a sdílí je se supervisor
smyčkou, takže nemá smysl je duplikovat.
"""

from __future__ import annotations

import os
from pathlib import Path

try:  # ať fungují i CLI skripty spuštěné bez `source .env`
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:  # noqa: BLE001
    pass

_PREFIX = "TRAPLINE_"


def _env(key: str, default: str = "") -> str:
    val = os.environ.get(_PREFIX + key) or os.environ.get(key) or default
    return val.strip()


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    def __init__(self) -> None:
        self.db_url: str = _env(
            "DB_URL",
            "mysql+pymysql://trapline:heslo@127.0.0.1:3306/trapline?charset=utf8mb4",
        )

        self.ollama_url: str = _env("OLLAMA_URL")
        #: SearXNG pro automatické hledání zdrojů (feedhunt). Prázdné = vypnuto.
        self.searxng_url: str = _env("SEARXNG_URL")
        self.llm_bulk: str = _env("LLM_BULK", "qwen3:4b")
        self.llm_main: str = _env("LLM_MAIN", "qwen3:14b")

        self.ntfy_url: str = _env("NTFY_URL", "https://ntfy.sh/")
        self.ntfy_topic: str = _env("NTFY_TOPIC")

        # --- Serverový browser (ADR-0006) --------------------------------
        #: Základní URL browserless/chromium (TrueNasBrowser.yaml), např.
        #: http://172.24.1.111:30061. Prázdné = poslední stupeň transportní
        #: vrstvy (skutečný Chrome) je vypnutý a zbývá jen přímý HTTP fetch.
        self.browser_url: str = _env("BROWSER_URL")
        self.browser_token: str = _env("BROWSER_TOKEN")

        # --- Watcher (pravidelná obchůzka) --------------------------------
        #: Interval obchůzky v hodinách (discovery → skóring → reference →
        #: alerty). 0 = plánovač vypnutý, zůstávají ruční tlačítka.
        self.watch_hours: float = float(_env("WATCH_HOURS", "12"))
        #: Jak často smí obchůzka hledat nové zdroje pro jednu past (hodiny).
        #: 0 = automatický hunt vypnutý, zůstává ruční tlačítko u pasti.
        self.hunt_hours: float = float(_env("HUNT_HOURS", "24"))

        # --- Crawler etiketa ---------------------------------------------
        self.user_agent: str = _env(
            "USER_AGENT", "Trapline/0.1 (+osobni cenovy monitor)"
        )
        self.request_delay_s: float = float(_env("REQUEST_DELAY_S", "2.5"))
        #: Kam gzipovat syrové odpovědi zdrojů kvůli zpětnému přeparsování
        #: (ADR-0004). Prázdné = snapshoty vypnuté. Default je relativní
        #: k CWD — v Dockeru /app/data/snapshots (volume, mimo git).
        self.snapshot_dir: str = _env("SNAPSHOT_DIR", "data/snapshots")

        # --- Zabezpečení GUI ---------------------------------------------
        #: Jedno sdílené heslo. Prázdné = GUI i API jsou otevřené.
        self.app_password: str = _env("APP_PASSWORD")
        #: Volitelné přebití podpisového klíče. Prázdné = odvodí se z hesla
        #: (viz api.auth._secret), aby relace přežily restart po aktualizaci.
        self.auth_secret: str = _env("AUTH_SECRET")
        #: Za HTTPS proxy zapni – cookie se pak posílá jen po šifrovaném
        #: spojení. Po LAN na http musí zůstat vypnuté, jinak se neuloží.
        self.auth_cookie_secure: bool = _truthy(_env("AUTH_COOKIE_SECURE", "false"))

        # --- Self-update z Gitu přes WEB UI ------------------------------
        #: Bez tohohle je celý /api/system/{check,update} zakázaný a GUI panel
        #: se ani nevykreslí. Default false — zapíná se vědomě v compose.
        self.update_enabled: bool = _truthy(_env("UPDATE_ENABLED", "false"))
        #: Kořen gitového pracovního stromu. Prázdné = odvodí se z umístění
        #: tohohle souboru (viz api.system._repo_dir).
        self.repo_dir: str = _env("REPO_DIR", "")
        self.repo_branch: str = _env("REPO_BRANCH", "main")

    @property
    def auth_enabled(self) -> bool:
        return bool(self.app_password)

    @property
    def browser_enabled(self) -> bool:
        return bool(self.browser_url)


settings = Settings()
