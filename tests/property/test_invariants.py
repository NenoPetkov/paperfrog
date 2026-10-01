from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from paperfrog.domain.types import Filters, SafeError
from paperfrog.risk.engine import Proposal, RiskEngine
from paperfrog.settings import Settings


@given(st.integers(min_value=0, max_value=16))
def test_configuration_cap_bounded(cap):
    assert (
        Settings(_env_file=None, max_strategy_entries_per_day=cap).max_strategy_entries_per_day
        <= 16
    )


@given(
    st.decimals(min_value="0", max_value="100000", allow_nan=False, allow_infinity=False, places=8)
)
def test_rounding_never_increases_quantity(quantity):
    f = Filters(Decimal(".001"), Decimal(".01"), Decimal(".001"), Decimal("100000"), Decimal("5"))
    result = f.round_quantity(quantity)
    assert Decimal(0) <= result <= quantity and result % f.step == 0


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=30)
@given(st.floats(min_value=0.500001, max_value=10, allow_nan=False, allow_infinity=False))
def test_gross_exposure_cannot_exceed_cap(store, health, now, ratio):
    s = Settings(_env_file=None)
    p = Proposal("p", "SOLUSDT", 10000, 500, 10000 * ratio, 10, 0, 0.01, 0.3, 0.001)
    with pytest.raises(SafeError):
        RiskEngine(s, store).reserve(p, health, now)
