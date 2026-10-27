import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { RunList } from "../src/components/RunList";
import { json, run, stubFetch } from "./helpers";

describe("RunList", () => {
  it("lists runs with status words, links and PR links", async () => {
    stubFetch(() =>
      json(200, { runs: [run(), run({ id: 2, status: "aborted", cost: null, pr_url: "https://github.com/octo/demo/pull/9" })] }),
    );
    render(<RunList />);
    const link = await screen.findByRole("link", { name: "#1" });
    expect(link.getAttribute("href")).toBe("#/runs/1");
    expect(screen.getByText("passed")).toBeTruthy();
    expect(screen.getByText("aborted")).toBeTruthy();
    expect(screen.getByText("unknown")).toBeTruthy();
    expect(screen.getAllByText("DeepSeek-V4-Flash")).toHaveLength(2);
    const pr = screen.getByRole("link", { name: "draft PR" });
    expect(pr.getAttribute("rel")).toContain("noopener");
  });

  it("shows an empty state", async () => {
    stubFetch(() => json(200, { runs: [] }));
    render(<RunList />);
    expect(await screen.findByText("No runs yet.")).toBeTruthy();
  });

  it("shows an error state", async () => {
    stubFetch(() => json(500, { detail: "boom" }));
    render(<RunList />);
    expect((await screen.findByRole("alert")).textContent).toContain("boom");
  });

  it("renders server text as text, not HTML", async () => {
    stubFetch(() => json(200, { runs: [run({ issue_title: "<img src=x onerror=alert(1)>" })] }));
    const { container } = render(<RunList />);
    expect(await screen.findByText("<img src=x onerror=alert(1)>", { exact: false })).toBeTruthy();
    expect(container.querySelector("img")).toBeNull();
  });
});
