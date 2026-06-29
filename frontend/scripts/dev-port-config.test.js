import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("dev server port config", () => {
  it("frees and then binds strictly to port 5173", () => {
    const packageJson = JSON.parse(readFileSync(new URL("../package.json", import.meta.url)));

    expect(packageJson.scripts.dev).toContain("scripts/free-port.mjs 5173");
    expect(packageJson.scripts.dev).toContain("--port 5173");
    expect(packageJson.scripts.dev).toContain("--strictPort");
  });
});
