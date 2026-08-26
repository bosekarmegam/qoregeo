# Brand assets

| File | Size | Use |
|---|---|---|
| `qoregeo-social.png` | 1280 x 640 | GitHub social preview, Open Graph, Twitter Card |
| `qoregeo-banner.png` | 1200 x 300 | README header |
| `qoregeo-mark.png` | 256 x 256 | avatar, favicon, docs logo |
| `*.svg` | vector | the editable source for each PNG |

Palette: background `#0B0D14`, accent `#00D4AA`, accent light `#00E5B8`,
muted text `#9AA3B8`.

## Setting the GitHub social preview

The preview card that appears when the repository link is shared on Slack,
X, LinkedIn or in a Discord embed is a repository **setting**, not a file in
the tree. Git cannot set it, so it has to be uploaded once:

1. Open **Settings** on the repository.
2. Scroll to **Social preview**.
3. Choose **Edit** and upload `assets/qoregeo-social.png`.

GitHub recommends 1280 x 640 and caps the file at 1 MB. The image here is
1280 x 640 and about 450 KB.

## Repository description and topics

These drive GitHub search and the card's subtitle. Set them from the gear icon
beside **About** on the repository home page.

**Description**

```
Spatial intelligence for Python with zero dependencies. GIS without GDAL:
distance, geofencing, shapefiles, clustering, spatial joins, routing and maps.
```

**Website**

```
https://pypi.org/project/qoregeo
```

**Topics**

```
gis  geospatial  python  spatial-analysis  geojson  shapefile  leaflet
geofencing  haversine  clustering  dbscan  spatial-join  route-optimization
geohash  utm  cartography  zero-dependencies  no-gdal  pure-python  mapping
```

## Regenerating the PNGs

The PNGs are rendered from the SVGs. Any tool that rasterises SVG will do:

```bash
# rsvg-convert
rsvg-convert -w 1280 -h 640 assets/qoregeo-social.svg -o assets/qoregeo-social.png
rsvg-convert -w 1200 -h 300 assets/qoregeo-banner.svg -o assets/qoregeo-banner.png
rsvg-convert -w 256  -h 256 assets/qoregeo-mark.svg   -o assets/qoregeo-mark.png

# or Inkscape
inkscape assets/qoregeo-social.svg -w 1280 -h 640 -o assets/qoregeo-social.png
```

Keep the social image under 1 MB, or GitHub will reject the upload.
