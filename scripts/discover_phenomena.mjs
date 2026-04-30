#!/usr/bin/env node
// discover_phenomena.mjs — Uses Copilot SDK plus live tools to discover
// timely, imageable phenomenon candidates at runtime.

import { CopilotClient, defineTool } from "@github/copilot-sdk";
import { parseArgs } from "node:util";
import { readFileSync } from "node:fs";

const approveAll = () => ({ kind: "approved" });
const USER_AGENT = "planetary-computer-teams-background/1.0 (+Copilot SDK)";

function extractResponse(response) {
  if (typeof response === "string") return response;
  if (response?.data?.content) return response.data.content;
  if (response?.content && typeof response.content === "string") {
    return response.content;
  }
  if (response?.message?.content) return response.message.content;
  if (response?.text) return response.text;
  return String(response ?? "");
}

function parseJSON(text) {
  let jsonText = text.trim();
  const fenceMatch = jsonText.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fenceMatch) {
    jsonText = fenceMatch[1].trim();
  }
  return JSON.parse(jsonText);
}

function normalizeWhitespace(text) {
  return String(text ?? "").replace(/\s+/g, " ").trim();
}

function stripCdata(text) {
  return normalizeWhitespace(String(text ?? "").replace(/^<!\[CDATA\[/, "").replace(/\]\]>$/, ""));
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { "User-Agent": USER_AGENT, ...(options.headers || {}) };
    const response = await fetch(url, { ...options, headers, signal: controller.signal });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} for ${url}`);
    }
    return response;
  } finally {
    clearTimeout(timer);
  }
}

async function fetchJson(url, options = {}, timeoutMs = 15000) {
  const response = await fetchWithTimeout(url, options, timeoutMs);
  return response.json();
}

async function fetchText(url, options = {}, timeoutMs = 15000) {
  const response = await fetchWithTimeout(url, options, timeoutMs);
  return response.text();
}

function getSeason(month, hemisphere) {
  if (hemisphere === "north") {
    if ([12, 1, 2].includes(month)) return "winter";
    if ([3, 4, 5].includes(month)) return "spring";
    if ([6, 7, 8].includes(month)) return "summer";
    return "autumn";
  }
  if ([12, 1, 2].includes(month)) return "summer";
  if ([3, 4, 5].includes(month)) return "autumn";
  if ([6, 7, 8].includes(month)) return "winter";
  return "spring";
}

function getCurrentContext() {
  const now = new Date();
  const month = now.getUTCMonth() + 1;
  const monthName = now.toLocaleString("en-US", {
    month: "long",
    timeZone: "UTC",
  });
  return {
    now_utc: now.toISOString(),
    month,
    month_name: monthName,
    north_hemisphere_season: getSeason(month, "north"),
    south_hemisphere_season: getSeason(month, "south"),
  };
}

function flattenCoordinates(coords, results = []) {
  if (!Array.isArray(coords)) return results;
  if (coords.length >= 2 && typeof coords[0] === "number" && typeof coords[1] === "number") {
    results.push(coords);
    return results;
  }
  for (const value of coords) {
    flattenCoordinates(value, results);
  }
  return results;
}

function geometryCentroid(geometry) {
  const points = flattenCoordinates(geometry?.coordinates);
  if (points.length === 0) {
    return null;
  }
  let minLon = Infinity;
  let maxLon = -Infinity;
  let minLat = Infinity;
  let maxLat = -Infinity;
  for (const [lon, lat] of points) {
    if (lon < minLon) minLon = lon;
    if (lon > maxLon) maxLon = lon;
    if (lat < minLat) minLat = lat;
    if (lat > maxLat) maxLat = lat;
  }
  return {
    lon: Number(((minLon + maxLon) / 2).toFixed(5)),
    lat: Number(((minLat + maxLat) / 2).toFixed(5)),
  };
}

function bboxFromScale(centerLon, centerLat, scaleKm, aspect) {
  const kmPerDegLat = 111.32;
  let kmPerDegLon = 111.32 * Math.cos((centerLat * Math.PI) / 180);
  if (kmPerDegLon < 1) {
    kmPerDegLon = 1;
  }
  const widthDeg = scaleKm / kmPerDegLon;
  const heightDeg = scaleKm / aspect / kmPerDegLat;
  return [
    Number((centerLon - widthDeg / 2).toFixed(6)),
    Number((centerLat - heightDeg / 2).toFixed(6)),
    Number((centerLon + widthDeg / 2).toFixed(6)),
    Number((centerLat + heightDeg / 2).toFixed(6)),
  ];
}

function extractTag(itemXml, tagName) {
  const match = itemXml.match(new RegExp(`<${tagName}>([\\s\\S]*?)<\\/${tagName}>`, "i"));
  return match ? stripCdata(match[1]) : "";
}

async function main() {
  const { values } = parseArgs({
    options: {
      stdin: { type: "boolean", default: false },
      model: { type: "string", default: "claude-sonnet-4" },
      timeout: { type: "string", default: "120000" },
    },
  });

  if (!values.stdin) {
    process.stderr.write("This script expects --stdin with JSON input.\n");
    console.log(JSON.stringify({ templates: [] }));
    process.exit(0);
  }

  const payload = JSON.parse(readFileSync(0, "utf-8"));
  const timeoutMs = parseInt(values.timeout, 10);
  const maxTemplates = Number(payload.max_templates || 8);
  const collections = Array.isArray(payload.collections) ? payload.collections : [];
  const stacUrl = String(payload.stac_url || "").replace(/\/+$/, "");
  const aspect = Number(payload.aspect || 16 / 9);
  const historySummary = payload.history_summary || "No prior AI selection history.";

  if (!stacUrl || collections.length === 0) {
    console.log(JSON.stringify({ templates: [] }));
    process.exit(0);
  }

  const tools = [
    defineTool("get_current_context", {
      description: "Return the current UTC date and rough seasonal context by hemisphere.",
      skipPermission: true,
      handler: () => getCurrentContext(),
    }),
    defineTool("get_recent_eonet_events", {
      description: "Fetch recent NASA EONET Earth events such as wildfire, volcano, sea ice, dust, storms, and blooms.",
      parameters: {
        type: "object",
        properties: {
          limit: { type: "integer", minimum: 1, maximum: 20 },
          days: { type: "integer", minimum: 1, maximum: 60 },
        },
        additionalProperties: false,
      },
      skipPermission: true,
      handler: async ({ limit = 10, days = 30 } = {}) => {
        const url = `https://eonet.gsfc.nasa.gov/api/v3/events?status=all&days=${days}&limit=${limit}`;
        const data = await fetchJson(url, {}, 20000);
        return {
          events: (data.events || []).slice(0, limit).map((event) => {
            const latestGeometry = event.geometry?.[event.geometry.length - 1];
            return {
              id: event.id,
              title: event.title,
              categories: (event.categories || []).map((category) => category.title),
              sources: (event.sources || []).map((source) => source.id),
              geometry_type: latestGeometry?.type,
              centroid: latestGeometry ? geometryCentroid(latestGeometry) : null,
              observed_at: latestGeometry?.date || null,
              link: event.link || null,
            };
          }),
        };
      },
    }),
    defineTool("get_earth_observatory_feed", {
      description: "Fetch recent NASA Earth Observatory image-of-the-day stories for timely large-scale Earth phenomena.",
      parameters: {
        type: "object",
        properties: {
          limit: { type: "integer", minimum: 1, maximum: 12 },
        },
        additionalProperties: false,
      },
      skipPermission: true,
      handler: async ({ limit = 8 } = {}) => {
        const xml = await fetchText(
          "https://earthobservatory.nasa.gov/feeds/image-of-the-day.rss",
          {},
          20000,
        );
        const items = [...xml.matchAll(/<item>([\s\S]*?)<\/item>/gi)]
          .slice(0, limit)
          .map((match) => {
            const itemXml = match[1];
            return {
              title: extractTag(itemXml, "title"),
              link: extractTag(itemXml, "link"),
              published_at: extractTag(itemXml, "pubDate"),
              description: extractTag(itemXml, "description"),
            };
          });
        return { items };
      },
    }),
    defineTool("geocode_location", {
      description: "Geocode a place name to a likely centroid using Nominatim.",
      parameters: {
        type: "object",
        properties: {
          query: { type: "string" },
        },
        required: ["query"],
        additionalProperties: false,
      },
      skipPermission: true,
      handler: async ({ query }) => {
        const url = new URL("https://nominatim.openstreetmap.org/search");
        url.searchParams.set("format", "jsonv2");
        url.searchParams.set("limit", "5");
        url.searchParams.set("q", query);
        const results = await fetchJson(
          url.toString(),
          {
            headers: { Accept: "application/json" },
          },
          15000,
        );
        return {
          results: (results || []).map((result) => ({
            display_name: result.display_name,
            lon: Number(parseFloat(result.lon).toFixed(6)),
            lat: Number(parseFloat(result.lat).toFixed(6)),
            class: result.class,
            type: result.type,
          })),
        };
      },
    }),
    defineTool("probe_recent_imagery", {
      description: "Check whether recent low-cloud imagery exists near a location and scale.",
      parameters: {
        type: "object",
        properties: {
          center_lon: { type: "number", minimum: -180, maximum: 180 },
          center_lat: { type: "number", minimum: -90, maximum: 90 },
          scale_km: { type: "number", minimum: 4, maximum: 250 },
          search_days: { type: "integer", minimum: 1, maximum: 90 },
          max_cloud_cover: { type: "number", minimum: 0, maximum: 100 },
        },
        required: ["center_lon", "center_lat", "scale_km"],
        additionalProperties: false,
      },
      skipPermission: true,
      handler: async ({
        center_lon,
        center_lat,
        scale_km,
        search_days = 30,
        max_cloud_cover = 15,
      }) => {
        const bbox = bboxFromScale(center_lon, center_lat, scale_km, aspect);
        const now = new Date();
        const since = new Date(now.getTime() - search_days * 24 * 60 * 60 * 1000).toISOString();
        const results = [];
        for (const collection of collections) {
          const body = {
            collections: [collection.id],
            limit: 6,
            bbox,
            datetime: `${since}/${now.toISOString()}`,
          };
          if (max_cloud_cover !== null && max_cloud_cover !== undefined) {
            body.query = { "eo:cloud_cover": { lte: max_cloud_cover } };
          }
          try {
            const data = await fetchJson(
              `${stacUrl}/search`,
              {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  Accept: "application/json",
                },
                body: JSON.stringify(body),
              },
              20000,
            );
            const features = (data.features || []).slice().sort((left, right) => {
              const cloudLeft = left.properties?.["eo:cloud_cover"] ?? 999;
              const cloudRight = right.properties?.["eo:cloud_cover"] ?? 999;
              if (cloudLeft !== cloudRight) {
                return cloudLeft - cloudRight;
              }
              const dateLeft = Date.parse(left.properties?.datetime || 0);
              const dateRight = Date.parse(right.properties?.datetime || 0);
              return dateRight - dateLeft;
            });
            results.push({
              collection: collection.id,
              count: features.length,
              best_item: features[0]
                ? {
                    id: features[0].id,
                    acquired_at:
                      features[0].properties?.datetime ||
                      features[0].properties?.start_datetime ||
                      null,
                    cloud_cover: features[0].properties?.["eo:cloud_cover"] ?? null,
                  }
                : null,
            });
          } catch (error) {
            results.push({
              collection: collection.id,
              count: 0,
              error: error.message,
            });
          }
        }
        return {
          bbox,
          any_results: results.some((result) => result.count > 0),
          results,
        };
      },
    }),
  ];

  const systemPrompt = `You are the dynamic discovery stage for a satellite Teams background generator.

You must propose timely, geographically concrete Earth phenomena to search in Sentinel-2 imagery RIGHT NOW.

Important rules:
- Do NOT use a fixed catalog or repeat a static set of known examples.
- You MUST call get_current_context.
- You MUST inspect at least one live current-context source: get_recent_eonet_events or get_earth_observatory_feed.
- For every final candidate, you MUST call probe_recent_imagery before including it.
- Use geocode_location whenever you need coordinates for a place name.
- Prefer phenomena that are visually legible in Sentinel-2, large enough to read at practical scales, and likely to make a good conversation piece.
- Avoid tiny, overly brittle, or speculative ideas.
- If current events are weak, use the season and current month to discover strong seasonal phenomena instead.
- Use the learning summary to avoid recent failure modes and over-specific stories.
- Diversify geographies and tags when possible.

Respond with ONLY valid JSON in this shape:
{
  "templates": [
    {
      "id": "short-slug",
      "name": "Short phenomenon title",
      "location": "Concrete place name",
      "center_lon": 0.0,
      "center_lat": 0.0,
      "scale_options_km": [12, 20, 32],
      "preferred_months": [4],
      "search_days": 30,
      "max_cloud_cover": 12,
      "min_land_fraction": 0.85,
      "render_hint": "Natural color",
      "expected_visual_signatures": ["signature 1", "signature 2"],
      "story_seed": "2-3 sentence seed that matches what should actually be visible.",
      "conversation_seed": "Meeting small-talk hook.",
      "timeliness_seed": "Why this is timely right now.",
      "why_visible_in_s2_seed": "Why this should read clearly at Sentinel-2 scale.",
      "backup_caption_if_signature_missing": "A safer caption if the exact signature is weaker than expected.",
      "discovery_source": "seasonal | eonet | earth-observatory | mixed",
      "discovery_rationale": "Why this made the shortlist now.",
      "tags": ["seasonal", "water", "dynamic"]
    }
  ]
}`;

  let client;
  try {
    client = new CopilotClient();
    await client.start();
  } catch (err) {
    process.stderr.write(`Failed to initialize Copilot client: ${err.message}\n`);
    console.log(JSON.stringify({ templates: [] }));
    process.exit(0);
  }

  try {
    const session = await client.createSession({
      model: values.model,
      systemMessage: { content: systemPrompt },
      tools,
      availableTools: tools.map((tool) => tool.name),
      onPermissionRequest: approveAll,
    });

    const prompt =
      `Discover up to ${maxTemplates} ranked dynamic phenomenon templates for the next background run.\n\n` +
      `Recent learning summary:\n${historySummary}\n\n` +
      `Collections available for later rendering:\n${JSON.stringify(collections, null, 2)}\n\n` +
      `Only include candidates that are seasonally/currently interesting AND have live recent imagery availability when probed.\n` +
      `Return ONLY the JSON object.`;

    const response = await session.sendAndWait({ prompt }, timeoutMs);
    let text = extractResponse(response);
    let parsed;

    try {
      parsed = parseJSON(text);
    } catch {
      process.stderr.write("First response was not JSON, retrying with correction...\n");
      const retry = await session.sendAndWait(
        {
          prompt:
            "Your previous response was not valid JSON. Respond again with ONLY the raw JSON object and nothing else.",
        },
        timeoutMs,
      );
      text = extractResponse(retry);
      parsed = parseJSON(text);
    }

    console.log(JSON.stringify(parsed));
  } catch (err) {
    process.stderr.write(`Dynamic discovery failed: ${err.message}\n`);
    console.log(JSON.stringify({ templates: [] }));
  }

  process.exit(0);
}

main();
