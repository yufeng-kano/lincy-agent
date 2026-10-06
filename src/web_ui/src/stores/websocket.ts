import { ref } from 'vue'
import { defineStore } from 'pinia'

export const useWebSocketStore = defineStore('websocket', () => {
  const connected = ref(false)
  let ws: WebSocket | null = null
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null
  const listeners: Array<(msg: Record<string, unknown>) => void> = []

  function connect() {
    if (ws && ws.readyState <= WebSocket.OPEN) return

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    ws = new WebSocket(`${proto}//${location.host}/ws`)

    ws.onopen = () => {
      connected.value = true
      if (reconnectTimer) {
        clearTimeout(reconnectTimer)
        reconnectTimer = null
      }
    }

    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data)
        listeners.forEach((fn) => fn(msg))
      } catch { /* ignore malformed */ }
    }

    ws.onclose = () => {
      connected.value = false
      ws = null
      reconnectTimer = setTimeout(connect, 3000)
    }

    ws.onerror = () => {
      ws?.close()
    }
  }

  function onMessage(fn: (msg: Record<string, unknown>) => void) {
    listeners.push(fn)
  }

  function sendPing() {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send('ping')
    }
  }

  return { connected, connect, onMessage, sendPing }
})
