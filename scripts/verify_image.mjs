#!/usr/bin/env node
// verify_image.mjs — Uses blob attachments with the Copilot SDK to verify a
// final rendered image and optionally salvage the story if the image is strong
// but the original claim is too specific.

import { CopilotClient } from "@github/copilot-sdk";
import { parseArgs } from "node:util";
import { readFileSync } from "node:fs";

const approveAll = () => ({ kind: "approved" });

function extractResponse(response) {
  if (typeof response === "string") return response;
  if (response?.data?.content) return response.data.content;
  if (response?.content && typeof response.content === "string")
    return response.content;
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

const SYSTEM_PROMPT = `You are a satellite imagery reviewer for a Teams background generator.

You are given:
1. A final rendered satellite image (attached)
2. The story that was written for it
3. Crop metadata

Your job is to decide whether the final image should be:
- accepted as-is,
- salvaged by rewriting the story,
- adjusted and re-rendered,
- or skipped entirely.

Scoring dimensions:
- visual_quality_score (1-5): how strong the image is as a background
- story_match_score (1-5): how well the current story matches what is actually visible
- conversation_score (1-5): how strong the resulting conversation hook is

Verdict meanings:
- "accept": The image and story are both good enough.
- "salvage": The image is good, but the story is too specific or slightly wrong. Rewrite the story to match the image.
- "adjust": The image could work if the crop/zoom/land mix changes. Provide adjustments.
- "skip": The image is not good enough even after considering salvage.

Use "salvage" when the image is visually strong but the original claim is brittle.
For example: a glacial landscape is still beautiful even if the promised autumn colors are missing.

Respond with ONLY valid JSON:
{
  "verdict": "accept" | "salvage" | "adjust" | "skip",
  "confidence": 1-5,
  "visual_quality_score": 1-5,
  "story_match_score": 1-5,
  "conversation_score": 1-5,
  "assessment": "What is actually visible and why this verdict fits.",
  "adjustments": {
    "center_lon": null,
    "center_lat": null,
    "scale_km": null,
    "min_land_fraction": null,
    "reason": ""
  },
  "salvaged_story": {
    "name": "",
    "description": "",
    "conversation_starter": "",
    "timeliness": "",
    "why_visible_in_s2": "",
    "backup_caption_if_signature_missing": ""
  }
}

If verdict is not "adjust", set adjustments to null fields.
If verdict is not "salvage", set salvaged_story to null fields.`;

async function main() {
  const { values } = parseArgs({
    options: {
      stdin: { type: "boolean", default: false },
      model: { type: "string", default: "claude-sonnet-4" },
      timeout: { type: "string", default: "60000" },
    },
  });

  if (!values.stdin) {
    process.stderr.write("This script expects --stdin with JSON input.\n");
    console.log(JSON.stringify({ verdict: "skip", confidence: 1, assessment: "stdin required" }));
    process.exit(0);
  }

  const timeoutMs = parseInt(values.timeout, 10);
  const payload = JSON.parse(readFileSync(0, "utf-8"));
  const imageBase64 = payload.image_base64;
  const suggestion = payload.suggestion || {};
  const cropMetadata = payload.crop_metadata || {};

  if (!imageBase64) {
    console.log(JSON.stringify({ verdict: "skip", confidence: 1, assessment: "missing image" }));
    process.exit(0);
  }

  let client;
  try {
    client = new CopilotClient();
    await client.start();
  } catch (err) {
    process.stderr.write(`Failed to initialize Copilot client: ${err.message}\n`);
    console.log(JSON.stringify({ verdict: "accept", confidence: 1, assessment: "could not verify" }));
    process.exit(0);
  }

  try {
    const session = await client.createSession({
      model: values.model,
      systemMessage: { content: SYSTEM_PROMPT },
      onPermissionRequest: approveAll,
    });

    const prompt =
      `Review this final rendered image.\n\n` +
      `Current story:\n${JSON.stringify(suggestion, null, 2)}\n\n` +
      `Crop metadata:\n${JSON.stringify(cropMetadata, null, 2)}\n\n` +
      `The image is attached. Look at the actual image and decide whether it should be accepted, salvaged, adjusted, or skipped.\n\n` +
      `Respond with ONLY the JSON object.`;

    const response = await session.sendAndWait(
      {
        prompt,
        attachments: [
          {
            type: "blob",
            data: imageBase64,
            mimeType: "image/jpeg",
            displayName: "final-render.jpg",
          },
        ],
      },
      timeoutMs,
    );

    let text = extractResponse(response);
    let parsed;
    try {
      parsed = parseJSON(text);
    } catch {
      process.stderr.write("First response was not JSON, retrying with correction...\n");
      const retry = await session.sendAndWait(
        {
          prompt:
            "Your previous response was not valid JSON. Respond again with ONLY the raw JSON object, " +
            "starting with { and ending with }. No prose, no markdown, no extra text.",
        },
        timeoutMs,
      );
      text = extractResponse(retry);
      try {
        parsed = parseJSON(text);
      } catch {
        const lower = text.toLowerCase();
        const guessedVerdict = lower.includes("salvage")
          ? "salvage"
          : lower.includes("adjust")
            ? "adjust"
            : lower.includes("skip")
              ? "skip"
              : "accept";
        parsed = {
          verdict: guessedVerdict,
          confidence: 2,
          visual_quality_score: 3,
          story_match_score: 2,
          conversation_score: 3,
          assessment: text.slice(0, 700),
          adjustments: {
            center_lon: null,
            center_lat: null,
            scale_km: null,
            min_land_fraction: null,
            reason: "",
          },
          salvaged_story: {
            name: "",
            description: "",
            conversation_starter: "",
            timeliness: "",
            why_visible_in_s2: "",
            backup_caption_if_signature_missing: "",
          },
        };
      }
    }

    console.log(JSON.stringify(parsed));
  } catch (err) {
    process.stderr.write(`Verification failed: ${err.message}\n`);
    console.log(
      JSON.stringify({
        verdict: "accept",
        confidence: 1,
        visual_quality_score: 3,
        story_match_score: 3,
        conversation_score: 3,
        assessment: `Verification error: ${err.message}`,
        adjustments: {
          center_lon: null,
          center_lat: null,
          scale_km: null,
          min_land_fraction: null,
          reason: "",
        },
        salvaged_story: {
          name: "",
          description: "",
          conversation_starter: "",
          timeliness: "",
          why_visible_in_s2: "",
          backup_caption_if_signature_missing: "",
        },
      }),
    );
  }

  process.exit(0);
}

main();
