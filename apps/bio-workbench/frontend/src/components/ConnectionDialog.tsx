import { useState } from "react";
import { FolderKey, LoaderCircle, Plug, X } from "lucide-react";
import type { Connection, WorkbenchApi } from "../api";
export function ConnectionDialog({
  connection,
  api,
  onClose,
  onConnected,
}: {
  connection?: Connection;
  api: WorkbenchApi;
  onClose: () => void;
  onConnected: () => Promise<void>;
}) {
  const [host, setHost] = useState(connection?.host ?? "");
  const [user, setUser] = useState(connection?.user ?? "root");
  const [port, setPort] = useState(connection?.port ?? 22);
  const [key, setKey] = useState(connection?.key_path ?? "");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  async function connect() {
    setBusy(true);
    setMessage("");
    try {
      await api.updateConnection({ host, user, port, key_path: key });
      const result = await api.checkConnection();
      if (result.ok === false || result.connected === false)
        throw new Error(result.message ?? "Connection check failed.");
      await onConnected();
      onClose();
    } catch (e) {
      setMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="dialog-backdrop" onClick={onClose}>
      <section
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="connection-title"
        onClick={(e) => e.stopPropagation()}
      >
        <header>
          <div className="dialog-icon">
            <Plug size={23} />
          </div>
          <button
            className="icon-button"
            aria-label="Close connection settings"
            onClick={onClose}
          >
            <X size={19} />
          </button>
        </header>
        <h2 id="connection-title">Connect your workbench</h2>
        <p>
          Your head stores constructs, job history, and results. This app
          connects using your local SSH configuration.
        </p>
        <label>
          Head hostname or IP
          <input
            autoFocus
            value={host}
            onChange={(e) => setHost(e.target.value)}
            placeholder="bio-head.example.com"
            autoComplete="off"
          />
        </label>
        <div className="field-row">
          <label className="grow">
            SSH user
            <input
              value={user}
              onChange={(e) => setUser(e.target.value)}
              autoComplete="off"
            />
          </label>
          <label>
            Port
            <input
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(e) => setPort(Number(e.target.value))}
            />
          </label>
        </div>
        <label>
          SSH key path <span className="muted">(optional)</span>
          <div className="key-input">
            <input
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="Leave empty to use your SSH agent"
              autoComplete="off"
            />
            {window.bioDesktop?.chooseSSHKey && (
              <button
                className="icon-button"
                aria-label="Choose SSH key"
                onClick={() => {
                  void window.bioDesktop?.chooseSSHKey?.().then((path) => {
                    if (path) setKey(path);
                  });
                }}
              >
                <FolderKey size={18} />
              </button>
            )}
          </div>
        </label>
        <p className="field-help">
          Choose a key that is already on this computer. Private key contents
          stay outside the interface.
        </p>
        {message && (
          <div className="inline-error" role="alert">
            {message}
          </div>
        )}
        <button
          className="button primary wide"
          disabled={
            busy || !host.trim() || !user.trim() || port < 1 || port > 65535
          }
          onClick={() => void connect()}
        >
          {busy ? (
            <LoaderCircle size={17} className="spin" />
          ) : (
            <Plug size={17} />
          )}
          Save & check connection
        </button>
      </section>
    </div>
  );
}
