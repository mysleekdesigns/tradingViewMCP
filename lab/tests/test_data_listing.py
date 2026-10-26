"""Archive symbol listing (S3 pagination, months) and coin selection. Offline."""

from __future__ import annotations

import httpx
import pytest

from lab.data.listing import list_months, list_symbols, select_symbols

LIST_URL = "https://s3.example/data.binance.vision"
NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'


def page(prefixes: list[str], keys: list[str], next_marker: str | None) -> bytes:
    cps = "".join(f"<CommonPrefixes><Prefix>{p}</Prefix></CommonPrefixes>" for p in prefixes)
    contents = "".join(f"<Contents><Key>{k}</Key></Contents>" for k in keys)
    trunc = "true" if next_marker is not None else "false"
    nm = f"<NextMarker>{next_marker}</NextMarker>" if next_marker else ""
    return (
        f'<?xml version="1.0"?><ListBucketResult {NS}><IsTruncated>{trunc}</IsTruncated>'
        f"{nm}{cps}{contents}</ListBucketResult>"
    ).encode()


def client_for(pages: dict[str, bytes], seen: list[httpx.Request] | None = None) -> httpx.Client:
    """``pages`` keyed by the request's ``marker`` value."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        marker = request.url.params.get("marker", "")
        if marker not in pages:
            return httpx.Response(500)
        return httpx.Response(200, content=pages[marker])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_list_symbols_follows_pagination() -> None:
    pre = "data/spot/monthly/klines/"
    seen: list[httpx.Request] = []
    pages = {
        "": page([pre + "BTCUSDT/", pre + "ETHUSDT/"], [], next_marker=pre + "ETHUSDT/"),
        pre + "ETHUSDT/": page([pre + "LUNAUSDT/"], [], next_marker=None),
    }
    assert list_symbols(LIST_URL, client=client_for(pages, seen)) == [
        "BTCUSDT",
        "ETHUSDT",
        "LUNAUSDT",
    ]
    assert [r.url.params["delimiter"] for r in seen] == ["/", "/"]
    assert all(r.url.params["prefix"] == pre for r in seen)


def test_listing_that_never_advances_is_an_error() -> None:
    pre = "data/spot/monthly/klines/"
    pages = {
        "": page([pre + "BTCUSDT/"], [], next_marker=pre + "BTCUSDT/"),
        pre + "BTCUSDT/": page([pre + "ABCUSDT/"], [], next_marker=pre + "BTCUSDT/"),
    }
    with pytest.raises(ValueError, match="does not advance"):
        list_symbols(LIST_URL, client=client_for(pages))


def test_bad_xml_is_named() -> None:
    with pytest.raises(ValueError, match="not valid XML"):
        list_symbols(LIST_URL, client=client_for({"": b"<nope"}))


def test_list_months_keeps_only_this_interval_zips() -> None:
    folder = "data/spot/monthly/klines/LUNAUSDT/1d/"
    keys = [
        folder + "LUNAUSDT-1d-2022-05.zip",
        folder + "LUNAUSDT-1d-2022-05.zip.CHECKSUM",
        folder + "LUNAUSDT-1d-2021-12.zip",
        folder + "LUNAUSDT-1dx-2021-11.zip",
    ]
    seen: list[httpx.Request] = []
    months = list_months(
        LIST_URL, "LUNAUSDT", "1d", client=client_for({"": page([], keys, None)}, seen)
    )
    assert months == ["2021-12", "2022-05"]
    assert seen[0].url.params["prefix"] == folder
    assert "delimiter" not in seen[0].url.params


def test_select_symbols_drops_leveraged_and_excluded_bases() -> None:
    symbols = [
        "BTCUSDT", "BTCUPUSDT", "BTCDOWNUSDT", "XRPUSDT", "XRPBULLUSDT", "XRPBEARUSDT",
        "JUPUSDT", "SYRUPUSDT", "SYRUSDC", "USDCUSDT", "BULLUSDT", "ETHBTC", "USDT",
    ]  # fmt: skip
    sel = select_symbols(symbols, quote="USDT", exclude_bases=["usdc", "BULL"])
    assert sel.selected == ["BTCUSDT", "JUPUSDT", "SYRUPUSDT", "XRPUSDT"]
    assert sel.excluded == {
        "BTCDOWNUSDT": "leveraged token on BTC",
        "BTCUPUSDT": "leveraged token on BTC",
        "XRPBEARUSDT": "leveraged token on XRP",
        "XRPBULLUSDT": "leveraged token on XRP",
        "USDCUSDT": "excluded base (stablecoin, fiat, gold or wrapped duplicate)",
        "BULLUSDT": "excluded base (stablecoin, fiat, gold or wrapped duplicate)",
    }


def test_select_symbols_rejects_bad_quote() -> None:
    with pytest.raises(ValueError, match="invalid quote"):
        select_symbols(["BTCUSDT"], quote="US-DT", exclude_bases=[])


def test_select_symbols_drops_names_unsafe_as_paths() -> None:
    sel = select_symbols(
        ["币安人生USDT", "../XUSDT", "abcUSDT", "A_BUSDT"], quote="USDT", exclude_bases=[]
    )
    assert sel.selected == ["币安人生USDT"]
    assert set(sel.excluded) == {"../XUSDT", "abcUSDT", "A_BUSDT"}
    assert all(v.startswith("invalid symbol name") for v in sel.excluded.values())
