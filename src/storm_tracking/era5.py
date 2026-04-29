"""ERA5 variable configuration and helper functions."""

import calendar

SINGLE_LEVEL_VARIABLES = [
    "2m_temperature",
    "2m_dewpoint_temperature",
    "10m_u_component_of_wind",
    "10m_v_component_of_wind",
    "surface_pressure",
    "mean_sea_level_pressure",
    "total_column_water_vapour",
    "convective_available_potential_energy",
    "convective_inhibition",
    "total_precipitation",
    "boundary_layer_height",
    "convective_precipitation",
    "k_index",
    "total_totals_index",
]

PRESSURE_LEVELS = ["500", "700", "850", "925"]

PRESSURE_LEVEL_VARIABLES = [
    "temperature",
    "specific_humidity",
    "u_component_of_wind",
    "v_component_of_wind",
    "geopotential",
]


def days_in_month(year: int, month: int) -> list[str]:
    """Return zero-padded day strings for every day in the given month.

    >>> days_in_month(2020, 2)
    ['01', '02', '03', '04', '05', '06', '07', '08', '09', '10', '11', '12', '13', '14', '15', '16', '17', '18', '19', '20', '21', '22', '23', '24', '25', '26', '27', '28', '29']
    >>> len(days_in_month(2023, 2))
    28
    """
    n = calendar.monthrange(year, month)[1]
    return [f"{d:02d}" for d in range(1, n + 1)]


def era5_area(
    north: float, south: float, east: float, west: float,
) -> list[float]:
    """Format a bounding box as an ERA5 area parameter [N, W, S, E].

    >>> era5_area(48.3, 45.3, 11.0, 5.5)
    [48.3, 5.5, 45.3, 11.0]
    """
    return [north, west, south, east]


def single_level_s3_key(prefix: str, year: int, month: int) -> str:
    """Build the S3 key for a single-levels ERA5 file.

    >>> single_level_s3_key('raw/era5/', 2020, 3)
    'raw/era5/single-levels/2020/03.grib'
    """
    return f"{prefix}single-levels/{year}/{month:02d}.grib"


def pressure_level_s3_key(prefix: str, year: int, month: int) -> str:
    """Build the S3 key for a pressure-levels ERA5 file.

    >>> pressure_level_s3_key('raw/era5/', 2020, 3)
    'raw/era5/pressure-levels/2020/03.grib'
    """
    return f"{prefix}pressure-levels/{year}/{month:02d}.grib"
