import { describe, expect, it } from "vitest";
import { parseMarkdown } from "./markdown";

describe("parseMarkdown", () => {
  it("parses paragraphs with strong and inline code spans", () => {
    const blocks = parseMarkdown("根据 `orders` 表返回 **45 条记录**。\n\n第二段。");

    expect(blocks).toEqual([
      {
        kind: "paragraph",
        segments: [
          { kind: "text", text: "根据 " },
          { kind: "code", text: "orders" },
          { kind: "text", text: " 表返回 " },
          { kind: "strong", text: "45 条记录" },
          { kind: "text", text: "。" },
        ],
      },
      {
        kind: "paragraph",
        segments: [{ kind: "text", text: "第二段。" }],
      },
    ]);
  });

  it("keeps unmatched markdown markers as plain text", () => {
    const blocks = parseMarkdown("金额 **未闭合，字段 `created_at");

    expect(blocks[0].segments).toEqual([
      { kind: "text", text: "金额 **未闭合，字段 `created_at" },
    ]);
  });
});
