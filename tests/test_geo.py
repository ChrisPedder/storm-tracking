"""Tests for storm_tracking.geo module."""

from storm_tracking.geo import (
    grid_neighbours,
    snap_to_grid,
    tile_name,
    tiles_for_bbox,
)


class TestTileName:
    def test_positive_coordinates(self):
        assert tile_name(46, 7) == "Copernicus_DSM_COG_10_N46_00_E007_00_DEM"

    def test_negative_latitude(self):
        assert tile_name(-1, 10) == "Copernicus_DSM_COG_10_S01_00_E010_00_DEM"

    def test_negative_longitude(self):
        assert tile_name(46, -1) == "Copernicus_DSM_COG_10_N46_00_W001_00_DEM"

    def test_both_negative(self):
        assert tile_name(-5, -120) == "Copernicus_DSM_COG_10_S05_00_W120_00_DEM"

    def test_zero_coordinates(self):
        result = tile_name(0, 0)
        assert result == "Copernicus_DSM_COG_10_N00_00_E000_00_DEM"


class TestTilesForBbox:
    def test_single_tile(self):
        tiles = tiles_for_bbox(46.5, 46.1, 7.5, 7.1)
        assert len(tiles) == 1
        assert tiles[0] == "Copernicus_DSM_COG_10_N46_00_E007_00_DEM"

    def test_four_tiles(self):
        tiles = tiles_for_bbox(47.5, 46.5, 8.5, 7.5)
        assert len(tiles) == 4

    def test_switzerland_coverage(self):
        tiles = tiles_for_bbox(48.3, 45.3, 11.0, 5.5)
        lat_count = 48 - 45 + 1  # floor(48.3) - floor(45.3) + 1 = 4
        lon_count = 11 - 5 + 1   # floor(11.0) - floor(5.5) + 1 = 7
        # floor(11.0) = 11, floor(5.5) = 5 => 11 - 5 + 1 = 7
        # floor(48.3) = 48, floor(45.3) = 45 => 48 - 45 + 1 = 4
        assert len(tiles) == lat_count * lon_count

    def test_contains_expected_tile(self):
        tiles = tiles_for_bbox(48.3, 45.3, 11.0, 5.5)
        assert "Copernicus_DSM_COG_10_N46_00_E007_00_DEM" in tiles


class TestSnapToGrid:
    def test_exact_grid_point(self):
        assert snap_to_grid(47.0, 8.0) == (47.0, 8.0)

    def test_rounds_to_nearest(self):
        assert snap_to_grid(46.83, 7.42) == (46.75, 7.5)

    def test_rounds_down(self):
        assert snap_to_grid(46.1, 7.1) == (46.0, 7.0)

    def test_rounds_up(self):
        assert snap_to_grid(46.88, 7.88) == (47.0, 8.0)
        assert snap_to_grid(46.9, 7.9) == (47.0, 8.0)

    def test_custom_resolution(self):
        assert snap_to_grid(46.55, 7.55, resolution=0.5) == (46.5, 7.5)


class TestGridNeighbours:
    def test_returns_nine_cells(self):
        result = grid_neighbours(47.0, 8.0)
        assert len(result) == 9

    def test_centre_is_first(self):
        result = grid_neighbours(47.0, 8.0)
        assert result[0] == ("C", 47.0, 8.0)

    def test_north_offset(self):
        result = grid_neighbours(47.0, 8.0, resolution=0.25)
        directions = {d: (la, lo) for d, la, lo in result}
        assert directions["N"] == (47.25, 8.0)

    def test_all_directions_present(self):
        result = grid_neighbours(47.0, 8.0)
        dirs = {d for d, _, _ in result}
        assert dirs == {"C", "N", "NE", "E", "SE", "S", "SW", "W", "NW"}

    def test_custom_resolution(self):
        result = grid_neighbours(47.0, 8.0, resolution=0.5)
        directions = {d: (la, lo) for d, la, lo in result}
        assert directions["N"] == (47.5, 8.0)
        assert directions["NE"] == (47.5, 8.5)
