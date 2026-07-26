import { describe, expect, it } from "vitest";
import { parseListeningPids, parsePidList } from "./free-port-utils.mjs";

describe("parseListeningPids", () => {
  it("returns unique listening process ids for the requested port", () => {
    const output = `
  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       39092
  TCP    127.0.0.1:5173         127.0.0.1:62763        ESTABLISHED     39092
  TCP    127.0.0.1:5174         0.0.0.0:0              LISTENING       25228
  TCP    [::1]:5173             [::]:0                 LISTENING       39093
`;

    expect(parseListeningPids(output, 5173)).toEqual([39092, 39093]);
  });
});

describe("parsePidList", () => {
  it("returns unique valid process ids from lsof output", () => {
    expect(parsePidList("39092\n39093\n39092\n\n")).toEqual([39092, 39093]);
  });
});
