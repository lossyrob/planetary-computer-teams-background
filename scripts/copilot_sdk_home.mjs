// copilot_sdk_home.mjs — Resolves a non-default COPILOT_HOME for the Copilot SDK.
//
// The SDK-spawned runtime persists session state (per-session folders plus the
// aggregate session-store.db) under COPILOT_HOME, which defaults to ~/.copilot.
// Passing this directory as `baseDirectory` to `new CopilotClient(...)` keeps the
// automated background-generation sessions out of the user's default Copilot
// store so external tools that watch ~/.copilot do not observe them.
//
// Override the location with the PC_TEAMS_COPILOT_HOME environment variable
// (absolute, or relative to the repo root). Defaults to <repo>/.cache/copilot-sdk-home,
// which is already git-ignored.

import { mkdirSync } from "node:fs";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export function resolveSdkHome() {
  const here = dirname(fileURLToPath(import.meta.url));
  const repoRoot = resolve(here, "..");
  const override = process.env.PC_TEAMS_COPILOT_HOME;
  const home = override
    ? isAbsolute(override)
      ? override
      : resolve(repoRoot, override)
    : join(repoRoot, ".cache", "copilot-sdk-home");
  mkdirSync(home, { recursive: true });
  return home;
}
