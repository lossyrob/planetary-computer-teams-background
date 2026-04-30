#!/usr/bin/env node
// suggest_location.mjs — Selects from already-rendered candidate previews and
// writes the final narrative for the chosen image.

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

const SYSTEM_PROMPT = `You are a satellite background curator. You are NOT inventing imaginary ideas first. You are choosing from ACTUAL recent satellite imagery candidates that have already been searched and preview-rendered.

Your job:
1. Look at the attached preview sheet.
2. Read the candidate metadata.
3. Pick the single candidate most likely to succeed as a visually striking Teams background.
4. Write the story to match the ACTUAL imagery you selected.

Selection priorities, in order:
- It looks strong in the preview sheet.
- It is likely to survive final verification.
- It is seasonally and temporally plausible.
- It has clear visual signatures that are visible at the chosen scale.
- It has a good conversation hook.

Important:
- Prefer a real, strong image over a clever but brittle story.
- If a candidate's seed idea is too specific, rewrite the story so it matches what the preview actually shows.
- Use the learning summary to avoid repeating recent failure modes.
- You may choose a candidate because the image is excellent even if the exact seed phenomenon is only partially present.

Respond with ONLY valid JSON:
{
  "selected_preview_id": "C01",
  "alternate_preview_ids": ["C04", "C07"],
  "selection_reason": "Why this candidate is the strongest available option right now.",
  "story": {
    "name": "Short evocative final title",
    "description": "2-3 sentences that match the chosen preview.",
    "conversation_starter": "A fact or question for meeting small talk.",
    "timeliness": "Why this is interesting right now, or why the timing still works.",
    "why_visible_in_s2": "Why the feature reads clearly at Sentinel-2 scale.",
    "expected_visual_signatures": ["signature 1", "signature 2"],
    "backup_caption_if_signature_missing": "A safer caption if the final render is strong but the original claim proves too specific."
  }
}`;

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
    console.log(JSON.stringify({ error: "stdin required" }));
    process.exit(0);
  }

  const timeoutMs = parseInt(values.timeout, 10);
  const payload = JSON.parse(readFileSync(0, "utf-8"));
  const previewSheetBase64 = payload.preview_sheet_base64;
  const candidates = payload.candidates || [];
  const learningSummary = payload.learning_summary || "No prior AI selection history.";
  const availabilitySummary = payload.availability_summary || "";
  const excludedIds = payload.excluded_preview_ids || [];

  if (!previewSheetBase64 || candidates.length === 0) {
    console.log(JSON.stringify({ error: "preview sheet and candidates are required" }));
    process.exit(0);
  }

  let client;
  try {
    client = new CopilotClient();
    await client.start();
  } catch (err) {
    process.stderr.write(`Failed to initialize Copilot client: ${err.message}\n`);
    console.log(JSON.stringify({ error: err.message }));
    process.exit(0);
  }

  try {
    const session = await client.createSession({
      model: values.model,
      systemMessage: { content: SYSTEM_PROMPT },
      onPermissionRequest: approveAll,
    });

    const candidateSummary = JSON.stringify(candidates, null, 2);
    const excludedText = excludedIds.length > 0
      ? `\nAlready rejected preview ids in this run: ${excludedIds.join(", ")}\nDo not pick them again.`
      : "";

    const prompt =
      `Here is the current pool of REAL candidate imagery.\n\n` +
      `Availability summary:\n${availabilitySummary || "None"}\n\n` +
      `Recent learning summary:\n${learningSummary}\n` +
      excludedText +
      `\n\nCandidate metadata (use the preview ids exactly as written):\n${candidateSummary}\n\n` +
      `The preview sheet is attached as an image. Choose the best candidate from the attached sheet and write a story that matches what is actually visible in the chosen preview.\n\n` +
      `Respond with ONLY the JSON object, no markdown, no explanation outside JSON.`;

    const response = await session.sendAndWait(
      {
        prompt,
        attachments: [
          {
            type: "blob",
            data: previewSheetBase64,
            mimeType: "image/jpeg",
            displayName: "candidate-preview-sheet.jpg",
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
            "Your previous response was not valid JSON. Respond again with ONLY the JSON object. " +
            "No preamble, no explanation, no markdown fences.",
        },
        timeoutMs,
      );
      text = extractResponse(retry);
      parsed = parseJSON(text);
    }

    console.log(JSON.stringify(parsed));
  } catch (err) {
    process.stderr.write(`Candidate selection failed: ${err.message}\n`);
    console.log(JSON.stringify({ error: err.message }));
  }

  process.exit(0);
}

main();
