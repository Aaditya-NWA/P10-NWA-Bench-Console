import React from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import FirmwarePanel from "./FirmwarePanel";
import * as apiModule from "../api";

vi.mock("../api");

describe("FirmwarePanel", () => {
  let clicked;

  beforeEach(() => {
    clicked = [];
    URL.createObjectURL = vi.fn(() => "blob:esp32");
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function () {
      clicked.push({ href: this.href, download: this.download });
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("fetches nothing until the operator asks", () => {
    render(<FirmwarePanel />);
    expect(apiModule.api).not.toHaveBeenCalled();
  });

  it("downloads esp32.zip from the backend on click", async () => {
    apiModule.api.mockResolvedValue({ blob: async () => new Blob(["zip"]) });
    render(<FirmwarePanel />);
    await userEvent.click(screen.getByRole("button", { name: /download esp32\.zip/i }));

    expect(apiModule.api).toHaveBeenCalledWith("/firmware/source", { raw: true });
    expect(clicked).toEqual([{ href: "blob:esp32", download: "esp32.zip" }]);
    expect(await screen.findByText(/check your downloads folder/i)).toBeTruthy();
  });

  it("shows the backend's reason when GitHub can't be reached", async () => {
    apiModule.api.mockRejectedValue({
      status: 502, message: "Could not reach GitHub — check this PC's internet connection.",
    });
    render(<FirmwarePanel />);
    await userEvent.click(screen.getByRole("button", { name: /download esp32\.zip/i }));

    expect(await screen.findByText(/could not reach github/i)).toBeTruthy();
    expect(clicked).toEqual([]);
    // The button comes back so the operator can retry once online.
    expect(screen.getByRole("button", { name: /download esp32\.zip/i }).disabled).toBe(false);
  });
});
