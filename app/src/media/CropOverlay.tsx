import { useRef } from 'react'
import type { PointerEvent } from 'react'
import { clamp } from './types'
import type { Crop } from './types'

type CropAction = 'move' | 'n' | 's' | 'e' | 'w' | 'ne' | 'nw' | 'se' | 'sw'
type ActiveCrop = { action: CropAction; x: number; y: number; crop: Crop }
const MIN_SIZE = 0.04
const handles: CropAction[] = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w']

export function CropOverlay({ crop, onChange, interactive = true }: {
  crop: Crop
  onChange: (crop: Crop) => void
  interactive?: boolean
}) {
  const rootRef = useRef<HTMLDivElement>(null)
  const activeRef = useRef<ActiveCrop | null>(null)

  const start = (event: PointerEvent<HTMLElement>, action: CropAction) => {
    if (!interactive) return
    event.preventDefault()
    event.stopPropagation()
    event.currentTarget.setPointerCapture(event.pointerId)
    activeRef.current = { action, x: event.clientX, y: event.clientY, crop }
  }

  const move = (event: PointerEvent<HTMLElement>) => {
    const active = activeRef.current
    const bounds = rootRef.current?.getBoundingClientRect()
    if (!active || !bounds?.width || !bounds.height) return
    event.preventDefault()
    const dx = (event.clientX - active.x) / bounds.width
    const dy = (event.clientY - active.y) / bounds.height
    const initial = active.crop
    if (active.action === 'move') {
      onChange({ ...initial, x: clamp(initial.x + dx, 0, 1 - initial.width), y: clamp(initial.y + dy, 0, 1 - initial.height) })
      return
    }

    let left = initial.x
    let top = initial.y
    let right = initial.x + initial.width
    let bottom = initial.y + initial.height
    if (active.action.includes('w')) left = clamp(initial.x + dx, 0, right - MIN_SIZE)
    if (active.action.includes('e')) right = clamp(initial.x + initial.width + dx, left + MIN_SIZE, 1)
    if (active.action.includes('n')) top = clamp(initial.y + dy, 0, bottom - MIN_SIZE)
    if (active.action.includes('s')) bottom = clamp(initial.y + initial.height + dy, top + MIN_SIZE, 1)
    onChange({ x: left, y: top, width: right - left, height: bottom - top })
  }

  const end = (event: PointerEvent<HTMLElement>) => {
    if (!activeRef.current) return
    activeRef.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  return <div ref={rootRef} className={`crop-overlay ${interactive ? '' : 'crop-overlay-passive'}`}>
    <div
      className="crop-window"
      style={{ left: `${crop.x * 100}%`, top: `${crop.y * 100}%`, width: `${crop.width * 100}%`, height: `${crop.height * 100}%` }}
      onPointerDown={(event) => start(event, 'move')}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={end}
    >
      <div className="crop-grid" />
      {interactive && handles.map((handle) => <span
        key={handle}
        className={`crop-handle crop-handle-${handle}`}
        onPointerDown={(event) => start(event, handle)}
        onPointerMove={move}
        onPointerUp={end}
        onPointerCancel={end}
      />)}
    </div>
  </div>
}
