export const MAX_PHOTO_SIZE = 10 * 1024 * 1024
export const MAX_VIDEO_SIZE = 50 * 1024 * 1024

export const mediaTypeForFile = (file: File) => file.type.startsWith('image/')
  ? (file.type === 'image/gif' ? 'animation' : 'photo')
  : file.type.startsWith('video/')
    ? 'video'
    : file.type.startsWith('audio/')
      ? 'audio'
      : 'document'

export const isEditableImage = (file: File) => file.type.startsWith('image/') && file.type !== 'image/gif'
export const isEditableVideo = (file: File) => file.type.startsWith('video/')
export const isEditableMedia = (file: File) => isEditableImage(file) || isEditableVideo(file)

export const fileSize = (value: number | null) => {
  if (value === null) return ''
  if (value < 1024) return `${value} Б`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} КБ`
  return `${(value / (1024 * 1024)).toFixed(1)} МБ`
}

export function editedFileName(originalName: string, extension: string) {
  const base = originalName.replace(/\.[^.]+$/, '') || 'media'
  return `${base}-edited.${extension}`
}

export const errorMessage = (reason: unknown, fallback: string) => reason instanceof Error && reason.message
  ? reason.message
  : fallback
