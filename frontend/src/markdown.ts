export type MarkdownSegment =
  | { kind: "text"; text: string }
  | { kind: "strong"; text: string }
  | { kind: "code"; text: string };

export interface MarkdownParagraph {
  kind: "paragraph";
  segments: MarkdownSegment[];
}

export function parseMarkdown(markdown: string): MarkdownParagraph[] {
  const paragraphs = markdown
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.trim())
    .filter(Boolean);

  return paragraphs.map((paragraph) => ({
    kind: "paragraph",
    segments: parseInlineSegments(paragraph),
  }));
}

function parseInlineSegments(text: string): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  let cursor = 0;

  while (cursor < text.length) {
    const codeIndex = text.indexOf("`", cursor);
    const strongIndex = text.indexOf("**", cursor);
    const next = nearestMarker(codeIndex, strongIndex);

    if (next.index === -1) {
      appendText(segments, text.slice(cursor));
      break;
    }

    if (next.index > cursor) {
      appendText(segments, text.slice(cursor, next.index));
    }

    const markerLength = next.kind === "strong" ? 2 : 1;
    const closeIndex = text.indexOf(next.kind === "strong" ? "**" : "`", next.index + markerLength);
    if (closeIndex === -1) {
      appendText(segments, text.slice(next.index));
      break;
    }

    const content = text.slice(next.index + markerLength, closeIndex);
    segments.push({ kind: next.kind, text: content });
    cursor = closeIndex + markerLength;
  }

  return segments.length ? segments : [{ kind: "text", text }];
}

function nearestMarker(
  codeIndex: number,
  strongIndex: number,
): { kind: "code" | "strong"; index: number } {
  if (codeIndex === -1 && strongIndex === -1) {
    return { kind: "code", index: -1 };
  }
  if (codeIndex === -1) {
    return { kind: "strong", index: strongIndex };
  }
  if (strongIndex === -1) {
    return { kind: "code", index: codeIndex };
  }
  return codeIndex < strongIndex
    ? { kind: "code", index: codeIndex }
    : { kind: "strong", index: strongIndex };
}

function appendText(segments: MarkdownSegment[], text: string) {
  if (!text) {
    return;
  }
  const last = segments[segments.length - 1];
  if (last?.kind === "text") {
    last.text += text;
    return;
  }
  segments.push({ kind: "text", text });
}
