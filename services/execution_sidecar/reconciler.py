from __future__ import annotations
from services.common.audit import audit


def reconcile(adapter):
    """Endpoint-complete reconciliation entry point (H-005).

    Uses the adapter's verified reconciliation when available so a failed
    exchange enumeration is reported as a failure instead of success text.
    """
    if hasattr(adapter, 'verified_reconcile'):
        result = adapter.verified_reconcile()
        audit('reconcile_requested', details=result)
        return result['detail'] if result.get('ok') else 'failed: ' + str(result.get('detail'))
    result = adapter.reconcile()
    audit('reconcile_requested', details={'result': result})
    return result
