"""
qoregeo.map_builder
===================
Generates standalone HTML files containing Leaflet.js interactive maps.
No server required — just open in a browser.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

Feature = Dict[str, Any]
Coord   = Tuple[float, float]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _auto_center(features: List[Feature]) -> Coord:
    """Return the centroid of all features."""
    lats = [f["geometry"]["coordinates"][1] for f in features]
    lngs = [f["geometry"]["coordinates"][0] for f in features]
    return (sum(lats) / len(lats), sum(lngs) / len(lngs))


def _props_to_popup(props: Dict[str, Any]) -> str:
    """Format a properties dict into an HTML popup string."""
    rows = "".join(
        f"<tr><td><b>{k}</b></td><td>{v}</td></tr>"
        for k, v in props.items()
        if not k.startswith("_")
    )
    if not rows:
        return "No properties"
    return f"<table style='font-size:12px;border-collapse:collapse'>{rows}</table>"


def _write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


# ─────────────────────────────────────────────────────────────────────────────
# Interactive marker map
# ─────────────────────────────────────────────────────────────────────────────

_MAP_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0f111a; color:#fff; }}
  #map {{ width:100vw; height:100vh; }}
  #brand {{
    position:fixed; bottom:16px; left:16px; z-index:9999;
    background:rgba(0,0,0,.7); backdrop-filter:blur(12px);
    border:1px solid rgba(0,212,170,.25);
    border-radius:10px; padding:10px 16px;
    font-size:13px; color:rgba(255,255,255,.7);
  }}
  #brand strong {{ color:#00D4AA; font-size:14px; }}
  #count {{
    position:fixed; top:16px; right:16px; z-index:9999;
    background:rgba(0,0,0,.7); backdrop-filter:blur(12px);
    border:1px solid rgba(0,212,170,.2);
    border-radius:8px; padding:8px 16px;
    font-size:13px; color:rgba(255,255,255,.6);
  }}
  #count span {{ color:#00D4AA; font-weight:600; }}
</style>
</head>
<body>
<div id="map"></div>
<div id="brand"><strong>QOREgeo</strong> · {title}</div>
<div id="count"><span id="n">0</span> features</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  if (typeof L === 'undefined') {{
    document.write('<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"><\\/script>');
  }}
</script>
<script>
document.addEventListener('DOMContentLoaded', () => {{
  try {{
    if (typeof L === 'undefined') throw new Error('Leaflet failed to load from CDNs');

    const DATA = {geojson};
    const TITLE = {title_json};

    const map = L.map('map', {{ zoomControl:true, preferCanvas:true }});

// Dark tile layer
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; <a href="https://www.openstreetmap.org">OSM</a> · <a href="https://carto.com">CARTO</a>',
  subdomains: 'abcd', maxZoom: 19
}}).addTo(map);

// Custom teal icon
const qIcon = L.divIcon({{
  className: '',
  html: `<div style="
    width:12px;height:12px;border-radius:50%;
    background:#00D4AA;border:2px solid rgba(255,255,255,.5);
    box-shadow:0 0 8px rgba(0,212,170,.7);
  "></div>`,
  iconSize: [12, 12], iconAnchor: [6, 6],
}});

const markers = L.featureGroup().addTo(map);
let count = 0;

DATA.features.forEach(feat => {{
  if (feat.geometry.type !== 'Point') return;
  const [lng, lat] = feat.geometry.coordinates;
  const props = feat.properties || {{}};
  const rows = Object.entries(props)
    .filter(([k]) => !k.startsWith('_'))
    .map(([k,v]) => `<tr><td style="padding:2px 8px 2px 0;color:#9898A8;font-weight:600">${{k}}</td><td style="padding:2px 0">${{v}}</td></tr>`)
    .join('');
  const popup = rows
    ? `<div style="font-family:monospace;font-size:12px;min-width:140px"><table>${{rows}}</table></div>`
    : '<div style="font-size:12px;color:#9898A8">No properties</div>';

  L.marker([lat, lng], {{ icon: qIcon }})
    .bindPopup(popup, {{ maxWidth: 300 }})
    .addTo(markers);
  count++;
}});

document.getElementById('n').textContent = count;

if (count > 0) {{
  map.fitBounds(markers.getBounds().pad(0.1));
  const zoom = {zoom};
  if (map.getZoom() < zoom - 2) map.setZoom(zoom);
}}
  }} catch (err) {{
    console.error('QOREgeo Error:', err);
    document.body.innerHTML += `<div style="position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#e74c3c;color:white;padding:20px;border-radius:8px;z-index:99999;box-shadow:0 4px 12px rgba(0,0,0,0.5);">
      <b style="font-size:16px;">Map Loading Error</b><br/><br/>${{err.message}}
    </div>`;
  }}
}});
</script>
</body>
</html>
"""


def build_map(
    features: List[Feature],
    output_path: str,
    title: str = "QOREgeo Map",
    zoom: int = 5,
    center: Optional[Coord] = None,
) -> None:
    geojson = {"type": "FeatureCollection", "features": features}
    html = _MAP_TEMPLATE.format(
        title=title,
        title_json=json.dumps(title),
        geojson=json.dumps(geojson),
        zoom=zoom,
    )
    _write(output_path, html)
    print(f"✅  Map saved → {output_path}  ({len(features)} features)")


# ─────────────────────────────────────────────────────────────────────────────
# Heatmap
# ─────────────────────────────────────────────────────────────────────────────

_HEAT_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background:#0f111a; }}
  #map {{ width:100vw; height:100vh; }}
  #brand {{
    position:fixed; bottom:16px; left:16px; z-index:9999;
    background:rgba(0,0,0,.7); backdrop-filter:blur(12px);
    border:1px solid rgba(0,212,170,.25);
    border-radius:10px; padding:10px 16px;
    font-size:13px; color:rgba(255,255,255,.7);
  }}
  #brand strong {{ color:#00D4AA; font-size:14px; }}
</style>
</head>
<body>
<div id="map"></div>
<div id="brand"><strong>QOREgeo</strong> · {title}</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
  if (typeof L === 'undefined') {{
    document.write('<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"><\\/script>');
  }}
</script>
<script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
<script>
  if (typeof L !== 'undefined' && typeof L.heatLayer === 'undefined') {{
    document.write('<script src="https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js"><\\/script>');
  }}
</script>
<script>
document.addEventListener('DOMContentLoaded', () => {{
  try {{
    if (typeof L === 'undefined') throw new Error('Leaflet failed to load from CDNs');

    const POINTS = {points};
    const TITLE  = {title_json};

    const map = L.map('map', {{ preferCanvas:true }});

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '&copy; OSM · CARTO', subdomains:'abcd', maxZoom:19
}}).addTo(map);

L.heatLayer(POINTS, {{
  radius: 25, blur: 18, maxZoom: 12,
  gradient: {{ 0.1:'#00416A', 0.3:'#007A64', 0.6:'#00D4AA', 0.85:'#FFFFFF', 1:'#FFD700' }}
}}).addTo(map);

if (POINTS.length > 0) {{
  const lats = POINTS.map(p => p[0]);
  const lngs = POINTS.map(p => p[1]);
  map.fitBounds([[Math.min(...lats), Math.min(...lngs)], [Math.max(...lats), Math.max(...lngs)]]);
}}
  }} catch (err) {{
    console.error('QOREgeo Error:', err);
    document.body.innerHTML += `<div style="position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:#e74c3c;color:white;padding:20px;border-radius:8px;z-index:99999;box-shadow:0 4px 12px rgba(0,0,0,0.5);">
      <b style="font-size:16px;">Map Loading Error</b><br/><br/>${{err.message}}
    </div>`;
  }}
}});
</script>
</body>
</html>
"""


def build_heatmap(
    features: List[Feature],
    output_path: str,
    title: str = "QOREgeo Heatmap",
    intensity_col: Optional[str] = None,
    zoom: int = 5,
    center: Optional[Coord] = None,
) -> None:
    points = []
    max_intensity = 1.0

    for feat in features:
        coords = feat["geometry"]["coordinates"]
        lat, lng = coords[1], coords[0]
        intensity = 1.0

        if intensity_col:
            raw = feat.get("properties", {}).get(intensity_col)
            if raw is not None:
                try:
                    intensity = float(raw)
                    max_intensity = max(max_intensity, intensity)
                except (TypeError, ValueError):
                    pass

        points.append([lat, lng, intensity])

    # Normalise intensities
    if max_intensity > 1:
        points = [[p[0], p[1], p[2] / max_intensity] for p in points]

    html = _HEAT_TEMPLATE.format(
        title=title,
        title_json=json.dumps(title),
        points=json.dumps(points),
        zoom=zoom,
    )
    _write(output_path, html)
    print(f"✅  Heatmap saved → {output_path}  ({len(features)} points)")
