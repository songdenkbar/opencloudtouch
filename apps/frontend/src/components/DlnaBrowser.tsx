import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  browseDlna,
  getDlnaServers,
  nextDlna,
  pauseDlna,
  playDlnaItem,
  previousDlna,
  resumeDlna,
  type DlnaItem,
  type DlnaServer,
} from "../api/dlna";
import "./DlnaBrowser.css";

interface DlnaBrowserProps {
  readonly deviceId: string;
}

interface BrowseLevel {
  id: string;
  title: string;
}

export default function DlnaBrowser({ deviceId }: DlnaBrowserProps) {
  const { t } = useTranslation();
  const [servers, setServers] = useState<DlnaServer[]>([]);
  const [serverId, setServerId] = useState("");
  const [items, setItems] = useState<DlnaItem[]>([]);
  const [path, setPath] = useState<BrowseLevel[]>([{ id: "0", title: "" }]);
  const [loading, setLoading] = useState(true);
  const [playingId, setPlayingId] = useState<string | null>(null);
  const [controlBusy, setControlBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const currentLevel = path[path.length - 1];

  const loadItems = useCallback(
    async (selectedServerId: string, objectId: string) => {
      setLoading(true);
      setError(null);

      try {
        const result = await browseDlna(selectedServerId, objectId);
        setItems(result.items);
      } catch (err) {
        console.error("[DLNA] Browse failed:", err);
        setError(t("dlna.browseFailed"));
      } finally {
        setLoading(false);
      }
    },
    [t]
  );

  useEffect(() => {
    let cancelled = false;

    async function loadServers() {
      setLoading(true);
      setError(null);

      try {
        const result = await getDlnaServers();
        if (cancelled) return;

        setServers(result);

        if (result.length > 0) {
          setServerId(result[0].id);
          await loadItems(result[0].id, "0");
        } else {
          setLoading(false);
        }
      } catch (err) {
        console.error("[DLNA] Discovery failed:", err);
        if (!cancelled) {
          setError(t("dlna.discoveryFailed"));
          setLoading(false);
        }
      }
    }

    void loadServers();

    return () => {
      cancelled = true;
    };
  }, [loadItems, t]);

  const handleServerChange = async (newServerId: string) => {
    setServerId(newServerId);
    setPath([{ id: "0", title: "" }]);
    await loadItems(newServerId, "0");
  };

  const openContainer = async (item: DlnaItem) => {
    setPath((current) => [...current, { id: item.id, title: item.title }]);
    await loadItems(serverId, item.id);
  };

  const goBack = async () => {
    if (path.length <= 1) return;

    const newPath = path.slice(0, -1);
    setPath(newPath);
    await loadItems(serverId, newPath[newPath.length - 1].id);
  };

  const playItem = async (item: DlnaItem) => {
    setPlayingId(item.id);
    setError(null);

    try {
      await playDlnaItem(serverId, item.id, deviceId, currentLevel.id);
    } catch (err) {
      console.error("[DLNA] Playback failed:", err);
      setError(t("dlna.playFailed"));
    } finally {
      setPlayingId(null);
    }
  };

  const controlPlayback = async (action: "previous" | "resume" | "pause" | "next") => {
    setControlBusy(true);
    setError(null);

    try {
      switch (action) {
        case "previous":
          await previousDlna(deviceId);
          break;
        case "resume":
          await resumeDlna(deviceId);
          break;
        case "pause":
          await pauseDlna(deviceId);
          break;
        case "next":
          await nextDlna(deviceId);
          break;
      }
    } catch (err) {
      console.error(`[DLNA] ${action} failed:`, err);
      setError(t("dlna.playFailed"));
    } finally {
      setControlBusy(false);
    }
  };

  return (
    <div className="dlna-browser">
      <div className="dlna-browser-header">
        {servers.length > 0 && (
          <select
            className="dlna-server-select"
            value={serverId}
            onChange={(event) => void handleServerChange(event.target.value)}
            aria-label={t("dlna.server")}
          >
            {servers.map((server) => (
              <option key={server.id} value={server.id}>
                {server.name}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="dlna-playback-controls">
        <button
          type="button"
          onClick={() => void controlPlayback("previous")}
          disabled={controlBusy}
          aria-label={t("dlna.previous")}
          title={t("dlna.previous")}
        >
          ⏮
        </button>
        <button
          type="button"
          onClick={() => void controlPlayback("resume")}
          disabled={controlBusy}
          aria-label={t("dlna.play")}
          title={t("dlna.play")}
        >
          ▶
        </button>
        <button
          type="button"
          onClick={() => void controlPlayback("pause")}
          disabled={controlBusy}
          aria-label={t("dlna.pause")}
          title={t("dlna.pause")}
        >
          ⏸
        </button>
        <button
          type="button"
          onClick={() => void controlPlayback("next")}
          disabled={controlBusy}
          aria-label={t("dlna.next")}
          title={t("dlna.next")}
        >
          ⏭
        </button>
      </div>

      {path.length > 1 && (
        <button className="dlna-back" onClick={() => void goBack()}>
          ← {currentLevel.title}
        </button>
      )}

      {loading && <div className="dlna-message">{t("common.loading")}</div>}

      {!loading && servers.length === 0 && !error && (
        <div className="dlna-message">{t("dlna.noServers")}</div>
      )}

      {error && <div className="dlna-message dlna-error">{error}</div>}

      {!loading && !error && (
        <div className="dlna-item-list">
          {items.map((item) => (
            <button
              key={item.id}
              className="dlna-item"
              onClick={() => (item.is_container ? void openContainer(item) : void playItem(item))}
              disabled={playingId === item.id}
            >
              <span className="dlna-item-icon">{item.is_container ? "📁" : "♪"}</span>

              <span className="dlna-item-text">
                <span className="dlna-item-title">{item.title}</span>

                {!item.is_container && (item.artist || item.album) && (
                  <span className="dlna-item-meta">
                    {[item.artist, item.album].filter(Boolean).join(" · ")}
                  </span>
                )}
              </span>

              <span className="dlna-item-action">
                {playingId === item.id ? "…" : item.is_container ? "›" : "▶"}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
