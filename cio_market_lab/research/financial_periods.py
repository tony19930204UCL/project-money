"""Deterministic period/unit-preserving research derivations, never order signals."""
from datetime import date
from decimal import Decimal, InvalidOperation


def classify_flow_period(start, end):
    try:
        end_date = date.fromisoformat(str(end))
        if start is None:
            return 'instant'
        days = (end_date - date.fromisoformat(str(start))).days + 1
    except (TypeError, ValueError):
        return 'invalid'
    if days <= 0:
        return 'invalid'
    for lower, upper, label in [(75, 105, 'quarterly'), (165, 200, 'half_year'),
                                (250, 290, 'nine_month'), (330, 380, 'annual')]:
        if lower <= days <= upper:
            return label
    return 'irregular'


def aligned_cash_flow_derivations(baseline):
    """OCF minus cash PP&E payments, only within one fully attested interval.

    This is a transparent research calculation, not management's FCF metric.
    No annualization, lease adjustment, currency conversion or cross-filing join.
    """
    concepts = {'NetCashProvidedByUsedInOperatingActivities': 'ocf',
                'PaymentsToAcquirePropertyPlantAndEquipment': 'capex'}
    grouped = {}
    fields = ('unit', 'start', 'end', 'filed', 'form', 'accn')
    for row in baseline:
        role = concepts.get(row.get('concept'))
        if role is None or not all(row.get(key) for key in fields):
            continue
        unit = row['unit']
        if not isinstance(unit, str) or len(unit) != 3 or not unit.isalpha():
            continue
        kind = classify_flow_period(row['start'], row['end'])
        if kind in {'invalid', 'instant'}:
            continue
        try:
            value = Decimal(str(row['val']))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            continue
        if not value.is_finite():
            continue
        key = tuple(row[key] for key in fields)
        group = grouped.setdefault(key, {})
        group.setdefault(role, set()).add(value)
    results = []
    for key, group in grouped.items():
        if set(group) != {'ocf', 'capex'} or any(len(values) != 1 for values in group.values()):
            continue  # Conflicting facts are not resolved by iteration order.
        ocf, capex = next(iter(group['ocf'])), next(iter(group['capex']))
        results.append({**dict(zip(fields, key)),
            'classification': 'DERIVED_NOT_COMPANY_REPORTED',
            'formula': 'operating_cash_flow - property_plant_equipment_payments',
            'value_decimal': str(ocf - capex),
            'period_kind': classify_flow_period(key[1], key[2]),
            'input_concepts': list(concepts)})
    return results
