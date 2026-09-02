import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DlnaBrowser from "../../src/components/DlnaBrowser";
import {
  browseDlna,
  getCurrentDlnaItem,
  getDlnaServers,
  nextDlna,
  pauseDlna,
  playDlnaItem,
  previousDlna,
  resumeDlna,
} from "../../src/api/dlna";

vi.mock("../../src/api/dlna", () => ({
  getDlnaServers: vi.fn(),
  getCurrentDlnaItem: vi.fn(),
  browseDlna: vi.fn(),
  playDlnaItem: vi.fn(),
  pauseDlna: vi.fn(),
  resumeDlna: vi.fn(),
  nextDlna: vi.fn(),
  previousDlna: vi.fn(),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
  }),
}));

const server = {
  id: "server-1",
  name: "Test Media Server",
  location: "http://192.168.1.10/device.xml",
  control_url: "http://192.168.1.10/ContentDirectory/Control",
};

const currentTrack = {
  id: "track-current",
  parent_id: "0",
  title: "Current Track",
  is_container: false,
  resource_url: "http://192.168.1.10/current.mp3",
  media_class: "object.item.audioItem.musicTrack",
  artist: "Test Artist",
  album: "Test Album",
};

describe("DlnaBrowser playback controls", () => {
  beforeEach(() => {
    vi.clearAllMocks();

    vi.mocked(getDlnaServers).mockResolvedValue([server]);
    vi.mocked(getCurrentDlnaItem).mockResolvedValue(null);
    vi.mocked(browseDlna).mockResolvedValue({
      server_id: "server-1",
      object_id: "0",
      items: [],
    });
    vi.mocked(previousDlna).mockResolvedValue({
      device_id: "device-1",
      item: currentTrack,
    });
    vi.mocked(resumeDlna).mockResolvedValue(undefined);
    vi.mocked(pauseDlna).mockResolvedValue(undefined);
    vi.mocked(nextDlna).mockResolvedValue({
      device_id: "device-1",
      item: currentTrack,
    });
  });

  it("renders playback controls", async () => {
    render(<DlnaBrowser deviceId="device-1" />);

    await screen.findByText("Test Media Server");

    expect(screen.getByTitle("dlna.previous")).toBeInTheDocument();
    expect(screen.getByTitle("dlna.play")).toBeInTheDocument();
    expect(screen.getByTitle("dlna.pause")).toBeInTheDocument();
    expect(screen.getByTitle("dlna.next")).toBeInTheDocument();
  });

  it("sends playback control commands for the selected device", async () => {
    render(<DlnaBrowser deviceId="device-1" />);

    await screen.findByText("Test Media Server");

    fireEvent.click(screen.getByTitle("dlna.previous"));
    await waitFor(() => expect(previousDlna).toHaveBeenCalledWith("device-1"));

    fireEvent.click(screen.getByTitle("dlna.play"));
    await waitFor(() => expect(resumeDlna).toHaveBeenCalledWith("device-1"));

    fireEvent.click(screen.getByTitle("dlna.pause"));
    await waitFor(() => expect(pauseDlna).toHaveBeenCalledWith("device-1"));

    fireEvent.click(screen.getByTitle("dlna.next"));
    await waitFor(() => expect(nextDlna).toHaveBeenCalledWith("device-1"));
  });

  it("plays a track from the current media server view", async () => {
    vi.mocked(browseDlna).mockResolvedValue({
      server_id: "server-1",
      object_id: "0",
      items: [
        {
          id: "track-42",
          parent_id: "0",
          title: "Test Track",
          is_container: false,
          resource_url: "http://192.168.1.10/track.mp3",
          media_class: "object.item.audioItem.musicTrack",
        },
      ],
    });

    vi.mocked(playDlnaItem).mockResolvedValueOnce({
      device_id: "device-1",
      item: {
        id: "track-42",
        parent_id: "0",
        title: "Test Track",
        is_container: false,
        resource_url: "http://192.168.1.10/track.mp3",
        media_class: "object.item.audioItem.musicTrack",
      },
    });

    render(<DlnaBrowser deviceId="device-1" />);

    fireEvent.click(await screen.findByText("Test Track"));

    await waitFor(() => {
      expect(playDlnaItem).toHaveBeenCalledWith("server-1", "track-42", "device-1", "0");
    });
  });

  it("disables playback controls while a command is in progress", async () => {
    let resolvePause!: () => void;
    vi.mocked(pauseDlna).mockImplementationOnce(
      () =>
        new Promise<void>((resolve) => {
          resolvePause = resolve;
        })
    );

    render(<DlnaBrowser deviceId="device-1" />);

    await screen.findByText("Test Media Server");

    fireEvent.click(screen.getByTitle("dlna.pause"));

    await waitFor(() => {
      expect(screen.getByTitle("dlna.previous")).toBeDisabled();
      expect(screen.getByTitle("dlna.play")).toBeDisabled();
      expect(screen.getByTitle("dlna.pause")).toBeDisabled();
      expect(screen.getByTitle("dlna.next")).toBeDisabled();
    });

    resolvePause();

    await waitFor(() => {
      expect(screen.getByTitle("dlna.previous")).not.toBeDisabled();
      expect(screen.getByTitle("dlna.play")).not.toBeDisabled();
      expect(screen.getByTitle("dlna.pause")).not.toBeDisabled();
      expect(screen.getByTitle("dlna.next")).not.toBeDisabled();
    });
  });

  it("handles a playback control failure without breaking the controls", async () => {
    vi.mocked(pauseDlna).mockRejectedValueOnce(new Error("failed"));

    render(<DlnaBrowser deviceId="device-1" />);

    await screen.findByText("Test Media Server");

    fireEvent.click(screen.getByTitle("dlna.pause"));

    await waitFor(() => expect(pauseDlna).toHaveBeenCalledWith("device-1"));

    expect(screen.getByTitle("dlna.pause")).toBeInTheDocument();
    expect(screen.getByTitle("dlna.next")).toBeInTheDocument();
  });

  it("shows the currently playing track with artist and album", async () => {
    vi.mocked(getCurrentDlnaItem).mockResolvedValue(currentTrack);

    render(<DlnaBrowser deviceId="device-1" />);

    expect(await screen.findByText("▶ Current Track")).toBeInTheDocument();
    expect(screen.getByText("Test Artist · Test Album")).toBeInTheDocument();

    expect(getCurrentDlnaItem).toHaveBeenCalledWith("device-1");
  });

  it("does not show a current track when playback is idle", async () => {
    vi.mocked(getCurrentDlnaItem).mockResolvedValue(null);

    render(<DlnaBrowser deviceId="device-1" />);

    await screen.findByText("Test Media Server");

    expect(screen.queryByText("▶ Current Track")).not.toBeInTheDocument();
  });

  it("shows information about stereo pairs and zones", async () => {
    render(<DlnaBrowser deviceId="device-1" />);

    await screen.findByText("Test Media Server");

    const info = screen.getByLabelText("dlna.stereoInfoLabel");
    expect(info).toBeInTheDocument();

    fireEvent.click(info);

    expect(screen.getByText("dlna.stereoInfo")).toBeInTheDocument();
  });
});
