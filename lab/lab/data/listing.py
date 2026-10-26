"""Symbols in the Binance public archive, delisted ones included.

``data.binance.vision`` is served from an S3 bucket whose ListObjects API
(``{list_url}?delimiter=/&prefix=data/spot/monthly/klines/``) returns every symbol folder that
ever had data. A universe built from that listing keeps LUNA, FTT and every other delisted coin,
so it is free of survivorship bias. Pages hold at most 1000 entries and continue via ``marker``.

Market data needs no API key.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass, field

import httpx

from lab.data.archive import _check_symbol

SPOT_KLINES_PREFIX = "data/spot/monthly/klines/"
LEVERAGED_SUFFIXES: tuple[str, ...] = ("UP", "DOWN", "BULL", "BEAR")
_MAX_PAGES = 1000  # 1M entries; a listing that never stops truncating is a bug, not data


def _ns(root: ET.Element) -> str:
    return root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""


def _list(
    list_url: str, prefix: str, *, client: httpx.Client, delimiter: bool
) -> tuple[list[str], list[str]]:
    """``(common prefixes, keys)`` under ``prefix``, following ``NextMarker`` pagination."""
    prefixes: list[str] = []
    keys: list[str] = []
    marker = ""
    for _ in range(_MAX_PAGES):
        params = {"prefix": prefix, "marker": marker}
        if delimiter:
            params["delimiter"] = "/"
        resp = client.get(list_url, params=params)
        resp.raise_for_status()
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise ValueError(f"archive listing for {prefix!r} is not valid XML: {exc}") from exc
        ns = _ns(root)
        page_prefixes = [e.text or "" for e in root.findall(f"{ns}CommonPrefixes/{ns}Prefix")]
        page_keys = [e.text or "" for e in root.findall(f"{ns}Contents/{ns}Key")]
        prefixes += page_prefixes
        keys += page_keys
        if (root.findtext(f"{ns}IsTruncated") or "").strip().lower() != "true":
            return prefixes, keys
        nxt = root.findtext(f"{ns}NextMarker") or max(page_prefixes + page_keys, default="")
        if not nxt or nxt <= marker:
            raise ValueError(f"archive listing for {prefix!r} is truncated but does not advance")
        marker = nxt
    raise ValueError(f"archive listing for {prefix!r} exceeded {_MAX_PAGES} pages")


def list_symbols(
    list_url: str, *, client: httpx.Client, prefix: str = SPOT_KLINES_PREFIX
) -> list[str]:
    """Every symbol folder under ``prefix`` (sorted), delisted symbols included."""
    prefixes, _ = _list(list_url, prefix, client=client, delimiter=True)
    out = {p[len(prefix) :].strip("/") for p in prefixes if p.startswith(prefix)}
    return sorted(s for s in out if s)


def list_months(
    list_url: str,
    symbol: str,
    interval: str,
    *,
    client: httpx.Client,
    prefix: str = SPOT_KLINES_PREFIX,
) -> list[str]:
    """``YYYY-MM`` months with a monthly kline zip for ``symbol``/``interval`` (sorted)."""
    folder = f"{prefix}{symbol}/{interval}/"
    _, keys = _list(list_url, folder, client=client, delimiter=False)
    pattern = re.compile(
        rf"^{re.escape(folder + symbol)}-{re.escape(interval)}-(\d{{4}}-\d{{2}})\.zip$"
    )
    return sorted({m.group(1) for k in keys if (m := pattern.match(k))})


@dataclass(frozen=True)
class SymbolSelection:
    selected: list[str]
    excluded: dict[str, str] = field(default_factory=dict)  # symbol -> reason


def select_symbols(
    symbols: Iterable[str], *, quote: str, exclude_bases: Iterable[str]
) -> SymbolSelection:
    """Keep ``*{quote}`` symbols that are ordinary coins.

    Dropped, with a reason: bases in ``exclude_bases`` (stablecoins, fiat, gold, wrapped
    duplicates, configured in lab.toml) and leveraged tokens, i.e. a base that is another listed
    base plus ``UP``/``DOWN``/``BULL``/``BEAR`` (``BTCUP``, ``XRPBEAR``). The root must itself be
    listed, so real coins that merely end in those letters (``JUP``, ``SYRUP``) stay in.
    """
    if not quote or not quote.isalnum():
        raise ValueError(f"invalid quote {quote!r}; expected e.g. 'USDT'")
    excluded_bases = {b.upper() for b in exclude_bases}
    by_base = {
        s[: -len(quote)]: s for s in set(symbols) if s.endswith(quote) and len(s) > len(quote)
    }
    bases = set(by_base)
    selected: list[str] = []
    excluded: dict[str, str] = {}
    for base in sorted(bases):
        symbol = by_base[base]
        try:
            _check_symbol(symbol)
        except ValueError:
            excluded[symbol] = "invalid symbol name (not letters/digits, or lowercase)"
            continue
        if base in excluded_bases:
            excluded[symbol] = "excluded base (stablecoin, fiat, gold or wrapped duplicate)"
            continue
        root = next((base[: -len(sfx)] for sfx in LEVERAGED_SUFFIXES if base.endswith(sfx)), None)
        if root is not None and len(root) >= 2 and root in bases:
            excluded[symbol] = f"leveraged token on {root}"
            continue
        selected.append(symbol)
    return SymbolSelection(selected=selected, excluded=excluded)
