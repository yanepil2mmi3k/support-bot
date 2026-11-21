import { editedFileName, MAX_VIDEO_SIZE } from './mediaType'
import { clamp } from './types'
import type { VideoEdit } from './types'

function abortError() {
  return new DOMException('Обработка отменена', 'AbortError')
}

function checkAborted(signal: AbortSignal) {
  if (signal.aborted) throw abortError()
}

async function validateVideo(blob: Blob) {
  const url = URL.createObjectURL(blob)
  const video = document.createElement('video')
  try {
    await new Promise<void>((resolve, reject) => {
      const timeout = window.setTimeout(() => reject(new Error('Не удалось проверить готовое видео')), 15000)
      video.onloadedmetadata = () => {
        window.clearTimeout(timeout)
        if (video.videoWidth > 0 && video.videoHeight > 0 && Number.isFinite(video.duration) && video.duration > 0) resolve()
        else reject(new Error('Готовое видео имеет неверные метаданные'))
      }
      video.onerror = () => {
        window.clearTimeout(timeout)
        reject(new Error('Браузер не может воспроизвести готовое видео'))
      }
      video.preload = 'metadata'
      video.src = url
    })
  } finally {
    video.removeAttribute('src')
    video.load()
    URL.revokeObjectURL(url)
  }
}

export async function exportVideo(file: File, edit: VideoEdit, signal: AbortSignal, onProgress: (progress: number) => void) {
  checkAborted(signal)
  const {
    ALL_FORMATS, BlobSource, BufferTarget, Conversion, Input, Mp4OutputFormat, Output,
    canEncodeAudio, canEncodeVideo,
  } = await import('mediabunny')
  checkAborted(signal)

  const input = new Input({ source: new BlobSource(file), formats: ALL_FORMATS })
  let conversion: Awaited<ReturnType<typeof Conversion.init>> | null = null
  const cancel = () => { if (conversion) void conversion.cancel() }
  signal.addEventListener('abort', cancel, { once: true })
  try {
    if (!await input.canRead()) throw new Error('Этот формат видео не поддерживается')
    checkAborted(signal)
    const videoTrack = await input.getPrimaryVideoTrack()
    if (!videoTrack) throw new Error('В файле нет видеодорожки')
    if (!await videoTrack.canDecode()) throw new Error('Этот видеокодек не поддерживается на вашем устройстве')
    const audioTrack = await input.getPrimaryAudioTrack()
    if (audioTrack && !await audioTrack.canDecode()) throw new Error('Аудиокодек не поддерживается. Отправьте оригинал или выберите другое видео.')

    const displayWidth = await videoTrack.getDisplayWidth()
    const displayHeight = await videoTrack.getDisplayHeight()
    const evenWidth = Math.max(2, Math.floor(displayWidth / 2) * 2)
    const evenHeight = Math.max(2, Math.floor(displayHeight / 2) * 2)
    const left = clamp(Math.floor(edit.crop.x * displayWidth / 2) * 2, 0, evenWidth - 2)
    const top = clamp(Math.floor(edit.crop.y * displayHeight / 2) * 2, 0, evenHeight - 2)
    const right = clamp(Math.floor((edit.crop.x + edit.crop.width) * displayWidth / 2) * 2, left + 2, evenWidth)
    const bottom = clamp(Math.floor((edit.crop.y + edit.crop.height) * displayHeight / 2) * 2, top + 2, evenHeight)
    const width = right - left
    const height = bottom - top
    if (!await canEncodeVideo('avc', { width, height })) throw new Error('На этом устройстве нельзя создать MP4 H.264. Отправьте оригинал или откройте миниапп в другом браузере.')
    if (audioTrack && !await canEncodeAudio('aac')) {
      const { registerAacEncoder } = await import('@mediabunny/aac-encoder')
      registerAacEncoder()
    }
    checkAborted(signal)

    const target = new BufferTarget()
    const output = new Output({ format: new Mp4OutputFormat({ fastStart: 'in-memory' }), target })
    conversion = await Conversion.init({
      input,
      output,
      tracks: 'primary',
      trim: { start: edit.start, end: edit.end },
      video: {
        crop: { left, top, width, height },
        codec: 'avc',
        forceTranscode: true,
        allowTransformationMetadata: false,
      },
      audio: { codec: 'aac', forceTranscode: true },
    })
    if (!conversion.isValid) throw new Error('Не удалось подготовить видео. Проверьте формат и кодеки исходного файла.')
    if (!conversion.utilizedTracks.some((track) => track.id === videoTrack.id)) throw new Error('Видеодорожку не удалось сохранить')
    if (audioTrack && !conversion.utilizedTracks.some((track) => track.id === audioTrack.id)) throw new Error('Звук не удалось сохранить. Отправьте оригинал или выберите другое видео.')

    conversion.onProgress = onProgress
    await conversion.execute()
    checkAborted(signal)
    const buffer = target.buffer
    if (!buffer?.byteLength) throw new Error('Получилось пустое видео')
    if (buffer.byteLength > MAX_VIDEO_SIZE) throw new Error('Видео после обработки больше 50 МБ. Сократите фрагмент или уменьшите область кадра.')
    const blob = new Blob([buffer], { type: 'video/mp4' })
    await validateVideo(blob)
    checkAborted(signal)
    return new File([blob], editedFileName(file.name, 'mp4'), { type: 'video/mp4', lastModified: Date.now() })
  } finally {
    signal.removeEventListener('abort', cancel)
    input.dispose()
  }
}
