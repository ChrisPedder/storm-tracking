"""Download ERA5 reanalysis data from the Copernicus Climate Data Store."""

import logging
import os
import sys
import tempfile

import boto3
import cdsapi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("era5-downloader")

S3_BUCKET = os.environ["S3_BUCKET"]
S3_PREFIX = os.environ.get("S3_PREFIX", "raw/era5/")
START_YEAR = int(os.environ.get("START_YEAR", "2010"))
END_YEAR = int(os.environ.get("END_YEAR", "2023"))
BBOX_NORTH = float(os.environ.get("BBOX_NORTH", "48.3"))
BBOX_SOUTH = float(os.environ.get("BBOX_SOUTH", "45.3"))
BBOX_WEST = float(os.environ.get("BBOX_WEST", "5.5"))
BBOX_EAST = float(os.environ.get("BBOX_EAST", "11.0"))

CDS_API_URL = os.environ.get("CDS_API_URL", "https://cds.climate.copernicus.eu/api")
CDS_API_KEY = os.environ["CDS_API_KEY"]

AREA = [BBOX_NORTH, BBOX_WEST, BBOX_SOUTH, BBOX_EAST]  # N, W, S, E

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

ALL_HOURS = [f"{h:02d}:00" for h in range(24)]

s3 = boto3.client("s3")


def s3_key_single(year: int, month: int) -> str:
    return f"{S3_PREFIX}single-levels/{year}/{month:02d}.grib"


def s3_key_pressure(year: int, month: int) -> str:
    return f"{S3_PREFIX}pressure-levels/{year}/{month:02d}.grib"


def already_exists(key: str) -> bool:
    try:
        s3.head_object(Bucket=S3_BUCKET, Key=key)
        return True
    except s3.exceptions.ClientError:
        return False


def days_in_month(year: int, month: int) -> list[str]:
    import calendar
    n = calendar.monthrange(year, month)[1]
    return [f"{d:02d}" for d in range(1, n + 1)]


def download_and_upload(
    client: cdsapi.Client,
    dataset: str,
    request: dict,
    s3_dest: str,
) -> None:
    """Download a CDS dataset to a temp file, then upload to S3."""
    with tempfile.NamedTemporaryFile(suffix=".grib", delete=True) as tmp:
        logger.info("Requesting %s -> %s", dataset, s3_dest)
        client.retrieve(dataset, request, tmp.name)
        file_size = os.path.getsize(tmp.name)
        logger.info("Downloaded %.1f MB, uploading to S3", file_size / 1e6)
        s3.upload_file(tmp.name, S3_BUCKET, s3_dest)
    logger.info("Uploaded to s3://%s/%s", S3_BUCKET, s3_dest)


def download_single_levels(client: cdsapi.Client, year: int, month: int) -> None:
    key = s3_key_single(year, month)
    if already_exists(key):
        logger.info("Skipping single-levels %d-%02d (exists)", year, month)
        return

    request = {
        "product_type": ["reanalysis"],
        "variable": SINGLE_LEVEL_VARIABLES,
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": days_in_month(year, month),
        "time": ALL_HOURS,
        "data_format": "grib",
        "area": AREA,
    }
    download_and_upload(
        client, "reanalysis-era5-single-levels", request, key,
    )


def download_pressure_levels(client: cdsapi.Client, year: int, month: int) -> None:
    key = s3_key_pressure(year, month)
    if already_exists(key):
        logger.info("Skipping pressure-levels %d-%02d (exists)", year, month)
        return

    request = {
        "product_type": ["reanalysis"],
        "variable": PRESSURE_LEVEL_VARIABLES,
        "pressure_level": PRESSURE_LEVELS,
        "year": [str(year)],
        "month": [f"{month:02d}"],
        "day": days_in_month(year, month),
        "time": ALL_HOURS,
        "data_format": "grib",
        "area": AREA,
    }
    download_and_upload(
        client, "reanalysis-era5-pressure-levels", request, key,
    )


def main() -> None:
    logger.info(
        "ERA5 downloader starting: years=%d-%d area=%s",
        START_YEAR, END_YEAR, AREA,
    )

    client = cdsapi.Client(url=CDS_API_URL, key=CDS_API_KEY)

    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            download_single_levels(client, year, month)
            download_pressure_levels(client, year, month)

    logger.info("ERA5 downloader complete")


if __name__ == "__main__":
    main()
