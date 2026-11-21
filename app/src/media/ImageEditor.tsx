import { useEffect, useRef, useState } from 'react'
import type { PointerEvent } from 'react'
import { Brush, Check, Crop as CropIcon, LoaderCircle, RotateCw, Trash2, Undo2 } from 'lucide-react'
import { CropOverlay } from './CropOverlay'
import { exportImage, loadImage, renderRotatedImage, rotatedSize } from './imageExport'
import { errorMessage, fileSize } from './mediaType'
import { clamp, FULL_CROP, rotateCrop, rotatePoint } from './types'
import type { BrushStroke, Crop, Point, Rotation } from './types'

const colors = ['#ff3b30', '#ffd60a', '#34c759', '#0a84ff', '#ffffff', '#000000']

export function ImageEditor({ file, onApply, onClose }: {
  file: File
  onApply: (file: File) => void
  onClose: () => void
}) {
  const [image, setImage] = useState<HTMLImageElement | null>(null)
  const [rotation, setRotation] = useState<Rotation>(0)
  const [crop, setCrop] = useState<Crop>(FULL_CROP)
  const [strokes, setStrokes] = useState<BrushStroke[]>([])
  const [mode, setMode] = useState<'crop' | 'draw'>('crop')
  const [color, setColor] = useState(colors[0])
  const [brushSize, setBrushSize] = useState(6)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const drawingRef = useRef(false)

  useEffect(() => {
    let active = true
    void loadImage(file).then((loaded) => {
      if (active) setImage(loaded)
    }).catch((reason) => {
      if (active) setError(errorMessage(reason, 'Не удалось открыть изображение'))
    })
    return () => { active = false }
  }, [file])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!image || !canvas) return
    const rendered = renderRotatedImage(image, rotation, strokes, 1600)
    canvas.width = rendered.width
    canvas.height = rendered.height
    canvas.getContext('2d')?.drawImage(rendered, 0, 0)
  }, [image, rotation, strokes])

  const size = image ? rotatedSize(image, rotation) : null
  const pointFromEvent = (event: PointerEvent<HTMLDivElement>): Point | null => {
    const bounds = stageRef.current?.getBoundingClientRect()
    if (!bounds?.width || !bounds.height) return null
    return {
      x: clamp((event.clientX - bounds.left) / bounds.width, 0, 1),
      y: clamp((event.clientY - bounds.top) / bounds.height, 0, 1),
    }
  }

  const startDrawing = (event: PointerEvent<HTMLDivElement>) => {
    if (mode !== 'draw' || busy) return
    const point = pointFromEvent(event)
    const bounds = stageRef.current?.getBoundingClientRect()
    if (!point || !bounds) return
    event.preventDefault()
    event.currentTarget.setPointerCapture(event.pointerId)
    drawingRef.current = true
    setStrokes((current) => [...current, { color, size: brushSize / bounds.width, points: [point] }])
  }

  const draw = (event: PointerEvent<HTMLDivElement>) => {
    if (!drawingRef.current || mode !== 'draw') return
    const point = pointFromEvent(event)
    if (!point) return
    event.preventDefault()
    setStrokes((current) => {
      if (!current.length) return current
      const next = [...current]
      const last = next[next.length - 1]
      next[next.length - 1] = { ...last, points: [...last.points, point] }
      return next
    })
  }

  const stopDrawing = (event: PointerEvent<HTMLDivElement>) => {
    drawingRef.current = false
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  const rotate = () => {
    const nextRotation = ((rotation + 90) % 360) as Rotation
    const sizeRatio = image ? rotatedSize(image, rotation).width / rotatedSize(image, nextRotation).width : 1
    setRotation(nextRotation)
    setCrop((current) => rotateCrop(current, 90))
    setStrokes((current) => current.map((stroke) => ({ ...stroke, size: stroke.size * sizeRatio, points: stroke.points.map((point) => rotatePoint(point, 90)) })))
  }

  const apply = async () => {
    if (!image || busy) return
    setBusy(true)
    setError('')
    try {
      onApply(await exportImage(file, image, rotation, crop, strokes))
    } catch (reason) {
      setError(errorMessage(reason, 'Не удалось обработать изображение'))
      setBusy(false)
    }
  }

  return <div className="media-editor-body">
    <div className="media-editor-scroll">
      <div className="media-editor-toolbar" aria-label="Инструменты фото">
        <button type="button" disabled={busy} onClick={() => setMode('crop')} className={mode === 'crop' ? 'active' : ''} aria-pressed={mode === 'crop'}><CropIcon size={18} />Обрезка</button>
        <button type="button" disabled={busy} onClick={() => setMode('draw')} className={mode === 'draw' ? 'active' : ''} aria-pressed={mode === 'draw'}><Brush size={18} />Рисование</button>
        <button type="button" disabled={busy || !image} onClick={rotate}><RotateCw size={18} />Повернуть</button>
      </div>

      {error && <p role="alert" className="mx-4 mb-3 rounded-xl bg-red-400/10 p-3 text-sm text-red-200 sm:mx-5">{error}</p>}
      {!image && !error && <div className="flex min-h-64 items-center justify-center text-sm muted"><LoaderCircle className="mr-2 animate-spin" size={20} />Открываем изображение…</div>}
      {image && size && <div className="media-stage-wrap">
        <div
          ref={stageRef}
          className={`media-editor-stage ${mode === 'draw' ? 'media-stage-draw' : ''}`}
          style={{ aspectRatio: `${size.width} / ${size.height}`, maxWidth: `calc(52dvh * ${size.width / size.height})` }}
          onPointerDown={startDrawing}
          onPointerMove={draw}
          onPointerUp={stopDrawing}
          onPointerCancel={stopDrawing}
        >
          <canvas ref={canvasRef} className="size-full" aria-label="Предпросмотр редактируемого фото" />
          <CropOverlay crop={crop} onChange={setCrop} interactive={mode === 'crop' && !busy} />
        </div>
      </div>}

      {mode === 'draw' && <div className="media-draw-controls">
        <div className="flex items-center gap-2" aria-label="Цвет кисти">
          {colors.map((item) => <button key={item} type="button" aria-label={`Цвет ${item}`} aria-pressed={color === item} onClick={() => setColor(item)} className={`media-color ${color === item ? 'active' : ''}`} style={{ background: item }} />)}
          <input type="color" value={color} onChange={(event) => setColor(event.target.value)} aria-label="Свой цвет" className="size-9 cursor-pointer bg-transparent" />
        </div>
        <label className="flex min-w-40 flex-1 items-center gap-3 text-xs muted">Толщина
          <input type="range" min="2" max="24" step="1" value={brushSize} onChange={(event) => setBrushSize(Number(event.target.value))} className="min-w-0 flex-1" />
          <span className="w-5 text-white">{brushSize}</span>
        </label>
        <div className="flex gap-1">
          <button type="button" disabled={!strokes.length} onClick={() => setStrokes((current) => current.slice(0, -1))} className="media-small-button" aria-label="Отменить последний штрих"><Undo2 size={18} /></button>
          <button type="button" disabled={!strokes.length} onClick={() => setStrokes([])} className="media-small-button text-red-300" aria-label="Удалить все штрихи"><Trash2 size={18} /></button>
        </div>
      </div>}
      <div className="flex items-center justify-between gap-3 px-4 pb-1 text-xs muted sm:px-5">
        <span>{fileSize(file.size)} · обработка на этом устройстве</span>
        <button type="button" disabled={busy} onClick={() => { setRotation(0); setCrop(FULL_CROP); setStrokes([]); setError('') }} className="min-h-11 underline">Сбросить</button>
      </div>
    </div>
    <footer className="media-editor-footer">
      <button type="button" disabled={busy} onClick={onClose} className="min-h-11 rounded-xl bg-white/10 px-5 text-sm font-semibold">Отмена</button>
      <button type="button" disabled={!image || busy} onClick={() => void apply()} className="accent-bg flex min-h-11 items-center justify-center gap-2 rounded-xl px-5 text-sm font-bold">{busy ? <LoaderCircle className="animate-spin" size={18} /> : <Check size={18} />}{busy ? 'Сохраняем…' : 'Применить'}</button>
    </footer>
  </div>
}
