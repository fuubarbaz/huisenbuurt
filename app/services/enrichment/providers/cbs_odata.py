"""Minimal CBS StatLine OData client.

Exists because two non-obvious CBS behaviours would otherwise be re-derived in
every provider that touches StatLine:

1. ``WijkenEnBuurten`` keys are **space-padded to 10 characters**. Filtering on
   an unpadded ``'GM0363'`` returns an empty list rather than an error, so the
   bug is silent and looks like missing data.
2. String columns come back padded too (``'Amsterdam      '``), and suppressed
   values for small areas come back as ``null`` rather than being absent.

Two further OData3 limitations, both verified against 86165NED, that matter to
anyone extending this client:

* ``$skip`` is **not supported** — it returns HTTP 500. Full-table reads have
  to be partitioned with ``startswith`` prefix filters instead of paged.
* Range comparisons on ``WijkenEnBuurten`` (``ge`` / ``lt``) are **silently
  ignored**: the server returns unfiltered head rows with a 200, so a broken
  query looks like real data. Only ``eq``, ``startswith`` and ``substringof``
  are trustworthy on this column.
* ``$top`` is capped at 5000 rows per request.
"""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

from app.core.http_client import HttpClient

log = logging.getLogger(__name__)

#: CBS's own tables (Kerncijfers wijken en buurten, etc).
OPENDATA_BASE = "https://opendata.cbs.nl/ODataApi/odata"

#: Third-party tables published through CBS — Politie's crime figures live here.
DATADERDEN_BASE = "https://dataderden.cbs.nl/ODataApi/odata"

#: CBS pads every dimension key to a fixed width, but the width differs *per
#: dimension*, not per table: WijkenEnBuurten is 10, SoortMisdrijf is 6.
#: Getting it wrong yields an empty 200, never an error.
AREA_KEY_WIDTH = 10
CRIME_KEY_WIDTH = 6

#: The national aggregate row, useful as a baseline for any rate.
NATIONAL_CODE = "NL00"

#: Server-side cap on rows per request; larger values return HTTP 500.
MAX_ROWS_PER_REQUEST = 5000


def pad(code: str, width: int) -> str:
    """Pad a CBS dimension key to the width that dimension is stored at."""
    return code.strip().ljust(width)


def pad_area(code: str) -> str:
    """Pad a CBS area code to the stored 10-character width."""
    return pad(code, AREA_KEY_WIDTH)


def any_of(field: str, values: Iterable[str], width: int) -> str:
    """Build an OData `(f eq 'a' or f eq 'b' ...)` clause with correct padding.

    Enumerated equality rather than a range, because ``ge``/``lt`` is honoured
    on some columns and silently ignored on others (see the module docstring).
    """
    clause = " or ".join(f"{field} eq '{pad(v, width)}'" for v in values)
    return f"({clause})"


def clean(value: Any) -> Any:
    """Strip CBS's fixed-width padding from strings; pass everything else through."""
    return value.strip() if isinstance(value, str) else value


class CBSODataClient:
    """Reads one StatLine table via the OData3 endpoint."""

    def __init__(self, http: HttpClient, dataset: str, base_url: str = OPENDATA_BASE) -> None:
        self.http = http
        self.dataset = dataset
        self.base_url = base_url.rstrip("/")

    @property
    def table_url(self) -> str:
        return f"{self.base_url}/{self.dataset}"

    async def rows_for_areas(
        self,
        area_codes: Sequence[str],
        select: Iterable[str],
        *,
        extra_filter: Optional[str] = None,
    ) -> dict[str, dict[str, Any]]:
        """Fetch several areas in one request, keyed by their unpadded code.

        Fetching the buurt, its wijk, its gemeente and the national row together
        costs one round-trip instead of four, which matters because the fallback
        chain needs them all anyway whenever a small buurt is suppressed.
        """
        if not area_codes:
            return {}

        fields = list(dict.fromkeys(["WijkenEnBuurten", *select]))
        area_clause = " or ".join(
            f"WijkenEnBuurten eq '{pad_area(code)}'" for code in area_codes
        )
        filt = f"({area_clause})"
        if extra_filter:
            filt = f"{filt} and ({extra_filter})"

        payload = await self.http.get_json(
            f"{self.table_url}/TypedDataSet",
            params={"$filter": filt, "$select": ",".join(fields), "$format": "json"},
            headers={"Accept": "application/json"},
        )

        out: dict[str, dict[str, Any]] = {}
        for row in payload.get("value") or []:
            cleaned = {k: clean(v) for k, v in row.items()}
            key = cleaned.get("WijkenEnBuurten")
            if key:
                out[key] = cleaned
        return out

    async def query(
        self,
        *,
        filter: str,
        select: Optional[Iterable[str]] = None,
        top: int = MAX_ROWS_PER_REQUEST,
    ) -> list[dict[str, Any]]:
        """Run one filtered TypedDataSet read, returning unpadded rows.

        Callers must keep the result under ``MAX_ROWS_PER_REQUEST``; there is no
        paging fallback because ``$skip`` is unsupported.
        """
        params: dict[str, Any] = {"$filter": filter, "$format": "json", "$top": top}
        if select:
            params["$select"] = ",".join(select)
        payload = await self.http.get_json(
            f"{self.table_url}/TypedDataSet",
            params=params,
            headers={"Accept": "application/json"},
        )
        rows = payload.get("value") or []
        if len(rows) >= top:
            log.warning(
                "%s returned %d rows, at or above the %d cap — result may be truncated",
                self.dataset, len(rows), top,
            )
        return [{k: clean(v) for k, v in row.items()} for row in rows]

    async def dimension_keys(self, dimension: str) -> list[str]:
        """List the (unpadded) keys of a dimension sub-table, in table order."""
        payload = await self.http.get_json(
            f"{self.table_url}/{dimension}",
            params={"$format": "json", "$top": MAX_ROWS_PER_REQUEST},
            headers={"Accept": "application/json"},
        )
        return [clean(r["Key"]) for r in (payload.get("value") or []) if r.get("Key")]

    @staticmethod
    def pct(numerator: Any, denominator: Any) -> Optional[float]:
        """Percentage guarded against CBS's nulls and zero denominators."""
        if numerator is None or not denominator:
            return None
        return round(100.0 * float(numerator) / float(denominator), 1)
