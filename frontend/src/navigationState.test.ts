import { describe, expect, it } from "vitest";
import { getNewChatButtonClassName } from "./navigationState";

describe("getNewChatButtonClassName", () => {
  it("marks new chat active only when no conversation is open", () => {
    expect(getNewChatButtonClassName("")).toBe("sidebar-nav-item active");
    expect(getNewChatButtonClassName("conv-1")).toBe("sidebar-nav-item");
  });
});
