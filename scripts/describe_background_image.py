#!/usr/bin/python
import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pystac
import requests
from PIL import Image
from shapely.geometry import shape

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gallery import ensure_in_gallery  # noqa: E402
from pc_teams_background import (  # noqa: E402
    ImageInfo,
    Settings,
    TeamsBackgroundGenerator,
    get_land_mask,
)

NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
BIGDATACLOUD_REVERSE_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
NOMINATIM_USER_AGENT = (
    "planetary-computer-teams-background/1.0 "
    "(https://github.com/lossyrob/planetary-computer-teams-background)"
)
REVERSE_GEOCODE_TIMEOUT_SECONDS = 20


def round_coordinate(value: float) -> float:
    return round(value, 6)


def isoformat_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def summarize_geometry(geometry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not geometry:
        return None

    geom_shape = shape(geometry)
    minx, miny, maxx, maxy = geom_shape.bounds
    centroid = geom_shape.centroid
    summary = {
        "bbox": [
            round_coordinate(minx),
            round_coordinate(miny),
            round_coordinate(maxx),
            round_coordinate(maxy),
        ],
        "centroid": {
            "lon": round_coordinate(centroid.x),
            "lat": round_coordinate(centroid.y),
        },
        "area_square_degrees": round(geom_shape.area, 8),
    }
    summary["map_links"] = {
        "openstreetmap": (
            "https://www.openstreetmap.org/"
            f"?mlat={summary['centroid']['lat']}&mlon={summary['centroid']['lon']}"
            f"#map=9/{summary['centroid']['lat']}/{summary['centroid']['lon']}"
        ),
        "google_maps": (
            "https://www.google.com/maps"
            f"?q={summary['centroid']['lat']},{summary['centroid']['lon']}"
        ),
    }
    return summary


def reverse_geocode(
    geometry_summary: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    if not geometry_summary:
        return None

    centroid = geometry_summary["centroid"]
    last_error: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    for zoom in (10, 8, 5):
        try:
            response = requests.get(
                NOMINATIM_REVERSE_URL,
                params={
                    "format": "jsonv2",
                    "lat": centroid["lat"],
                    "lon": centroid["lon"],
                    "zoom": zoom,
                    "addressdetails": 1,
                },
                headers={"User-Agent": NOMINATIM_USER_AGENT},
                timeout=REVERSE_GEOCODE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = str(exc)
            continue

        if payload.get("display_name") or payload.get("address"):
            break
        if payload.get("error"):
            last_error = payload["error"]
            payload = None

    if payload is None:
        try:
            response = requests.get(
                BIGDATACLOUD_REVERSE_URL,
                params={
                    "latitude": centroid["lat"],
                    "longitude": centroid["lon"],
                    "localityLanguage": "en",
                },
                timeout=REVERSE_GEOCODE_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            return {
                "error": last_error or str(exc) or "No reverse geocode result found."
            }

        locality_info = payload.get("localityInfo") or {}
        informative = locality_info.get("informative") or []
        top_informative = informative[0] if informative else {}
        nearest_named_place = (
            payload.get("city")
            or payload.get("locality")
            or top_informative.get("name")
        )
        return {
            "source": "bigdatacloud",
            "display_name": nearest_named_place,
            "nearest_named_place": nearest_named_place,
            "county": None,
            "state": payload.get("principalSubdivision"),
            "country": payload.get("countryName"),
            "country_code": payload.get("countryCode"),
        }

    address = payload.get("address") or {}
    locality = (
        address.get("city")
        or address.get("town")
        or address.get("village")
        or address.get("municipality")
        or address.get("county")
        or address.get("state_district")
        or address.get("state")
    )

    return {
        "source": "nominatim",
        "display_name": payload.get("display_name"),
        "nearest_named_place": locality,
        "county": address.get("county"),
        "state": address.get("state") or address.get("region"),
        "country": address.get("country"),
        "country_code": address.get("country_code"),
    }


def build_description(
    settings: Settings, image_info: ImageInfo, include_reverse_geocode: bool
) -> Dict[str, Any]:
    target_item = image_info.target_item
    properties = target_item.get("properties") or {}
    source_geometry_kind = image_info.source_geometry_kind
    requested_geometry = image_info.requested_geometry
    crop_geometry = image_info.crop_geometry
    crop_fit_strategy = image_info.crop_fit_strategy
    crop_fit_scale_ratio = image_info.crop_fit_scale_ratio
    land_fraction = image_info.land_fraction
    min_land_fraction = image_info.min_land_fraction
    land_mask_source = image_info.land_mask_source

    if target_item.get("geometry") and (
        requested_geometry is None or crop_geometry is None
    ):
        backfill_settings = settings.copy(
            update={"min_land_fraction": image_info.min_land_fraction or 0.0}
        )
        generator = TeamsBackgroundGenerator(backfill_settings)
        item = pystac.Item.from_dict(target_item)
        target_geometry = properties.get("aoi_geom") or target_item.get("geometry")
        with contextlib.redirect_stdout(io.StringIO()):
            requested_geometry, source_geometry_kind = generator.get_render_geom(
                item, target_geometry, image_info.is_aoi
            )
            crop_geometry, crop_fit_strategy, crop_fit_scale_ratio, land_fraction = (
                generator.fit_bg_geom_to_item_footprint(
                    requested_geometry, item.geometry
                )
            )
        land_mask_source = land_mask_source or "natural-earth-50m"

    if crop_geometry and land_fraction is None:
        land_fraction = get_land_mask().get_land_fraction(shape(crop_geometry))
        land_mask_source = land_mask_source or "natural-earth-50m"

    crop_summary = summarize_geometry(
        crop_geometry or requested_geometry or target_item.get("geometry")
    )
    requested_summary = summarize_geometry(requested_geometry)
    item_summary = summarize_geometry(target_item.get("geometry"))

    result: Dict[str, Any] = {
        "image_path": str(settings.get_image_path()),
        "thumbnail_path": str(settings.get_thumbnail_path()),
        "image_info_path": str(settings.get_image_info_path()),
        "last_changed": isoformat_or_none(image_info.last_changed),
        "is_aoi": image_info.is_aoi,
        "source_geometry_kind": source_geometry_kind,
        "crop_fit_strategy": crop_fit_strategy,
        "crop_fit_scale_ratio": crop_fit_scale_ratio,
        "land_fraction": land_fraction,
        "min_land_fraction": min_land_fraction,
        "land_mask_source": land_mask_source,
        "item": {
            "id": target_item.get("id"),
            "collection": target_item.get("collection"),
            "acquired_at": properties.get("datetime")
            or properties.get("start_datetime")
            or properties.get("end_datetime"),
            "platform": properties.get("platform"),
            "mgrs_tile": properties.get("s2:mgrs_tile"),
            "cloud_cover": properties.get("eo:cloud_cover"),
            "aoi_id": properties.get("aoi"),
        },
        "crop_geometry": crop_summary,
        "requested_geometry": requested_summary,
        "item_geometry": item_summary,
    }

    aoi_geometry = properties.get("aoi_geom")
    if aoi_geometry:
        result["aoi_geometry"] = summarize_geometry(aoi_geometry)

    if image_info.ai_suggestion:
        result["ai_suggestion"] = image_info.ai_suggestion

    if include_reverse_geocode:
        result["geography"] = reverse_geocode(crop_summary)

    return result


def print_text_summary(description: Dict[str, Any]) -> None:
    ai = description.get("ai_suggestion")
    if ai:
        print(f"AI Suggestion: \"{ai.get('name', '')}\"")
        if ai.get("description"):
            print(f"  {ai['description']}")
        if ai.get("conversation_starter"):
            print(f"  Conversation starter: \"{ai['conversation_starter']}\"")
        if ai.get("timeliness"):
            print(f"  Timeliness: {ai['timeliness']}")
        selection = ai.get("_selection") or {}
        if selection.get("selection_reason"):
            print(f"  Selection reason: {selection['selection_reason']}")
        verification = ai.get("_verification") or {}
        if verification.get("verdict"):
            print(
                "  Verification: "
                f"{verification.get('verdict')} "
                f"(confidence {verification.get('confidence', '?')}/5)"
            )
        if verification.get("assessment"):
            print(f"  Verification notes: {verification['assessment']}")
        salvage = ai.get("_salvage") or {}
        if salvage.get("applied"):
            print("  Story was salvaged to better match the final image.")
        print()

    item = description["item"]
    crop = description.get("crop_geometry") or {}
    centroid = crop.get("centroid") or {}
    geography = description.get("geography") or {}

    print(f"Image: {description['image_path']}")
    print(f"Info JSON: {description['image_info_path']}")
    print(
        f"Source item: {item.get('collection')}/{item.get('id')} at "
        f"{item.get('acquired_at')}"
    )
    print(
        f"Rendered crop: {description.get('source_geometry_kind')} -> "
        f"{description.get('crop_fit_strategy')}"
    )
    if description.get("crop_fit_scale_ratio") is not None:
        print("Crop fit scale ratio: " f"{description.get('crop_fit_scale_ratio'):.3f}")
    if description.get("land_fraction") is not None:
        land_text = f"Land fraction: {description.get('land_fraction'):.1%}"
        if description.get("min_land_fraction") is not None:
            land_text += f" (minimum {description.get('min_land_fraction'):.1%})"
        print(land_text)
    print("Crop centroid: " f"{centroid.get('lat')}, {centroid.get('lon')}")
    print(f"Crop bbox: {crop.get('bbox')}")
    if geography:
        if geography.get("error"):
            print(f"Reverse geocode: {geography['error']}")
        else:
            print(
                "Geography: "
                f"{geography.get('nearest_named_place')}, "
                f"{geography.get('state')}, {geography.get('country')}"
            )
            print(f"Nominatim: {geography.get('display_name')}")
    map_links = crop.get("map_links") or {}
    if map_links:
        print(f"OpenStreetMap: {map_links.get('openstreetmap')}")
        print(f"Google Maps: {map_links.get('google_maps')}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--settings-file",
        help="Optional path to a settings YAML file. Defaults to settings.yaml.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the description as JSON.",
    )
    parser.add_argument(
        "--skip-reverse-geocode",
        action="store_true",
        help="Skip the OpenStreetMap reverse-geocode lookup.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    settings = (
        Settings.from_yaml(args.settings_file)
        if args.settings_file
        else Settings.load()
    )
    image_info = ImageInfo.from_yaml(settings.get_image_info_path())
    with Image.open(settings.get_image_path()) as live_image:
        ensure_in_gallery(live_image.copy(), image_info.dict())
    description = build_description(
        settings,
        image_info,
        include_reverse_geocode=not args.skip_reverse_geocode,
    )

    if args.json:
        print(json.dumps(description, indent=2))
    else:
        print_text_summary(description)
    return 0


if __name__ == "__main__":
    sys.exit(main())
