import { describe, expect, it } from "vitest";
import { createSseParser, type SseMessage } from "../src/sse";

describe("createSseParser", () => {
  it("parses events split across arbitrary chunks", () => {
    const got: SseMessage[] = [];
    const feed = createSseParser((m) => got.push(m));
    const text = 'event: baseline\ndata: {"kind": "baseline"}\n\nevent: end\ndata: {"status": "passed"}\n\n';
    for (let i = 0; i < text.length; i += 7) feed(text.slice(i, i + 7));
    expect(got).toEqual([
      { event: "baseline", data: { kind: "baseline" } },
      { event: "end", data: { status: "passed" } },
    ]);
  });

  it("handles CRLF, multi-line data, plain text and blocks without data", () => {
    const got: SseMessage[] = [];
    const feed = createSseParser((m) => got.push(m));
    feed(": comment\r\n\r\ndata: line one\r\ndata: line two\r\n\r\n");
    expect(got).toEqual([{ event: "message", data: "line one\nline two" }]);
  });
});
