"""Reads `config.ini` and produces NektoAudioClient instances."""

from __future__ import annotations

import logging
from configparser import ConfigParser
from dataclasses import dataclass
from typing import Any, Iterable

import structlog


@dataclass
class ClientSpec:
    name: str
    token: str
    user_agent: str
    search_criteria: dict[str, Any]


def _parse_age_range(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) != 2:
        return None
    try:
        return [int(parts[0]), int(parts[1])]
    except ValueError:
        return None


def _parse_wish_age(raw: str | None) -> list[list[int]] | None:
    if not raw:
        return None
    out: list[list[int]] = []
    for chunk in raw.split("-"):
        parsed = _parse_age_range(chunk)
        if parsed:
            out.append(parsed)
    return out or None


def _parse_sex(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().upper()
    if raw in ("M", "F"):
        return raw
    return None


def setup_logging(debug: bool) -> None:
    level = logging.DEBUG if debug else logging.INFO
    logging.basicConfig(level=level, format="%(message)s")
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(level),
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
    )


def load_config(path: str = "config.ini") -> tuple[bool, list[ClientSpec]]:
    cfg = ConfigParser()
    if not cfg.read(path, encoding="utf-8"):
        raise FileNotFoundError(
            f"Config file {path!r} not found. Copy config.example.ini -> config.ini and fill in tokens."
        )
    debug = cfg.getboolean("settings", "debug", fallback=False)
    names = cfg.get("settings", "clients").split()
    out: list[ClientSpec] = []
    for name in names:
        section = f"client/{name}"
        if not cfg.has_section(section):
            raise ValueError(f"Missing config section: [{section}]")
        token = cfg.get(section, "token", fallback="").strip()
        if not token:
            raise ValueError(f"client/{name}: token is empty")
        ua = cfg.get(section, "ua", fallback="").strip()
        if not ua:
            raise ValueError(f"client/{name}: ua is empty")

        my_sex = _parse_sex(cfg.get(section, "my-sex", fallback=None))
        wish_sex = _parse_sex(cfg.get(section, "wish-sex", fallback=None))
        my_age = _parse_age_range(cfg.get(section, "my-age", fallback=None))
        wish_age = _parse_wish_age(cfg.get(section, "wish-age", fallback=None))

        # searchCriteria field names mirror nekto's text-chat protocol
        # (mySex / wishSex / myAge / wishAge). audiochat accepts the same shape.
        criteria: dict[str, Any] = {}
        if my_sex:
            criteria["mySex"] = my_sex
        if wish_sex:
            criteria["wishSex"] = wish_sex
        if my_age:
            criteria["myAge"] = my_age
        if wish_age:
            criteria["wishAge"] = wish_age

        out.append(ClientSpec(name=name, token=token, user_agent=ua, search_criteria=criteria))
    return debug, out
