#!/usr/bin/env python3
"""config -- load tabkit's rules from a TOML config file.

Design notes
------------
* **Never fatal.** A missing, unreadable or malformed config falls back to the
  built-in defaults, and records a warning. A tab-auditing tool that refuses to
  start because of a typo in an optional file is worse than one that runs with
  slightly different rules and says so.
* **Shallow merge, per section.** Unknown keys are ignored rather than raising,
  so a config written for a newer version still loads on an older one.
* **No third-party dependencies.** Uses ``tomllib`` where available (Python
  3.11+) and a small, strict fallback parser otherwise, because requiring a pip
  install to read a 30-line config is a poor trade.
"""

from __future__ import annotations

import os
from typing import Any

try:  # Python 3.11+
    import tomllib  # type: ignore
except ModuleNotFoundError:  # pragma: no cover - exercised on older runtimes
    tomllib = None  # type: ignore

# --------------------------------------------------------------------------- #
# Defaults. These MUST stay functional without any file present: the config
# file is an override mechanism, not a requirement.
# --------------------------------------------------------------------------- #

DEFAULTS: dict[str, Any] = {
    "wrapper": {
        "carrier_params": ["uri", "url"],
        "max_depth": 5,
        # Publicly-published tab-suspender wrappers. These are shipped as
        # defaults because they are published extension ids and marker paths,
        # not personal data, and a fresh checkout should detect the common
        # cases without configuration. Anything specific to one machine goes in
        # tabkit.local.toml instead.
        "detector": [
            {"name": "generic_suspender_extension", "match": "suspended.html"},
            {"name": "workona", "match": "workona.com/suspended"},
            {"name": "great_suspender", "match": "noogafoofpebimajpfpamcfhoaifemoa"},
            {"name": "marvelous_suspender", "match": "plpkmjcnhhnpkblimgenmdhghfgghdpp"},
        ],
    },
    "dedupe": {
        "meaningless_prefixes": ["chrome://newtab", "chrome://new-tab-page"],
        "collapse_trailing_slash_before_query": True,
        "fragments_are_significant": True,
        # Off by default: enabling it changes how dedupe keys are constructed,
        # which is a behaviour change rather than a bug fix.
        "strip_www_prefix": False,
    },
    "privacy": {
        "redact_hosts": [],
        "redact_local_paths": True,
        "local_path_placeholder": "<user>",
    },
    "open": {
        "default_batch": 20,
        "per_tab_delay_seconds": 0.35,
        "per_batch_delay_seconds": 3.0,
    },
    "limits": {
        "max_close_fraction": 0.40,
        "min_session_tabs": 5,
    },
}

CONFIG_ENV_VAR = "TABKIT_CONFIG"

#: Warnings collected during the most recent :func:`load` call. Scripts print
#: these so a silently-ignored config is visible rather than mysterious.
WARNINGS: list[str] = []


def candidate_paths(cwd: str | None = None) -> list[str]:
    """Config locations, in priority order."""
    here = cwd or os.getcwd()
    env = os.environ.get(CONFIG_ENV_VAR)
    paths = []
    if env:
        paths.append(os.path.expanduser(env))
    paths.append(os.path.join(here, "tabkit.local.toml"))
    paths.append(os.path.join(os.path.expanduser("~/.config/tabkit/config.toml")))
    return paths


def _parse_toml(text: str) -> dict[str, Any]:
    """Parse TOML, preferring the stdlib implementation."""
    if tomllib is not None:
        return tomllib.loads(text)
    return _parse_toml_fallback(text)


def _parse_toml_fallback(text: str) -> dict[str, Any]:
    """Minimal TOML subset parser: ``[section]``, ``key = value``, arrays.

    Covers exactly the constructs this project's config uses. Deliberately
    strict -- anything it does not understand raises, so a misread config fails
    loudly here and is caught by the caller, rather than being silently
    half-applied.
    """
    root: dict[str, Any] = {}
    section: dict[str, Any] = root
    pending_array_of_tables: str | None = None

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        if line.startswith("[[") and line.endswith("]]"):
            name = line[2:-2].strip()
            parent_name = name.split(".")[0]
            root.setdefault(parent_name, {})
            root[parent_name].setdefault(name, [])
            entry: dict[str, Any] = {}
            root[parent_name][name].append(entry)
            section = entry
            pending_array_of_tables = name
            continue

        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            section = root.setdefault(name, {})
            pending_array_of_tables = None
            continue

        if "=" not in line:
            raise ValueError(f"unparsable config line: {raw!r}")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            if not inner:
                parsed: Any = []
            else:
                parsed = [
                    _parse_scalar(p.strip())
                    for p in _split_top_level(inner)
                ]
        else:
            parsed = _parse_scalar(value)

        if pending_array_of_tables:
            section[key] = parsed
        else:
            section[key] = parsed

    return root


def _split_top_level(inner: str) -> list[str]:
    """Split a TOML array body on commas that are not inside quotes."""
    parts: list[str] = []
    current = ""
    in_quotes = False
    for ch in inner:
        if ch == '"':
            in_quotes = not in_quotes
            current += ch
        elif ch == "," and not in_quotes:
            parts.append(current)
            current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current)
    return parts


def _parse_scalar(value: str) -> Any:
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1].replace('\\"', '"')
    if value.startswith("'") and value.endswith("'") and len(value) >= 2:
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    raise ValueError(f"unparsable config value: {value!r}")


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base``, returning a new dict.

    Lists are replaced wholesale, not concatenated: a user who lists three
    detectors means exactly those three, not those three plus the defaults.
    """
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load(path: str | None = None, cwd: str | None = None) -> dict[str, Any]:
    """Load config, layered over :data:`DEFAULTS`.

    Passing an explicit ``path`` is an error if it does not exist, because the
    caller asked for that specific file. Auto-discovery is not: a config file
    that is simply absent is the normal case.
    """
    WARNINGS.clear()
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}

    explicit = path is not None
    targets = [os.path.expanduser(path)] if explicit else candidate_paths(cwd)

    for target in targets:
        if not os.path.isfile(target):
            if explicit:
                raise FileNotFoundError(f"config not found: {target}")
            continue
        try:
            with open(target, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            WARNINGS.append(f"could not read config {target}: {exc}")
            continue

        try:
            parsed = _parse_toml(raw.decode("utf-8"))
        except Exception as exc:
            WARNINGS.append(f"ignoring malformed config {target}: {exc}")
            continue

        if not isinstance(parsed, dict):
            WARNINGS.append(f"ignoring config {target}: top level is not a table")
            continue

        merged = _deep_merge(merged, parsed)
        if not explicit:
            # First match wins for auto-discovery; do not keep layering.
            break

    _validate(merged)
    return merged


def _validate(cfg: dict[str, Any]) -> None:
    """Coerce and sanity-check values, appending warnings for anything absurd."""
    wrapper = cfg.setdefault("wrapper", {})
    params = wrapper.get("carrier_params")
    if not isinstance(params, list) or not params:
        wrapper["carrier_params"] = list(DEFAULTS["wrapper"]["carrier_params"])
        WARNINGS.append("wrapper.carrier_params was empty; using defaults")

    depth = wrapper.get("max_depth", 5)
    if not isinstance(depth, int) or depth < 1:
        wrapper["max_depth"] = 5
        WARNINGS.append("wrapper.max_depth must be a positive integer; using 5")
    elif depth > 50:
        wrapper["max_depth"] = 50
        WARNINGS.append("wrapper.max_depth capped at 50")

    detectors = wrapper.get("detector") or []
    if not isinstance(detectors, list):
        wrapper["detector"] = []
        WARNINGS.append("wrapper.detector must be a list; ignoring")
    else:
        clean = []
        for d in detectors:
            if isinstance(d, dict) and isinstance(d.get("match"), str) and d["match"]:
                clean.append({"name": str(d.get("name") or d["match"]), "match": d["match"]})
            else:
                WARNINGS.append(f"skipping malformed detector entry: {d!r}")
        wrapper["detector"] = clean

    limits = cfg.setdefault("limits", {})
    frac = limits.get("max_close_fraction", 0.4)
    if not isinstance(frac, (int, float)) or not (0 < frac <= 1):
        limits["max_close_fraction"] = 0.4
        WARNINGS.append("limits.max_close_fraction must be in (0, 1]; using 0.4")

    batch = cfg.setdefault("open", {}).get("default_batch", 20)
    if not isinstance(batch, int) or batch < 1:
        cfg["open"]["default_batch"] = 20
        WARNINGS.append("open.default_batch must be >= 1; using 20")


_cached: dict[str, Any] | None = None


def current(reload: bool = False) -> dict[str, Any]:
    """Process-wide cached config."""
    global _cached
    if _cached is None or reload:
        _cached = load()
    return _cached
