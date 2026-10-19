# qoregeo recipes

## Store locator: the 3 nearest stores to a customer

```bash
qoregeo call nearest '{"path": "stores.csv", "point": {"lat": 12.9716, "lng": 77.5946}, "k": 3}'
```

```python
from qoregeo import GeoEngine
geo = GeoEngine().load("stores.csv")
for f in geo.k_nearest((12.9716, 77.5946), k=3):
    print(f["properties"]["name"], f["properties"]["_distance"], "km")
```

## Delivery zone: who is inside 5 km, and a map to share

```python
geo = GeoEngine().load("customers.csv")
zone = geo.buffer((28.6139, 77.2090), 5)
inside = geo.filter_by_polygon(zone)
inside.save("in_zone.csv", csv_safe=True)
inside.map("zone.html", title="Customers within 5 km", overlays=[zone])
inside.image("zone.png", overlays=[zone])          # for e-mail / slides
```

## Geocode a short list of addresses, then map them

Public Nominatim: max 1 request/second, no bulk jobs. Fine for tens of rows; for
thousands run your own server (`Geocoder(url="http://localhost:8080")`).

```python
import csv
from qoregeo import GeoEngine
from qoregeo.geocode import Geocoder

g, geo = Geocoder(email="you@example.com"), GeoEngine()
with open("addresses.csv", encoding="utf-8") as fh:
    for row in csv.DictReader(fh):
        hit = g.geocode(row["address"], country_codes="in")
        if hit:
            geo.add_point(hit["lat"], hit["lng"], **row, match=hit["name"])
        else:
            print("not found:", row["address"])
geo.save("geocoded.csv")          # keep the results — don't geocode the same rows twice
geo.show(title="Geocoded addresses")
```

## Corridor along a route

```python
route = {"type": "LineString", "coordinates": [[77.21, 28.61], [75.79, 26.91], [72.88, 19.08]]}
along = geo.filter_by_line(route, distance=10)             # within 10 km of the road
print(geo.length(route), "km")
```

## Which regions does a pipeline cross?

```python
regions = GeoEngine().load("districts.geojson")
crossed = regions.filter_intersects(pipeline_linestring)
```

## Data across the 180° meridian (Fiji, Alaska, NZ)

Nothing special to do: filters, bbox (`west > east`), centroid, maps and images all
treat the data as one cluster.

## Heatmap of sales

```bash
qoregeo map sales.csv --heatmap --intensity revenue -o sales_heat.html --serve
```

## Export a filtered subset for GIS software

```bash
qoregeo export parcels.geojson -o big.geojson --where "area_ha>=10"
qoregeo export shops.csv -o shops.geojson          # CSV → GeoJSON (QGIS, Kepler, Mapbox)
```

## Look at the result (agents)

After any filter, render a preview and check the points are where you expect:

```bash
qoregeo image result.geojson -o check.png
```

or the `preview_map` MCP tool, which returns the PNG to you directly.
