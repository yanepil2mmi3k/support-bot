import { Paperclip } from 'lucide-react'
import { fileSize, mediaTypeForFile } from './mediaType'
import { useObjectUrl } from './useObjectUrl'

export function LocalFilePreview({ file }: { file: File }) {
  const url = useObjectUrl(file)
  const type = mediaTypeForFile(file)
  const caption = <div className="mb-2 flex flex-wrap items-center gap-2 text-xs muted"><span className="break-anywhere">{file.name}</span><span>{fileSize(file.size)}</span></div>
  if (type === 'photo' || type === 'animation') return <div>{caption}{url && <img src={url} alt={file.name} className="max-h-64 max-w-full rounded-xl object-contain" />}</div>
  if (type === 'video') return <div>{caption}{url && <video src={url} controls playsInline preload="metadata" className="max-h-64 w-full rounded-xl bg-black" />}</div>
  if (type === 'audio') return <div>{caption}{url && <audio src={url} controls preload="metadata" className="w-full" />}</div>
  return <div className="flex items-center gap-2 rounded-xl bg-black/15 p-3 text-sm"><Paperclip size={18} className="shrink-0 accent" /><span className="min-w-0 break-anywhere">{file.name}</span><span className="shrink-0 text-xs muted">{fileSize(file.size)}</span></div>
}
