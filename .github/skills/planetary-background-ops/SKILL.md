---
name: planetary-background-ops
description: Regenerate the current Teams background image on demand and explain where the current image came from using the rendered crop metadata.
---

# Planetary background ops

Use this skill when the user asks to:

- regenerate the current Teams background right now
- explain where the current Teams background image is from
- identify the geography or provenance of the current background
- inspect the current background metadata

## Working rules

1. Run commands from the repository root.
2. Prefer `.\.venv\Scripts\python.exe` when it exists. Otherwise use `python`.
3. Use `settings.yaml` unless the user explicitly points at a different settings file.
4. Ground geography answers in the rendered crop geometry from the current `*-info.json`. Do not describe the entire AOI or full item footprint as if all of it is visible in the image.

## Regenerate the current image

Run:

```powershell
.\.venv\Scripts\python.exe .\pc_teams_background.py -f --settings-file .\settings.yaml
```

Then report the output image path from `settings.yaml` and, if useful, follow up with the describe command below so the user can see what changed.

## Explain where the current image is from

Run:

```powershell
.\.venv\Scripts\python.exe .\scripts\describe_background_image.py --settings-file .\settings.yaml --json
```

Use the result to answer with:

- acquisition time
- collection and item ID
- MGRS tile and cloud cover when present
- rendered crop centroid and bbox
- nearest named place / state / country from reverse geocoding when available
- map links for the rendered crop centroid

If reverse geocoding fails, say so plainly and fall back to the rendered crop centroid, bbox, and map links.
