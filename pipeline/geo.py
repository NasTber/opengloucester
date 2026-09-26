"""Point-in-polygon lookup for ward and precinct boundaries (GeoJSON, lon/lat)."""

from __future__ import annotations

import json
from pathlib import Path


def _in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(lon: float, lat: float, rings: list) -> bool:
    # First ring is the outer boundary; any others are holes.
    return _in_ring(lon, lat, rings[0]) and not any(_in_ring(lon, lat, hole) for hole in rings[1:])


def _bbox(geometry: dict) -> tuple[float, float, float, float]:
    polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    xs = [p[0] for poly in polys for p in poly[0]]
    ys = [p[1] for poly in polys for p in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


class PrecinctLookup:
    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.features = [(f, _bbox(f["geometry"])) for f in data["features"]]

    def find(self, lat: float | None, lon: float | None) -> dict | None:
        """Return the feature properties (ward, precinct, ...) containing the point, or None."""
        if lat is None or lon is None:
            return None
        for feature, (x0, y0, x1, y1) in self.features:
            if not (x0 <= lon <= x1 and y0 <= lat <= y1):
                continue
            geometry = feature["geometry"]
            polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
            if any(_in_polygon(lon, lat, rings) for rings in polys):
                return feature["properties"]
        return None
