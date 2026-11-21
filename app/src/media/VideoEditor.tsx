import { useEffect, useRef, useState } from 'react'
import { Check, Crop as CropIcon, LoaderCircle, Scissors } from 'lucide-react'
import { CropOverlay } from './CropOverlay'
import { errorMessage, fileSize } from './mediaType'
import { clamp, FULL_CROP } from './types'
import type { Crop } from './types'
import { useObjectUrl } from './useObjectUrl'
import { exportVideo } from './videoExport'

function formatTime(value: number) {
  const minutes = Math.floor(value / 60)
  const seconds = value - minutes * 60
  return `${minutes}:${seconds.toFixed(1).padStart(4, '0')}`
}

export function VideoEditor({ file, onApply, onClose }: {
  file: File
  onApply: (file: File) => void
  onClose: () => void
}) {
  const url = useObjectUrl(file)
  const videoRef = useRef<HTMLVideoElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const activeRef = useRef(true)
  const [duration, setDuration] = useState(0)
  const [start, setStart] = useState(0)
  const [end, setEnd] = useState(0)
  const [crop, setCrop] = useState<Crop>(FULL_CROP)
  const [dimensions, setDimensions] = useState<{ width: number; height: number } | null>(null)
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState('')

  useEffect(() => {
    activeRef.current = true
    const video = videoRef.current
    return () => {
      activeRef.current = false
      abortRef.current?.abort()
      video?.pause()
    }
  }, [])

  const loadedMetadata = () => {
    const video = videoRef.current
    if (!video || !Number.isFinite(video.duration) || video.duration <= 0) {
      setError('Не удалось определить длительность видео')
      return
    }
    setDuration(video.duration)
    setStart(0)
    setEnd(video.duration)
    setDimensions({ width: video.videoWidth, height: video.videoHeight })
    setError('')
  }

  const updateStart = (value: number) => {
    const next = clamp(value, 0, Math.max(0, end - Math.min(0.1, duration)))
    setStart(next)
    if (videoRef.current) videoRef.current.currentTime = next
  }

  const updateEnd = (value: number) => {
    const next = clamp(value, Math.min(duration, start + Math.min(0.1, duration)), duration)
    setEnd(next)
    if (videoRef.current) videoRef.current.currentTime = Math.max(start, next - 0.05)
  }

  const apply = async () => {
    if (busy || !duration || end <= start) return
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(true)
    setProgress(0)
    setError('')
    videoRef.current?.pause()
    try {
      const result = await exportVideo(file, { start, end, crop }, controller.signal, (value) => {
        if (activeRef.current) setProgress(clamp(value, 0, 1))
      })
      if (activeRef.current) onApply(result)
    } catch (reason) {
      if (!activeRef.current) return
      if (controller.signal.aborted) setError('Обработка отменена. Исходное видео не изменено.')
      else setError(errorMessage(reason, 'Не удалось обработать видео. Попробуйте отправить оригинал.'))
    } finally {
      if (activeRef.current) {
        setBusy(false)
        setProgress(0)
      }
    }
  }

  const minimum = Math.min(0.1, duration)
  const step = duration ? Math.min(0.1, duration / 100) : 0.1
  return <div className="media-editor-body">
    <div className="media-editor-scroll">
      <div className="media-editor-toolbar" aria-label="Инструменты видео">
        <span className="active"><Scissors size={18} />Фрагмент</span>
        <span className="active"><CropIcon size={18} />Кадрирование</span>
        <button type="button" disabled={busy} onClick={() => setCrop(FULL_CROP)}>Сбросить кадр</button>
      </div>

      {error && <p role="alert" className="mx-4 mb-3 rounded-xl bg-red-400/10 p-3 text-sm text-red-200 sm:mx-5">{error}</p>}
      {!url && <div className="flex min-h-64 items-center justify-center text-sm muted"><LoaderCircle className="mr-2 animate-spin" size={20} />Открываем видео…</div>}
      {url && <div className="media-stage-wrap">
        <div className="media-editor-stage" style={dimensions ? { aspectRatio: `${dimensions.width} / ${dimensions.height}`, maxWidth: `calc(52dvh * ${dimensions.width / dimensions.height})` } : undefined}>
          <video
            ref={videoRef}
            src={url}
            controls
            playsInline
            preload="metadata"
            className="size-full bg-black object-contain"
            onLoadedMetadata={loadedMetadata}
            onError={() => setError('Браузер не может воспроизвести это видео. Закройте редактор, чтобы отправить оригинал.')}
            onPlay={(event) => {
              const video = event.currentTarget
              if (video.currentTime < start || video.currentTime >= end) video.currentTime = start
            }}
            onTimeUpdate={(event) => {
              const video = event.currentTarget
              if (end > start && video.currentTime >= end) video.pause()
            }}
          />
          {dimensions && <CropOverlay crop={crop} onChange={setCrop} interactive={!busy} />}
          {busy && <div className="media-export-overlay"><LoaderCircle className="animate-spin accent" size={28} /><span>Обработка видео: {Math.round(progress * 100)}%</span><progress max="1" value={progress} /></div>}
        </div>
      </div>}

      {duration > 0 && <div className="media-trim-controls">
        <div className="flex items-center justify-between gap-3 text-xs"><span className="font-semibold">Выбранный фрагмент</span><span className="muted">{formatTime(end - start)} · {formatTime(start)} — {formatTime(end)}</span></div>
        <button type="button" disabled={busy} onClick={() => {
          const video = videoRef.current
          if (!video) return
          if (video.paused) {
            video.currentTime = start
            void video.play()
          } else video.pause()
        }} className="min-h-11 self-start rounded-xl bg-white/10 px-4 text-sm font-semibold hover:bg-white/15">Просмотреть фрагмент / пауза</button>
        <label className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-center gap-3 text-xs muted">Начало
          <input type="range" aria-label="Начало фрагмента" disabled={busy} min="0" max={Math.max(0, duration - minimum)} step={step} value={start} onChange={(event) => updateStart(Number(event.target.value))} />
        </label>
        <label className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-center gap-3 text-xs muted">Конец
          <input type="range" aria-label="Конец фрагмента" disabled={busy} min={Math.min(duration, start + minimum)} max={duration} step={step} value={end} onChange={(event) => updateEnd(Number(event.target.value))} />
        </label>
        <p className="text-xs leading-5 muted">Перетащите рамку на видео для кадрирования. Готовый файл будет сохранён в MP4 со звуком.</p>
      </div>}
      <div className="px-4 pb-1 text-xs muted sm:px-5">{fileSize(file.size)} · обработка на этом устройстве</div>
    </div>
    <footer className="media-editor-footer">
      {busy
        ? <button type="button" onClick={() => abortRef.current?.abort()} className="min-h-11 rounded-xl bg-white/10 px-5 text-sm font-semibold">Отменить обработку</button>
        : <button type="button" onClick={onClose} className="min-h-11 rounded-xl bg-white/10 px-5 text-sm font-semibold">Отмена</button>}
      <button type="button" disabled={!duration || busy || end <= start} onClick={() => void apply()} className="accent-bg flex min-h-11 items-center justify-center gap-2 rounded-xl px-5 text-sm font-bold">{busy ? <LoaderCircle className="animate-spin" size={18} /> : <Check size={18} />}{busy ? `${Math.round(progress * 100)}%` : 'Применить'}</button>
    </footer>
  </div>
}
