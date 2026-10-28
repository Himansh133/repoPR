import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { RunForm } from "../src/components/RunForm";
import { json, stubFetch } from "./helpers";

async function fill(repo: string, issue: string, title = "", body = "") {
  await userEvent.type(screen.getByLabelText(/Repository/), repo);
  await userEvent.type(screen.getByLabelText("Issue number"), issue);
  if (title) await userEvent.type(screen.getByLabelText("Issue title (optional)"), title);
  if (body) await userEvent.type(screen.getByLabelText("Issue body (optional)"), body);
  await userEvent.click(screen.getByRole("button", { name: "Start run" }));
}

describe("RunForm", () => {
  it("posts the run and opens its page", async () => {
    const fetchMock = stubFetch(() => json(202, { id: 12, status: "running" }));
    render(<RunForm />);
    await fill("/home/me/repo", "3", "Crash", "details");
    await vi.waitFor(() => expect(window.location.hash).toBe("#/runs/12"));
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/runs");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      repo: "/home/me/repo", issue: 3, issue_title: "Crash", issue_body: "details", allow_unpriced: false,
    });
  });

  it("leaves out title and body when fetching from GitHub", async () => {
    const fetchMock = stubFetch(() => json(202, { id: 1 }));
    render(<RunForm />);
    await fill("https://github.com/o/r", "5");
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      repo: "https://github.com/o/r", issue: 5, allow_unpriced: false,
    });
  });

  it("validates before sending", async () => {
    const fetchMock = stubFetch(() => json(202, { id: 1 }));
    render(<RunForm />);
    await fill("/r", "0");
    expect((await screen.findByRole("alert")).textContent).toContain("1 or more");
    await userEvent.clear(screen.getByLabelText("Issue number"));
    await userEvent.type(screen.getByLabelText("Issue number"), "2");
    await userEvent.type(screen.getByLabelText("Issue body (optional)"), "body only");
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect((await screen.findByRole("alert")).textContent).toContain("title");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("shows the server's reason when a run cannot start", async () => {
    stubFetch(() => json(409, { detail: "A run is already in progress; try again when it finishes" }));
    render(<RunForm />);
    await fill("/r", "1");
    expect((await screen.findByRole("alert")).textContent).toContain("already in progress");
    expect(window.location.hash).toBe("");
  });
});
