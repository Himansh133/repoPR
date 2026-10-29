import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RunDetail } from "../src/components/RunDetail";
import { detail, json, sseResponse, stubFetch } from "./helpers";

const DIFF = "--- a/shop/cart.py\n+++ b/shop/cart.py\n@@ -1,2 +1,3 @@\n def f():\n+    if not prices:\n-    old\n+    <script>alert(1)</script>";
const EVENTS = [
  'event: transition\ndata: {"ts": 1793135956.5, "state": "INVESTIGATE", "kind": "transition", "data": {"to": "INVESTIGATE"}}\n\n',
  'event: tests_run\ndata: {"ts": 1793135957.5, "state": "TEST", "kind": "tests_run", "data": {"status": "passed"}}\n\nevent: end\ndata: {"status": "passed"}\n\n',
];

function api(over: Record<string, unknown> = {}) {
  return stubFetch((url) => {
    if (url === "/runs/1") return json(200, detail(over));
    if (url === "/runs/1/events") return sseResponse(EVENTS);
    if (url === "/runs/1/diff") return json(200, { patches: [{ commit_sha: "c1", files: ["shop/cart.py"], added: 2, removed: 1, diff: DIFF }] });
    if (url === "/runs/1/tests") {
      return json(200, { executions: [
        { kind: "baseline", status: "failed", exit_code: 1, duration_s: 0.5, failed_tests: ["tests/test_cart.py::test_empty"], truncated: false, reason: null },
        { kind: "after_patch", status: "passed", exit_code: 0, duration_s: 0.4, failed_tests: [], truncated: false, reason: null },
      ] });
    }
    return json(404, { detail: "not found" });
  });
}

describe("RunDetail", () => {
  it("shows the header and streams the timeline to its end", async () => {
    api();
    render(<RunDetail runId={1} />);
    expect(await screen.findByRole("heading", { name: /Run #1: Crash on empty cart/ })).toBeTruthy();
    expect(await screen.findByText("Finished: passed")).toBeTruthy();
    const list = screen.getByRole("list", { name: "Agent events" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("tests_run")).toBeTruthy();
  });

  it("says when a run does not exist", async () => {
    stubFetch(() => json(404, { detail: "No run with id 9" }));
    render(<RunDetail runId={9} />);
    expect((await screen.findByRole("alert")).textContent).toBe("No run with id 9.");
  });

  it("moves between tabs with the arrow keys and shows tests", async () => {
    api();
    render(<RunDetail runId={1} />);
    const timelineTab = await screen.findByRole("tab", { name: "Timeline" });
    timelineTab.focus();
    await userEvent.keyboard("{ArrowRight}{ArrowRight}");
    const testsTab = screen.getByRole("tab", { name: "Tests" });
    expect(testsTab.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(testsTab);
    expect(await screen.findByText("tests/test_cart.py::test_empty")).toBeTruthy();
    expect(screen.getByText("Before the patch:")).toBeTruthy();
    expect(screen.getByText("After patch 1:")).toBeTruthy();
  });

  it("colors diff lines and keeps diff text as text", async () => {
    api();
    const { container } = render(<RunDetail runId={1} />);
    await userEvent.click(await screen.findByRole("tab", { name: "Diff" }));
    const line = (text: string) => screen.findByText((_, el) => el?.tagName === "DIV" && el.textContent === text);
    expect((await line("+    if not prices:")).className).toContain("text-success");
    expect((await line("-    old")).className).toContain("text-error");
    expect((await line("@@ -1,2 +1,3 @@")).className.split(" ")).toContain("text-primary-text");
    expect(await line("+    <script>alert(1)</script>")).toBeTruthy();
    expect(container.querySelector("script")).toBeNull();
  });

  it("reloads the run when the stream ends with a new status", async () => {
    const fetchMock = api({ status: "running", report: null });
    render(<RunDetail runId={1} />);
    await screen.findByText("Finished: passed");
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([u]) => u === "/runs/1")).toHaveLength(2));
  });
});
