import { useState, useRef, useCallback } from 'react'
import { tokenStore } from '../api/client'

interface SSECallbacks {
  onDelta?: (delta: string) => void
  onChart?: (payload: Record<string, unknown>) => void
  onDone?: (fullContent: string) => void
  onError?: (message: string) => void
}

export function useSSE() {
  const [isStreaming, setIsStreaming] = useState(false)
  const [lastError, setLastError] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)

  const sendMessage = useCallback(async (
    sessionId: string,
    content: string,
    callbacks: SSECallbacks
  ) => {
    setIsStreaming(true)
    setLastError(null)
    abortRef.current = new AbortController()

    const token = tokenStore.getAccess()
    const baseUrl = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1'

    try {
      const response = await fetch(`${baseUrl}/ai/sessions/${sessionId}/messages`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}`,
        },
        body: JSON.stringify({ content }),
        signal: abortRef.current.signal,
      })

      if (!response.ok) {
        const errData = await response.json().catch(() => ({}))
        const msg = errData?.error?.message || errData?.detail?.message || `HTTP ${response.status}`
        setLastError(msg)
        callbacks.onError?.(msg)
        setIsStreaming(false)
        return
      }

      const reader = response.body?.getReader()
      if (!reader) {
        setLastError('No response body')
        setIsStreaming(false)
        return
      }

      const decoder = new TextDecoder()
      let buffer = ''
      let fullContent = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue
          const jsonStr = line.slice(6).trim()
          if (!jsonStr) continue

          try {
            const event = JSON.parse(jsonStr)
            switch (event.type) {
              case 'message':
                fullContent += event.delta
                callbacks.onDelta?.(event.delta)
                break
              case 'chart':
                callbacks.onChart?.(event.payload)
                break
              case 'done':
                callbacks.onDone?.(fullContent)
                break
              case 'error':
                setLastError(event.message)
                callbacks.onError?.(event.message)
                break
            }
          } catch {
            // skip unparseable lines
          }
        }
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const msg = err instanceof Error ? err.message : 'Unknown error'
      setLastError(msg)
      callbacks.onError?.(msg)
    } finally {
      setIsStreaming(false)
    }
  }, [])

  const abort = useCallback(() => {
    abortRef.current?.abort()
    setIsStreaming(false)
  }, [])

  return { sendMessage, isStreaming, lastError, abort }
}
