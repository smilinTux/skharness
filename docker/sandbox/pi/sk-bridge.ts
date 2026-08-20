/** Pinned Pi extension exposing only SK operations granted by the launch profile. */
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { Type } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const run = promisify(execFile);
const operations = [
  "capstone.card.read", "capstone.card.claim", "capstone.progress.append",
  "arena.progress.append", "arena.result.append", "arena.verdict.append",
  "arena.experiment.search", "arena.experiment.reproduce", "arena.experiment.mutate",
  "arena.negative.search",
  "memory.recall", "memory.scratch.append", "memory.proposal.append",
] as const;

export default function skBridge(pi: ExtensionAPI) {
  for (const operation of operations) {
    pi.registerTool({
      name: operation.replaceAll(".", "_"),
      label: operation,
      description: `Invoke the profile-scoped SK operation ${operation}.`,
      parameters: Type.Object({
        payload: Type.Record(Type.String(), Type.Unknown(), {
          description: "Input validated by the Python policy layer before dispatch.",
        }),
      }),
      async execute(_id, params) {
        const profile = process.env.SKHARNESS_PI_PROFILE;
        if (!profile) throw new Error("SKHARNESS_PI_PROFILE is not set");
        const { stdout } = await run("skharness-pi-bridge", [
          "--profile", profile, "--operation", operation,
          "--payload", JSON.stringify(params.payload),
        ], { timeout: 35_000, maxBuffer: 1024 * 1024 });
        const result = JSON.parse(stdout);
        return { content: [{ type: "text", text: JSON.stringify(result) }],
          details: { operation, result } };
      },
    });
  }
}
