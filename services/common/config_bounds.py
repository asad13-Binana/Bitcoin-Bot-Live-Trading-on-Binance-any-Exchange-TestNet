from __future__ import annotations
"""Typed, bounded environment configuration (fixes V101-NEW-007).

Every risk-relevant environment value is parsed through these helpers so a
malformed or unsafe private environment fails startup with a precise error
instead of silently weakening a guard (for example a negative cooldown).
"""
import math
import os


class ConfigError(ValueError):
    """Raised when an environment value is malformed or outside safe bounds."""


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == '':
        value = int(default)
    else:
        try:
            value = int(str(raw).strip())
        except Exception as exc:
            raise ConfigError(f'{name} must be an integer, got {raw!r}') from exc
    if not minimum <= value <= maximum:
        raise ConfigError(f'{name}={value} outside safe bounds [{minimum}, {maximum}]')
    return value


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == '':
        value = float(default)
    else:
        try:
            value = float(str(raw).strip())
        except Exception as exc:
            raise ConfigError(f'{name} must be a number, got {raw!r}') from exc
    if not math.isfinite(value):
        raise ConfigError(f'{name} must be finite, got {value!r}')
    if not minimum <= value <= maximum:
        raise ConfigError(f'{name}={value} outside safe bounds [{minimum}, {maximum}]')
    return value


def env_choice(name: str, default: str, choices) -> str:
    value = str(os.getenv(name, default) or default).strip().lower()
    allowed = {str(c).lower() for c in choices}
    if value not in allowed:
        raise ConfigError(f'{name}={value!r} not one of {sorted(allowed)}')
    return value
