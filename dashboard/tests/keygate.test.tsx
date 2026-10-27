import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { getKey } from "../src/api";
import { KeyGate } from "../src/components/KeyGate";
import { json, stubFetch } from "./helpers";

describe("KeyGate", () => {
  it("rejects a wrong key and stores nothing", async () => {
    stubFetch(() => json(401, { detail: "Missing or wrong X-API-Key" }));
    const onDone = vi.fn();
    render(<KeyGate onDone={onDone} />);
    await userEvent.type(screen.getByLabelText(/API key/), "wrong-key-wrong-key");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect((await screen.findByRole("alert")).textContent).toBe("Wrong key.");
    expect(onDone).not.toHaveBeenCalled();
    expect(getKey()).toBeNull();
  });

  it("checks a right key with the server, then stores it for this tab", async () => {
    const fetchMock = stubFetch(() => json(200, { runs: [] }));
    const onDone = vi.fn();
    render(<KeyGate onDone={onDone} />);
    await userEvent.type(screen.getByLabelText(/API key/), "right-key-123456789");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));
    await vi.waitFor(() => expect(onDone).toHaveBeenCalledOnce());
    expect(new Headers(fetchMock.mock.calls[0][1].headers).get("X-API-Key")).toBe("right-key-123456789");
    expect(getKey()).toBe("right-key-123456789");
  });
});
