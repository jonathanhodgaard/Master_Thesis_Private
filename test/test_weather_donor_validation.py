import numpy as np
import pandas as pd
import pypsa
import pytest

from scripts.check_weather_donor import validate_donor


def donor():
    n = pypsa.Network()
    hours = pd.date_range("2019-07-01", "2020-07-01", freq="h", inclusive="left")
    hours = hours[~((hours.month == 2) & (hours.day == 29))]
    n.set_snapshots(hours)
    n.add("Bus", "DK0 0AC")
    n.add("Load", "transport", bus="DK0 0AC", p_set=1.0)
    return n


def test_valid_donor_uses_8760_hours_without_leap_day():
    n = donor()
    assert len(n.snapshots) == 8760
    validate_donor(n, "2019-07-01", "2020-07-01")


@pytest.mark.parametrize("invalid", [np.nan, np.inf])
def test_invalid_demand_is_rejected_with_asset_name(invalid):
    n = donor()
    n.loads_t.p_set["transport"] = 1.0
    n.loads_t.p_set.iloc[10, 0] = invalid
    with pytest.raises(ValueError, match=r"Load.p_set: 1 invalid values.*transport"):
        validate_donor(n, "2019-07-01", "2020-07-01")


def test_calendar_with_missing_hour_is_rejected():
    n = donor()
    n.set_snapshots(n.snapshots[:-1])
    with pytest.raises(ValueError, match="calendar"):
        validate_donor(n, "2019-07-01", "2020-07-01")
