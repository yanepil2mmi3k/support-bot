import { useEffect, useState } from 'react'

export function useObjectUrl(blob: Blob | null) {
  const [loaded, setLoaded] = useState<{ blob: Blob; url: string } | null>(null)

  useEffect(() => {
    if (!blob) return
    let active = true
    const url = URL.createObjectURL(blob)
    void Promise.resolve().then(() => {
      if (active) setLoaded({ blob, url })
    })
    return () => {
      active = false
      URL.revokeObjectURL(url)
    }
  }, [blob])

  return blob && loaded?.blob === blob ? loaded.url : ''
}
