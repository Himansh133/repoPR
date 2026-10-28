import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApprovePanel } from "../src/components/ApprovePanel";
import type { Run } from "../src/types";
import { detail, json, stubFetch } from "./helpers";

const passed = detail() as Run;

describe("ApprovePanel", () => {
  it("is hidden for runs that did not pass", () => {
    for (const status of ["failed", "running", "aborted", "inconclusive", "error"] as const) {
      const { container, unmount } = render(<ApprovePanel run={{ ...passed, status }} onChange={vi.fn()} />);
      expect(container.textContent).toBe("");
      unmount();
    }
  });

  it("shows the PR link instead of buttons once a PR exists", () => {
    render(<ApprovePanel run={{ ...passed, pr_url: "https://github.com/octo/demo/pull/9" }} onChange={vi.fn()} />);
    expect(screen.getByRole("link").getAttribute("href")).toBe("https://github.com/octo/demo/pull/9");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("needs a second click, then sends confirm: true", async () => {
    const fetchMock = stubFetch(() => json(200, { id: 1, pr_url: "https://github.com/octo/demo/pull/9" }));
    const onChange = vi.fn();
    render(<ApprovePanel run={passed} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve…" }));
    expect(fetchMock).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("checkbox", { name: /Close issue #7/ }));
    await userEvent.type(screen.getByLabelText("Note (optional)"), "looks good");
    await userEvent.click(screen.getByRole("button", { name: "Create draft PR" }));
    await vi.waitFor(() => expect(onChange).toHaveBeenCalledOnce());
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/runs/1/approve");
    expect(JSON.parse(String(init.body))).toEqual({ confirm: true, closes_issue: true, note: "looks good" });
  });

  it("cancel sends nothing", async () => {
    const fetchMock = stubFetch(() => json(200, {}));
    render(<ApprovePanel run={passed} onChange={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve…" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve…" })).toBeTruthy();
  });

  it("reject calls only /reject and says nothing was sent", async () => {
    const fetchMock = stubFetch(() => json(200, { id: 1, decision: "rejected" }));
    render(<ApprovePanel run={passed} onChange={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(await screen.findByText("Rejected. Nothing was sent to GitHub.")).toBeTruthy();
    expect(fetchMock.mock.calls.map(([u]) => u)).toEqual(["/runs/1/reject"]);
  });

  it("shows server errors", async () => {
    stubFetch(() => json(502, { detail: "Approved, but the draft PR was not created: 422" }));
    render(<ApprovePanel run={passed} onChange={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve…" }));
    await userEvent.click(screen.getByRole("button", { name: "Create draft PR" }));
    expect((await screen.findByRole("alert")).textContent).toContain("not created: 422");
  });
});
