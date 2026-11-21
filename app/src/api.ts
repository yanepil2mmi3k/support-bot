declare global {
  interface Window {
    Telegram?: { WebApp?: { initData?: string; ready?: () => void; expand?: () => void } }
  }
}

const API_ROOT = import.meta.env.VITE_API_URL || '/api'

function requestHeaders(options: RequestInit, json: boolean) {
  const headers = new Headers(options.headers)
  const initData = window.Telegram?.WebApp?.initData || ''
  if (initData) headers.set('X-Telegram-Init-Data', initData)
  if (json && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  return headers
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    headers: requestHeaders(options, !(options.body instanceof FormData)),
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) throw new Error(data.detail || 'Не удалось выполнить действие')
  return data as T
}

export async function apiBlob(path: string, options: RequestInit = {}): Promise<Blob> {
  const apiPath = path.startsWith(`${API_ROOT}/`) ? path.slice(API_ROOT.length) : path
  const response = await fetch(`${API_ROOT}${apiPath}`, {
    ...options,
    headers: requestHeaders(options, false),
  })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    throw new Error(data.detail || 'Не удалось загрузить медиа')
  }
  return response.blob()
}

export function initTelegram() {
  window.Telegram?.WebApp?.ready?.()
  window.Telegram?.WebApp?.expand?.()
}
