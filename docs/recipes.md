<div align="center">

[← API Reference](api-reference.md) · **Recipes** · [FAQ →](faq.md)

</div>

---

# Recipes

Complete, runnable solutions to problems that come up repeatedly. Copy one and
change the column names.

---

## Contents

1. [Nearest branch to every customer](#1-nearest-branch-to-every-customer)
2. [Delivery zone coverage](#2-delivery-zone-coverage)
3. [Which district is each record in?](#3-which-district-is-each-record-in)
4. [Plan a service round](#4-plan-a-service-round)
5. [Find the real clusters](#5-find-the-real-clusters)
6. [Clean a messy export](#6-clean-a-messy-export)
7. [Shapefile to web map, no GDAL](#7-shapefile-to-web-map-no-gdal)
8. [Corridor analysis along a route](#8-corridor-analysis-along-a-route)
9. [Where should the next depot go?](#9-where-should-the-next-depot-go)
10. [A daily report from cron](#10-a-daily-report-from-cron)
11. [Geocode an address list](#11-geocode-an-address-list)
12. [Speed up a large dataset](#12-speed-up-a-large-dataset)

---

## 1. Nearest branch to every customer

Assign each customer to their closest branch and flag anyone too far away to
serve.

```python
from qoregeo import GeoEngine

customers = GeoEngine().load("customers.csv").clean()
branches  = GeoEngine().load("branches.csv")

MAX_SERVICE_KM = 25
assigned, orphans = [], []

for customer in customers:
    lat, lng = customer["geometry"]["coordinates"][1], customer["geometry"]["coordinates"][0]
    match = branches.nearest((lat, lng))

    record = {
        **customer,
        "properties": {
            **customer["properties"],
            "branch": match["feature"]["properties"]["name"],
            "branch_km": match["distance"],
            "in_range": match["distance"] <= MAX_SERVICE_KM,
        },
    }
    (assigned if record["properties"]["in_range"] else orphans).append(record)

result = GeoEngine().load_data(assigned + orphans)
result.save("customers_assigned.geojson")

print(f"{len(assigned)} in range, {len(orphans)} beyond {MAX_SERVICE_KM} km")
print(result.value_counts("branch"))
result.map("assignment.html", colour_by="branch", tooltip_field="name")
```

Build the index first if `branches` is large: `branches.build_index()`.

---

## 2. Delivery zone coverage

Which orders fall inside a delivery radius, and how much of the zone is
actually being used?

```python
from qoregeo import GeoEngine

orders = GeoEngine().load("orders.csv")
kitchen = (28.6315, 77.2167)

zone = orders.buffer(kitchen, radius=5, unit="km")

inside  = orders.within(zone)
outside = orders.outside(zone)

print(f"Zone area: {orders.area(zone):.1f} km²")
print(f"Deliverable: {inside.count()} of {orders.count()} orders")
print(f"Rejected:    {outside.count()}")

# How far out are the rejects? That tells you what a bigger zone would buy.
near_misses = outside.filter_by_radius(*kitchen, radius=8).count()
print(f"{near_misses} more orders would fit in an 8 km zone")

GeoEngine().load_data(
    inside.get_features()
    + [{"type": "Feature", "geometry": zone, "properties": {"name": "delivery zone"}}]
).map("zone.html", title="Delivery Coverage")
```

---

## 3. Which district is each record in?

The standard spatial join, plus a check on what failed to match.

```python
from qoregeo import GeoEngine

incidents = GeoEngine().load("incidents.csv").clean()
districts = GeoEngine().load("districts.shp")

tagged = incidents.spatial_join(districts, keep=["name", "code"], prefix="district_")

matched   = tagged.query("district_name is not null")
unmatched = tagged.query("district_name is null")

print(f"{matched.count()} matched, {unmatched.count()} outside every district")
print(matched.value_counts("district_name"))

if unmatched.count():
    unmatched.save("unmatched.geojson")     # usually bad coordinates, worth a look

matched.save("incidents_by_district.geojson")
matched.map("incidents.html", colour_by="district_name")
```

---

## 4. Plan a service round

Order the stops, time the run, and draw it.

```python
from qoregeo import GeoEngine, travel_time
from qoregeo.routing import route_line

depot = (28.6139, 77.2090)
stops = GeoEngine().load("today.csv").query("status == 'pending'")

route = stops.optimise_route(start=depot, round_trip=True)

print(f"{stops.count()} stops, {route['total_distance']} km")
print(f"2-opt improved the greedy tour by {route['improvement_pct']}%")

schedule = travel_time(route["total_distance"], speed_kmh=35,
                       stop_minutes=10, stops=stops.count())
print(f"About {schedule['total_hours']} hours including stops")

for n, leg in enumerate(route["legs"], start=1):
    print(f"{n:3d}. {leg['distance']:7.1f} km {leg['direction']}")

GeoEngine().load_data(
    route["engine"].get_features()
    + [{"type": "Feature", "geometry": route_line(route), "properties": {"name": "route"}}]
).map("round.html", title="Service Round")
```

Remember these are straight-line distances. They order the stops correctly, but
the kilometre figure will be under the real road distance.

---

## 5. Find the real clusters

Cluster, then check the clustering is not an illusion.

```python
from qoregeo import GeoEngine

reports = GeoEngine().load("reports.csv").clean()

# Is there anything to find?
signal = reports.pattern()
print(f"Pattern: {signal['pattern']} (ratio {signal['ratio']})")

if signal["pattern"] != "clustered":
    print("No more clustered than random. Treat any grouping with suspicion.")

clustered = reports.cluster(eps_km=1.5, min_samples=8)
counts = clustered.value_counts("_cluster")
print(f"{len([k for k in counts if k != '-1'])} clusters, {counts.get('-1', 0)} noise")

# Profile each cluster
for label in sorted(k for k in counts if k != "-1"):
    group = clustered.query(f"_cluster == {label}")
    lat, lng = group.centroid()
    print(f"cluster {label}: {group.count():4d} reports "
          f"at {lat:.4f}, {lng:.4f}, spread {group.dispersion()['mean_km']:.2f} km")

clustered.query("_cluster != -1").map("clusters.html", colour_by="_cluster")
clustered.query("_cluster == -1").save("outliers.geojson")
```

---

## 6. Clean a messy export

The pipeline for a file you did not produce.

```python
from qoregeo import GeoEngine

raw = GeoEngine().load("export.csv")
print(f"Loaded {raw.count()} rows")

report = raw.validate()
print(f"{report['valid']} valid, {report['invalid']} broken, "
      f"{report['likely_swapped']} look like swapped coordinates")

for issue in report["issues"][:10]:
    print(f"  row {issue['index']}: {issue['problem']}")

clean = (raw
    .clean()                              # no geometry, no coordinates
    .fix_coordinates()                    # unambiguous lat/lng swaps
    .dropna(["name", "id"])               # rows missing essentials
    .dedupe(tolerance_km=0.03))           # the same place twice, 30 m apart

print(f"{clean.count()} usable ({raw.count() - clean.count()} dropped)")
clean.save("clean.geojson")
```

---

## 7. Shapefile to web map, no GDAL

```python
from qoregeo import GeoEngine

districts = (GeoEngine()
    .load("census_districts.shp")
    .simplify(tolerance=0.005))           # boundary files are far too detailed for a map

print(f"{districts.count()} districts, {districts.area():,.0f} km² total")

districts.choropleth("population.html",
                     value_col="POP2021",
                     label_col="DIST_NAME",
                     bins=6,
                     title="Population by District")

districts.save("districts.geojson")       # for the front end
districts.svg("districts.svg", theme="light")   # for the report
```

From the shell, the same conversion is one line:

```bash
qoregeo convert census_districts.shp districts.geojson
```

---

## 8. Corridor analysis along a route

Everything within a set distance of a road, river or pipeline.

```python
from qoregeo import GeoEngine

villages = GeoEngine().load("villages.csv")

alignment = [
    (28.6139, 77.2090),
    (27.1767, 78.0081),
    (26.4499, 80.3319),
    (25.3176, 82.9739),
]

corridor = villages.buffer_line(alignment, radius=10, unit="km")

affected = villages.within(corridor)
print(f"{affected.count()} villages within 10 km of the alignment")
print(f"Population affected: {affected.stats('population')['sum']:,.0f}")

affected.sort_by("population", reverse=True).head(20).save("priority.geojson")

GeoEngine().load_data(
    affected.get_features()
    + [{"type": "Feature", "geometry": corridor, "properties": {"name": "10 km corridor"}}]
).map("corridor.html", title="Alignment Impact")
```

---

## 9. Where should the next depot go?

```python
from qoregeo import GeoEngine

demand = GeoEngine().load("orders.csv").clean()

print("Unweighted centre:", demand.centroid())
print("Volume-weighted:  ", demand.centre_of_mass("order_count"))
print("Revenue-weighted: ", demand.centre_of_mass("revenue"))

# For several depots, cluster and take each centre.
result = demand.kmeans(k=3, seed=7)
for n, (lat, lng) in enumerate(result["centroids"]):
    served = result["engine"].query(f"_cluster == {n}")
    print(f"depot {n}: {lat:.4f}, {lng:.4f} serving {served.count()} orders, "
          f"mean {served.dispersion()['mean_km']:.1f} km away")

result["engine"].map("depots.html", colour_by="_cluster")
```

`inertia` falls as `k` rises. Compare a few values and pick where it stops
falling steeply.

---

## 10. A daily report from cron

```python
#!/usr/bin/env python3
"""Nightly spatial report. Writes to reports/YYYY-MM-DD/."""
from datetime import date
from pathlib import Path

from qoregeo import GeoEngine, QOREgeoError

OUT = Path("reports") / date.today().isoformat()
OUT.mkdir(parents=True, exist_ok=True)

try:
    data = GeoEngine().load("/data/live.csv").clean()
except QOREgeoError as exc:
    raise SystemExit(f"Could not load today's data:\n{exc}")

summary = data.describe()
(OUT / "summary.txt").write_text(
    f"{summary['features']} features\n"
    f"columns: {', '.join(summary['columns'])}\n"
    f"extent:  {summary['bounds']}\n"
    f"pattern: {data.pattern()['pattern']}\n"
)

data.map(OUT / "map.html", colour_by="region", quiet=True)
data.heatmap(OUT / "density.html", intensity_col="value", quiet=True)
data.png(OUT / "overview.png", theme="light")
data.save(OUT / "snapshot.geojson")

for cell in data.hotspots(cell_km=10, min_count=20)[:5]:
    print(f"{cell['count']:5d} at {cell['centre'][0]:.3f}, {cell['centre'][1]:.3f}")
```

`map()` and friends accept `Path` objects as well as strings.

---

## 11. Geocode an address list

Needs network access, and it is slow by design: the public service asks for one
request per second.

```python
from qoregeo import GeoEngine, Geocoder

USER_AGENT = "acme-store-locator/1.0 (ops@acme.example)"

stores = GeoEngine().load("stores_without_coords.csv",
                          lat_col="lat", lng_col="lng")

located = stores.geocode("address", user_agent=USER_AGENT, country="in")
found = located.query("_geocoded is not null")

print(f"Geocoded {found.count()} of {stores.count()}")
found.save("stores_located.geojson")
located.query("_geocoded is null").save("geocode_failures.csv")
```

One-off lookups:

```python
coder = Geocoder(user_agent=USER_AGENT)
result = coder.geocode("India Gate, New Delhi")
print(result["lat"], result["lng"], result["display_name"])

print(coder.reverse(28.6129, 77.2295)["display_name"])
```

For thousands of addresses, run your own Nominatim and point the geocoder at
it:

```python
coder = Geocoder(user_agent=USER_AGENT,
                 base_url="http://localhost:8080",
                 min_interval=0)
```

---

## 12. Speed up a large dataset

```python
from qoregeo import GeoEngine

geo = GeoEngine().load("500k_points.csv")

# 1 · Index once, query many times. Automatic at 500+ features,
#     but call it explicitly to control the resolution.
geo.build_index(cell_size_km=2)
print(geo.index_stats())

# 2 · Narrow with the cheapest filter first.
region = geo.filter_by_bbox(18, 72, 20, 74)      # bbox before radius
nearby = region.filter_by_radius(19.07, 72.87, radius=10)

# 3 · Drop columns you will not use before rendering.
nearby.select(["name", "value"]).map("out.html")

# 4 · Simplify boundaries before drawing or point-in-polygon work.
districts = GeoEngine().load("districts.shp").simplify(0.002)
```

| Operation | 50 000 points |
|---|---|
| Build the index | 79 ms |
| Radius query, brute force | 58 ms |
| Radius query, indexed | 0.21 ms |

The index is exact, not approximate. It returns precisely what a full scan
would.

---

## Next

| You want to… | Go to |
|---|---|
| Understand an operation in depth | [Documentation home](README.md) |
| Look up an exact signature | [API Reference](api-reference.md) |
| Fix an error | [FAQ &amp; Troubleshooting](faq.md) |

---

<div align="center">

[← API Reference](api-reference.md) · [Documentation home](README.md) · [FAQ →](faq.md)

</div>
