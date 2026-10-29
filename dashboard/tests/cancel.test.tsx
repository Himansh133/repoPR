import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { CancelPanel } from "../src/components/CancelPanel";
import type { Run } from "../src/types";
import { detail, json, stubFetch } from "./helpers";

const running = detail({ status: "running" }) as Run;

describe("CancelPanel", () => {
  it("is hidden for finished runs", () => {
    const { container } = render(<CancelPanel run={{ ...running, status: "passed" }} />);
    expect(container.textContent).toBe("");
  });

  it("needs a second click, then posts /cancel", async () => {
    const fetchMock = stubFetch(() => json(202, { id: 1, cancelling: true }));
    render(<CancelPanel run={running} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel run…" }));
    expect(fetchMock).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Stop this run" }));
    expect(await screen.findByText(/Cancelling/)).toBeTruthy();
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/runs/1/cancel");
    expect(init.method).toBe("POST");
  });

  it("keep running sends nothing", async () => {
    const fetchMock = stubFetch(() => json(202, {}));
    render(<CancelPanel run={running} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel run…" }));
    await userEvent.click(screen.getByRole("button", { name: "Keep running" }));
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Cancel run…" })).toBeTruthy();
  });

  it("shows server errors", async () => {
    stubFetch(() => json(409, { detail: "Run #1 is not running (it is passed)" }));
    render(<CancelPanel run={running} />);
    await userEvent.click(screen.getByRole("button", { name: "Cancel run…" }));
    await userEvent.click(screen.getByRole("button", { name: "Stop this run" }));
    expect((await screen.findByRole("alert")).textContent).toContain("not running");
  });
});
