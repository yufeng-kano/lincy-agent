/** sessionStorage cache so F5 can paint last proxy usage before network returns. */

const PREFIX = 'lincy.proxy-accounts.'

export function loadProxyAccountsCache<T>(key: 'claude' | 'codex'): T | null {
  try {
    const raw = sessionStorage.getItem(PREFIX + key)
    if (!raw) return null
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

export function saveProxyAccountsCache(key: 'claude' | 'codex', value: unknown): void {
  try {
    sessionStorage.setItem(PREFIX + key, JSON.stringify(value))
  } catch {
    // Private mode / quota — skip; next load just shows Loading again.
  }
}
