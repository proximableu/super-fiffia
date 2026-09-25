"""Typed config loader for the F&S knowledge base.

Loads ``config/settings.yaml`` and ``config/taxonomy.yaml`` into typed objects
and exposes module-level ``settings`` and ``taxonomy``. Runtime configuration
lives in ``settings.yaml`` (see ``CONTRACT.md`` §3) and the vocabulary lives in
``taxonomy.yaml`` (see ``CONTRACT.md`` §4).

Environment overrides (used by Docker Compose):
    FS_DSN          overrides ``db.dsn``
    FS_OLLAMA_URL   overrides ``ollama.base_url``
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Repository root -- the ``config/`` directory sits next to this file.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config"


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
@dataclass
class DbSettings:
    dsn: str
    pool_min: int
    pool_max: int


@dataclass
class OllamaSettings:
    base_url: str
    embed_model: str
    llm_model: str


@dataclass
class RetrievalSettings:
    pool: int
    top_k_records: int
    top_k_rag: int
    rrf_k: int


@dataclass
class AgentSettings:
    max_turns: int


@dataclass
class UiSettings:
    lang_default: str


@dataclass
class StatsSettings:
    role_name: str
    role_password: str


@dataclass
class Settings:
    db: DbSettings
    ollama: OllamaSettings
    retrieval: RetrievalSettings
    agent: AgentSettings
    ui: UiSettings
    stats: StatsSettings


def _to_int(value: Any, key: str) -> int:
    """Coerce a YAML scalar to ``int``, raising a clear error on bad input."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"settings: {key} must be an integer, got {value!r}")
    return value


def _load_settings() -> Settings:
    with open(_CONFIG_DIR / "settings.yaml", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    def section(name: str) -> dict:
        block = raw.get(name)
        if block is None:
            raise ValueError(f"settings.yaml: missing required section '{name}'")
        if not isinstance(block, dict):
            raise ValueError(f"settings.yaml: section '{name}' must be a mapping")
        return block

    db = section("db")
    ollama = section("ollama")
    retrieval = section("retrieval")
    agent = section("agent")
    ui = section("ui")
    stats = section("stats")

    settings = Settings(
        db=DbSettings(
            dsn=db["dsn"],
            pool_min=_to_int(db["pool_min"], "db.pool_min"),
            pool_max=_to_int(db["pool_max"], "db.pool_max"),
        ),
        ollama=OllamaSettings(
            base_url=ollama["base_url"],
            embed_model=ollama["embed_model"],
            llm_model=ollama["llm_model"],
        ),
        retrieval=RetrievalSettings(
            pool=_to_int(retrieval["pool"], "retrieval.pool"),
            top_k_records=_to_int(retrieval["top_k_records"], "retrieval.top_k_records"),
            top_k_rag=_to_int(retrieval["top_k_rag"], "retrieval.top_k_rag"),
            rrf_k=_to_int(retrieval["rrf_k"], "retrieval.rrf_k"),
        ),
        agent=AgentSettings(max_turns=_to_int(agent["max_turns"], "agent.max_turns")),
        ui=UiSettings(lang_default=ui["lang_default"]),
        stats=StatsSettings(
            role_name=stats["role_name"],
            role_password=stats["role_password"],
        ),
    )

    # Optional environment overrides (Docker Compose friendly).
    env_dsn = os.environ.get("FS_DSN")
    if env_dsn:
        settings.db = DbSettings(
            dsn=env_dsn,
            pool_min=settings.db.pool_min,
            pool_max=settings.db.pool_max,
        )
    env_ollama = os.environ.get("FS_OLLAMA_URL")
    if env_ollama:
        settings.ollama = OllamaSettings(
            base_url=env_ollama,
            embed_model=settings.ollama.embed_model,
            llm_model=settings.ollama.llm_model,
        )

    return settings


# --------------------------------------------------------------------------- #
# taxonomy
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    id: str
    label_sv: str
    label_en: str
    article_numbers: list[str] = field(default_factory=list)


@dataclass
class Category:
    id: str
    label_sv: str
    label_en: str
    products: list[Product] = field(default_factory=list)


@dataclass
class Taxonomy:
    categories: list[Category] = field(default_factory=list)


def _load_taxonomy() -> Taxonomy:
    with open(_CONFIG_DIR / "taxonomy.yaml", encoding="utf-8") as fh:
        raw: dict = yaml.safe_load(fh) or {}

    categories_raw = raw.get("categories")
    if not categories_raw:
        raise ValueError("taxonomy.yaml: missing required 'categories' list")

    categories: list[Category] = []
    for cat in categories_raw:
        products = [
            Product(
                id=prod["id"],
                label_sv=prod.get("label_sv", prod["id"]),
                label_en=prod.get("label_en", prod["id"]),
                article_numbers=list(prod.get("article_numbers", [])),
            )
            for prod in cat.get("products", [])
        ]
        categories.append(
            Category(
                id=cat["id"],
                label_sv=cat.get("label_sv", cat["id"]),
                label_en=cat.get("label_en", cat["id"]),
                products=products,
            )
        )

    return Taxonomy(categories=categories)


# --------------------------------------------------------------------------- #
# module-level singletons
# --------------------------------------------------------------------------- #
settings: Settings = _load_settings()
taxonomy: Taxonomy = _load_taxonomy()
