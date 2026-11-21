import { useEffect } from 'react'
import { createPortal } from 'react-dom'
import { X } from 'lucide-react'
import { isEditableImage, isEditableVideo } from './mediaType'
import { ImageEditor } from './ImageEditor'
import { VideoEditor } from './VideoEditor'

export function MediaEditorDialog({ file, onApply, onClose }: {
  file: File
  onApply: (file: File) => void
  onClose: () => void
}) {
  useEffect(() => {
    const close = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      event.preventDefault()
      event.stopImmediatePropagation()
      onClose()
    }
    window.addEventListener('keydown', close, true)
    return () => window.removeEventListener('keydown', close, true)
  }, [onClose])

  const image = isEditableImage(file)
  const video = isEditableVideo(file)
  return createPortal(<div className="media-editor-backdrop" role="presentation" onMouseDown={onClose}>
    <section role="dialog" aria-modal="true" aria-label={image ? 'Редактор фото' : 'Редактор видео'} className="media-editor-dialog panel" onMouseDown={(event) => event.stopPropagation()}>
      <header className="flex shrink-0 items-center justify-between gap-4 border-b border-white/10 px-4 py-3 sm:px-5">
        <div className="min-w-0">
          <h2 className="text-lg font-bold">{image ? 'Редактор фото' : 'Редактор видео'}</h2>
          <p className="truncate text-xs muted">{file.name}</p>
        </div>
        <button type="button" onClick={onClose} className="flex size-11 shrink-0 items-center justify-center rounded-lg muted hover:bg-white/10 hover:text-white" aria-label="Закрыть редактор"><X size={20} /></button>
      </header>
      {image && <ImageEditor file={file} onApply={onApply} onClose={onClose} />}
      {video && <VideoEditor file={file} onApply={onApply} onClose={onClose} />}
      {!image && !video && <div className="p-5"><p className="text-sm muted">Этот формат нельзя редактировать. Закройте редактор, чтобы отправить оригинал.</p></div>}
    </section>
  </div>, document.body)
}
