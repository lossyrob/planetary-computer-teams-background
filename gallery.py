"""Gallery module — archives background images and generates an HTML gallery."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from PIL.Image import Image as PILImage

GALLERY_DIR = Path(__file__).parent / "gallery"
IMAGES_DIR = GALLERY_DIR / "images"
MANIFEST_PATH = GALLERY_DIR / "manifest.json"
HISTORY_PATH = GALLERY_DIR / "ai-selection-history.json"
HTML_PATH = GALLERY_DIR / "index.html"
GALLERY_THUMB_SIZE = (520, 390)

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NOMINATIM_USER_AGENT = (
    "planetary-computer-teams-background/1.0 "
    "(https://github.com/lossyrob/planetary-computer-teams-background)"
)


def _reverse_geocode(lat: float, lon: float) -> Optional[str]:
    """Quick reverse geocode to get a place name. Returns None on failure."""
    try:
        resp = requests.get(
            NOMINATIM_REVERSE_URL,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 8},
            headers={"User-Agent": NOMINATIM_USER_AGENT},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("display_name")
    except Exception:
        return None


def _load_manifest() -> List[Dict[str, Any]]:
    if MANIFEST_PATH.exists():
        try:
            with open(MANIFEST_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def _save_manifest(entries: List[Dict[str, Any]]) -> None:
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST_PATH, "w") as f:
        json.dump(entries, f, indent=2, default=str)


def _manifest_sort_key(entry: Dict[str, Any]) -> datetime:
    timestamp = entry.get("timestamp")
    if not timestamp:
        return datetime.min
    try:
        return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min


def _sort_manifest(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(entries, key=_manifest_sort_key, reverse=True)


def load_ai_history() -> List[Dict[str, Any]]:
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def append_ai_history(record: Dict[str, Any]) -> None:
    GALLERY_DIR.mkdir(parents=True, exist_ok=True)
    history = load_ai_history()
    history.insert(0, record)
    with open(HISTORY_PATH, "w") as f:
        json.dump(history[:200], f, indent=2, default=str)


def summarize_ai_history(limit: int = 10) -> str:
    history = load_ai_history()[:limit]
    if not history:
        return "No prior AI selection history."

    lines = []
    for entry in history:
        verdict_raw = entry.get("verdict")
        verdict = str(verdict_raw).strip() if verdict_raw else "unknown"
        title = entry.get("title") or entry.get("template_name") or "Unknown"
        reason = (entry.get("assessment") or "").strip().replace("\n", " ")
        if len(reason) > 180:
            reason = reason[:177] + "..."
        lines.append(f"- {verdict.upper()}: {title} — {reason or 'No notes.'}")
    return "\n".join(lines)


def _build_entry(
    image_filename: str,
    thumb_filename: str,
    info_dict: Dict[str, Any],
    geography: Optional[str],
) -> Dict[str, Any]:
    """Build a gallery manifest entry from image info."""
    target_item = info_dict.get("target_item", {})
    properties = target_item.get("properties", {})
    ai = info_dict.get("ai_suggestion")
    crop_geom = info_dict.get("crop_geometry")

    centroid = None
    if crop_geom:
        from shapely.geometry import shape as shapely_shape

        s = shapely_shape(crop_geom)
        centroid = {"lat": round(s.centroid.y, 6), "lon": round(s.centroid.x, 6)}

    title = ""
    if ai:
        title = ai.get("name", "")
    if not title and geography:
        title = geography.split(",")[0].strip()
    if not title:
        title = target_item.get("id", "Unknown")

    timestamp = info_dict.get("last_changed")
    if hasattr(timestamp, "isoformat"):
        timestamp = timestamp.isoformat()

    entry: Dict[str, Any] = {
        "image": f"images/{image_filename}",
        "thumbnail": f"images/{thumb_filename}",
        "timestamp": timestamp,
        "title": title,
        "source_type": info_dict.get("source_geometry_kind", "unknown"),
        "item": {
            "id": target_item.get("id"),
            "collection": target_item.get("collection"),
            "platform": properties.get("platform"),
            "acquired_at": (
                properties.get("datetime")
                or properties.get("start_datetime")
            ),
            "cloud_cover": properties.get("eo:cloud_cover"),
        },
        "crop_fit_strategy": info_dict.get("crop_fit_strategy"),
        "land_fraction": info_dict.get("land_fraction"),
    }

    if centroid:
        entry["centroid"] = centroid
        entry["map_links"] = {
            "osm": (
                f"https://www.openstreetmap.org/"
                f"?mlat={centroid['lat']}&mlon={centroid['lon']}"
                f"#map=9/{centroid['lat']}/{centroid['lon']}"
            ),
            "google": (
                f"https://www.google.com/maps"
                f"?q={centroid['lat']},{centroid['lon']}"
            ),
        }

    if geography:
        entry["geography"] = geography

    if ai:
        entry["ai_suggestion"] = {
            "name": ai.get("name"),
            "description": ai.get("description"),
            "conversation_starter": ai.get("conversation_starter"),
            "timeliness": ai.get("timeliness"),
            "why_visible_in_s2": ai.get("why_visible_in_s2"),
            "scale_km": ai.get("scale_km"),
        }
        selection = ai.get("_selection")
        if selection:
            entry["selection"] = {
                "selection_reason": selection.get("selection_reason"),
                "selected_preview_id": selection.get("selected_preview_id"),
                "alternate_preview_ids": selection.get("alternate_preview_ids") or [],
            }
        verification = ai.get("_verification")
        if verification:
            entry["verification"] = {
                "verdict": verification.get("verdict"),
                "confidence": verification.get("confidence"),
                "visual_quality_score": verification.get("visual_quality_score"),
                "story_match_score": verification.get("story_match_score"),
                "conversation_score": verification.get("conversation_score"),
                "assessment": verification.get("assessment"),
            }
        salvage = ai.get("_salvage")
        if salvage:
            entry["salvage"] = salvage

    return entry


def ensure_in_gallery(image: PILImage, info_dict: Dict[str, Any]) -> bool:
    """Archive the current image if it is missing from the gallery manifest."""
    timestamp = info_dict.get("last_changed")
    if hasattr(timestamp, "isoformat"):
        timestamp = timestamp.isoformat()
    manifest = _load_manifest()
    if timestamp and any(entry.get("timestamp") == timestamp for entry in manifest):
        regenerate_html(_sort_manifest(manifest))
        return False

    archive_to_gallery(image, info_dict)
    return True


def archive_to_gallery(image: PILImage, info_dict: Dict[str, Any]) -> None:
    """Archive an image and its metadata to the gallery."""
    try:
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now()
        entry_id = timestamp.strftime("%Y-%m-%d_%H%M%S")
        image_filename = f"{entry_id}.jpg"
        thumb_filename = f"{entry_id}_thumb.jpg"

        # Save full image as JPEG
        image_path = IMAGES_DIR / image_filename
        image.convert("RGB").save(image_path, "JPEG", quality=90)

        # Save thumbnail
        thumb = image.copy()
        thumb.thumbnail(GALLERY_THUMB_SIZE)
        thumb_path = IMAGES_DIR / thumb_filename
        thumb.convert("RGB").save(thumb_path, "JPEG", quality=85)

        # Reverse geocode from crop centroid
        geography = None
        crop_geom = info_dict.get("crop_geometry")
        if crop_geom:
            from shapely.geometry import shape as shapely_shape

            s = shapely_shape(crop_geom)
            geography = _reverse_geocode(s.centroid.y, s.centroid.x)

        # Build entry and update manifest
        entry = _build_entry(image_filename, thumb_filename, info_dict, geography)
        manifest = _load_manifest()
        manifest.insert(0, entry)  # newest first
        manifest = _sort_manifest(manifest)
        _save_manifest(manifest)

        # Regenerate HTML
        regenerate_html(manifest)
        print(f"Archived to gallery ({len(manifest)} images total)")

    except Exception as e:
        print(f"Gallery archive failed (non-fatal): {e}")


def regenerate_html(entries: List[Dict[str, Any]]) -> None:
    """Regenerate the gallery HTML page from manifest entries."""
    gallery_data_json = json.dumps(entries, indent=2, default=str)
    html = HTML_TEMPLATE.replace("/*GALLERY_DATA*/[]", gallery_data_json)
    with open(HTML_PATH, "w", encoding="utf-8") as f:
        f.write(html)


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Satellite Background Gallery</title>
<style>
:root {
  --bg: #0d1117;
  --surface: #161b22;
  --surface-hover: #1c2128;
  --border: #30363d;
  --text: #e6edf3;
  --text-muted: #8b949e;
  --text-secondary: #c9d1d9;
  --accent: #58a6ff;
  --accent-soft: #1f6feb33;
  --tag-ai: #a371f7;
  --tag-aoi: #3fb950;
  --tag-random: #d29922;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.5;
}
header {
  text-align: center;
  padding: 2.5rem 1rem 1.5rem;
  border-bottom: 1px solid var(--border);
  margin-bottom: 2rem;
}
header h1 { font-size: 1.75rem; font-weight: 600; }
header p { color: var(--text-muted); margin-top: 0.5rem; font-size: 0.95rem; }

.gallery {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 1.5rem;
  max-width: 1480px;
  margin: 0 auto;
  padding: 0 1.5rem 3rem;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
  transition: transform 0.15s ease, border-color 0.15s ease;
}
.card:hover {
  transform: translateY(-3px);
  border-color: var(--accent);
}
.card img {
  width: 100%;
  aspect-ratio: 4/3;
  object-fit: cover;
  display: block;
}
.card-body { padding: 1rem 1.1rem; }
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 0.75rem;
  margin-bottom: 0.4rem;
}
.card-title {
  font-size: 1rem;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.tag {
  font-size: 0.7rem;
  font-weight: 600;
  padding: 0.15rem 0.5rem;
  border-radius: 99px;
  white-space: nowrap;
  flex-shrink: 0;
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.tag-ai { background: #a371f722; color: var(--tag-ai); }
.tag-aoi { background: #3fb95022; color: var(--tag-aoi); }
.tag-random { background: #d2992222; color: var(--tag-random); }
.card-date { color: var(--text-muted); font-size: 0.82rem; }
.card-desc {
  color: var(--text-secondary);
  font-size: 0.88rem;
  margin-top: 0.5rem;
  overflow: hidden;
  text-overflow: ellipsis;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
}
.card-geo {
  color: var(--text-muted);
  font-size: 0.82rem;
  margin-top: 0.35rem;
}

/* Modal */
.modal-backdrop {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.85);
  z-index: 1000;
  overflow-y: auto;
  padding: 2rem;
}
.modal-backdrop.active { display: flex; justify-content: center; align-items: flex-start; }
.modal {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  max-width: 960px;
  width: 100%;
  margin: 2rem auto;
  overflow: hidden;
}
.modal img {
  width: 100%;
  display: block;
}
.modal-body { padding: 1.5rem; }
.modal-title { font-size: 1.35rem; font-weight: 600; margin-bottom: 0.25rem; }
.modal-date { color: var(--text-muted); font-size: 0.9rem; margin-bottom: 1rem; }
.modal-section {
  margin-top: 1.25rem;
  padding-top: 1.25rem;
  border-top: 1px solid var(--border);
}
.modal-section h3 {
  font-size: 0.85rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
  margin-bottom: 0.6rem;
}
.modal-section p {
  color: var(--text-secondary);
  font-size: 0.95rem;
  line-height: 1.6;
}
.conversation-starter {
  background: var(--accent-soft);
  border-left: 3px solid var(--accent);
  padding: 0.75rem 1rem;
  border-radius: 0 6px 6px 0;
  margin-top: 0.75rem;
  font-style: italic;
  color: var(--text);
}
.detail-grid {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 0.3rem 1rem;
  font-size: 0.9rem;
}
.detail-grid dt { color: var(--text-muted); }
.detail-grid dd { color: var(--text-secondary); }
.detail-grid a { color: var(--accent); text-decoration: none; }
.detail-grid a:hover { text-decoration: underline; }
.close-btn {
  position: absolute;
  top: 1rem;
  right: 1.5rem;
  background: rgba(0,0,0,0.6);
  border: none;
  color: white;
  font-size: 1.5rem;
  width: 2.5rem;
  height: 2.5rem;
  border-radius: 50%;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1001;
}
.close-btn:hover { background: rgba(0,0,0,0.8); }
.empty-state {
  text-align: center;
  padding: 4rem 2rem;
  color: var(--text-muted);
}
</style>
</head>
<body>

<header>
  <h1>&#x1f6f0;&#xfe0f; Satellite Background Gallery</h1>
  <p id="subtitle"></p>
</header>

<div class="gallery" id="gallery"></div>

<div class="modal-backdrop" id="modal-backdrop">
  <button class="close-btn" id="close-btn">&times;</button>
  <div class="modal" id="modal"></div>
</div>

<script>
const GALLERY_DATA = /*GALLERY_DATA*/[];

function formatDate(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleDateString('en-US', {
    year: 'numeric', month: 'long', day: 'numeric',
    hour: '2-digit', minute: '2-digit'
  });
}

function formatShortDate(ts) {
  if (!ts) return '';
  const d = new Date(ts);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function sourceTag(type) {
  if (type === 'ai-suggestion') return '<span class="tag tag-ai">AI</span>';
  if (type === 'aoi' || type === 'aoi-item-overlap') return '<span class="tag tag-aoi">AOI</span>';
  return '<span class="tag tag-random">Random</span>';
}

function getDescription(entry) {
  const ai = entry.ai_suggestion;
  if (ai && ai.description) return ai.description;
  if (entry.geography) return entry.geography;
  return '';
}

function getGeoShort(entry) {
  if (entry.geography) {
    const parts = entry.geography.split(',').map(s => s.trim());
    if (parts.length >= 2) return parts.slice(0, 2).join(', ');
    return parts[0];
  }
  if (entry.centroid) return `${entry.centroid.lat.toFixed(2)}\u00b0, ${entry.centroid.lon.toFixed(2)}\u00b0`;
  return '';
}

function renderGallery() {
  const container = document.getElementById('gallery');
  const subtitle = document.getElementById('subtitle');
  subtitle.textContent = `${GALLERY_DATA.length} background${GALLERY_DATA.length !== 1 ? 's' : ''} archived`;

  if (GALLERY_DATA.length === 0) {
    container.innerHTML = '<div class="empty-state"><p>No images archived yet. Generate a background to get started.</p></div>';
    return;
  }

  container.innerHTML = GALLERY_DATA.map((entry, i) => `
    <div class="card" onclick="showModal(${i})">
      <img src="${entry.thumbnail}" alt="${entry.title}" loading="lazy">
      <div class="card-body">
        <div class="card-header">
          <div class="card-title">${entry.title}</div>
          ${sourceTag(entry.source_type)}
        </div>
        <div class="card-date">${formatShortDate(entry.timestamp)}</div>
        ${getDescription(entry) ? `<div class="card-desc">${getDescription(entry)}</div>` : ''}
        ${getGeoShort(entry) ? `<div class="card-geo">\ud83d\udccd ${getGeoShort(entry)}</div>` : ''}
      </div>
    </div>
  `).join('');
}

function showModal(index) {
  const entry = GALLERY_DATA[index];
  const ai = entry.ai_suggestion || {};
  const item = entry.item || {};
  const links = entry.map_links || {};
  const selection = entry.selection || {};
  const salvage = entry.salvage || {};

  let html = `<img src="${entry.image}" alt="${entry.title}">`;
  html += '<div class="modal-body">';
  html += `<div class="modal-title">${entry.title}</div>`;
  html += `<div class="modal-date">${formatDate(entry.timestamp)}</div>`;

  if (ai.description) {
    html += '<div class="modal-section">';
    html += '<h3>About this image</h3>';
    html += `<p>${ai.description}</p>`;
    if (ai.conversation_starter) {
      html += `<div class="conversation-starter">\ud83d\udcac ${ai.conversation_starter}</div>`;
    }
    html += '</div>';
  }

  if (ai.timeliness) {
    html += '<div class="modal-section">';
    html += '<h3>Why now?</h3>';
    html += `<p>${ai.timeliness}</p>`;
    html += '</div>';
  }

  if (ai.why_visible_in_s2) {
    html += '<div class="modal-section">';
    html += '<h3>Why it looks great from space</h3>';
    html += `<p>${ai.why_visible_in_s2}</p>`;
    html += '</div>';
  }

  if (selection.selection_reason) {
    html += '<div class="modal-section">';
    html += '<h3>Why the AI picked this</h3>';
    html += `<p>${selection.selection_reason}</p>`;
    html += '</div>';
  }

  const veri = entry.verification;
  if (veri && veri.assessment) {
    const verdictEmoji = veri.verdict === 'accept' ? '\u2705' : veri.verdict === 'adjust' ? '\ud83d\udd04' : '\u274c';
    const stars = '\u2b50'.repeat(veri.confidence || 0);
    html += '<div class="modal-section">';
    html += `<h3>${verdictEmoji} AI Verification ${stars}</h3>`;
    html += `<p>${veri.assessment}</p>`;
    if (
      veri.visual_quality_score != null ||
      veri.story_match_score != null ||
      veri.conversation_score != null
    ) {
      html += `<p><strong>Scores:</strong> visual ${veri.visual_quality_score ?? '-'} / story ${veri.story_match_score ?? '-'} / conversation ${veri.conversation_score ?? '-'}</p>`;
    }
    html += '</div>';
  }

  if (salvage.applied) {
    html += '<div class="modal-section">';
    html += '<h3>\ud83e\ude79 Salvaged story</h3>';
    html += `<p>${salvage.assessment || 'The image was kept but the story was rewritten to better match what was visible.'}</p>`;
    html += '</div>';
  }

  html += '<div class="modal-section">';
  html += '<h3>Details</h3>';
  html += '<dl class="detail-grid">';
  if (entry.geography) {
    html += `<dt>Location</dt><dd>${entry.geography}</dd>`;
  }
  if (entry.centroid) {
    html += `<dt>Coordinates</dt><dd>${entry.centroid.lat.toFixed(4)}\u00b0N, ${entry.centroid.lon.toFixed(4)}\u00b0E</dd>`;
  }
  if (links.osm) {
    html += `<dt>Maps</dt><dd><a href="${links.osm}" target="_blank">OpenStreetMap</a> · <a href="${links.google}" target="_blank">Google Maps</a></dd>`;
  }
  if (item.platform) html += `<dt>Satellite</dt><dd>${item.platform}</dd>`;
  if (item.acquired_at) html += `<dt>Captured</dt><dd>${formatDate(item.acquired_at)}</dd>`;
  if (item.cloud_cover != null) html += `<dt>Cloud cover</dt><dd>${item.cloud_cover.toFixed(1)}%</dd>`;
  if (entry.land_fraction != null) html += `<dt>Land fraction</dt><dd>${(entry.land_fraction * 100).toFixed(1)}%</dd>`;
  if (ai.scale_km != null) html += `<dt>Scale</dt><dd>${Number(ai.scale_km).toFixed(0)} km wide</dd>`;
  if (selection.selected_preview_id) html += `<dt>Preview</dt><dd>${selection.selected_preview_id}</dd>`;
  if (item.collection) html += `<dt>Collection</dt><dd>${item.collection}</dd>`;
  if (item.id) html += `<dt>Item ID</dt><dd style="font-size:0.8rem;word-break:break-all">${item.id}</dd>`;
  html += '</dl></div></div>';

  document.getElementById('modal').innerHTML = html;
  document.getElementById('modal-backdrop').classList.add('active');
  document.body.style.overflow = 'hidden';
}

function closeModal() {
  document.getElementById('modal-backdrop').classList.remove('active');
  document.body.style.overflow = '';
}

document.getElementById('modal-backdrop').addEventListener('click', (e) => {
  if (e.target === document.getElementById('modal-backdrop')) closeModal();
});
document.getElementById('close-btn').addEventListener('click', closeModal);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

renderGallery();
</script>
</body>
</html>"""
