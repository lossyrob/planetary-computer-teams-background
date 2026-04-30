#!/usr/bin/python
import argparse
import base64
import io
import json
import math
import os
import random
import re
import sys
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union
from urllib.parse import parse_qs
from uuid import uuid4

import subprocess

import dateparser
import pystac
import requests
import shapefile
import yaml
from PIL import Image, ImageDraw, ImageFont, ImageOps
from PIL.Image import Image as PILImage
from pydantic import BaseModel, validator
from pyproj import Transformer
from pystac_client import Client
from shapely.geometry import box, mapping, shape
from shapely.ops import transform
from shapely.strtree import STRtree

AOI_LAST_ITEM_DT_KEY = "last_item_datetime"
REQUEST_TIMEOUT_SECONDS = 30
MIN_AOI_ITEM_COVERAGE_RATIO = 0.6
FOOTPRINT_FIT_GRID_SIZE = 61
FOOTPRINT_FIT_SCALE_STEPS = 10
LAND_FIT_SCALE_STEPS = 16
MIN_LAND_FIT_SCALE_RATIO = 0.1
LAND_FRACTION_EQUAL_AREA_CRS = "EPSG:6933"
AI_PREVIEW_COLUMNS = 3
AI_PREVIEW_PADDING = 18
NATURAL_EARTH_LAND_URL = (
    "https://naturalearth.s3.amazonaws.com/50m_physical/ne_50m_land.zip"
)
NATURAL_EARTH_LAND_ARCHIVE_NAME = "ne_50m_land.zip"
NATURAL_EARTH_LAND_DATASET_NAME = "ne_50m_land"
NATURAL_EARTH_LAND_REQUIRED_SUFFIXES = (".shp", ".shx", ".dbf")

NODE_SUBPROCESS_CREATIONFLAGS = (
    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
)


class SettingsError(Exception):
    pass


def expand_path(path_str: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path_str)))


def resolve_settings_path(path_str: str, settings_dir: Path) -> str:
    path = expand_path(path_str)
    if path.is_absolute():
        return str(path)
    return str((settings_dir / path).resolve())


def slugify_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def get_teams_image_folder_candidates() -> List[Path]:
    candidates: List[Path] = []
    local_appdata = os.environ.get("LOCALAPPDATA")
    appdata = os.environ.get("APPDATA")

    if local_appdata:
        candidates.append(
            Path(local_appdata)
            / "Packages"
            / "MSTeams_8wekyb3d8bbwe"
            / "LocalCache"
            / "Microsoft"
            / "MSTeams"
            / "Backgrounds"
            / "Uploads"
        )
        candidates.append(
            Path(local_appdata) / "Microsoft" / "MSTeams" / "Backgrounds" / "Uploads"
        )

    if appdata:
        candidates.append(
            Path(appdata) / "Microsoft" / "Teams" / "Backgrounds" / "Uploads"
        )

    return candidates


def detect_teams_image_folder() -> Path:
    candidates = get_teams_image_folder_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise SettingsError(
        "Could not find a Teams background Uploads folder. "
        "Set teams_image_folder in settings.yaml. "
        f"Looked in: {', '.join(str(candidate) for candidate in candidates)}"
    )


def parse_render_options(options: str) -> Dict[str, Union[str, List[str]]]:
    parsed = parse_qs(options, keep_blank_values=True)
    result: Dict[str, Union[str, List[str]]] = {}
    for key, values in parsed.items():
        result[key] = values[0] if len(values) == 1 else values
    return result


def make_feature(geometry: Dict[str, Any]) -> Dict[str, Any]:
    return {"type": "Feature", "geometry": geometry, "properties": {}}


def to_utc_isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def linspace(start: float, stop: float, steps: int) -> List[float]:
    if steps <= 1:
        return [start]
    step_size = (stop - start) / (steps - 1)
    return [start + (step_size * idx) for idx in range(steps)]


def get_default_land_mask_cache_dir() -> Path:
    return Path(__file__).resolve().parent / ".cache" / "natural-earth"


def ensure_natural_earth_land_dataset() -> Path:
    cache_dir = get_default_land_mask_cache_dir()
    dataset_root = cache_dir / NATURAL_EARTH_LAND_DATASET_NAME
    required_paths = [
        dataset_root.with_suffix(suffix)
        for suffix in NATURAL_EARTH_LAND_REQUIRED_SUFFIXES
    ]
    if all(path.exists() for path in required_paths):
        return dataset_root.with_suffix(".shp")

    cache_dir.mkdir(parents=True, exist_ok=True)
    print("Downloading Natural Earth land polygons...")
    response = requests.get(NATURAL_EARTH_LAND_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    archive_path = cache_dir / NATURAL_EARTH_LAND_ARCHIVE_NAME
    archive_path.write_bytes(response.content)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        archive.extractall(cache_dir)

    if not all(path.exists() for path in required_paths):
        raise SettingsError(
            "Natural Earth land polygons did not extract correctly into "
            f"{cache_dir}."
        )

    return dataset_root.with_suffix(".shp")


class NaturalEarthLandMask:
    def __init__(self, dataset_path: Path):
        self._project_to_equal_area = Transformer.from_crs(
            "EPSG:4326",
            LAND_FRACTION_EQUAL_AREA_CRS,
            always_xy=True,
        ).transform
        self._land_geometries = self._load_land_geometries(dataset_path)
        self._tree = STRtree(self._land_geometries)

    def _load_land_geometries(self, dataset_path: Path) -> List[Any]:
        land_geometries: List[Any] = []
        with shapefile.Reader(str(dataset_path)) as reader:
            for record in reader.iterShapes():
                geom = shape(record.__geo_interface__)
                if geom.is_empty:
                    continue
                projected = transform(self._project_to_equal_area, geom)
                if projected.is_empty:
                    continue
                land_geometries.append(projected)

        if not land_geometries:
            raise SettingsError(
                f"No Natural Earth land polygons were loaded from {dataset_path}."
            )
        return land_geometries

    def project_geometry(self, geometry: Any) -> Any:
        return transform(self._project_to_equal_area, geometry)

    def _resolve_query_matches(self, matches: Any) -> List[Any]:
        if len(matches) == 0:
            return []
        first_match = matches[0]
        if hasattr(first_match, "geom_type"):
            return list(matches)
        return [self._land_geometries[int(idx)] for idx in matches]

    def get_land_geometries(self, geometry: Any) -> List[Any]:
        projected_geometry = self.project_geometry(geometry)
        return self._resolve_query_matches(self._tree.query(projected_geometry))

    def get_land_fraction(
        self, geometry: Any, candidate_land_geometries: Optional[List[Any]] = None
    ) -> float:
        projected_geometry = self.project_geometry(geometry)
        if projected_geometry.is_empty or projected_geometry.area <= 0:
            return 0.0

        land_geometries = candidate_land_geometries
        if land_geometries is None:
            land_geometries = self._resolve_query_matches(
                self._tree.query(projected_geometry)
            )

        land_area = 0.0
        for land_geometry in land_geometries:
            if not land_geometry.intersects(projected_geometry):
                continue
            land_area += land_geometry.intersection(projected_geometry).area

        return min(1.0, max(0.0, land_area / projected_geometry.area))


@lru_cache(maxsize=1)
def get_land_mask() -> NaturalEarthLandMask:
    return NaturalEarthLandMask(ensure_natural_earth_land_dataset())


class FilterConfig(BaseModel):
    property: str
    op: str
    value: Any

    def to_cql_op(self) -> Dict[str, Any]:
        return {"op": self.op, "args": [{"property": self.property}, self.value]}


class CollectionConfig(BaseModel):
    id: str
    rendering_option: Optional[str] = None
    search_days: int = 30
    filters: Optional[List[FilterConfig]] = None


class AOIsConfig(BaseModel):
    feature_collection_path: str
    refresh_days: int = 1

    @validator("feature_collection_path")
    def _validate_fc_path(cls, v: str) -> str:
        path = expand_path(v)
        if not path.exists():
            raise ValueError(f"Feature collection path {path} does not exist")
        return str(path)


class AiSuggestionsConfig(BaseModel):
    enabled: bool = False
    model: str = "claude-sonnet-4"
    timeout_seconds: int = 120
    fallback_to_aois: bool = True
    verify_images: bool = True
    salvage_images: bool = True
    max_adjust_rounds: int = 2
    max_suggestions_tried: int = 3
    max_templates: int = 8
    max_items_per_template: int = 2
    max_preview_candidates: int = 12
    preview_width: int = 320
    preview_height: int = 240
    history_limit: int = 10


class APIURLConfig(BaseModel):
    stac: str
    info: str
    item_crop: str = "https://planetarycomputer.microsoft.com/api/data/v1/item/crop"


class Settings(BaseModel):
    image_name: str = "pc-teams-background.png"
    teams_image_folder: Optional[str] = None
    collections: List[CollectionConfig]
    width: int
    height: int
    thumbnail_width: int
    thumbnail_height: int
    apis: APIURLConfig
    max_search_results: int = 1000
    aois: Optional[AOIsConfig] = None
    ai_suggestions: Optional[AiSuggestionsConfig] = None
    image_info_path: Optional[str] = None
    force_regen_after: Optional[str] = None
    mirror_image: bool = False
    min_land_fraction: float = 0.0
    min_crop_fit_scale_ratio: float = 0.0

    def get_image_folder(self) -> Path:
        if self.teams_image_folder:
            path = expand_path(self.teams_image_folder)
            if not path.exists():
                raise SettingsError(
                    f"Configured Teams image folder does not exist: {path}"
                )
            return path
        return detect_teams_image_folder()

    def use_modern_thumbnail_name(self) -> bool:
        image_folder = self.get_image_folder()
        modern_thumb = next(image_folder.glob("*_thumb.*"), None)
        legacy_thumb = next(image_folder.glob("*_thumbnail.*"), None)

        if modern_thumb and not legacy_thumb:
            return True
        if legacy_thumb and not modern_thumb:
            return False

        return "msteams" in str(image_folder).lower()

    def get_image_path(self) -> Path:
        return self.get_image_folder() / self.image_name

    def get_thumbnail_path(self) -> Path:
        image_path = self.get_image_path()
        if self.use_modern_thumbnail_name():
            return image_path.with_name(f"{image_path.stem}_thumb{image_path.suffix}")

        return image_path.with_name(f"{image_path.stem}_thumbnail.jpg")

    def get_image_info_path(self) -> Path:
        if self.image_info_path:
            return expand_path(self.image_info_path)

        image_folder = self.get_image_folder()
        return image_folder.joinpath(f"{Path(self.image_name).stem}-info.json")

    def get_force_regen_after_time(self, created_at: datetime) -> Optional[datetime]:
        if self.force_regen_after is None:
            return None
        dt = dateparser.parse(f"{self.force_regen_after} ago")
        assert dt
        delta = datetime.now() - dt
        return created_at + delta

    def get_collection_config(self, collection_id: str) -> CollectionConfig:
        result = next(filter(lambda c: c.id == collection_id, self.collections), None)
        if not result:
            raise ValueError(f"Collection {collection_id} not found")
        return result

    @validator("force_regen_after")
    def _validate_force_regen_after(cls, v: str) -> str:
        if v is not None:
            x = dateparser.parse(f"{v} ago")
            if not x:
                raise ValueError(f"Invalid force regen after date phrase: {v}")
        return v

    @validator("min_land_fraction")
    def _validate_min_land_fraction(cls, v: float) -> float:
        if v < 0 or v > 1:
            raise ValueError("min_land_fraction must be between 0 and 1.")
        return v

    @validator("min_crop_fit_scale_ratio")
    def _validate_min_crop_fit_scale_ratio(cls, v: float) -> float:
        if v < 0 or v > 1:
            raise ValueError("min_crop_fit_scale_ratio must be between 0 and 1.")
        return v

    @classmethod
    def from_yaml(cls, yaml_path: Union[str, Path]) -> "Settings":
        yaml_path = Path(yaml_path)
        with open(yaml_path) as f:
            settings = yaml.safe_load(f) or {}

        settings_dir = yaml_path.parent.resolve()
        if settings.get("teams_image_folder"):
            settings["teams_image_folder"] = resolve_settings_path(
                settings["teams_image_folder"], settings_dir
            )
        if settings.get("image_info_path"):
            settings["image_info_path"] = resolve_settings_path(
                settings["image_info_path"], settings_dir
            )
        aois = settings.get("aois")
        if aois and aois.get("feature_collection_path"):
            aois["feature_collection_path"] = resolve_settings_path(
                aois["feature_collection_path"], settings_dir
            )

        return cls(**settings)

    @classmethod
    def load(cls) -> "Settings":
        if os.environ.get("PC_TEAMS_BG_SETTINGS_FILE"):
            path = expand_path(os.environ["PC_TEAMS_BG_SETTINGS_FILE"])
        else:
            HERE = Path(__file__).parent
            path = HERE / "settings.yaml"
        return cls.from_yaml(path)


class ImageInfo(BaseModel):
    target_item: Dict[str, Any]
    cql: Dict[str, Any]
    render_params: Dict[str, Any]
    is_aoi: bool
    last_changed: Optional[datetime] = None
    source_geometry_kind: Optional[str] = None
    requested_geometry: Optional[Dict[str, Any]] = None
    crop_geometry: Optional[Dict[str, Any]] = None
    crop_fit_strategy: Optional[str] = None
    crop_fit_scale_ratio: Optional[float] = None
    land_fraction: Optional[float] = None
    min_land_fraction: Optional[float] = None
    land_mask_source: Optional[str] = None
    ai_suggestion: Optional[Dict[str, Any]] = None

    @classmethod
    def from_yaml(cls, yaml_path: Union[str, Path]) -> "ImageInfo":
        with open(yaml_path) as f:
            settings = yaml.safe_load(f)
        return cls(**settings)


@dataclass
class CandidateVariant:
    preview_id: str
    template: Dict[str, Any]
    target_item: pystac.Item
    scale_km: float
    requested_geometry: Dict[str, Any]
    crop_geometry: Dict[str, Any]
    render_params: Dict[str, Union[str, List[str]]]
    crop_fit_strategy: str
    crop_fit_scale_ratio: Optional[float]
    land_fraction: Optional[float]
    effective_min_land_fraction: float
    preview_image: PILImage
    collection_id: str
    rendering_option_name: Optional[str]

    def to_selector_summary(self) -> Dict[str, Any]:
        properties = self.target_item.properties
        centroid = shape(self.crop_geometry).centroid
        return {
            "preview_id": self.preview_id,
            "template_id": self.template["id"],
            "seed_name": self.template["name"],
            "location": self.template["location"],
            "scale_km": self.scale_km,
            "expected_visual_signatures": self.template["expected_visual_signatures"],
            "story_seed": self.template["story_seed"],
            "conversation_seed": self.template["conversation_seed"],
            "timeliness_seed": self.template.get("timeliness_seed"),
            "why_visible_in_s2_seed": self.template.get("why_visible_in_s2_seed"),
            "backup_caption_if_signature_missing": self.template[
                "backup_caption_if_signature_missing"
            ],
            "discovery_source": self.template.get("discovery_source"),
            "discovery_rationale": self.template.get("discovery_rationale"),
            "captured_at": to_utc_isoformat(get_datetime(self.target_item)),
            "cloud_cover": properties.get("eo:cloud_cover"),
            "platform": properties.get("platform"),
            "collection": self.collection_id,
            "land_fraction": self.land_fraction,
            "min_land_fraction": self.effective_min_land_fraction,
            "crop_fit_strategy": self.crop_fit_strategy,
            "rendering_option": self.rendering_option_name,
            "centroid": {
                "lon": round(centroid.x, 5),
                "lat": round(centroid.y, 5),
            },
            "tags": self.template.get("tags", []),
        }


def cql_add_geom_arg(cql: Dict[str, Any], geom: Dict[str, Any]):
    result = deepcopy(cql)
    result["filter"]["args"].append(
        {"op": "s_intersects", "args": [{"property": "geometry"}, geom]}
    )
    return result


def cql_add_after_arg(cql: Dict[str, Any], after: str) -> Dict[str, Any]:
    result = deepcopy(cql)
    result["filter"]["args"].append(
        {
            "op": "anyinteracts",
            "args": [
                {"property": "datetime"},
                {"interval": [after, to_utc_isoformat(datetime.now(timezone.utc))]},
            ],
        }
    )
    return result


def ensure_ids(fc_path: Path) -> None:
    """Ensure a feature collection has IDs set.

    If not, set them and write out the file.
    """
    with open(fc_path) as f:
        feature_collection = json.load(f)
    write = False
    for feature in feature_collection["features"]:
        feature.setdefault("properties", {})
        if "id" not in feature:
            write = True
            feature["id"] = str(uuid4())
    if write:
        with open(fc_path, "w") as f:
            json.dump(feature_collection, f, indent=2)


def get_datetime(item: pystac.Item) -> datetime:
    dt = item.datetime or item.common_metadata.start_datetime
    if not dt:
        raise ValueError(f"Item {item.id} has no datetime")
    return dt


class TeamsBackgroundGenerator:
    def __init__(self, settings: Settings, force: bool = False):
        self.settings = settings
        self.force = force
        self._render_params_cache: Dict[
            Tuple[str, Optional[str]], Dict[str, Union[str, List[str]]]
        ] = {}

    def should_generate_new_background(self) -> bool:
        image_path = self.settings.get_image_path()
        if not image_path.exists():
            print(f"Image {image_path} does not exist, creating...")
            return True

        image_info_path = self.settings.get_image_info_path()
        if image_info_path.exists():
            image_info = ImageInfo.from_yaml(image_info_path)
            if image_info.is_aoi and self.settings.aois:
                last_changed = image_info.last_changed
                if (
                    last_changed
                    and (datetime.now() - last_changed).days
                    < self.settings.aois.refresh_days
                ):
                    print("Not regenerating AOI image because AOI item is recent")
                    return False

        stats = image_path.stat()
        created_at = datetime.fromtimestamp(stats.st_ctime, tz=timezone.utc)
        accessed_at = datetime.fromtimestamp(stats.st_atime, tz=timezone.utc)
        if (accessed_at - created_at).total_seconds() > 2:
            print("Background image has been read after creating; regenerating")
            return True
        else:
            force_regen_after_time = self.settings.get_force_regen_after_time(
                created_at
            )
            if (
                force_regen_after_time
                and datetime.now(tz=timezone.utc) > force_regen_after_time
            ):
                print(
                    "Background image has not regenerated in a while, regenerating..."
                )
                return True
            else:
                print("Background image has not been read after creating.")
                return False

    def get_base_cql(
        self, collection_id: str, additional_filters: Optional[List[FilterConfig]]
    ) -> Dict[str, Any]:
        return {
            "filter-lang": "cql2-json",
            "filter": {
                "op": "and",
                "args": [
                    {"op": "=", "args": [{"property": "collection"}, collection_id]},
                ]
                + [f.to_cql_op() for f in (additional_filters or [])],
            },
        }

    def get_bg_geom(self, base_geom: Dict[str, Any]) -> Dict[str, Any]:
        geom = shape(base_geom)
        envelope = geom.envelope
        bounds: List[float] = list(envelope.bounds)
        width: float = bounds[2] - bounds[0]
        height: float = bounds[3] - bounds[1]
        if width <= 0 or height <= 0:
            raise ValueError("Target geometry envelope must have a non-zero area")

        target_aspect = self.settings.width / self.settings.height
        envelope_aspect = width / height
        if envelope_aspect >= target_aspect:
            rect_width = width
            rect_height = width / target_aspect
        else:
            rect_width = height * target_aspect
            rect_height = height

        center_x = (bounds[0] + bounds[2]) / 2
        center_y = (bounds[1] + bounds[3]) / 2
        return mapping(
            box(
                center_x - (rect_width / 2),
                center_y - (rect_height / 2),
                center_x + (rect_width / 2),
                center_y + (rect_height / 2),
            )
        )

    def scale_rect(self, rect: Any, scale: float) -> Any:
        rect_minx, rect_miny, rect_maxx, rect_maxy = rect.bounds
        rect_width = rect_maxx - rect_minx
        rect_height = rect_maxy - rect_miny
        return box(
            rect.centroid.x - ((rect_width * scale) / 2),
            rect.centroid.y - ((rect_height * scale) / 2),
            rect.centroid.x + ((rect_width * scale) / 2),
            rect.centroid.y + ((rect_height * scale) / 2),
        )

    def find_rect_within_item_footprint(
        self,
        item_shape,
        desired_rect,
    ):
        center = desired_rect.centroid
        minx, miny, maxx, maxy = item_shape.bounds
        rect_minx, rect_miny, rect_maxx, rect_maxy = desired_rect.bounds
        rect_width = rect_maxx - rect_minx
        rect_height = rect_maxy - rect_miny

        min_center_x = minx + (rect_width / 2)
        max_center_x = maxx - (rect_width / 2)
        min_center_y = miny + (rect_height / 2)
        max_center_y = maxy - (rect_height / 2)
        if min_center_x > max_center_x or min_center_y > max_center_y:
            return None

        center_x_values = linspace(min_center_x, max_center_x, FOOTPRINT_FIT_GRID_SIZE)
        center_y_values = linspace(min_center_y, max_center_y, FOOTPRINT_FIT_GRID_SIZE)
        center_x_values.append(min(max(center.x, min_center_x), max_center_x))
        center_y_values.append(min(max(center.y, min_center_y), max_center_y))

        candidates = {
            (round(candidate_x, 10), round(candidate_y, 10))
            for candidate_x in center_x_values
            for candidate_y in center_y_values
        }

        ranked_candidates = sorted(
            candidates,
            key=lambda candidate: (
                (candidate[0] - center.x) ** 2 + (candidate[1] - center.y) ** 2
            ),
        )
        for candidate_x, candidate_y in ranked_candidates:
            candidate_rect = box(
                candidate_x - (rect_width / 2),
                candidate_y - (rect_height / 2),
                candidate_x + (rect_width / 2),
                candidate_y + (rect_height / 2),
            )
            if item_shape.covers(candidate_rect):
                return candidate_rect

        return None

    def find_land_compliant_rect_within_item_footprint(
        self,
        item_shape: Any,
        desired_rect: Any,
        candidate_land_geometries: List[Any],
    ) -> Tuple[Optional[Any], Optional[float], float]:
        center = desired_rect.centroid
        minx, miny, maxx, maxy = item_shape.bounds
        rect_minx, rect_miny, rect_maxx, rect_maxy = desired_rect.bounds
        rect_width = rect_maxx - rect_minx
        rect_height = rect_maxy - rect_miny

        min_center_x = minx + (rect_width / 2)
        max_center_x = maxx - (rect_width / 2)
        min_center_y = miny + (rect_height / 2)
        max_center_y = maxy - (rect_height / 2)
        if min_center_x > max_center_x or min_center_y > max_center_y:
            return None, None, 0.0

        center_x_values = linspace(min_center_x, max_center_x, FOOTPRINT_FIT_GRID_SIZE)
        center_y_values = linspace(min_center_y, max_center_y, FOOTPRINT_FIT_GRID_SIZE)
        center_x_values.append(min(max(center.x, min_center_x), max_center_x))
        center_y_values.append(min(max(center.y, min_center_y), max_center_y))

        candidates = {
            (round(candidate_x, 10), round(candidate_y, 10))
            for candidate_x in center_x_values
            for candidate_y in center_y_values
        }
        ranked_candidates = sorted(
            candidates,
            key=lambda candidate: (
                (candidate[0] - center.x) ** 2 + (candidate[1] - center.y) ** 2
            ),
        )

        best_land_fraction = 0.0
        land_mask = get_land_mask()
        for candidate_x, candidate_y in ranked_candidates:
            candidate_rect = box(
                candidate_x - (rect_width / 2),
                candidate_y - (rect_height / 2),
                candidate_x + (rect_width / 2),
                candidate_y + (rect_height / 2),
            )
            if not item_shape.covers(candidate_rect):
                continue

            land_fraction = land_mask.get_land_fraction(
                candidate_rect, candidate_land_geometries
            )
            best_land_fraction = max(best_land_fraction, land_fraction)
            if land_fraction >= self.settings.min_land_fraction:
                return candidate_rect, land_fraction, best_land_fraction

        return None, None, best_land_fraction

    def fit_bg_geom_to_item_footprint_without_land_requirement(
        self, base_geom: Dict[str, Any], item_geom: Dict[str, Any]
    ) -> Tuple[
        Optional[Dict[str, Any]], Optional[str], Optional[float], Optional[float]
    ]:
        desired_rect = shape(self.get_bg_geom(base_geom))
        item_shape = shape(item_geom)
        if item_shape.covers(desired_rect):
            return mapping(desired_rect), "requested", None, None

        moved_rect = self.find_rect_within_item_footprint(item_shape, desired_rect)
        if moved_rect is not None:
            print("Moved crop window inside the item footprint.")
            return mapping(moved_rect), "moved-within-item-footprint", None, None

        rect_minx, rect_miny, rect_maxx, rect_maxy = desired_rect.bounds
        rect_width = rect_maxx - rect_minx
        rect_height = rect_maxy - rect_miny

        best_rect = None
        low = 0.0
        high = 1.0
        for _ in range(FOOTPRINT_FIT_SCALE_STEPS):
            scale = (low + high) / 2
            candidate_rect = box(
                desired_rect.centroid.x - ((rect_width * scale) / 2),
                desired_rect.centroid.y - ((rect_height * scale) / 2),
                desired_rect.centroid.x + ((rect_width * scale) / 2),
                desired_rect.centroid.y + ((rect_height * scale) / 2),
            )
            fitted_rect = self.find_rect_within_item_footprint(
                item_shape, candidate_rect
            )
            if fitted_rect is not None:
                low = scale
                best_rect = fitted_rect
            else:
                high = scale

        if best_rect is not None:
            print(
                "Shrank crop window to fit within the item footprint "
                f"({low:.1%} of the original crop size)."
            )
            return mapping(best_rect), "shrunk-within-item-footprint", low, None

        print(
            "Could not fit the desired crop window inside the item footprint. "
            "Using the item footprint bounds."
        )
        return mapping(item_shape.envelope), "item-footprint-envelope", None, None

    def fit_bg_geom_to_item_footprint_with_land_requirement(
        self, base_geom: Dict[str, Any], item_geom: Dict[str, Any]
    ) -> Tuple[
        Optional[Dict[str, Any]], Optional[str], Optional[float], Optional[float]
    ]:
        desired_rect = shape(self.get_bg_geom(base_geom))
        item_shape = shape(item_geom)
        land_mask = get_land_mask()
        candidate_land_geometries = land_mask.get_land_geometries(item_shape)
        if not candidate_land_geometries:
            print(
                "Item footprint does not intersect any Natural Earth land polygons. "
                "Skipping it because no crop can satisfy the land fraction requirement."
            )
            return None, None, None, 0.0

        best_land_fraction = 0.0
        for scale in linspace(1.0, MIN_LAND_FIT_SCALE_RATIO, LAND_FIT_SCALE_STEPS):
            candidate_rect = self.scale_rect(desired_rect, scale)
            fitted_rect, land_fraction, scale_best_land_fraction = (
                self.find_land_compliant_rect_within_item_footprint(
                    item_shape,
                    candidate_rect,
                    candidate_land_geometries,
                )
            )
            best_land_fraction = max(best_land_fraction, scale_best_land_fraction)
            if fitted_rect is None or land_fraction is None:
                continue

            if scale < 1.0:
                print(
                    "Shrank crop window to satisfy the land coverage requirement "
                    f"({scale:.1%} of the original crop size, "
                    f"{land_fraction:.1%} land)."
                )
                return (
                    mapping(fitted_rect),
                    "shrunk-within-item-footprint",
                    scale,
                    land_fraction,
                )

            if item_shape.covers(desired_rect) and fitted_rect.equals(desired_rect):
                print(
                    "Requested crop window satisfies the land coverage requirement "
                    f"({land_fraction:.1%} land)."
                )
                return mapping(fitted_rect), "requested", None, land_fraction

            print(
                "Moved crop window inside the item footprint to satisfy the land "
                f"coverage requirement ({land_fraction:.1%} land)."
            )
            return (
                mapping(fitted_rect),
                "moved-within-item-footprint",
                None,
                land_fraction,
            )

        print(
            "Could not find a crop window with at least "
            f"{self.settings.min_land_fraction:.1%} land coverage. "
            f"Best available crop had {best_land_fraction:.1%} land."
        )
        return None, None, None, best_land_fraction

    def fit_bg_geom_to_item_footprint(
        self, base_geom: Dict[str, Any], item_geom: Dict[str, Any]
    ) -> Tuple[
        Optional[Dict[str, Any]], Optional[str], Optional[float], Optional[float]
    ]:
        if self.settings.min_land_fraction <= 0:
            return self.fit_bg_geom_to_item_footprint_without_land_requirement(
                base_geom, item_geom
            )
        return self.fit_bg_geom_to_item_footprint_with_land_requirement(
            base_geom, item_geom
        )

    def crop_fit_scale_ratio_is_acceptable(
        self,
        crop_fit_strategy: Optional[str],
        crop_fit_scale_ratio: Optional[float],
    ) -> bool:
        if self.settings.min_crop_fit_scale_ratio <= 0:
            return True
        if crop_fit_strategy != "shrunk-within-item-footprint":
            return True
        if crop_fit_scale_ratio is None:
            return True
        return crop_fit_scale_ratio >= self.settings.min_crop_fit_scale_ratio

    def reject_if_overzoomed(
        self,
        crop_fit_strategy: Optional[str],
        crop_fit_scale_ratio: Optional[float],
        context: str,
    ) -> bool:
        if self.crop_fit_scale_ratio_is_acceptable(
            crop_fit_strategy, crop_fit_scale_ratio
        ):
            return False
        print(
            f"Skipping {context} because it would retain only "
            f"{crop_fit_scale_ratio:.1%} of the requested crop size "
            f"(minimum {self.settings.min_crop_fit_scale_ratio:.1%})."
        )
        return True

    def get_render_geom(
        self, item: pystac.Item, target_geom: Dict[str, Any], is_aoi: bool
    ) -> Tuple[Dict[str, Any], str]:
        if not is_aoi or not item.geometry:
            return target_geom, "item"

        aoi_shape = shape(target_geom)
        item_shape = shape(item.geometry)
        intersection = aoi_shape.intersection(item_shape)
        if intersection.is_empty:
            print(
                "AOI does not overlap the selected item footprint. "
                "Using the item footprint instead."
            )
            return item.geometry, "item-footprint-fallback"

        if aoi_shape.area == 0:
            return mapping(intersection), "aoi-item-overlap"

        coverage_ratio = intersection.area / aoi_shape.area
        if coverage_ratio >= MIN_AOI_ITEM_COVERAGE_RATIO:
            return target_geom, "aoi"

        print(
            "AOI is much larger than the selected item coverage "
            f"({coverage_ratio:.1%} of the AOI). "
            "Using the AOI/item overlap to avoid a mostly black background."
        )
        return mapping(intersection), "aoi-item-overlap"

    def get_render_params(
        self, collection_id: str, render_options_name: Optional[str] = None
    ) -> Dict[str, Union[str, List[str]]]:
        cache_key = (collection_id, render_options_name)
        if cache_key in self._render_params_cache:
            return deepcopy(self._render_params_cache[cache_key])

        resp = requests.get(
            self.settings.apis.info,
            params={"collection": collection_id},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        mosaic_info = resp.json()
        render_options = mosaic_info.get("renderOptions") or []
        if not render_options:
            raise SettingsError(
                f"No render options found for collection {collection_id}."
            )

        selected_option = render_options[0]
        if render_options_name:
            selected_option = next(
                (
                    option
                    for option in render_options
                    if option["name"] == render_options_name
                ),
                None,
            )
            if selected_option is None:
                available_options = ", ".join(
                    option["name"] for option in render_options
                )
                raise SettingsError(
                    f"Render option '{render_options_name}' was not found for "
                    f"{collection_id}. Available options: {available_options}"
                )

        result = parse_render_options(selected_option["options"])
        self._render_params_cache[cache_key] = deepcopy(result)
        return result

    def fetch_image(
        self,
        item: pystac.Item,
        geometry: Dict[str, Any],
        render_params: Dict[str, Union[str, List[str]]],
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> PILImage:
        collection_id = item.collection_id
        if not collection_id:
            raise ValueError(f"Item {item.id} has no collection_id")

        item_crop_url = self.settings.apis.item_crop.rstrip("/")
        target_width = width or self.settings.width
        target_height = height or self.settings.height
        resp = requests.post(
            f"{item_crop_url}/{target_width}x{target_height}.png",
            params={
                "collection": collection_id,
                "item": item.id,
                "resampling": "bilinear",
                **render_params,
            },
            json=make_feature(geometry),
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        with Image.open(io.BytesIO(resp.content)) as image:
            return image.copy()

    @staticmethod
    def bbox_from_scale(
        center_lon: float, center_lat: float, scale_km: float, aspect: float
    ) -> Dict[str, Any]:
        """Compute a GeoJSON bbox geometry from center + scale_km."""
        km_per_deg_lat = 111.32
        km_per_deg_lon = 111.32 * math.cos(math.radians(center_lat))
        if km_per_deg_lon < 1:
            km_per_deg_lon = 1

        width_deg = scale_km / km_per_deg_lon
        height_deg = (scale_km / aspect) / km_per_deg_lat

        return mapping(
            box(
                center_lon - width_deg / 2,
                center_lat - height_deg / 2,
                center_lon + width_deg / 2,
                center_lat + height_deg / 2,
            )
        )

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _normalize_preferred_months(self, value: Any) -> List[int]:
        if not isinstance(value, list):
            return []
        months: List[int] = []
        for item in value:
            month = self._coerce_int(item)
            if month is None or month < 1 or month > 12:
                continue
            if month not in months:
                months.append(month)
        return months

    def _normalize_scale_options(self, template: Dict[str, Any]) -> List[float]:
        raw_values = template.get("scale_options_km")
        if not isinstance(raw_values, list):
            raw_values = []
        if not raw_values:
            fallback_scale = self._coerce_float(template.get("scale_km"))
            if fallback_scale is not None:
                raw_values = [fallback_scale * 0.75, fallback_scale, fallback_scale * 1.35]

        scale_options: List[float] = []
        for value in raw_values:
            scale_km = self._coerce_float(value)
            if scale_km is None or scale_km < 4 or scale_km > 250:
                continue
            rounded = round(scale_km, 1)
            if rounded not in scale_options:
                scale_options.append(rounded)

        if not scale_options:
            scale_options = [12.0, 24.0, 40.0]

        return scale_options[:4]

    def normalize_discovered_template(
        self,
        template: Dict[str, Any],
        index: int,
        seen_ids: Set[str],
    ) -> Optional[Dict[str, Any]]:
        name = str(
            template.get("name") or template.get("title") or template.get("location") or ""
        ).strip()
        location = str(template.get("location") or name).strip()
        center_lon = self._coerce_float(template.get("center_lon"))
        center_lat = self._coerce_float(template.get("center_lat"))
        if (
            not name
            or not location
            or center_lon is None
            or center_lat is None
            or center_lon < -180
            or center_lon > 180
            or center_lat < -90
            or center_lat > 90
        ):
            return None

        template_id = slugify_text(str(template.get("id") or name or location))
        if not template_id:
            template_id = f"dynamic-{index:02d}"
        while template_id in seen_ids:
            template_id = f"{template_id}-{index:02d}"
        seen_ids.add(template_id)

        preferred_months = self._normalize_preferred_months(
            template.get("preferred_months")
        ) or [datetime.now(timezone.utc).month]
        search_days = self._coerce_int(template.get("search_days"))
        if search_days is None:
            search_days = min(
                (
                    collection.search_days or 30
                    for collection in self.settings.collections
                ),
                default=30,
            )
        search_days = max(7, min(90, search_days))

        max_cloud_cover = self._coerce_float(template.get("max_cloud_cover"))
        if max_cloud_cover is None:
            max_cloud_cover = 12.0
        max_cloud_cover = max(0.0, min(80.0, max_cloud_cover))

        min_land_fraction = self._coerce_float(template.get("min_land_fraction"))
        if min_land_fraction is None:
            min_land_fraction = self.settings.min_land_fraction
        min_land_fraction = max(0.0, min(1.0, min_land_fraction))

        expected_visual_signatures = [
            str(signature).strip()
            for signature in template.get("expected_visual_signatures", [])
            if str(signature).strip()
        ][:5]
        if not expected_visual_signatures:
            expected_visual_signatures = [
                "large-scale visible geometry",
                "clear color or texture contrast",
            ]

        tags = [
            str(tag).strip()
            for tag in template.get("tags", [])
            if str(tag).strip()
        ][:6]
        if "dynamic" not in tags:
            tags.append("dynamic")

        render_hint = str(
            template.get("render_hint")
            or self.settings.collections[0].rendering_option
            or ""
        ).strip()
        story_seed = str(
            template.get("story_seed")
            or template.get("description")
            or f"A timely satellite view near {location}."
        ).strip()
        conversation_seed = str(
            template.get("conversation_seed")
            or template.get("conversation_starter")
            or f"What is happening here right now, and why does it read so clearly from orbit?"
        ).strip()
        backup_caption = str(
            template.get("backup_caption_if_signature_missing")
            or f"A recent satellite view near {location}."
        ).strip()
        timeliness_seed = str(
            template.get("timeliness_seed") or template.get("timeliness") or ""
        ).strip()
        why_visible_in_s2_seed = str(
            template.get("why_visible_in_s2_seed")
            or template.get("why_visible_in_s2")
            or ""
        ).strip()
        discovery_source = str(
            template.get("discovery_source") or template.get("source_kind") or "dynamic"
        ).strip()
        discovery_rationale = str(
            template.get("discovery_rationale") or template.get("why_now") or ""
        ).strip()

        return {
            "id": template_id,
            "name": name,
            "location": location,
            "center_lon": round(center_lon, 6),
            "center_lat": round(center_lat, 6),
            "scale_options_km": self._normalize_scale_options(template),
            "preferred_months": preferred_months,
            "search_days": search_days,
            "max_cloud_cover": max_cloud_cover,
            "min_land_fraction": min_land_fraction,
            "render_hint": render_hint,
            "expected_visual_signatures": expected_visual_signatures,
            "story_seed": story_seed,
            "conversation_seed": conversation_seed,
            "backup_caption_if_signature_missing": backup_caption,
            "timeliness_seed": timeliness_seed,
            "why_visible_in_s2_seed": why_visible_in_s2_seed,
            "discovery_source": discovery_source,
            "discovery_rationale": discovery_rationale,
            "tags": tags,
        }

    def discover_ai_templates(self) -> List[Dict[str, Any]]:
        ai_config = self.settings.ai_suggestions
        if not ai_config:
            return []

        script_path = Path(__file__).parent / "scripts" / "discover_phenomena.mjs"
        if not script_path.exists():
            print(f"AI discovery script not found at {script_path}")
            return []

        from gallery import summarize_ai_history

        payload = {
            "max_templates": ai_config.max_templates,
            "history_summary": summarize_ai_history(ai_config.history_limit),
            "stac_url": self.settings.apis.stac.rstrip("/"),
            "aspect": self.settings.width / self.settings.height,
            "collections": [
                {
                    "id": collection.id,
                    "rendering_option": collection.rendering_option,
                    "search_days": collection.search_days,
                    "filters": [
                        filter_config.dict()
                        for filter_config in (collection.filters or [])
                    ],
                }
                for collection in self.settings.collections
            ],
        }
        discovery_timeout_seconds = max(ai_config.timeout_seconds, 120)
        timeout_ms = str(discovery_timeout_seconds * 1000)
        try:
            print("Discovering dynamic phenomena from current context...")
            result = subprocess.run(
                [
                    "node",
                    str(script_path),
                    "--stdin",
                    "--model",
                    ai_config.model,
                    "--timeout",
                    timeout_ms,
                ],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=discovery_timeout_seconds + 30,
                creationflags=NODE_SUBPROCESS_CREATIONFLAGS,
            )
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    print(f"  [AI discover] {line}")
            data = json.loads(result.stdout or "{}")
        except Exception as exc:
            print(f"Dynamic discovery failed: {exc}")
            return []

        raw_templates = data.get("templates") if isinstance(data, dict) else data
        if not isinstance(raw_templates, list):
            print("Dynamic discovery did not return a template list.")
            return []

        normalized_templates: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        for index, template in enumerate(raw_templates, start=1):
            if not isinstance(template, dict):
                continue
            normalized_template = self.normalize_discovered_template(
                template, index, seen_ids
            )
            if normalized_template is None:
                continue
            normalized_templates.append(normalized_template)
            if len(normalized_templates) >= ai_config.max_templates:
                break

        print(f"Discovered {len(normalized_templates)} dynamic phenomena.")
        return normalized_templates

    def get_ai_base_cql(
        self,
        collection_id: str,
        collection_filters: Optional[List[FilterConfig]],
        template: Dict[str, Any],
    ) -> Dict[str, Any]:
        filters = list(collection_filters or [])
        max_cloud_cover = template.get("max_cloud_cover")
        if max_cloud_cover is not None:
            filters.append(
                FilterConfig(
                    property="eo:cloud_cover",
                    op="<=",
                    value=max_cloud_cover,
                )
            )
        return self.get_base_cql(collection_id, filters)

    def build_default_story(
        self,
        candidate: CandidateVariant,
        selection_reason: str = "",
    ) -> Dict[str, Any]:
        template = candidate.template
        return {
            "name": template["name"],
            "description": template["story_seed"],
            "conversation_starter": template["conversation_seed"],
            "timeliness": template.get("timeliness_seed")
            or f"Recent low-cloud imagery is currently available over {template['location']}.",
            "why_visible_in_s2": template.get("why_visible_in_s2_seed")
            or (
                "Sentinel-2 can resolve "
                + ", ".join(template["expected_visual_signatures"][:2])
                + "."
            ),
            "expected_visual_signatures": list(template["expected_visual_signatures"]),
            "backup_caption_if_signature_missing": template[
                "backup_caption_if_signature_missing"
            ],
            "source_template_id": template["id"],
            "_selection": {
                "selection_reason": selection_reason,
                "selected_preview_id": candidate.preview_id,
                "alternate_preview_ids": [],
            },
        }

    def complete_story_fields(
        self,
        story: Optional[Dict[str, Any]],
        candidate: CandidateVariant,
    ) -> Dict[str, Any]:
        result = deepcopy(story or {})
        defaults = self.build_default_story(
            candidate,
            result.get("_selection", {}).get("selection_reason", ""),
        )
        for key, value in defaults.items():
            if key not in result or result[key] in (None, "", []):
                result[key] = value
        result.setdefault(
            "expected_visual_signatures",
            list(candidate.template["expected_visual_signatures"]),
        )
        result.setdefault(
            "backup_caption_if_signature_missing",
            candidate.template["backup_caption_if_signature_missing"],
        )
        result.setdefault("source_template_id", candidate.template["id"])
        result.setdefault("scale_km", candidate.scale_km)
        result.setdefault("location", candidate.template["location"])
        return result

    def build_candidate_variant(
        self,
        template: Dict[str, Any],
        item: pystac.Item,
        scale_km: float,
    ) -> Optional[CandidateVariant]:
        if not item.geometry:
            return None

        collection_id = item.collection_id
        if not collection_id:
            return None

        collection_config = self.settings.get_collection_config(collection_id)
        rendering_option_name = (
            template.get("render_hint") or collection_config.rendering_option
        )
        render_params = self.get_render_params(collection_id, rendering_option_name)
        requested_geometry = self.bbox_from_scale(
            template["center_lon"],
            template["center_lat"],
            scale_km,
            self.settings.width / self.settings.height,
        )
        effective_min_land_fraction = template.get("min_land_fraction")
        if effective_min_land_fraction is None:
            effective_min_land_fraction = self.settings.min_land_fraction

        saved_min_land = self.settings.min_land_fraction
        self.settings.min_land_fraction = effective_min_land_fraction
        try:
            crop_geometry, crop_fit_strategy, crop_fit_scale_ratio, land_fraction = (
                self.fit_bg_geom_to_item_footprint(requested_geometry, item.geometry)
            )
        finally:
            self.settings.min_land_fraction = saved_min_land

        if crop_geometry is None or crop_fit_strategy is None:
            return None
        if self.reject_if_overzoomed(
            crop_fit_strategy,
            crop_fit_scale_ratio,
            f"preview candidate {item.id} at {scale_km:.1f}km",
        ):
            return None

        preview_image = self.fetch_image(
            item,
            crop_geometry,
            render_params,
            width=self.settings.ai_suggestions.preview_width
            if self.settings.ai_suggestions
            else None,
            height=self.settings.ai_suggestions.preview_height
            if self.settings.ai_suggestions
            else None,
        )
        return CandidateVariant(
            preview_id="",
            template=template,
            target_item=item,
            scale_km=scale_km,
            requested_geometry=requested_geometry,
            crop_geometry=crop_geometry,
            render_params=render_params,
            crop_fit_strategy=crop_fit_strategy,
            crop_fit_scale_ratio=crop_fit_scale_ratio,
            land_fraction=land_fraction,
            effective_min_land_fraction=effective_min_land_fraction,
            preview_image=preview_image,
            collection_id=collection_id,
            rendering_option_name=rendering_option_name,
        )

    def build_ai_candidate_pool(self) -> Tuple[List[CandidateVariant], str]:
        ai_config = self.settings.ai_suggestions
        if not ai_config or not ai_config.enabled:
            return [], ""

        print("Scanning real imagery to build an AI candidate pool...")
        client = Client.open(self.settings.apis.stac)
        aspect = self.settings.width / self.settings.height
        raw_candidates: List[CandidateVariant] = []
        availability_lines: List[str] = []

        for template in self.discover_ai_templates():
            search_scale = max(template["scale_options_km"]) * 1.2
            search_geom = self.bbox_from_scale(
                template["center_lon"],
                template["center_lat"],
                search_scale,
                aspect,
            )
            template_candidates: List[CandidateVariant] = []
            selected_items: List[pystac.Item] = []

            for collection_config in self.settings.collections:
                base_cql = self.get_ai_base_cql(
                    collection_config.id,
                    collection_config.filters,
                    template,
                )
                cql = cql_add_geom_arg(base_cql, search_geom)
                search_after = to_utc_isoformat(
                    datetime.now(timezone.utc)
                    - timedelta(days=template.get("search_days", collection_config.search_days))
                )
                cql = cql_add_after_arg(cql, search_after)

                items = list(
                    client.search(
                        filter=cql,
                        max_items=max(ai_config.max_items_per_template * 4, 6),
                    ).items()
                )
                items = sorted(
                    items,
                    key=lambda item: (
                        item.properties.get("eo:cloud_cover")
                        if item.properties.get("eo:cloud_cover") is not None
                        else 999.0,
                        -get_datetime(item).timestamp(),
                    ),
                )

                for item in items:
                    if any(existing.id == item.id for existing in selected_items):
                        continue
                    selected_items.append(item)
                    if len(selected_items) >= ai_config.max_items_per_template:
                        break

            for item in selected_items:
                for scale_km in template["scale_options_km"]:
                    try:
                        candidate = self.build_candidate_variant(template, item, scale_km)
                    except requests.RequestException as exc:
                        print(
                            f"  Skipping preview for {template['name']} on {item.id}: {exc}"
                        )
                        continue
                    if candidate is not None:
                        template_candidates.append(candidate)

            if template_candidates:
                raw_candidates.extend(template_candidates)
                best_cloud = min(
                    (
                        candidate.target_item.properties.get("eo:cloud_cover", 999.0)
                        for candidate in template_candidates
                    ),
                    default=999.0,
                )
                unique_dates = sorted(
                    {
                        str(get_datetime(candidate.target_item).date())
                        for candidate in template_candidates
                    }
                )
                availability_lines.append(
                    f"{template['name']}: {len(template_candidates)} preview(s), "
                    f"best cloud {best_cloud:.1f}%, dates {', '.join(unique_dates[:3])}"
                )
            else:
                availability_lines.append(
                    f"{template['name']}: no usable recent low-cloud imagery"
                )

        candidates = raw_candidates[: ai_config.max_preview_candidates]
        for idx, candidate in enumerate(candidates, start=1):
            candidate.preview_id = f"C{idx:02d}"

        print(f"Built {len(candidates)} preview candidates from current imagery.")
        return candidates, "\n".join(availability_lines)

    def build_candidate_preview_sheet(
        self, candidates: List[CandidateVariant]
    ) -> PILImage:
        ai_config = self.settings.ai_suggestions
        if not ai_config:
            raise SettingsError("AI suggestions are not configured.")

        columns = min(AI_PREVIEW_COLUMNS, max(1, len(candidates)))
        rows = math.ceil(len(candidates) / columns)
        label_height = 56
        header_height = 42
        tile_width = ai_config.preview_width
        tile_height = ai_config.preview_height
        sheet_width = AI_PREVIEW_PADDING + columns * (tile_width + AI_PREVIEW_PADDING)
        sheet_height = (
            header_height
            + AI_PREVIEW_PADDING
            + rows * (tile_height + label_height + AI_PREVIEW_PADDING)
        )
        sheet = Image.new("RGB", (sheet_width, sheet_height), (13, 17, 23))
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default()
        draw.text(
            (AI_PREVIEW_PADDING, 12),
            "Actual recent candidate imagery - choose from what really exists",
            fill=(230, 237, 243),
            font=font,
        )

        for index, candidate in enumerate(candidates):
            column = index % columns
            row = index // columns
            origin_x = AI_PREVIEW_PADDING + column * (tile_width + AI_PREVIEW_PADDING)
            origin_y = (
                header_height
                + AI_PREVIEW_PADDING
                + row * (tile_height + label_height + AI_PREVIEW_PADDING)
            )

            frame = Image.new("RGB", (tile_width, tile_height), (0, 0, 0))
            preview = candidate.preview_image.copy()
            preview.thumbnail((tile_width, tile_height))
            paste_x = (tile_width - preview.width) // 2
            paste_y = (tile_height - preview.height) // 2
            frame.paste(preview, (paste_x, paste_y))
            sheet.paste(frame, (origin_x, origin_y))

            draw.rectangle(
                (origin_x, origin_y, origin_x + tile_width, origin_y + tile_height),
                outline=(48, 54, 61),
            )
            draw.rectangle(
                (origin_x, origin_y, origin_x + 54, origin_y + 22),
                fill=(31, 111, 235),
            )
            draw.text(
                (origin_x + 8, origin_y + 6),
                candidate.preview_id,
                fill=(255, 255, 255),
                font=font,
            )

            label_y = origin_y + tile_height + 6
            cloud_cover = candidate.target_item.properties.get("eo:cloud_cover")
            cloud_text = (
                f"{cloud_cover:.1f}% cloud"
                if isinstance(cloud_cover, (int, float))
                else "cloud n/a"
            )
            draw.text(
                (origin_x, label_y),
                candidate.template["name"][:40],
                fill=(230, 237, 243),
                font=font,
            )
            draw.text(
                (origin_x, label_y + 14),
                (
                    f"{candidate.scale_km:.0f}km | "
                    f"{get_datetime(candidate.target_item).date()} | {cloud_text}"
                ),
                fill=(139, 148, 158),
                font=font,
            )
            draw.text(
                (origin_x, label_y + 28),
                candidate.template["location"][:42],
                fill=(139, 148, 158),
                font=font,
            )

        return sheet

    def select_candidate_from_pool(
        self,
        candidates: List[CandidateVariant],
        availability_summary: str,
        excluded_preview_ids: List[str],
    ) -> Optional[Tuple[CandidateVariant, Dict[str, Any]]]:
        ai_config = self.settings.ai_suggestions
        if not ai_config:
            return None

        script_path = Path(__file__).parent / "scripts" / "suggest_location.mjs"
        if not script_path.exists():
            print(f"AI selector script not found at {script_path}")
            return None

        filtered_candidates = [
            candidate
            for candidate in candidates
            if candidate.preview_id not in excluded_preview_ids
        ]
        if not filtered_candidates:
            return None

        from gallery import summarize_ai_history

        preview_sheet = self.build_candidate_preview_sheet(filtered_candidates)
        preview_buffer = io.BytesIO()
        preview_sheet.save(preview_buffer, "JPEG", quality=85)
        preview_sheet_base64 = base64.b64encode(preview_buffer.getvalue()).decode("ascii")
        payload = {
            "preview_sheet_base64": preview_sheet_base64,
            "candidates": [
                candidate.to_selector_summary() for candidate in filtered_candidates
            ],
            "availability_summary": availability_summary,
            "learning_summary": summarize_ai_history(ai_config.history_limit),
            "excluded_preview_ids": excluded_preview_ids,
        }

        timeout_ms = str(ai_config.timeout_seconds * 1000)
        try:
            print("Letting the AI choose from the real candidate pool...")
            result = subprocess.run(
                [
                    "node",
                    str(script_path),
                    "--stdin",
                    "--model",
                    ai_config.model,
                    "--timeout",
                    timeout_ms,
                ],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=ai_config.timeout_seconds + 30,
                creationflags=NODE_SUBPROCESS_CREATIONFLAGS,
            )
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    print(f"  [AI select] {line}")
            data = json.loads(result.stdout or "{}")
        except Exception as exc:
            print(f"AI candidate selection failed, falling back to first candidate: {exc}")
            data = {}

        selected_preview_id = data.get("selected_preview_id")
        selected_candidate = next(
            (
                candidate
                for candidate in filtered_candidates
                if candidate.preview_id == selected_preview_id
            ),
            filtered_candidates[0],
        )
        story = self.complete_story_fields(data.get("story"), selected_candidate)
        alternate_preview_ids = [
            preview_id
            for preview_id in data.get("alternate_preview_ids", [])
            if any(
                candidate.preview_id == preview_id for candidate in filtered_candidates
            )
            and preview_id != selected_candidate.preview_id
        ]
        story["_selection"] = {
            "selection_reason": data.get(
                "selection_reason",
                "Fell back to the first candidate because the selector did not return a valid choice.",
            ),
            "selected_preview_id": selected_candidate.preview_id,
            "alternate_preview_ids": alternate_preview_ids,
        }
        return selected_candidate, story

    def render_selected_candidate(
        self,
        candidate: CandidateVariant,
        center_lon: float,
        center_lat: float,
        scale_km: float,
        min_land_fraction: float,
    ) -> Optional[Tuple[PILImage, Dict[str, Any]]]:
        if not candidate.target_item.geometry:
            return None

        requested_geometry = self.bbox_from_scale(
            center_lon,
            center_lat,
            scale_km,
            self.settings.width / self.settings.height,
        )
        saved_min_land = self.settings.min_land_fraction
        self.settings.min_land_fraction = min_land_fraction
        try:
            crop_geometry, crop_fit_strategy, crop_fit_scale_ratio, land_fraction = (
                self.fit_bg_geom_to_item_footprint(
                    requested_geometry, candidate.target_item.geometry
                )
            )
        finally:
            self.settings.min_land_fraction = saved_min_land

        if crop_geometry is None or crop_fit_strategy is None:
            return None
        if self.reject_if_overzoomed(
            crop_fit_strategy,
            crop_fit_scale_ratio,
            f"candidate {candidate.preview_id}",
        ):
            return None

        rendered = self.fetch_image(
            candidate.target_item,
            crop_geometry,
            candidate.render_params,
        )
        crop_center = shape(crop_geometry).centroid
        return rendered, {
            "requested_geometry": requested_geometry,
            "crop_geometry": crop_geometry,
            "crop_fit_strategy": crop_fit_strategy,
            "crop_fit_scale_ratio": crop_fit_scale_ratio,
            "land_fraction": land_fraction,
            "scale_km": scale_km,
            "center_lon": round(crop_center.x, 5),
            "center_lat": round(crop_center.y, 5),
            "min_land_fraction": min_land_fraction,
        }

    def record_ai_history(
        self,
        candidate: CandidateVariant,
        story: Dict[str, Any],
        verdict: Dict[str, Any],
        scale_km: float,
    ) -> None:
        from gallery import append_ai_history

        append_ai_history(
            {
                "timestamp": to_utc_isoformat(datetime.now(timezone.utc)),
                "template_id": candidate.template["id"],
                "template_name": candidate.template["name"],
                "preview_id": candidate.preview_id,
                "title": story.get("name"),
                "item_id": candidate.target_item.id,
                "verdict": verdict.get("verdict"),
                "assessment": verdict.get("assessment"),
                "visual_quality_score": verdict.get("visual_quality_score"),
                "story_match_score": verdict.get("story_match_score"),
                "conversation_score": verdict.get("conversation_score"),
                "scale_km": scale_km,
                "captured_at": to_utc_isoformat(get_datetime(candidate.target_item)),
            }
        )

    def merge_salvaged_story(
        self,
        story: Dict[str, Any],
        verdict: Dict[str, Any],
        candidate: CandidateVariant,
    ) -> Dict[str, Any]:
        merged = self.complete_story_fields(story, candidate)
        salvaged_story = verdict.get("salvaged_story") or {}
        for key in (
            "name",
            "description",
            "conversation_starter",
            "timeliness",
            "why_visible_in_s2",
            "backup_caption_if_signature_missing",
        ):
            if salvaged_story.get(key):
                merged[key] = salvaged_story[key]

        merged["_salvage"] = {
            "applied": True,
            "assessment": verdict.get("assessment"),
        }
        return merged

    def verify_rendered_image(
        self,
        image: PILImage,
        suggestion: Dict[str, Any],
        crop_metadata: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Send rendered image to the AI for verification."""
        ai_config = self.settings.ai_suggestions
        if not ai_config or not ai_config.verify_images:
            return {
                "verdict": "accept",
                "confidence": 5,
                "visual_quality_score": 5,
                "story_match_score": 5,
                "conversation_score": 5,
                "assessment": "Verification disabled.",
            }

        script_path = Path(__file__).parent / "scripts" / "verify_image.mjs"
        if not script_path.exists():
            return {
                "verdict": "accept",
                "confidence": 1,
                "visual_quality_score": 3,
                "story_match_score": 3,
                "conversation_score": 3,
                "assessment": "Verify script not found.",
            }

        image_buffer = io.BytesIO()
        image.convert("RGB").save(image_buffer, "JPEG", quality=86)
        image_base64 = base64.b64encode(image_buffer.getvalue()).decode("ascii")
        input_data = json.dumps(
            {
                "image_base64": image_base64,
                "suggestion": suggestion,
                "crop_metadata": crop_metadata,
            }
        )
        timeout_ms = str(ai_config.timeout_seconds * 1000)

        try:
            print("  Verifying final render with AI vision...")
            result = subprocess.run(
                [
                    "node",
                    str(script_path),
                    "--stdin",
                    "--model",
                    ai_config.model,
                    "--timeout",
                    timeout_ms,
                ],
                input=input_data,
                capture_output=True,
                text=True,
                timeout=ai_config.timeout_seconds + 30,
                creationflags=NODE_SUBPROCESS_CREATIONFLAGS,
            )
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    print(f"    [verify] {line}")

            verdict = json.loads(result.stdout or "{}")
            print(
                f"  Verdict: {verdict.get('verdict')} "
                f"(confidence {verdict.get('confidence', '?')}/5)"
            )
            if verdict.get("visual_quality_score") is not None:
                print(
                    "  Scores: "
                    f"visual {verdict.get('visual_quality_score')}/5, "
                    f"story {verdict.get('story_match_score')}/5, "
                    f"conversation {verdict.get('conversation_score')}/5"
                )
            if verdict.get("assessment"):
                print(f"  Assessment: {verdict['assessment']}")
            return verdict
        except Exception as exc:
            print(f"  Verification failed (non-fatal): {exc}")
            return {
                "verdict": "accept",
                "confidence": 1,
                "visual_quality_score": 3,
                "story_match_score": 3,
                "conversation_score": 3,
                "assessment": f"Verification error: {exc}",
            }

    def try_ai_suggestions(
        self,
    ) -> Optional[Tuple[PILImage, Dict[str, Any], Dict[str, Any]]]:
        """Select from actual imagery candidates, then verify or salvage the final render."""
        ai_config = self.settings.ai_suggestions
        if not ai_config or not ai_config.enabled:
            return None

        candidates, availability_summary = self.build_ai_candidate_pool()
        if not candidates:
            return None

        excluded_preview_ids: List[str] = []
        attempts = 0
        while attempts < ai_config.max_suggestions_tried:
            selection = self.select_candidate_from_pool(
                candidates,
                availability_summary,
                excluded_preview_ids,
            )
            if not selection:
                break

            candidate, story = selection
            attempts += 1
            story = self.complete_story_fields(story, candidate)
            print(
                f"AI selected {candidate.preview_id}: {story.get('name', candidate.template['name'])}"
            )

            requested_center = shape(candidate.requested_geometry).centroid
            current_center_lon = requested_center.x
            current_center_lat = requested_center.y
            current_scale_km = candidate.scale_km
            current_min_land_fraction = candidate.effective_min_land_fraction

            for adjust_round in range(ai_config.max_adjust_rounds + 1):
                render_result = self.render_selected_candidate(
                    candidate,
                    current_center_lon,
                    current_center_lat,
                    current_scale_km,
                    current_min_land_fraction,
                )
                if render_result is None:
                    print(
                        f"  Could not render candidate {candidate.preview_id} "
                        f"at {current_scale_km:.1f}km."
                    )
                    break

                rendered, render_meta = render_result
                crop_metadata = {
                    "preview_id": candidate.preview_id,
                    "template_id": candidate.template["id"],
                    "center_lat": render_meta["center_lat"],
                    "center_lon": render_meta["center_lon"],
                    "scale_km": render_meta["scale_km"],
                    "acquired_at": to_utc_isoformat(get_datetime(candidate.target_item)),
                    "land_fraction": render_meta["land_fraction"],
                    "cloud_cover": candidate.target_item.properties.get("eo:cloud_cover"),
                }

                if not ai_config.verify_images:
                    story["_verification"] = {
                        "verdict": "accept",
                        "confidence": 5,
                        "assessment": "Verification disabled.",
                    }
                    return self._finalize_ai_image(
                        rendered,
                        candidate.target_item,
                        render_meta["crop_geometry"],
                        render_meta["requested_geometry"],
                        candidate.render_params,
                        render_meta["crop_fit_strategy"],
                        render_meta["crop_fit_scale_ratio"],
                        render_meta["land_fraction"],
                        story,
                        current_scale_km,
                        current_min_land_fraction,
                    )

                verdict = self.verify_rendered_image(rendered, story, crop_metadata)
                self.record_ai_history(candidate, story, verdict, current_scale_km)
                verdict_type = verdict.get("verdict", "accept")

                if verdict_type == "accept":
                    story["_verification"] = verdict
                    return self._finalize_ai_image(
                        rendered,
                        candidate.target_item,
                        render_meta["crop_geometry"],
                        render_meta["requested_geometry"],
                        candidate.render_params,
                        render_meta["crop_fit_strategy"],
                        render_meta["crop_fit_scale_ratio"],
                        render_meta["land_fraction"],
                        story,
                        current_scale_km,
                        current_min_land_fraction,
                    )

                if verdict_type == "salvage":
                    if ai_config.salvage_images:
                        story = self.merge_salvaged_story(story, verdict, candidate)
                    story["_verification"] = verdict
                    return self._finalize_ai_image(
                        rendered,
                        candidate.target_item,
                        render_meta["crop_geometry"],
                        render_meta["requested_geometry"],
                        candidate.render_params,
                        render_meta["crop_fit_strategy"],
                        render_meta["crop_fit_scale_ratio"],
                        render_meta["land_fraction"],
                        story,
                        current_scale_km,
                        current_min_land_fraction,
                    )

                if verdict_type == "adjust" and adjust_round < ai_config.max_adjust_rounds:
                    adjustments = verdict.get("adjustments") or {}
                    if adjustments.get("center_lon") is not None:
                        current_center_lon = adjustments["center_lon"]
                    if adjustments.get("center_lat") is not None:
                        current_center_lat = adjustments["center_lat"]
                    if adjustments.get("scale_km") is not None:
                        current_scale_km = adjustments["scale_km"]
                    if adjustments.get("min_land_fraction") is not None:
                        current_min_land_fraction = adjustments["min_land_fraction"]
                    reason = adjustments.get("reason") or "No reason provided."
                    print(f"  Adjusting candidate {candidate.preview_id}: {reason}")
                    continue

                print(
                    f"  Rejecting candidate {candidate.preview_id}: "
                    f"{verdict.get('assessment', 'No assessment provided.')}"
                )
                excluded_preview_ids.append(candidate.preview_id)
                break

        print("No AI candidates passed selection and verification.")
        return None

    def _finalize_ai_image(
        self,
        rendered: PILImage,
        item: pystac.Item,
        bg_geom: Dict[str, Any],
        render_geom: Dict[str, Any],
        render_params: Dict[str, Any],
        strategy: str,
        scale_ratio: Optional[float],
        land_frac: Optional[float],
        suggestion: Dict[str, Any],
        scale_km: float,
        min_land_fraction: float,
    ) -> Tuple[PILImage, Dict[str, Any], Dict[str, Any]]:
        """Package a successful AI render into the tuple expected by generate()."""
        collection_id = item.collection_id
        assert collection_id
        collection_config = self.settings.get_collection_config(collection_id)
        cql = self.get_base_cql(collection_id, collection_config.filters)

        finalized_suggestion = deepcopy(suggestion)
        finalized_suggestion["scale_km"] = scale_km
        finalized_suggestion["selected_min_land_fraction"] = min_land_fraction

        info = ImageInfo(
            target_item=item.to_dict(),
            cql=cql,
            render_params=render_params,
            is_aoi=False,
            last_changed=datetime.now(),
            source_geometry_kind="ai-selection",
            requested_geometry=render_geom,
            crop_geometry=bg_geom,
            crop_fit_strategy=strategy,
            crop_fit_scale_ratio=scale_ratio,
            land_fraction=land_frac,
            min_land_fraction=min_land_fraction,
            land_mask_source="natural-earth-50m",
            ai_suggestion=finalized_suggestion,
        )
        return rendered, info.dict(), finalized_suggestion

    def get_target_items(self) -> List[pystac.Item]:
        client = Client.open(self.settings.apis.stac)
        target_aoi_items: List[pystac.Item] = []
        target_random_items: List[pystac.Item] = []

        for collection_config in self.settings.collections:
            collection_id = collection_config.id
            search_after = to_utc_isoformat(
                datetime.now(timezone.utc)
                - timedelta(days=collection_config.search_days)
            )

            base_cql = self.get_base_cql(collection_id, collection_config.filters)

            if self.settings.aois:
                fc_path = Path(self.settings.aois.feature_collection_path)

                print("Finding items that intersect AOIs...")
                features = json.loads(fc_path.read_text())["features"]
                for feature in features:
                    properties = feature.setdefault("properties", {})
                    aoi_id = feature["id"]
                    aoi_cql = cql_add_geom_arg(base_cql, feature["geometry"])
                    aoi_cql = cql_add_after_arg(aoi_cql, search_after)
                    aoi_item = next(
                        client.search(
                            filter=aoi_cql,
                            max_items=self.settings.max_search_results,
                        ).items(),
                        None,
                    )
                    if aoi_item:
                        # Check if the item was already used.
                        this_dt = get_datetime(aoi_item)
                        last_dt: Optional[datetime] = None
                        if AOI_LAST_ITEM_DT_KEY in properties:
                            last_dt = datetime.fromisoformat(
                                properties.get(AOI_LAST_ITEM_DT_KEY)
                            )
                        if not last_dt or this_dt > last_dt:
                            print(f"Found new item that intersects AOI {aoi_id}...")
                            aoi_item.properties["aoi"] = aoi_id
                            aoi_item.properties["aoi_geom"] = feature["geometry"]
                            target_aoi_items.append(aoi_item)

            if not target_aoi_items:
                print("Finding random items...")
                items = list(
                    client.search(
                        filter=cql_add_after_arg(base_cql, search_after),
                        max_items=self.settings.max_search_results,
                    ).items()
                )

                if not items:
                    print(f"WARNING: No items found. Skipping {collection_id}.")

                print(f"Found {len(items)} items")
                if len(items) == self.settings.max_search_results:
                    print("(limit hit)")

                target_random_items.extend(items)

        return target_aoi_items or target_random_items

    def set_aoi_item_info(self, item: pystac.Item) -> None:
        # Set the properties of an AOI to
        if self.settings.aois:
            aoi_id = item.properties["aoi"]
            item_dt = get_datetime(item)
            fc_path = self.settings.aois.feature_collection_path
            with open(fc_path) as f:
                feature_collection = json.load(f)
            for feature in feature_collection["features"]:
                if feature["id"] == aoi_id:
                    properties = feature.setdefault("properties", {})
                    properties[AOI_LAST_ITEM_DT_KEY] = item_dt.isoformat()
            with open(fc_path, "w") as f:
                json.dump(feature_collection, f, indent=2)

    def generate(self) -> bool:
        if self.force:
            print("Forcing regeneration...")
        else:
            if not self.should_generate_new_background():
                print("No need to generate new background")
                return False

        # Try AI suggestions first (includes verification loop)
        ai_result = self.try_ai_suggestions()
        if ai_result:
            rendered, info_dict, suggestion = ai_result
            bg_image = rendered
            if self.settings.mirror_image:
                bg_image = ImageOps.mirror(bg_image)
            bg_image.convert("RGB").save(self.settings.get_image_path())
            thumbnail = bg_image.resize(
                (self.settings.thumbnail_width, self.settings.thumbnail_height)
            )
            thumbnail.convert("RGB").save(self.settings.get_thumbnail_path())

            print("Writing info...")
            image_info = ImageInfo(**{
                k: v for k, v in info_dict.items()
                if k in ImageInfo.__fields__
            })
            with open(self.settings.get_image_info_path(), "w") as f:
                f.write(image_info.json(indent=2))

            from gallery import archive_to_gallery

            archive_to_gallery(bg_image, info_dict)
            print("Done.")
            return True

        ai_config = self.settings.ai_suggestions
        if ai_config and ai_config.enabled and not ai_config.fallback_to_aois:
            raise Exception(
                "ERROR: AI suggestions found no matching imagery and "
                "fallback_to_aois is disabled."
            )

        # Existing AOI / random item logic
        if self.settings.aois:
            ensure_ids(Path(self.settings.aois.feature_collection_path))

        target_items = self.get_target_items()
        if not target_items:
            raise Exception("ERROR: No target items found for the configured search.")

        random.shuffle(target_items)
        saw_land_filter_skip = False
        for target_item in target_items:
            is_aoi = False
            if target_item.properties.get("aoi"):
                is_aoi = True
                target_geom = target_item.properties["aoi_geom"]
            else:
                if not target_item.geometry:
                    raise Exception(f"Item {target_item.id} has no geometry")
                target_geom = target_item.geometry

            result = self._render_item(target_item, target_geom, is_aoi)
            if result:
                return True
            else:
                saw_land_filter_skip = True
                continue

        if saw_land_filter_skip and self.settings.min_land_fraction > 0:
            raise Exception(
                "ERROR: No candidate item could satisfy the configured "
                f"min_land_fraction of {self.settings.min_land_fraction:.1%}."
            )
        raise Exception("ERROR: No target items found for the configured search.")

    def _render_item(
        self,
        target_item: pystac.Item,
        target_geom: Dict[str, Any],
        is_aoi: bool,
        source_geometry_kind: Optional[str] = None,
        ai_suggestion: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Render a single item to the Teams background. Returns True on success."""
        collection_id = target_item.collection_id
        assert collection_id

        collection_config = self.settings.get_collection_config(collection_id)
        render_options = collection_config.rendering_option

        print(f"Generating background image from item {target_item.id}...")
        if source_geometry_kind is None:
            render_geom, source_geometry_kind = self.get_render_geom(
                target_item, target_geom, is_aoi
            )
        else:
            render_geom = target_geom

        if not target_item.geometry:
            raise ValueError(f"Item {target_item.id} has no geometry")
        (
            bg_geom,
            crop_fit_strategy,
            crop_fit_scale_ratio,
            land_fraction,
        ) = self.fit_bg_geom_to_item_footprint(render_geom, target_item.geometry)
        if bg_geom is None or crop_fit_strategy is None:
            print(
                f"Skipping item {target_item.id} because no crop satisfied the "
                f"{self.settings.min_land_fraction:.1%} land coverage "
                "requirement."
            )
            return False
        if self.reject_if_overzoomed(
            crop_fit_strategy,
            crop_fit_scale_ratio,
            f"item {target_item.id}",
        ):
            return False

        render_params = self.get_render_params(collection_id, render_options)
        cql = self.get_base_cql(collection_id, collection_config.filters)
        image = self.fetch_image(target_item, bg_geom, render_params)
        bg_image = image
        if self.settings.mirror_image:
            bg_image = ImageOps.mirror(bg_image)
        bg_image.convert("RGB").save(self.settings.get_image_path())
        thumbnail = bg_image.resize(
            (self.settings.thumbnail_width, self.settings.thumbnail_height)
        )
        thumbnail.convert("RGB").save(self.settings.get_thumbnail_path())

        if is_aoi:
            self.set_aoi_item_info(target_item)

        print("Writing info...")
        image_info = ImageInfo(
            target_item=target_item.to_dict(),
            cql=cql,
            render_params=render_params,
            is_aoi=is_aoi,
            last_changed=datetime.now(),
            source_geometry_kind=source_geometry_kind,
            requested_geometry=render_geom,
            crop_geometry=bg_geom,
            crop_fit_strategy=crop_fit_strategy,
            crop_fit_scale_ratio=crop_fit_scale_ratio,
            land_fraction=land_fraction,
            min_land_fraction=self.settings.min_land_fraction,
            land_mask_source="natural-earth-50m",
            ai_suggestion=ai_suggestion,
        )
        with open(self.settings.get_image_info_path(), "w") as f:
            f.write(image_info.json(indent=2))

        # Archive to gallery
        from gallery import archive_to_gallery

        archive_to_gallery(bg_image, image_info.dict())

        print("Done.")
        return True


def run(force: bool = False, settings_file: Optional[Union[str, Path]] = None) -> bool:
    if settings_file is not None:
        settings = Settings.from_yaml(settings_file)
    else:
        settings = Settings.load()
    generator = TeamsBackgroundGenerator(settings, force)
    return generator.generate()


def build_arg_parser() -> argparse.ArgumentParser:
    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("-f", "--force", action="store_true")
    arg_parser.add_argument("-d", "--debug", action="store_true")
    arg_parser.add_argument(
        "--settings-file",
        help="Optional path to a settings YAML file. Defaults to settings.yaml.",
    )
    return arg_parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        run(force=args.force, settings_file=args.settings_file)
    except Exception as e:
        if args.debug:
            raise
        print(f"ERROR: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
