"""Geographic utility functions for the storm tracking pipeline."""

import math


def tile_name(lat: int, lon: int) -> str:
    """Build the Copernicus DEM GLO-30 tile directory name for a 1x1 degree cell.

    >>> tile_name(46, 7)
    'Copernicus_DSM_COG_10_N46_00_E007_00_DEM'
    >>> tile_name(-1, -10)
    'Copernicus_DSM_COG_10_S01_00_W010_00_DEM'
    """
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM"


def tiles_for_bbox(
    north: float, south: float, east: float, west: float,
) -> list[str]:
    """Return all 1x1 degree Copernicus DEM tile names covering a bounding box.

    >>> sorted(tiles_for_bbox(47.5, 46.5, 8.5, 7.5))
    ['Copernicus_DSM_COG_10_N46_00_E007_00_DEM', 'Copernicus_DSM_COG_10_N46_00_E008_00_DEM', 'Copernicus_DSM_COG_10_N47_00_E007_00_DEM', 'Copernicus_DSM_COG_10_N47_00_E008_00_DEM']
    """
    lat_min = math.floor(south)
    lat_max = math.floor(north)
    lon_min = math.floor(west)
    lon_max = math.floor(east)

    return [
        tile_name(lat, lon)
        for lat in range(lat_min, lat_max + 1)
        for lon in range(lon_min, lon_max + 1)
    ]


def snap_to_grid(lat: float, lon: float, resolution: float = 0.25) -> tuple[float, float]:
    """Snap a coordinate to the nearest ERA5 grid point.

    >>> snap_to_grid(46.83, 7.42)
    (46.75, 7.5)
    >>> snap_to_grid(47.0, 8.0)
    (47.0, 8.0)
    """
    snapped_lat = round(round(lat / resolution) * resolution, 4)
    snapped_lon = round(round(lon / resolution) * resolution, 4)
    return snapped_lat, snapped_lon


def grid_neighbours(
    lat: float, lon: float, resolution: float = 0.25,
) -> list[tuple[str, float, float]]:
    """Return the 3x3 grid patch (centre + 8 neighbours) around a grid point.

    Returns a list of (direction, lat, lon) tuples. Directions are compass
    labels: C, N, NE, E, SE, S, SW, W, NW.

    >>> neighbours = grid_neighbours(47.0, 8.0)
    >>> len(neighbours)
    9
    >>> neighbours[0]
    ('C', 47.0, 8.0)
    """
    offsets = [
        ("C", 0, 0),
        ("N", 1, 0),
        ("NE", 1, 1),
        ("E", 0, 1),
        ("SE", -1, 1),
        ("S", -1, 0),
        ("SW", -1, -1),
        ("W", 0, -1),
        ("NW", 1, -1),
    ]
    return [
        (direction, round(lat + dlat * resolution, 4), round(lon + dlon * resolution, 4))
        for direction, dlat, dlon in offsets
    ]
