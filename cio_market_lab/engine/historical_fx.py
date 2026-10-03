from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import csv
import hashlib
import io
import urllib.request
from typing import Iterable
from pathlib import Path
from urllib.parse import urlparse


class FxReportingBlocked(ValueError):
    """Historical FX evidence is absent or cannot support a conversion."""


@dataclass(frozen=True)
class FxRateReceipt:
    pair: str
    rate: Decimal | float | str
    observed_at: datetime
    source: str
    source_url: str
    source_date: date
    provenance: str = "OFFICIAL_SOURCE"

    def __post_init__(self):
        if self.pair not in {"USD/TWD", "TWD/USD"}:
            raise ValueError("pair must be USD/TWD or TWD/USD")
        try:
            rate = Decimal(str(self.rate))
        except (InvalidOperation, ValueError):
            raise ValueError("rate must be a positive finite number")
        if not rate.is_finite() or rate <= 0:
            raise ValueError("rate must be a positive finite number")
        object.__setattr__(self, "rate", rate)
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be a timezone-aware timestamp")
        if not isinstance(self.source_date, date):
            raise ValueError("source_date is required")
        if not self.source or not self.source.strip():
            raise ValueError("source is required")
        parsed = urlparse(self.source_url or "")
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("source_url must be a valid https URL")
        if self.provenance not in {"OFFICIAL_SOURCE", "TEST_ONLY"}:
            raise ValueError("provenance must be OFFICIAL_SOURCE or TEST_ONLY")


@dataclass(frozen=True)
class FxConversionReceipt:
    strategy_id: str
    converted_at: datetime
    native_amount: Decimal
    native_currency: str
    reporting_amount: Decimal
    reporting_currency: str
    rate: Decimal
    rate_pair: str
    rate_observed_at: datetime
    rate_source: str
    rate_source_url: str
    rate_source_date: date
    rate_provenance: str

    @property
    def fx_receipt(self) -> FxRateReceipt:
        """Reconstruct the exact typed rate evidence carried by this receipt."""
        return FxRateReceipt(
            pair=self.rate_pair, rate=self.rate, observed_at=self.rate_observed_at,
            source=self.rate_source, source_url=self.rate_source_url,
            source_date=self.rate_source_date, provenance=self.rate_provenance,
        )


@dataclass(frozen=True)
class NavReportingResult:
    strategy_id: str
    as_of: datetime
    native_amount: Decimal
    native_currency: str
    reporting_amount: Decimal
    reporting_currency: str
    conversion_receipt: FxConversionReceipt


def _aware(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FxReportingBlocked(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def acquire_fred_usd_twd(*, raw_path: str, observed_at: datetime | None = None,
                         timeout: float = 20.0) -> tuple[FxRateReceipt, dict]:
    """Fetch FRED DEXTAUS (TWD per USD), retaining exact CSV and its hash."""
    url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXTAUS"
    request_started = datetime.now(timezone.utc)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        if response.status != 200:
            raise FxReportingBlocked(f"FRED HTTP status {response.status}")
        raw = response.read()
    actual_observed = observed_at or datetime.now(timezone.utc)
    if actual_observed < request_started:
        raise FxReportingBlocked("observed_at cannot be backdated before acquisition")
    Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
    Path(raw_path).write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
    if reader.fieldnames != ["observation_date", "DEXTAUS"]:
        raise FxReportingBlocked("unexpected FRED DEXTAUS CSV schema")
    rows = [row for row in reader if row.get("DEXTAUS") not in (None, ".", "")]
    if not rows:
        raise FxReportingBlocked("FRED DEXTAUS has no observations")
    last = rows[-1]
    receipt = FxRateReceipt(
        pair="USD/TWD", rate=last["DEXTAUS"], observed_at=actual_observed,
        source="Federal Reserve Bank of St. Louis FRED DEXTAUS",
        source_url="https://fred.stlouisfed.org/series/DEXTAUS",
        source_date=date.fromisoformat(last["observation_date"]),
        provenance="OFFICIAL_SOURCE",
    )
    metadata = {
        "provider": "FRED / Board of Governors of the Federal Reserve System",
        "series_id": "DEXTAUS", "units": "Taiwan Dollars to One U.S. Dollar",
        "pair": "USD/TWD", "raw_path": str(raw_path),
        "raw_sha256": digest, "raw_bytes": len(raw),
        "observation_count_nonmissing": len(rows),
        "source_date": last["observation_date"],
        "acquired_observed_at": actual_observed.isoformat(),
    }
    return receipt, metadata


def lookup_rate_as_of(receipts: Iterable[FxRateReceipt], pair: str, as_of: datetime,
                      max_age: timedelta = timedelta(days=5)) -> FxRateReceipt:
    cutoff = _aware(as_of, "as_of")
    if pair not in {"USD/TWD", "TWD/USD"}:
        raise FxReportingBlocked("unsupported requested currency pair")
    if max_age.total_seconds() < 0:
        raise FxReportingBlocked("max_age must be nonnegative")
    candidates = [r for r in receipts if r.pair == pair]
    if not candidates:
        raise FxReportingBlocked(f"missing FX rate receipt for {pair}")
    eligible = []
    for receipt in candidates:
        observed = _aware(receipt.observed_at, "observed_at")
        if observed > cutoff or receipt.source_date > cutoff.date():
            continue
        eligible.append(receipt)
    if not eligible:
        raise FxReportingBlocked(f"future FX receipt for {pair}; no historical as-of rate")
    selected = max(eligible, key=lambda r: (r.source_date, _aware(r.observed_at, "observed_at")))
    age = cutoff - _aware(selected.observed_at, "observed_at")
    source_age = cutoff - datetime.combine(selected.source_date, datetime.min.time(), tzinfo=timezone.utc)
    if age > max_age or source_age > max_age:
        raise FxReportingBlocked(f"stale FX receipt for {pair}: age={age}")
    return selected


def convert_nav(*, strategy_id: str, native_amount: Decimal | float | str,
                native_currency: str, reporting_currency: str,
                rate_receipts: Iterable[FxRateReceipt], as_of: datetime,
                max_age: timedelta = timedelta(days=5)) -> NavReportingResult:
    if not strategy_id or not strategy_id.strip():
        raise FxReportingBlocked("strategy_id is required")
    if native_currency not in {"USD", "TWD"} or reporting_currency not in {"USD", "TWD"}:
        raise FxReportingBlocked("currency must be USD or TWD")
    if native_currency == reporting_currency:
        raise FxReportingBlocked("reporting conversion requires different currencies; native NAV is already reported in its currency")
    try:
        amount = Decimal(str(native_amount))
    except (InvalidOperation, ValueError):
        raise FxReportingBlocked("native_amount must be finite")
    if not amount.is_finite():
        raise FxReportingBlocked("native_amount must be finite")
    pair = f"{native_currency}/{reporting_currency}"
    selected = lookup_rate_as_of(rate_receipts, pair, as_of, max_age)
    reporting_amount = amount * selected.rate
    stamp = _aware(as_of, "as_of")
    conversion = FxConversionReceipt(
        strategy_id=strategy_id, converted_at=stamp, native_amount=amount,
        native_currency=native_currency, reporting_amount=reporting_amount,
        reporting_currency=reporting_currency, rate=selected.rate, rate_pair=selected.pair,
        rate_observed_at=_aware(selected.observed_at, "observed_at"),
        rate_source=selected.source, rate_source_url=selected.source_url,
        rate_source_date=selected.source_date, rate_provenance=selected.provenance,
    )
    return NavReportingResult(strategy_id, stamp, amount, native_currency,
                              reporting_amount, reporting_currency, conversion)
