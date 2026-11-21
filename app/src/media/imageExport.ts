import { editedFileName, MAX_PHOTO_SIZE } from './mediaType'
import type { BrushStroke, Crop, Rotation } from './types'

export function loadImage(file: File) {
  return new Promise<HTMLImageElement>((resolve, reject) => {
    const url = URL.createObjectURL(file)
    const image = new Image()
    image.onload = () => {
      URL.revokeObjectURL(url)
      resolve(image)
    }
    image.onerror = () => {
      URL.revokeObjectURL(url)
      reject(new Error('Не удалось прочитать изображение'))
    }
    image.src = url
  })
}

export function rotatedSize(image: HTMLImageElement, rotation: Rotation) {
  return rotation === 90 || rotation === 270
    ? { width: image.naturalHeight, height: image.naturalWidth }
    : { width: image.naturalWidth, height: image.naturalHeight }
}

export function renderRotatedImage(image: HTMLImageElement, rotation: Rotation, strokes: BrushStroke[], maxDimension = Number.POSITIVE_INFINITY) {
  const size = rotatedSize(image, rotation)
  const scale = Math.min(1, maxDimension / Math.max(size.width, size.height))
  const canvas = document.createElement('canvas')
  canvas.width = Math.max(1, Math.round(size.width * scale))
  canvas.height = Math.max(1, Math.round(size.height * scale))
  const context = canvas.getContext('2d')
  if (!context) throw new Error('Браузер не поддерживает обработку изображений')

  context.save()
  context.scale(scale, scale)
  if (rotation === 90) {
    context.translate(size.width, 0)
    context.rotate(Math.PI / 2)
  } else if (rotation === 180) {
    context.translate(size.width, size.height)
    context.rotate(Math.PI)
  } else if (rotation === 270) {
    context.translate(0, size.height)
    context.rotate(-Math.PI / 2)
  }
  context.drawImage(image, 0, 0)
  context.restore()

  context.lineCap = 'round'
  context.lineJoin = 'round'
  strokes.forEach((stroke) => {
    if (!stroke.points.length) return
    context.strokeStyle = stroke.color
    context.lineWidth = Math.max(1, stroke.size * canvas.width)
    context.beginPath()
    const first = stroke.points[0]
    context.moveTo(first.x * canvas.width, first.y * canvas.height)
    if (stroke.points.length === 1) context.lineTo(first.x * canvas.width + 0.01, first.y * canvas.height + 0.01)
    stroke.points.slice(1).forEach((point) => context.lineTo(point.x * canvas.width, point.y * canvas.height))
    context.stroke()
  })
  return canvas
}

function canvasToBlob(canvas: HTMLCanvasElement, type: string, quality?: number) {
  return new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error('Не удалось сохранить изображение')), type, quality)
  })
}

export async function exportImage(file: File, image: HTMLImageElement, rotation: Rotation, crop: Crop, strokes: BrushStroke[]) {
  const source = renderRotatedImage(image, rotation, strokes)
  const left = Math.max(0, Math.round(crop.x * source.width))
  const top = Math.max(0, Math.round(crop.y * source.height))
  const width = Math.max(1, Math.min(source.width - left, Math.round(crop.width * source.width)))
  const height = Math.max(1, Math.min(source.height - top, Math.round(crop.height * source.height)))
  const ratio = Math.max(width / height, height / width)
  if (ratio > 20) throw new Error('Соотношение сторон после обрезки не должно превышать 20:1')

  const scale = Math.min(1, 10000 / (width + height))
  const output = document.createElement('canvas')
  output.width = Math.max(1, Math.floor(width * scale))
  output.height = Math.max(1, Math.floor(height * scale))
  const context = output.getContext('2d')
  if (!context) throw new Error('Браузер не поддерживает обработку изображений')
  const jpeg = file.type === 'image/jpeg'
  if (jpeg) {
    context.fillStyle = '#ffffff'
    context.fillRect(0, 0, output.width, output.height)
  }
  context.drawImage(source, left, top, width, height, 0, 0, output.width, output.height)

  const blob = await canvasToBlob(output, jpeg ? 'image/jpeg' : 'image/png', jpeg ? 0.92 : undefined)
  if (!blob.size) throw new Error('Получилось пустое изображение')
  if (blob.size > MAX_PHOTO_SIZE) throw new Error('Изображение после обработки больше 10 МБ. Уменьшите область обрезки.')
  return new File([blob], editedFileName(file.name, jpeg ? 'jpg' : 'png'), { type: blob.type, lastModified: Date.now() })
}
