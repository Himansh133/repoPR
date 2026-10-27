import { act, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { setKey } from "../src/api";
import { App } from "../src/App";
import { json, run, stubFetch } from "./helpers";

describe("App", () => {
  it("asks for the key first", () => {
    render(<App />);
    expect(screen.getByLabelText(/API key/)).toBeTruthy();
  });

  it("shows runs when a key is stored, and a run page from the hash", async () => {
    setKey("k-1234567890abcdef");
    stubFetch(() => json(200, { runs: [run()] }));
    render(<App />);
    expect(await screen.findByRole("link", { name: "#1" })).toBeTruthy();
    act(() => {
      window.location.hash = "#/runs/1";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(screen.getByRole("heading", { name: "Run #1" })).toBeTruthy();
  });

  it("returns to the key screen when the key stops working", async () => {
    setKey("k-1234567890abcdef");
    stubFetch(() => json(401, { detail: "Missing or wrong X-API-Key" }));
    render(<App />);
    expect(await screen.findByLabelText(/API key/)).toBeTruthy();
  });
});
