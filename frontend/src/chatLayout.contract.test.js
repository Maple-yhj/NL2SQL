import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const appSource = readFileSync(new URL("./App.tsx", import.meta.url), "utf8");
const stylesSource = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

describe("ChatGPT-style chat layout contract", () => {
  it("keeps the composer in a dedicated sticky bottom shell", () => {
    expect(appSource).toContain('className="composer-shell"');
    expect(stylesSource).toMatch(/\.composer-shell\s*{[^}]*position:\s*sticky/s);
    expect(stylesSource).toMatch(/\.composer-shell\s*{[^}]*bottom:\s*0/s);
  });

  it("uses a centered empty-chat greeting instead of a large card", () => {
    expect(appSource).toContain("你好，");
    expect(appSource).not.toContain("Ask a warehouse question to begin.");
    expect(stylesSource).toMatch(/\.empty-thread\s*{[^}]*place-items:\s*center/s);
  });

  it("keeps user messages compact and content-sized", () => {
    expect(stylesSource).toMatch(/\.message\.user\s+\.bubble\s*{[^}]*width:\s*fit-content/s);
    expect(stylesSource).toMatch(/\.message\.user\s+\.bubble\s*{[^}]*max-width:\s*min\(70%/s);
  });
});
