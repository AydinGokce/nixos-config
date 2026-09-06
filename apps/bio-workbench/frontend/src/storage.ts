// Electron runs on a fresh loopback origin each launch. The preload owns durable
// local state so draft/annotation persistence does not depend on that port.
export function readLocal(key: string): string | null {
  return window.bioDesktop?.state
    ? window.bioDesktop.state.get(key)
    : localStorage.getItem(key);
}
export function writeLocal(key: string, value: string): void {
  if (window.bioDesktop?.state) window.bioDesktop.state.set(key, value);
  else localStorage.setItem(key, value);
}
