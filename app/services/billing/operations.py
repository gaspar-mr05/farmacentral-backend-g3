from datetime import timedelta

from app.clients.billing import BillingRateLimitError
from app.models import BillingSyncState


def block_billing(session, period_id, stream, exc, retry_at, now):
    if getattr(exc, "status_code", None) not in {401, 403, 429} and not isinstance(
        exc, BillingRateLimitError
    ):
        return False
    state = session.get(BillingSyncState, (period_id, stream))
    if state is None:
        state = BillingSyncState(period_id=period_id, stream=stream)
        session.add(state)
    state.last_attempt_at = now
    state.last_error = type(exc).__name__
    state.http_status = getattr(exc, "status_code", None)
    state.next_retry_at = retry_at or now + timedelta(minutes=1)
    session.commit()
    return True
