export type Crop = {
  x: number
  y: number
  width: number
  height: number
}

export type Point = {
  x: number
  y: number
}

export type BrushStroke = {
  color: string
  size: number
  points: Point[]
}

export type Rotation = 0 | 90 | 180 | 270

export type VideoEdit = {
  start: number
  end: number
  crop: Crop
}

export const FULL_CROP: Crop = { x: 0, y: 0, width: 1, height: 1 }

export const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value))

export function rotatePoint(point: Point, rotation: Rotation): Point {
  if (rotation === 90) return { x: 1 - point.y, y: point.x }
  if (rotation === 180) return { x: 1 - point.x, y: 1 - point.y }
  if (rotation === 270) return { x: point.y, y: 1 - point.x }
  return point
}

export function rotateCrop(crop: Crop, rotation: Rotation): Crop {
  if (rotation === 90) return { x: 1 - crop.y - crop.height, y: crop.x, width: crop.height, height: crop.width }
  if (rotation === 180) return { x: 1 - crop.x - crop.width, y: 1 - crop.y - crop.height, width: crop.width, height: crop.height }
  if (rotation === 270) return { x: crop.y, y: 1 - crop.x - crop.width, width: crop.height, height: crop.width }
  return crop
}
