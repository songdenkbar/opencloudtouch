import { throwIfNotOk } from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "";

export interface DlnaServer {
  id: string;
  name: string;
  location: string;
  control_url: string;
}

export interface DlnaItem {
  id: string;
  parent_id: string;
  title: string;
  is_container: boolean;
  resource_url?: string | null;
  media_class?: string | null;
  artist?: string | null;
  album?: string | null;
  genre?: string | null;
  creator?: string | null;
  album_art_url?: string | null;
  duration?: string | null;
  size?: number | null;
  bitrate?: number | null;
  sample_frequency?: number | null;
  audio_channels?: number | null;
  protocol_info?: string | null;
}

interface DlnaBrowseResponse {
  server_id: string;
  object_id: string;
  items: DlnaItem[];
}

interface DlnaPlayResponse {
  device_id: string;
  item: DlnaItem;
}

export async function getDlnaServers(): Promise<DlnaServer[]> {
  const response = await fetch(`${API_BASE_URL}/api/dlna/servers`);
  await throwIfNotOk(response, "Failed to discover DLNA servers");
  return response.json();
}

export async function browseDlna(serverId: string, objectId = "0"): Promise<DlnaBrowseResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/dlna/servers/${encodeURIComponent(serverId)}/browse?object_id=${encodeURIComponent(objectId)}`
  );
  await throwIfNotOk(response, "Failed to browse DLNA server");
  return response.json();
}

export async function playDlnaItem(
  serverId: string,
  objectId: string,
  deviceId: string,
  parentId: string
): Promise<DlnaPlayResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/dlna/servers/${encodeURIComponent(serverId)}` +
      `/items/${encodeURIComponent(objectId)}` +
      `/play/${encodeURIComponent(deviceId)}` +
      `?parent_id=${encodeURIComponent(parentId)}`,
    { method: "POST" }
  );

  await throwIfNotOk(response, "Failed to play DLNA item");
  return response.json();
}

export async function pauseDlna(deviceId: string): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/api/dlna/devices/${encodeURIComponent(deviceId)}/pause`,
    { method: "POST" }
  );
  await throwIfNotOk(response, "Failed to pause DLNA playback");
}

export async function resumeDlna(deviceId: string): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/api/dlna/devices/${encodeURIComponent(deviceId)}/resume`,
    { method: "POST" }
  );
  await throwIfNotOk(response, "Failed to resume DLNA playback");
}

export async function nextDlna(deviceId: string): Promise<DlnaPlayResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/dlna/devices/${encodeURIComponent(deviceId)}/next`,
    { method: "POST" }
  );
  await throwIfNotOk(response, "Failed to play next DLNA item");
  return response.json();
}

export async function previousDlna(deviceId: string): Promise<DlnaPlayResponse> {
  const response = await fetch(
    `${API_BASE_URL}/api/dlna/devices/${encodeURIComponent(deviceId)}/previous`,
    { method: "POST" }
  );
  await throwIfNotOk(response, "Failed to play previous DLNA item");
  return response.json();
}
