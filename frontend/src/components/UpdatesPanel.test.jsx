import React from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import UpdatesPanel from "./UpdatesPanel";
import * as desktop from "../desktop";

vi.mock("../desktop");

describe("UpdatesPanel", () => {
  beforeEach(() => {
    desktop.isDesktop.mockReturnValue(true);
    desktop.onUpdateProgress.mockResolvedValue(() => {});
  });

  it("checks nothing until the operator asks", () => {
    render(<UpdatesPanel />);
    // The whole point of the manual design: mounting the panel must not
    // contact the release channel, or the repository would have to be
    // public whenever anyone opens Settings.
    expect(desktop.checkForUpdate).not.toHaveBeenCalled();
  });

  it("says 'No Updates Available.' when the channel reports current", async () => {
    desktop.checkForUpdate.mockResolvedValue({
      state: "none", current_version: "1.0.0",
    });
    render(<UpdatesPanel />);
    await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));

    expect(await screen.findByText("No Updates Available.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /update now/i })).toBeNull();
  });

  it("shows the title, size and Update Now when one is available", async () => {
    desktop.checkForUpdate.mockResolvedValue({
      state: "available",
      current_version: "1.0.0",
      version: "1.1.0",
      title: "Fix torque sign on CCW runs",
      size_bytes: 41_400_000,
    });
    render(<UpdatesPanel />);
    await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));

    expect(await screen.findByText(/Version 1\.1\.0/)).toBeTruthy();
    expect(screen.getByText("Fix torque sign on CCW runs")).toBeTruthy();
    expect(screen.getByText("41.4 MB")).toBeTruthy();
    expect(screen.getByRole("button", { name: /update now/i })).toBeTruthy();
  });

  it("omits the size when the server did not report one", async () => {
    desktop.checkForUpdate.mockResolvedValue({
      state: "available", current_version: "1.0.0", version: "1.1.0",
      title: "Something", size_bytes: null,
    });
    render(<UpdatesPanel />);
    await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));

    await screen.findByText(/Version 1\.1\.0/);
    // A missing Content-Length must not render as "—  MB" or "0.0 MB";
    // an absent size is simply not shown.
    expect(screen.queryByText(/MB/)).toBeNull();
  });

  it("distinguishes an unreachable channel from being up to date", async () => {
    desktop.checkForUpdate.mockResolvedValue({
      state: "unreachable", current_version: "1.0.0", detail: "404",
    });
    render(<UpdatesPanel />);
    await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));

    // A private repository answers exactly like one with no releases.
    // Calling that "up to date" would hide a closed release window, so
    // the two must not share wording.
    expect(await screen.findByText(/could not reach the update server/i)).toBeTruthy();
    expect(screen.queryByText("No Updates Available.")).toBeNull();
  });

  it("shows the shell's refusal verbatim when the bench is busy", async () => {
    desktop.checkForUpdate.mockResolvedValue({
      state: "available", current_version: "1.0.0", version: "1.1.0",
      title: "New", size_bytes: 1_000_000,
    });
    desktop.installUpdate.mockRejectedValue(
      new Error("A throttle profile is running. Stop it before installing an update.")
    );

    render(<UpdatesPanel />);
    await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));
    await userEvent.click(await screen.findByRole("button", { name: /update now/i }));

    // The message names which condition blocks the install; the operator
    // is at the bench and can clear it.
    expect(
      await screen.findByText(/a throttle profile is running/i)
    ).toBeTruthy();
  });

  it("subscribes to progress before starting the download", async () => {
    desktop.checkForUpdate.mockResolvedValue({
      state: "available", current_version: "1.0.0", version: "1.1.0",
      title: "New", size_bytes: 1_000_000,
    });
    const order = [];
    desktop.onUpdateProgress.mockImplementation(async () => {
      order.push("subscribe"); return () => {};
    });
    desktop.installUpdate.mockImplementation(async () => {
      order.push("install"); return undefined;
    });

    render(<UpdatesPanel />);
    await userEvent.click(screen.getByRole("button", { name: /check for updates/i }));
    await userEvent.click(await screen.findByRole("button", { name: /update now/i }));

    // Subscribing after the download starts loses the first chunks and
    // the bar jumps from 0 to part-way.
    await waitFor(() => expect(order).toEqual(["subscribe", "install"]));
  });

  it("explains itself instead of failing when run in a browser", async () => {
    desktop.isDesktop.mockReturnValue(false);
    render(<UpdatesPanel />);

    expect(screen.getByText(/handled by the desktop application/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /check for updates/i })).toBeNull();
  });
});
