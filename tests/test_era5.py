"""Tests for storm_tracking.era5 module."""

from storm_tracking.era5 import (
    PRESSURE_LEVEL_VARIABLES,
    PRESSURE_LEVELS,
    SINGLE_LEVEL_VARIABLES,
    days_in_month,
    era5_area,
    pressure_level_s3_key,
    single_level_s3_key,
)


class TestDaysInMonth:
    def test_january(self):
        result = days_in_month(2023, 1)
        assert len(result) == 31
        assert result[0] == "01"
        assert result[-1] == "31"

    def test_february_common_year(self):
        assert len(days_in_month(2023, 2)) == 28

    def test_february_leap_year(self):
        assert len(days_in_month(2020, 2)) == 29

    def test_february_century_non_leap(self):
        assert len(days_in_month(1900, 2)) == 28

    def test_february_400_year_leap(self):
        assert len(days_in_month(2000, 2)) == 29

    def test_april(self):
        assert len(days_in_month(2023, 4)) == 30

    def test_values_are_zero_padded(self):
        result = days_in_month(2023, 1)
        assert result[0] == "01"
        assert result[8] == "09"
        assert result[9] == "10"


class TestEra5Area:
    def test_standard_bbox(self):
        result = era5_area(48.3, 45.3, 11.0, 5.5)
        assert result == [48.3, 5.5, 45.3, 11.0]

    def test_order_is_nwse(self):
        north, south, east, west = 50.0, 40.0, 15.0, 5.0
        result = era5_area(north, south, east, west)
        assert result[0] == north
        assert result[1] == west
        assert result[2] == south
        assert result[3] == east


class TestS3Keys:
    def test_single_level_key(self):
        result = single_level_s3_key("raw/era5/", 2020, 3)
        assert result == "raw/era5/single-levels/2020/03.grib"

    def test_pressure_level_key(self):
        result = pressure_level_s3_key("raw/era5/", 2020, 12)
        assert result == "raw/era5/pressure-levels/2020/12.grib"

    def test_single_digit_month_zero_padded(self):
        result = single_level_s3_key("raw/era5/", 2023, 1)
        assert "/01.grib" in result


class TestVariableConfigs:
    def test_cape_in_single_level_vars(self):
        assert "convective_available_potential_energy" in SINGLE_LEVEL_VARIABLES

    def test_cin_in_single_level_vars(self):
        assert "convective_inhibition" in SINGLE_LEVEL_VARIABLES

    def test_temperature_in_pressure_vars(self):
        assert "temperature" in PRESSURE_LEVEL_VARIABLES

    def test_geopotential_in_pressure_vars(self):
        assert "geopotential" in PRESSURE_LEVEL_VARIABLES

    def test_pressure_levels_include_key_levels(self):
        assert "500" in PRESSURE_LEVELS
        assert "850" in PRESSURE_LEVELS
