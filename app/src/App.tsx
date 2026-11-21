import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent as ReactKeyboardEvent } from 'react'
import {
  Archive, ArrowDown, ArrowLeft, ArrowUp, BellRing, Check, CircleHelp, Clock3, Download,
  FilePenLine, Headphones, Inbox, LoaderCircle, MessageSquareText, MoreHorizontal, Paperclip,
  Pencil, Pin, Play, Plus, RefreshCw, RotateCcw, Search, Send, Star, Trash2, UserRound,
  Users, Volume2, VolumeX, X,
} from 'lucide-react'
import './App.css'
import { api, apiBlob, initTelegram } from './api'
import { LocalFilePreview } from './media/LocalFilePreview'
import { MediaEditorDialog } from './media/MediaEditorDialog'
import { fileSize, isEditableMedia, mediaTypeForFile } from './media/mediaType'
import type { Bootstrap, ChatMessage, Employee, FaqItem, Profile, QuickReply, QuickReplyItem, Status, Ticket } from './types'

const time = (value: string) => new Intl.DateTimeFormat('ru', { hour: '2-digit', minute: '2-digit' }).format(new Date(value))
const date = (value: string) => new Intl.DateTimeFormat('ru', { day: '2-digit', month: 'short', year: 'numeric' }).format(new Date(value))
const dateTime = (value: string) => new Intl.DateTimeFormat('ru', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }).format(new Date(value))
const isStatusClosed = (status?: Status) => Boolean(status?.closed || status?.archive)

function Modal({ title, onClose, children, wide = false }: { title: string; onClose: () => void; children: React.ReactNode; wide?: boolean }) {
  useEffect(() => {
    const close = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [onClose])
  return <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/60 p-0 sm:items-center sm:p-6" onMouseDown={onClose}>
    <section role="dialog" aria-modal="true" aria-label={title} className={`modal-panel panel max-h-[92dvh] w-full overflow-y-auto overflow-x-hidden rounded-t-2xl border p-5 shadow-2xl sm:rounded-2xl ${wide ? 'max-w-3xl' : 'max-w-xl'}`} onMouseDown={(event) => event.stopPropagation()}>
      <div className="mb-5 flex items-center justify-between gap-4">
        <h2 className="min-w-0 break-anywhere text-xl font-bold">{title}</h2>
        <button className="flex size-11 shrink-0 items-center justify-center rounded-lg muted transition hover:bg-white/5 hover:text-white" onClick={onClose} aria-label="Закрыть"><X size={20} /></button>
      </div>
      {children}
    </section>
  </div>
}

function StatusPill({ ticket, data }: { ticket: Ticket; data: Bootstrap }) {
  const status = data.statuses.find((item) => item.key === ticket.status)
  const closed = isStatusClosed(status)
  return <span className={`max-w-full truncate rounded-full px-2.5 py-1 text-xs font-semibold ${closed ? 'soft-neutral' : ticket.status === 'new' ? 'soft-warning' : 'soft-accent'}`}>{status?.title || ticket.status}</span>
}

function UnreadBadge({ count, label }: { count: number; label: string }) {
  if (count === 0) return null
  return <span className="accent-bg inline-flex h-5 min-w-5 shrink-0 items-center justify-center rounded-full px-1 text-[11px] font-extrabold leading-none" aria-label={`${label}: ${count}`}>{count > 99 ? '99+' : count}</span>
}

const mediaCache = new Map<string, Promise<Blob>>()

function getMedia(path: string) {
  const cached = mediaCache.get(path)
  if (cached) return cached
  const request = apiBlob(path).catch((reason) => {
    mediaCache.delete(path)
    throw reason
  })
  mediaCache.set(path, request)
  return request
}

function MediaAttachment({ mediaUrl, mediaType, fileName, mimeType, size, label, legacy = false }: {
  mediaUrl: string | null
  mediaType: string | null
  fileName?: string | null
  mimeType?: string | null
  size?: number | null
  label?: string | null
  legacy?: boolean
}) {
  const [loaded, setLoaded] = useState<{ key: string; url: string } | null>(null)
  const [loadError, setLoadError] = useState<{ key: string; message: string } | null>(null)
  const type = mediaType || 'document'
  const isTgs = type === 'sticker' && (mimeType === 'application/x-tgsticker' || fileName?.toLowerCase().endsWith('.tgs') || mediaUrl?.toLowerCase().endsWith('.tgs'))
  const source = loaded && loaded.key === mediaUrl ? loaded.url : ''
  const error = loadError && loadError.key === mediaUrl ? loadError.message : ''

  useEffect(() => {
    let active = true
    let objectUrl = ''
    if (!mediaUrl || isTgs) return () => { active = false }
    void getMedia(mediaUrl).then((blob) => {
      if (!active) return
      objectUrl = URL.createObjectURL(blob)
      setLoaded({ key: mediaUrl, url: objectUrl })
    }).catch((reason) => {
      if (active) setLoadError({ key: mediaUrl, message: reason instanceof Error ? reason.message : 'Не удалось загрузить медиа' })
    })
    return () => {
      active = false
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [mediaUrl, isTgs])

  const caption = <div className="mb-2 flex flex-wrap items-center gap-2 text-xs muted"><span>{label || fileName || 'Вложение'}</span>{size !== undefined && size !== null && <span>{fileSize(size)}</span>}</div>
  if (!mediaUrl) {
    if (type === 'text' && !label) return null
    return <div className="mt-2 text-xs italic muted">{label || 'Медиа недоступно'}{legacy ? ' (старое сообщение)' : ''}</div>
  }
  if (isTgs) return <div className="mt-2 rounded-xl bg-black/15 p-3 text-xs leading-5 muted">{label || 'Анимированный стикер'}: формат TGS не поддерживается для предпросмотра.</div>
  if (error) return <div className="mt-2 rounded-xl bg-red-400/10 p-3 text-xs text-red-200">{error}</div>
  if (!source) return <div className="mt-2 flex min-h-20 items-center justify-center rounded-xl bg-black/15 text-xs muted"><LoaderCircle className="mr-2 animate-spin" size={17} />Загружаем медиа…</div>

  if (type === 'photo' || (type === 'sticker' && (mimeType === 'image/webp' || fileName?.toLowerCase().endsWith('.webp') || mediaUrl.toLowerCase().endsWith('.webp')))) return <div className="mt-2">{caption}<img src={source} alt={label || fileName || 'Изображение'} loading="lazy" className="max-h-96 max-w-full rounded-xl object-contain" /></div>
  if (['video', 'animation', 'video_note'].includes(type)) return <div className="mt-2">{caption}<video src={source} controls playsInline preload="metadata" className="max-h-96 w-full rounded-xl bg-black" /></div>
  if (type === 'audio' || type === 'voice') return <div className="mt-2 min-w-0">{caption}<audio src={source} controls preload="metadata" className="w-full" /></div>
  return <div className="mt-2"><a href={source} download={fileName || 'attachment'} className="flex min-h-11 max-w-full items-center gap-3 rounded-xl bg-black/15 px-3 py-2 text-sm transition hover:bg-black/25"><Download className="shrink-0 accent" size={19} /><span className="min-w-0 break-anywhere">{fileName || label || 'Скачать документ'}{size !== undefined && size !== null && <span className="ml-2 text-xs muted">{fileSize(size)}</span>}</span></a></div>
}

function statusExplanation(status: Status) {
  if (status.description) return status.description
  if (status.archive) return 'Обращение закрыто и находится в архиве. Переписка недоступна до переоткрытия.'
  if (status.closed) return 'Обращение закрыто. Переписка недоступна до переоткрытия.'
  if (status.key === 'new') return 'Новое обращение ожидает назначения ответственного.'
  return 'Обращение открыто, переписка с клиентом доступна.'
}

function TicketDetails({ ticket, data, onUpdate, onClaim, onPin, onReminder, busy }: {
  ticket: Ticket
  data: Bootstrap
  busy: boolean
  onUpdate: (payload: Record<string, unknown>, success?: string) => Promise<void>
  onClaim: () => Promise<void>
  onPin: () => Promise<void>
  onReminder: (minutes: number | null) => Promise<void>
}) {
  const [note, setNote] = useState(ticket.note)
  const [title, setTitle] = useState(ticket.title || '')
  const [statusQuery, setStatusQuery] = useState('')
  const [employeeQuery, setEmployeeQuery] = useState('')
  const [customMinutes, setCustomMinutes] = useState('60')
  const [reminderAt, setReminderAt] = useState('')
  const status = data.statuses.find((item) => item.key === ticket.status)
  const closed = isStatusClosed(status)
  const workStatus = data.statuses.find((item) => item.key === 'work') || data.statuses.find((item) => !isStatusClosed(item) && item.key !== 'new')
  const archiveStatus = data.statuses.find((item) => item.archive) || data.statuses.find((item) => item.closed)
  const reopenStatus = workStatus || data.statuses.find((item) => !isStatusClosed(item))
  const statuses = data.statuses.filter((item) => `${item.title} ${item.key}`.toLowerCase().includes(statusQuery.trim().toLowerCase()))
  const employees = data.employees.filter((employee) => employee.active !== false && `${employee.nickname} ${employee.name}`.toLowerCase().includes(employeeQuery.trim().toLowerCase()))
  const minutes = Number(customMinutes)
  const validMinutes = Number.isInteger(minutes) && minutes >= 1 && minutes <= 10080

  const setAbsoluteReminder = () => {
    const absoluteMinutes = Math.ceil((new Date(reminderAt).getTime() - Date.now()) / 60000)
    if (absoluteMinutes >= 1 && absoluteMinutes <= 10080) void onReminder(absoluteMinutes)
  }

  const updateStatus = async (next: Status) => {
    if (next.key === ticket.status) return
    if (next.archive && !window.confirm(`Закрыть и архивировать обращение #${ticket.number}?`)) return
    await onUpdate({ status: next.key }, `Статус изменён: ${next.title}`)
  }

  return <div className="min-w-0 space-y-6">
    <div>
      <div className="mb-2 flex min-w-0 items-center gap-2"><StatusPill ticket={ticket} data={data} /><span className="shrink-0 text-sm muted">#{ticket.number}</span></div>
      <h2 className="break-anywhere text-xl font-bold">{ticket.client}</h2>
      <p className="mt-1 break-anywhere text-sm muted">{ticket.username ? `@${ticket.username}` : 'Username не указан'} · ID {ticket.user_id ?? 'не указан'}</p>
    </div>

    <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-1 2xl:grid-cols-2">
      {!ticket.employee_id && <button disabled={busy} onClick={() => void onClaim()} className="accent-bg flex min-h-11 items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold transition hover:brightness-110 sm:col-span-2 xl:col-span-1 2xl:col-span-2"><Check size={18} />Принять обращение</button>}
      {ticket.employee_id && !closed && workStatus && ticket.status !== workStatus.key && <button disabled={busy} onClick={() => void updateStatus(workStatus)} className="accent-bg flex min-h-11 items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold transition hover:brightness-110"><Play size={17} />В работу</button>}
      {ticket.employee_id && !closed && archiveStatus && <button disabled={busy} onClick={() => void updateStatus(archiveStatus)} className="flex min-h-11 items-center justify-center gap-2 rounded-xl bg-red-400/10 px-4 text-sm font-semibold text-red-200 transition hover:bg-red-400/15"><Archive size={17} />Закрыть</button>}
      {ticket.employee_id && closed && reopenStatus && <button disabled={busy} onClick={() => void updateStatus(reopenStatus)} className="accent-bg flex min-h-11 items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold transition hover:brightness-110"><RotateCcw size={17} />Переоткрыть</button>}
      <button disabled={busy || !ticket.thread_id} onClick={() => void onPin()} className="flex min-h-11 items-center justify-center gap-2 rounded-xl bg-white/10 px-4 text-sm font-semibold transition hover:bg-white/15"><Pin size={17} />Обновить закреп</button>
    </div>

    <dl className="grid grid-cols-2 gap-4 border-y border-white/10 py-4 text-sm">
      <div className="min-w-0"><dt className="muted">Категория</dt><dd className="mt-1 break-anywhere font-medium">{ticket.category}</dd></div>
      <div className="min-w-0"><dt className="muted">Ответственный</dt><dd className="mt-1 break-anywhere font-medium">{ticket.employee || 'Не назначен'}</dd></div>
      <div><dt className="muted">Создано</dt><dd className="mt-1 font-medium">{date(ticket.created_at)}</dd></div>
      <div><dt className="muted">Обновлено</dt><dd className="mt-1 font-medium">{time(ticket.updated_at)}</dd></div>
    </dl>

    <div>
      <h3 className="mb-2 text-sm font-bold">Первичное обращение</h3>
      <p className="break-anywhere whitespace-pre-wrap text-sm leading-6 muted">{ticket.description}</p>
      {(ticket.media_url || ticket.media_type) && <MediaAttachment mediaUrl={ticket.media_url} mediaType={ticket.media_type} label="Вложение к первичному обращению" />}
    </div>

    <div>
      <label htmlFor={`ticket-title-${ticket.number}`} className="text-sm font-bold">Своё название</label>
      <input id={`ticket-title-${ticket.number}`} value={title} disabled={busy} maxLength={128} onChange={(event) => setTitle(event.target.value)} placeholder="Короткое название обращения" className="panel-deep mt-2 min-h-11 w-full rounded-xl border px-3 text-base placeholder:text-slate-500 sm:text-sm" />
      <button disabled={busy || !title.trim() || title.trim() === (ticket.title || '')} onClick={() => void onUpdate({ title: title.trim() }, 'Название сохранено')} className="mt-2 min-h-11 w-full rounded-xl bg-white/10 px-4 text-sm font-semibold transition hover:bg-white/15">Сохранить название</button>
    </div>

    <div>
      <label htmlFor={`ticket-note-${ticket.number}`} className="text-sm font-bold">Рабочее описание</label>
      <textarea id={`ticket-note-${ticket.number}`} value={note} disabled={busy} onChange={(event) => setNote(event.target.value)} maxLength={3000} rows={5} placeholder="Контекст для команды, договорённости, следующий шаг…" className="panel-deep mt-2 w-full resize-none rounded-xl border p-3 text-base leading-6 placeholder:text-slate-500 sm:text-sm" />
      <button disabled={busy || note === ticket.note} onClick={() => void onUpdate({ note }, 'Описание сохранено')} className="mt-2 min-h-11 w-full rounded-xl bg-white/10 px-4 text-sm font-semibold transition hover:bg-white/15">Сохранить описание</button>
    </div>

    <section aria-labelledby={`assignee-${ticket.number}`}>
      <h3 id={`assignee-${ticket.number}`} className="text-sm font-bold">Ответственный и передача</h3>
      <p className="mt-1 text-sm muted">{ticket.employee || 'Сначала примите обращение'}</p>
      <label className="relative mt-3 block"><Search className="pointer-events-none absolute left-3 top-3.5 muted" size={17} /><input value={employeeQuery} disabled={busy || !ticket.employee_id} onChange={(event) => setEmployeeQuery(event.target.value)} placeholder="Найти активного сотрудника" aria-label="Поиск сотрудника" className="panel-deep min-h-11 w-full rounded-xl border pl-10 pr-3 text-base placeholder:text-slate-500 sm:text-sm" /></label>
      <div className="scrollbar mt-2 max-h-48 space-y-1 overflow-y-auto">
        {employees.map((employee) => <button key={employee.id} disabled={busy || !ticket.employee_id} onClick={async () => {
          if (employee.id === ticket.employee_id) return
          if (!window.confirm(`Передать обращение #${ticket.number} сотруднику ${employee.nickname}?`)) return
          await onUpdate({ employee_id: employee.id }, `Обращение передано: ${employee.nickname}`)
        }} className={`flex min-h-11 w-full items-center justify-between gap-3 rounded-lg px-3 text-left text-sm transition ${employee.id === ticket.employee_id ? 'soft-accent' : 'hover:bg-white/5'}`}><span className="min-w-0 break-anywhere font-medium">{employee.nickname}<span className="ml-2 font-normal muted">{employee.name}</span></span>{employee.id === ticket.employee_id && <Check size={17} className="shrink-0" />}</button>)}
        {employees.length === 0 && <p className="p-3 text-sm muted">Активные сотрудники не найдены.</p>}
      </div>
    </section>

    <section aria-labelledby={`statuses-${ticket.number}`}>
      <h3 id={`statuses-${ticket.number}`} className="text-sm font-bold">Все статусы</h3>
      <label className="relative mt-2 block"><Search className="pointer-events-none absolute left-3 top-3.5 muted" size={17} /><input value={statusQuery} disabled={busy} onChange={(event) => setStatusQuery(event.target.value)} placeholder="Поиск по названию или key" aria-label="Поиск статуса" className="panel-deep min-h-11 w-full rounded-xl border pl-10 pr-3 text-base placeholder:text-slate-500 sm:text-sm" /></label>
      <div className="scrollbar mt-2 max-h-56 space-y-1 overflow-y-auto">
        {statuses.map((item) => <button key={item.key} disabled={busy || !ticket.employee_id} onClick={() => void updateStatus(item)} aria-pressed={item.key === ticket.status} className={`min-h-11 w-full rounded-lg px-3 py-2 text-left transition ${item.key === ticket.status ? 'soft-accent' : 'hover:bg-white/5'}`}><span className="block break-anywhere text-sm font-semibold">{item.title}</span><span className="text-xs muted">{item.key}</span></button>)}
        {statuses.length === 0 && <p className="p-3 text-sm muted">Статусы не найдены.</p>}
      </div>
      {status && <div className="mt-3 rounded-xl bg-white/5 p-3"><p className="text-sm leading-6 muted">{statusExplanation(status)}</p><div className="mt-2 flex flex-wrap gap-2">{status.closed && <span className="soft-neutral rounded-full px-2.5 py-1 text-xs font-semibold">Закрытый</span>}{status.archive && <span className="soft-neutral rounded-full px-2.5 py-1 text-xs font-semibold">Архивный</span>}{status.pauses_reminders && <span className="soft-warning rounded-full px-2.5 py-1 text-xs font-semibold">Пауза напоминаний</span>}</div></div>}
    </section>

    <section aria-labelledby={`reminder-${ticket.number}`}>
      <h3 id={`reminder-${ticket.number}`} className="flex items-center gap-2 text-sm font-bold"><BellRing size={17} />Напоминание</h3>
      <p className="mt-2 text-sm leading-6 muted">{ticket.reminder?.active && ticket.reminder.next_run_at ? `Следующее: ${dateTime(ticket.reminder.next_run_at)}` : 'Напоминания выключены.'}</p>
      {status?.pauses_reminders && <p className="soft-warning mt-2 rounded-lg p-3 text-xs leading-5">Текущий статус приостанавливает напоминания.</p>}
      <div className="mt-3 grid grid-cols-3 gap-2">
        {[15, 30, 60, 180, 720, 1440].map((preset) => <button key={preset} disabled={busy || !ticket.employee_id} onClick={() => void onReminder(preset)} className="min-h-11 rounded-lg bg-white/10 px-2 text-xs font-semibold transition hover:bg-white/15">{preset < 60 ? `${preset} мин` : preset < 1440 ? `${preset / 60} ч` : '1 день'}</button>)}
      </div>
      <form className="mt-2 flex gap-2" onSubmit={(event) => { event.preventDefault(); if (validMinutes) void onReminder(minutes) }}>
        <input type="number" min="1" max="10080" step="1" inputMode="numeric" value={customMinutes} disabled={busy || !ticket.employee_id} onChange={(event) => setCustomMinutes(event.target.value)} aria-label="Свой интервал в минутах" className="panel-deep min-h-11 min-w-0 flex-1 rounded-xl border px-3 text-base sm:text-sm" />
        <button type="submit" disabled={busy || !ticket.employee_id || !validMinutes} className="min-h-11 shrink-0 rounded-xl bg-white/10 px-3 text-sm font-semibold hover:bg-white/15">Установить</button>
      </form>
      <form className="mt-2 space-y-2" onSubmit={(event) => { event.preventDefault(); setAbsoluteReminder() }}>
        <label className="block text-xs font-semibold muted">Или конкретные дата и время
          <input type="datetime-local" value={reminderAt} disabled={busy || !ticket.employee_id} onChange={(event) => setReminderAt(event.target.value)} aria-label="Дата и время напоминания" className="panel-deep mt-1 min-h-11 w-full rounded-xl border px-3 text-base sm:text-sm" />
        </label>
        <button type="submit" disabled={busy || !ticket.employee_id || !reminderAt} className="min-h-11 w-full rounded-xl bg-white/10 px-3 text-sm font-semibold hover:bg-white/15">Установить по времени</button>
      </form>
      {ticket.reminder?.active && <button disabled={busy} onClick={() => void onReminder(null)} className="mt-2 min-h-11 w-full rounded-xl bg-white/5 px-4 text-sm font-semibold muted transition hover:bg-white/10 hover:text-white">Выключить напоминание</button>}
    </section>

    {closed && <p className="rounded-xl bg-white/5 p-3 text-sm leading-6 muted">Для продолжения переписки переоткройте обращение или выберите открытый статус.</p>}
  </div>
}

function ProfileContent({ profile, busy, onSave }: { profile: Profile; busy: boolean; onSave: (greeting: string) => Promise<void> }) {
  const [greeting, setGreeting] = useState(profile.greeting)
  return <div className="space-y-5">
    <dl className="grid grid-cols-1 gap-4 rounded-xl bg-white/5 p-4 text-sm sm:grid-cols-2">
      <div><dt className="muted">Имя</dt><dd className="mt-1 break-anywhere font-semibold">{profile.name || 'Не указано'}</dd></div>
      <div><dt className="muted">Никнейм</dt><dd className="mt-1 break-anywhere font-semibold">{profile.nickname || 'Не указан'}</dd></div>
      <div><dt className="muted">Пол</dt><dd className="mt-1 break-anywhere font-semibold">{profile.gender === 'f' ? 'Женский' : profile.gender === 'm' ? 'Мужской' : 'Не указан'}</dd></div>
      <div><dt className="muted">График</dt><dd className="mt-1 break-anywhere font-semibold">{profile.schedule || 'Не указан'}</dd></div>
    </dl>
    <div>
      <label htmlFor="profile-greeting" className="text-sm font-bold">Приветствие клиента</label>
      <textarea id="profile-greeting" value={greeting} disabled={busy} onChange={(event) => setGreeting(event.target.value)} rows={5} maxLength={2000} className="panel-deep mt-2 w-full resize-y rounded-xl border p-3 text-base leading-6 sm:text-sm" />
      <button disabled={busy || greeting === profile.greeting} onClick={() => void onSave(greeting)} className="accent-bg mt-2 flex min-h-11 w-full items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold"><Check size={17} />Сохранить приветствие</button>
    </div>
    {profile.ratings_visible && <section>
      <h3 className="mb-3 text-sm font-bold">Оценки и отзывы</h3>
      {profile.reviews.length === 0 ? <p className="rounded-xl bg-white/5 p-4 text-sm muted">Отзывов пока нет.</p> : <div className="space-y-3">{profile.reviews.map((review) => <article key={`${review.ticket_number}-${review.created_at}`} className="rounded-xl border border-white/10 p-4"><div className="flex flex-wrap items-center justify-between gap-2"><span className="font-semibold">Обращение #{review.ticket_number}</span><span className="flex items-center gap-1 text-sm font-bold text-amber-200"><Star size={16} fill="currentColor" />{review.rating}/5</span></div><div className="mt-1 text-xs muted">{date(review.created_at)}</div>{review.comment && <p className="mt-3 break-anywhere whitespace-pre-wrap text-sm leading-6">{review.comment}</p>}</article>)}</div>}
    </section>}
  </div>
}

function FaqEditor({ initial, onSave, busy }: { initial: FaqItem[]; onSave: (items: FaqItem[]) => Promise<void>; busy: boolean }) {
  const [items, setItems] = useState(initial)
  const update = (index: number, field: keyof FaqItem, value: string) => setItems((current) => current.map((item, i) => i === index ? { ...item, [field]: value } : item))
  return <div className="space-y-5">
    <p className="text-sm leading-6 muted">Эти вопросы клиент видит после `/start` и после закрытия обращения. Изменения сразу попадут в клиентского бота.</p>
    {items.map((item, index) => <div key={`${item.id}-${index}`} className="rounded-xl border border-white/10 p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <span className="text-xs font-bold uppercase tracking-wider muted">Ответ {index + 1}</span>
        <button disabled={items.length === 1} onClick={() => setItems((current) => current.filter((_, i) => i !== index))} className="flex size-11 items-center justify-center rounded-lg text-red-300 transition hover:bg-red-400/10" aria-label={`Удалить ответ ${index + 1}`}><Trash2 size={17} /></button>
      </div>
      <label className="block text-sm font-semibold">Вопрос
        <input value={item.question} maxLength={200} onChange={(event) => update(index, 'question', event.target.value)} className="panel-deep mt-2 min-h-11 w-full rounded-xl border px-3 text-base sm:text-sm" />
      </label>
      <label className="mt-3 block text-sm font-semibold">Ответ
        <textarea value={item.answer} maxLength={4000} onChange={(event) => update(index, 'answer', event.target.value)} rows={4} className="panel-deep mt-2 w-full resize-y rounded-xl border p-3 text-base leading-6 sm:text-sm" />
      </label>
    </div>)}
    <div className="flex flex-col-reverse gap-3 sm:flex-row sm:justify-between">
      <button disabled={items.length >= 30} onClick={() => setItems((current) => [...current, { id: `faq_${Date.now()}`, question: '', answer: '' }])} className="flex min-h-11 items-center justify-center gap-2 rounded-xl bg-white/10 px-4 text-sm font-semibold hover:bg-white/15"><Plus size={18} />Добавить ответ</button>
      <button disabled={busy || items.some((item) => !item.question.trim() || !item.answer.trim())} onClick={() => onSave(items)} className="accent-bg flex min-h-11 items-center justify-center gap-2 rounded-xl px-5 text-sm font-bold hover:brightness-110"><Check size={18} />Сохранить изменения</button>
    </div>
  </div>
}

const orderedQuickReplyItems = (reply: QuickReply) => [...reply.items].sort((a, b) => a.position - b.position)
const quickReplyText = (reply: QuickReply) => reply.text

type QuickReplyDraftFile = { key: string; file: File }
let nextQuickReplyFileId = 0
const quickReplyDraftFile = (file: File): QuickReplyDraftFile => ({ key: `${Date.now()}-${nextQuickReplyFileId++}`, file })

function appendQuickReplyFiles(body: FormData, files: QuickReplyDraftFile[]) {
  files.forEach(({ file }, index) => {
    body.append(`file_${index}`, file)
    body.append(`media_type_${index}`, mediaTypeForFile(file))
  })
}

function MediaOrderControls({ label, first, last, busy, onUp, onDown, onDelete }: {
  label: string
  first: boolean
  last: boolean
  busy: boolean
  onUp: () => void
  onDown: () => void
  onDelete: () => void
}) {
  return <div className="flex shrink-0 items-center gap-1">
    <button type="button" disabled={busy || first} onClick={onUp} className="flex size-11 items-center justify-center rounded-lg muted hover:bg-white/10 hover:text-white" aria-label={`Переместить ${label} выше`}><ArrowUp size={17} /></button>
    <button type="button" disabled={busy || last} onClick={onDown} className="flex size-11 items-center justify-center rounded-lg muted hover:bg-white/10 hover:text-white" aria-label={`Переместить ${label} ниже`}><ArrowDown size={17} /></button>
    <button type="button" disabled={busy} onClick={onDelete} className="flex size-11 items-center justify-center rounded-lg text-red-300 hover:bg-red-400/10" aria-label={`Удалить ${label}`}><Trash2 size={17} /></button>
  </div>
}

function QuickReplyEditor({ initial, onChanged, onDeleted }: {
  initial: QuickReply | null
  onChanged: () => Promise<void>
  onDeleted: () => void
}) {
  const initialItems = initial ? orderedQuickReplyItems(initial) : []
  const originalText = initial ? quickReplyText(initial) : ''
  const [title, setTitle] = useState(initial?.title || '')
  const [text, setText] = useState(originalText)
  const [items, setItems] = useState<QuickReplyItem[]>(initialItems)
  const [files, setFiles] = useState<QuickReplyDraftFile[]>([])
  const [editingFile, setEditingFile] = useState<QuickReplyDraftFile | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')
  const fileInputRef = useRef<HTMLInputElement>(null)
  const mediaItems = items.filter((item) => item.content_type !== 'text')
  const hasContent = Boolean(text.trim() || mediaItems.length || files.length)
  const dirty = files.length > 0 || title.trim() !== (initial?.title || '') || text !== originalText || items.map((item) => item.id).join(',') !== initialItems.map((item) => item.id).join(',')

  const moveExisting = (id: number, direction: -1 | 1) => {
    setItems((current) => {
      const media = current.filter((item) => item.content_type !== 'text')
      const from = media.findIndex((item) => item.id === id)
      const to = from + direction
      if (from < 0 || to < 0 || to >= media.length) return current
      const next = [...current]
      const fromIndex = next.findIndex((item) => item.id === id)
      const toIndex = next.findIndex((item) => item.id === media[to].id)
      ;[next[fromIndex], next[toIndex]] = [next[toIndex], next[fromIndex]]
      return next
    })
  }
  const moveFile = (index: number, direction: -1 | 1) => {
    setFiles((current) => {
      const to = index + direction
      if (to < 0 || to >= current.length) return current
      const next = [...current]
      ;[next[index], next[to]] = [next[to], next[index]]
      return next
    })
  }
  const removeExisting = async (id: number) => {
    if (busy || !window.confirm('Удалить вложение из быстрого ответа?')) return
    setBusy(true)
    setError('')
    setSuccess('')
    try {
      await api(`/quick-reply-items/${id}`, { method: 'DELETE' })
      setItems((current) => current.filter((item) => item.id !== id))
      await onChanged()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось удалить вложение')
    } finally {
      setBusy(false)
    }
  }
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !dirty) return
    if (!title.trim()) { setError('Укажите название быстрого ответа'); return }
    if (!hasContent) { setError('Добавьте текст или хотя бы одно вложение'); return }
    setBusy(true)
    setError('')
    setSuccess('')
    try {
      if (initial) {
        await api(`/quick-replies/${initial.id}`, { method: 'PATCH', body: JSON.stringify({ title: title.trim(), text: text.trim(), item_order: items.map((item) => item.id) }) })
        if (files.length) {
          const body = new FormData()
          appendQuickReplyFiles(body, files)
          await api(`/quick-replies/${initial.id}/items`, { method: 'POST', body })
        }
      } else {
        const body = new FormData()
        body.append('title', title.trim())
        body.append('text', text.trim())
        appendQuickReplyFiles(body, files)
        await api('/quick-replies', { method: 'POST', body })
      }
      await onChanged()
      if (!initial) { setTitle(''); setText(''); setItems([]) }
      setFiles([])
      setSuccess(initial ? 'Быстрый ответ сохранён' : 'Быстрый ответ создан')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось сохранить быстрый ответ')
    } finally {
      setBusy(false)
    }
  }
  const removeReply = async () => {
    if (!initial || busy || !window.confirm(`Удалить быстрый ответ «${initial.title}»?`)) return
    setBusy(true)
    setError('')
    setSuccess('')
    try {
      await api(`/quick-replies/${initial.id}`, { method: 'DELETE' })
      onDeleted()
      await onChanged()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось удалить быстрый ответ')
    } finally {
      setBusy(false)
    }
  }

  return <form onSubmit={save} className="min-w-0 space-y-4">
    <h3 className="text-sm font-bold">{initial ? 'Редактирование быстрого ответа' : 'Новый быстрый ответ'}</h3>
    {(error || success) && <p aria-live="polite" className={`break-anywhere rounded-xl p-3 text-sm ${error ? 'bg-red-400/10 text-red-200' : 'soft-accent'}`}>{error || success}</p>}
    <label className="block text-sm font-semibold">Название
      <input required maxLength={200} value={title} disabled={busy} onChange={(event) => setTitle(event.target.value)} placeholder="Например: Инструкция по подключению" className="panel-deep mt-2 min-h-11 w-full rounded-xl border px-3 text-base placeholder:text-slate-500 sm:text-sm" />
    </label>
    <label className="block text-sm font-semibold">Текст ответа
      <textarea value={text} disabled={busy} onChange={(event) => setText(event.target.value)} rows={6} maxLength={4096} placeholder="Полный текст, который увидит клиент…" className="panel-deep mt-2 w-full resize-y rounded-xl border p-3 text-base leading-6 placeholder:text-slate-500 sm:text-sm" />
    </label>
    <div>
      <button type="button" disabled={busy} onClick={() => fileInputRef.current?.click()} className="flex min-h-11 w-full items-center justify-center gap-2 rounded-xl bg-white/10 px-4 text-sm font-semibold transition hover:bg-white/15"><Plus size={18} />Добавить медиа</button>
      <input ref={fileInputRef} type="file" multiple disabled={busy} className="hidden" tabIndex={-1} onChange={(event) => {
        const selectedFiles = Array.from(event.target.files || []).map(quickReplyDraftFile)
        setFiles((current) => [...current, ...selectedFiles])
        event.target.value = ''
      }} />
      <p className="mt-2 text-xs leading-5 muted">Можно выбрать несколько файлов. Порядок отправки меняется стрелками.</p>
    </div>
    {mediaItems.length > 0 && <section aria-label="Сохранённые вложения" className="space-y-3">
      <h4 className="text-xs font-bold uppercase tracking-wider muted">Сохранённые вложения</h4>
      {mediaItems.map((item, index) => <div key={item.id} className="rounded-xl border border-white/10 p-3">
        <div className="mb-2 flex items-center justify-between gap-2"><span className="min-w-0 break-anywhere text-xs font-semibold muted">Вложение {index + 1}</span><MediaOrderControls label={`вложение ${index + 1}`} first={index === 0} last={index === mediaItems.length - 1} busy={busy} onUp={() => moveExisting(item.id, -1)} onDown={() => moveExisting(item.id, 1)} onDelete={() => void removeExisting(item.id)} /></div>
        <MediaAttachment mediaUrl={item.media_url} mediaType={item.content_type} fileName={item.file_name} mimeType={item.mime_type} size={item.file_size} label={item.text || item.file_name} />
      </div>)}
    </section>}
    {files.length > 0 && <section aria-label="Новые вложения" className="space-y-3">
      <h4 className="text-xs font-bold uppercase tracking-wider muted">Новые вложения</h4>
      {files.map((draftFile, index) => <div key={draftFile.key} className="rounded-xl border border-white/10 p-3">
        <div className="mb-2 flex items-center justify-between gap-2"><span className="text-xs font-semibold muted">Новое вложение {index + 1}</span><MediaOrderControls label={`новое вложение ${index + 1}`} first={index === 0} last={index === files.length - 1} busy={busy} onUp={() => moveFile(index, -1)} onDown={() => moveFile(index, 1)} onDelete={() => setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))} /></div>
        <LocalFilePreview file={draftFile.file} />
        {isEditableMedia(draftFile.file) && <button type="button" disabled={busy} onClick={() => setEditingFile(draftFile)} className="mt-2 flex min-h-11 items-center gap-2 rounded-xl bg-white/10 px-4 text-sm font-semibold hover:bg-white/15"><Pencil size={17} />Изменить</button>}
      </div>)}
    </section>}
    <button type="submit" disabled={busy || !dirty || !title.trim() || !hasContent} className="accent-bg flex min-h-11 w-full items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold hover:brightness-110">{busy ? <LoaderCircle className="animate-spin" size={18} /> : <Check size={18} />}{initial ? 'Сохранить изменения' : 'Создать быстрый ответ'}</button>
    {initial && <button type="button" disabled={busy} onClick={() => void removeReply()} className="min-h-11 w-full rounded-xl bg-red-400/10 px-4 text-sm font-semibold text-red-200 hover:bg-red-400/15">Удалить быстрый ответ</button>}
    {editingFile && <MediaEditorDialog file={editingFile.file} onClose={() => setEditingFile(null)} onApply={(file) => {
      setFiles((current) => current.map((item) => item.key === editingFile.key ? { ...item, file } : item))
      setEditingFile(null)
    }} />}
  </form>
}

function QuickRepliesManager() {
  const [replies, setReplies] = useState<QuickReply[]>([])
  const [selected, setSelected] = useState<number | 'new'>('new')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const loadReplies = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true)
    setError('')
    try {
      const result = await api<{ quick_replies: QuickReply[] }>('/quick-replies')
      setReplies(result.quick_replies)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось загрузить быстрые ответы')
    } finally {
      if (showLoading) setLoading(false)
    }
  }, [])
  useEffect(() => { void loadReplies() }, [loadReplies])
  const selectedReply = selected === 'new' ? null : replies.find((reply) => reply.id === selected) || null
  return <div className="grid min-w-0 gap-5 md:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
    <section className="min-w-0">
      <div className="mb-3 flex items-center justify-between gap-2"><h3 className="text-sm font-bold">Библиотека</h3><button onClick={() => setSelected('new')} className="flex min-h-11 items-center gap-2 rounded-xl bg-white/10 px-3 text-xs font-semibold hover:bg-white/15"><Plus size={17} />Новый</button></div>
      {error && <div className="mb-3 rounded-xl bg-red-400/10 p-3 text-sm text-red-200"><p className="break-anywhere">{error}</p><button onClick={() => void loadReplies()} className="mt-2 min-h-11 underline">Повторить</button></div>}
      {loading ? <div className="py-8 text-center"><LoaderCircle className="mx-auto animate-spin accent" /><p className="mt-2 text-sm muted">Загружаем ответы…</p></div> : <div className="scrollbar max-h-64 space-y-1 overflow-y-auto md:max-h-[60dvh]">
        {replies.map((reply) => <button key={reply.id} onClick={() => setSelected(reply.id)} className={`min-h-11 w-full rounded-xl border p-3 text-left transition ${selected === reply.id ? 'border-teal-300/25 bg-teal-300/10' : 'border-transparent hover:bg-white/5'}`}><span className="block break-anywhere text-sm font-semibold">{reply.title}</span><span className="mt-1 line-clamp-2 break-anywhere text-xs leading-5 muted">{quickReplyText(reply) || `${reply.items.filter((item) => item.content_type !== 'text').length} влож.`}</span></button>)}
        {replies.length === 0 && <p className="rounded-xl bg-white/5 p-4 text-sm muted">Быстрых ответов пока нет. Создайте первый.</p>}
      </div>}
    </section>
    <QuickReplyEditor key={selectedReply ? JSON.stringify(selectedReply) : 'new'} initial={selectedReply} onChanged={() => loadReplies(false)} onDeleted={() => setSelected('new')} />
  </div>
}

function QuickReplyPicker({ ticketNumber, onSent }: { ticketNumber: number; onSent: (messages: ChatMessage[]) => void }) {
  const [replies, setReplies] = useState<QuickReply[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const loadReplies = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await api<{ quick_replies: QuickReply[] }>('/quick-replies')
      setReplies(result.quick_replies)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось загрузить быстрые ответы')
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => { void loadReplies() }, [loadReplies])
  const selected = replies.find((reply) => reply.id === selectedId) || null
  const sendReply = async () => {
    if (!selected || busy || !window.confirm(`Вы уверены, что хотите отправить «${selected.title}»?`)) return
    setBusy(true)
    setError('')
    try {
      const result = await api<{ messages: ChatMessage[] }>(`/tickets/${ticketNumber}/quick-replies/${selected.id}`, { method: 'POST' })
      onSent(result.messages)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось отправить быстрый ответ')
    } finally {
      setBusy(false)
    }
  }
  if (loading) return <div className="py-10 text-center"><LoaderCircle className="mx-auto animate-spin accent" /><p className="mt-3 text-sm muted">Загружаем быстрые ответы…</p></div>
  return <div className="space-y-4">
    {error && <div className="rounded-xl bg-red-400/10 p-3 text-sm text-red-200"><p className="break-anywhere">{error}</p>{replies.length === 0 && <button onClick={() => void loadReplies()} className="mt-2 min-h-11 underline">Повторить</button>}</div>}
    {replies.length === 0 && !error ? <p className="rounded-xl bg-white/5 p-4 text-sm muted">Быстрых ответов пока нет. Создайте их через кнопку со знаком вопроса в верхней панели.</p> : <div className="grid min-w-0 gap-4 md:grid-cols-[minmax(0,0.75fr)_minmax(0,1.25fr)]">
      <div className="scrollbar max-h-52 space-y-1 overflow-y-auto md:max-h-[55dvh]">
        {replies.map((reply) => <button key={reply.id} onClick={() => { setSelectedId(reply.id); setError('') }} className={`min-h-11 w-full rounded-xl border p-3 text-left text-sm font-semibold transition ${selectedId === reply.id ? 'border-teal-300/25 bg-teal-300/10' : 'border-transparent hover:bg-white/5'}`}><span className="break-anywhere">{reply.title}</span></button>)}
      </div>
      <section className="min-w-0 rounded-xl bg-white/5 p-4" aria-label="Предпросмотр быстрого ответа">
        {!selected ? <p className="py-6 text-center text-sm muted">Выберите название, чтобы увидеть полный ответ.</p> : <>
          <h3 className="break-anywhere font-bold">{selected.title}</h3>
          {quickReplyText(selected) && <p className="mt-3 break-anywhere whitespace-pre-wrap text-sm leading-6">{quickReplyText(selected)}</p>}
          {orderedQuickReplyItems(selected).filter((item) => item.content_type !== 'text').map((item) => <MediaAttachment key={item.id} mediaUrl={item.media_url} mediaType={item.content_type} fileName={item.file_name} mimeType={item.mime_type} size={item.file_size} label={item.text || item.file_name} />)}
          <button disabled={busy} onClick={() => void sendReply()} className="accent-bg mt-5 flex min-h-11 w-full items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold hover:brightness-110">{busy ? <LoaderCircle className="animate-spin" size={18} /> : <Send size={18} />}Отправить</button>
        </>}
      </section>
    </div>}
  </div>
}

type EmployeeForm = {
  telegram_id: string
  name: string
  nickname: string
  gender: string
  greeting: string
  schedule: string
}

const emptyEmployeeForm = (): EmployeeForm => ({ telegram_id: '', name: '', nickname: '', gender: '', greeting: '', schedule: 'Пн-Пт 09:00-18:00' })

function EmployeesManager({ onChanged, currentTelegramId }: { onChanged: () => Promise<void>; currentTelegramId: number }) {
  const [employees, setEmployees] = useState<Employee[]>([])
  const [selected, setSelected] = useState<number | 'new'>('new')
  const [form, setForm] = useState<EmployeeForm>(emptyEmployeeForm)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const loadEmployees = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const result = await api<{ employees: Employee[] }>('/employees')
      setEmployees(result.employees)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось загрузить сотрудников')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void loadEmployees() }, [loadEmployees])

  const update = (field: keyof EmployeeForm, value: string) => setForm((current) => ({ ...current, [field]: value }))
  const choose = (employee: Employee) => {
    setSelected(employee.id)
    setForm({ telegram_id: String(employee.telegram_id), name: employee.name, nickname: employee.nickname, gender: employee.gender, greeting: employee.greeting, schedule: employee.schedule === 'Не задан' ? '' : employee.schedule })
    setError('')
    setSuccess('')
  }
  const chooseNew = () => {
    setSelected('new')
    setForm(emptyEmployeeForm())
    setError('')
    setSuccess('')
  }
  const refresh = async () => {
    await loadEmployees()
    await onChanged()
  }
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (!form.name.trim() || !form.nickname.trim()) { setError('Укажите имя и никнейм сотрудника'); return }
    const telegramId = Number(form.telegram_id)
    if (selected === 'new' && (!Number.isInteger(telegramId) || telegramId <= 0)) { setError('Telegram ID должен быть положительным целым числом'); return }
    setBusy(true)
    setError('')
    setSuccess('')
    try {
      if (selected === 'new') {
        await api('/employees', { method: 'POST', body: JSON.stringify({ telegram_id: telegramId, name: form.name.trim(), nickname: form.nickname.trim(), gender: form.gender }) })
        setForm(emptyEmployeeForm())
      } else {
        await api(`/employees/${selected}`, { method: 'PATCH', body: JSON.stringify({ name: form.name.trim(), nickname: form.nickname.trim(), gender: form.gender, greeting: form.greeting, schedule: form.schedule }) })
      }
      await refresh()
      setSuccess(selected === 'new' ? 'Сотрудник создан' : 'Данные сотрудника сохранены')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось сохранить сотрудника')
    } finally {
      setBusy(false)
    }
  }
  const toggleActive = async (employee: Employee) => {
    const action = employee.active ? 'Деактивировать' : 'Активировать'
    if (busy || !window.confirm(`${action} сотрудника ${employee.nickname}?`)) return
    setBusy(true)
    setError('')
    setSuccess('')
    try {
      await api(`/employees/${employee.id}`, { method: 'PATCH', body: JSON.stringify({ active: !employee.active }) })
      await refresh()
      setSuccess(employee.active ? 'Сотрудник деактивирован' : 'Сотрудник активирован')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Не удалось изменить статус сотрудника')
    } finally {
      setBusy(false)
    }
  }

  const selectedEmployee = selected === 'new' ? null : employees.find((employee) => employee.id === selected) || null
  return <div className="grid min-w-0 gap-5 md:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
    <section className="min-w-0">
      <div className="mb-3 flex items-center justify-between gap-2"><h3 className="text-sm font-bold">Все сотрудники</h3><button onClick={chooseNew} disabled={busy} className="flex min-h-11 items-center gap-2 rounded-xl bg-white/10 px-3 text-xs font-semibold hover:bg-white/15"><Plus size={17} />Новый</button></div>
      {loading ? <div className="py-8 text-center"><LoaderCircle className="mx-auto animate-spin accent" /><p className="mt-2 text-sm muted">Загружаем список…</p></div> : <div className="scrollbar max-h-64 space-y-1 overflow-y-auto md:max-h-[55dvh]">
        {employees.map((employee) => <button key={employee.id} onClick={() => choose(employee)} disabled={busy} className={`min-h-11 w-full rounded-xl border p-3 text-left transition ${selected === employee.id ? 'border-teal-300/25 bg-teal-300/10' : 'border-transparent hover:bg-white/5'}`}><span className="flex items-center justify-between gap-2"><span className="min-w-0 break-anywhere text-sm font-semibold">{employee.nickname}</span><span className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${employee.active ? 'soft-accent' : 'soft-neutral'}`}>{employee.active ? 'Активен' : 'Неактивен'}</span></span><span className="mt-1 block break-anywhere text-xs muted">{employee.name} · ID {employee.telegram_id}</span></button>)}
        {employees.length === 0 && <p className="rounded-xl bg-white/5 p-4 text-sm muted">Сотрудников пока нет.</p>}
      </div>}
    </section>
    <form onSubmit={save} className="min-w-0 space-y-3">
      <h3 className="text-sm font-bold">{selected === 'new' ? 'Новый сотрудник' : 'Редактирование сотрудника'}</h3>
      {(error || success) && <p aria-live="polite" className={`break-anywhere rounded-xl p-3 text-sm ${error ? 'bg-red-400/10 text-red-200' : 'soft-accent'}`}>{error || success}</p>}
      {selected === 'new' && <label className="block text-sm font-semibold">Telegram ID<input required inputMode="numeric" value={form.telegram_id} disabled={busy} onChange={(event) => update('telegram_id', event.target.value)} className="panel-deep mt-1 min-h-11 w-full rounded-xl border px-3 text-base sm:text-sm" /></label>}
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-sm font-semibold">Имя<input required maxLength={100} value={form.name} disabled={busy} onChange={(event) => update('name', event.target.value)} className="panel-deep mt-1 min-h-11 w-full rounded-xl border px-3 text-base sm:text-sm" /></label>
        <label className="block text-sm font-semibold">Никнейм<input required maxLength={24} value={form.nickname} disabled={busy} onChange={(event) => update('nickname', event.target.value)} className="panel-deep mt-1 min-h-11 w-full rounded-xl border px-3 text-base sm:text-sm" /></label>
      </div>
      <label className="block text-sm font-semibold">Пол<select required value={form.gender} disabled={busy} onChange={(event) => update('gender', event.target.value)} className="panel-deep mt-1 min-h-11 w-full rounded-xl border px-3 text-base sm:text-sm"><option value="">Выберите пол</option><option value="m">Мужской</option><option value="f">Женский</option></select></label>
      <label className="block text-sm font-semibold">График<input value={form.schedule} disabled={busy || selected === 'new'} placeholder="Пн-Пт 09:00-18:00; пусто — без графика" onChange={(event) => update('schedule', event.target.value)} className="panel-deep mt-1 min-h-11 w-full rounded-xl border px-3 text-base placeholder:text-slate-500 sm:text-sm" /></label>
      {selected !== 'new' && <label className="block text-sm font-semibold">Приветствие<textarea value={form.greeting} disabled={busy} rows={4} maxLength={2000} onChange={(event) => update('greeting', event.target.value)} className="panel-deep mt-1 w-full resize-y rounded-xl border p-3 text-base leading-6 sm:text-sm" /></label>}
      <button type="submit" disabled={busy || loading} className="accent-bg flex min-h-11 w-full items-center justify-center gap-2 rounded-xl px-4 text-sm font-bold">{busy ? <LoaderCircle className="animate-spin" size={18} /> : <Check size={18} />}{selected === 'new' ? 'Создать сотрудника' : 'Сохранить изменения'}</button>
      {selectedEmployee && selectedEmployee.telegram_id !== currentTelegramId && <button type="button" disabled={busy} onClick={() => void toggleActive(selectedEmployee)} className={`min-h-11 w-full rounded-xl px-4 text-sm font-semibold ${selectedEmployee.active ? 'bg-red-400/10 text-red-200 hover:bg-red-400/15' : 'soft-accent'}`}>{selectedEmployee.active ? 'Деактивировать сотрудника' : 'Активировать сотрудника'}</button>}
    </form>
  </div>
}

export default function App() {
  const [data, setData] = useState<Bootstrap | null>(null)
  const [selectedNumber, setSelectedNumber] = useState<number | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState<'open' | 'new' | 'closed'>('open')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [loadingMessages, setLoadingMessages] = useState(false)
  const [error, setError] = useState('')
  const [toast, setToast] = useState('')
  const [detailsOpen, setDetailsOpen] = useState(false)
  const [faqOpen, setFaqOpen] = useState(false)
  const [quickRepliesOpen, setQuickRepliesOpen] = useState(false)
  const [quickReplyPickerOpen, setQuickReplyPickerOpen] = useState(false)
  const [employeesOpen, setEmployeesOpen] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [profileLoading, setProfileLoading] = useState(false)
  const [profileError, setProfileError] = useState('')
  const [editing, setEditing] = useState<number | null>(null)
  const [editText, setEditText] = useState('')
  const [attachment, setAttachment] = useState<File | null>(null)
  const [mediaEditorOpen, setMediaEditorOpen] = useState(false)
  const [soundEnabled, setSoundEnabled] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)
  const composerRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const audioContextRef = useRef<AudioContext | null>(null)
  const soundEnabledRef = useRef(false)
  const ticketSnapshot = useRef(new Map<number, { lastUserMessage: string | null; status: string }>())
  const snapshotReady = useRef(false)
  const didSelect = useRef(false)
  const messagesRequest = useRef(0)

  const notify = useCallback((text: string) => {
    setToast(text)
    window.setTimeout(() => setToast(''), 3000)
  }, [])

  const playSignal = useCallback(() => {
    const context = audioContextRef.current
    if (!soundEnabledRef.current || !context || context.state !== 'running') return
    const oscillator = context.createOscillator()
    const gain = context.createGain()
    const now = context.currentTime
    oscillator.type = 'sine'
    oscillator.frequency.setValueAtTime(880, now)
    oscillator.frequency.exponentialRampToValueAtTime(660, now + 0.14)
    gain.gain.setValueAtTime(0.0001, now)
    gain.gain.exponentialRampToValueAtTime(0.08, now + 0.015)
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.16)
    oscillator.connect(gain)
    gain.connect(context.destination)
    oscillator.start(now)
    oscillator.stop(now + 0.17)
    oscillator.onended = () => { oscillator.disconnect(); gain.disconnect() }
  }, [])

  const toggleSound = async () => {
    if (soundEnabledRef.current) {
      soundEnabledRef.current = false
      setSoundEnabled(false)
      const context = audioContextRef.current
      if (context?.state === 'running') await context.suspend()
      return
    }
    try {
      let context = audioContextRef.current
      if (!context || context.state === 'closed') context = new AudioContext()
      audioContextRef.current = context
      if (context.state === 'suspended') await context.resume()
      soundEnabledRef.current = true
      setSoundEnabled(true)
    } catch {
      notify('Не удалось включить звук в этом браузере')
    }
  }

  const load = useCallback(async (quiet = false) => {
    try {
      const result = await api<Bootstrap>('/bootstrap')
      let shouldSignal = false
      if (snapshotReady.current) {
        shouldSignal = result.tickets.some((ticket) => {
          const previous = ticketSnapshot.current.get(ticket.number)
          return !previous || (previous.lastUserMessage !== ticket.last_user_message_at && previous.status === ticket.status)
        })
      }
      ticketSnapshot.current = new Map(result.tickets.map((ticket) => [ticket.number, { lastUserMessage: ticket.last_user_message_at, status: ticket.status }]))
      if (shouldSignal) playSignal()
      snapshotReady.current = true
      setData(result)
      setError('')
      setSelectedNumber((current) => {
        if (current !== null || didSelect.current) return current
        const requestedNumber = Number(new URLSearchParams(window.location.search).get('ticket'))
        const requestedTicket = Number.isInteger(requestedNumber) && requestedNumber > 0
          ? result.tickets.find((ticket) => ticket.number === requestedNumber)
          : undefined
        if (requestedTicket) return requestedTicket.number
        if (!window.matchMedia('(min-width: 768px)').matches) return current
        return result.tickets.find((ticket) => ticket.employee_id)?.number ?? result.tickets[0]?.number ?? null
      })
    } catch (reason) {
      if (!quiet) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить данные')
    }
  }, [playSignal])

  const selected = data?.tickets.find((ticket) => ticket.number === selectedNumber) || null
  const selectedEmployeeId = selected?.employee_id
  const updateUnreadCount = useCallback((number: number, count: number) => {
    setData((current) => current ? { ...current, tickets: current.tickets.map((ticket) => ticket.number === number ? { ...ticket, unread_count: count } : ticket) } : current)
  }, [])
  const loadMessages = useCallback(async (initial = false) => {
    const request = ++messagesRequest.current
    if (!selectedNumber || selectedEmployeeId === undefined) { setMessages([]); setLoadingMessages(false); return }
    if (initial) { setLoadingMessages(true); setMessages([]) }
    try {
      if (selectedEmployeeId) {
        const result = await api<{ ticket: Ticket; messages: ChatMessage[] }>(`/tickets/${selectedNumber}/messages`)
        if (request === messagesRequest.current) {
          setMessages(result.messages)
          updateUnreadCount(selectedNumber, result.ticket.unread_count)
        }
      } else {
        const result = await api<{ ticket: Ticket }>(`/tickets/${selectedNumber}/read`, { method: 'POST' })
        if (request === messagesRequest.current) updateUnreadCount(selectedNumber, result.ticket.unread_count)
      }
    } catch (reason) {
      if (initial && request === messagesRequest.current) setError(reason instanceof Error ? reason.message : 'Не удалось загрузить переписку')
    } finally {
      if (request === messagesRequest.current) setLoadingMessages(false)
    }
  }, [selectedNumber, selectedEmployeeId, updateUnreadCount])

  useEffect(() => { initTelegram(); void load(); const timer = window.setInterval(() => void load(true), 12000); return () => window.clearInterval(timer) }, [load])
  useEffect(() => () => {
    soundEnabledRef.current = false
    const context = audioContextRef.current
    if (context && context.state !== 'closed') void context.close()
  }, [])
  useEffect(() => { void loadMessages(true); const timer = window.setInterval(() => void loadMessages(false), 8000); return () => window.clearInterval(timer) }, [loadMessages])
  useEffect(() => { endRef.current?.scrollIntoView({ block: 'end' }) }, [messages.length])
  useEffect(() => {
    const composer = composerRef.current
    if (!composer) return
    composer.style.height = 'auto'
    composer.style.height = `${Math.min(composer.scrollHeight, 160)}px`
  }, [draft])

  const filtered = useMemo(() => data?.tickets.filter((ticket) => {
    const status = data.statuses.find((item) => item.key === ticket.status)
    const closed = isStatusClosed(status)
    const inFilter = filter === 'new' ? ticket.status === 'new' : filter === 'closed' ? closed : !closed && ticket.status !== 'new'
    const haystack = `${ticket.number} ${ticket.client} ${ticket.username || ''} ${ticket.category} ${ticket.description} ${ticket.title || ''}`.toLowerCase()
    return inFilter && haystack.includes(query.toLowerCase())
  }) || [], [data, filter, query])
  const unreadChats = useMemo(() => {
    const counts = { open: 0, new: 0, closed: 0 }
    data?.tickets.forEach((ticket) => {
      if (ticket.unread_count === 0) return
      const status = data.statuses.find((item) => item.key === ticket.status)
      const section = ticket.status === 'new' ? 'new' : isStatusClosed(status) ? 'closed' : 'open'
      counts[section] += 1
    })
    return counts
  }, [data])

  const run = async (action: () => Promise<void>, success: string) => {
    setBusy(true); setError('')
    try { await action(); await load(true); notify(success) }
    catch (reason) { setError(reason instanceof Error ? reason.message : 'Действие не выполнено') }
    finally { setBusy(false) }
  }

  const updateTicket = async (payload: Record<string, unknown>, success = 'Обращение обновлено') => {
    if (!selected) return
    await run(async () => { await api(`/tickets/${selected.number}`, { method: 'PATCH', body: JSON.stringify(payload) }) }, success)
  }
  const claim = async () => {
    if (!selected) return
    await run(async () => { await api(`/tickets/${selected.number}/claim`, { method: 'POST' }) }, 'Обращение принято')
  }
  const pin = async () => {
    if (!selected) return
    await run(async () => { await api(`/tickets/${selected.number}/pin`, { method: 'POST' }) }, 'Закреп обновлён')
  }
  const setReminder = async (minutes: number | null) => {
    if (!selected) return
    await run(async () => {
      await api(`/tickets/${selected.number}/reminder`, minutes === null ? { method: 'DELETE' } : { method: 'PUT', body: JSON.stringify({ minutes }) })
    }, minutes === null ? 'Напоминание выключено' : 'Напоминание установлено')
  }
  const clearAttachment = () => {
    setAttachment(null)
    setMediaEditorOpen(false)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }
  const send = async (event: FormEvent | ReactKeyboardEvent<HTMLTextAreaElement>) => {
    event.preventDefault()
    if (!selected || (!attachment && !draft.trim()) || busy) return
    if (attachment && draft.length > 1024) { setError('Подпись к файлу не должна превышать 1024 символа'); return }
    const text = draft.trim()
    const file = attachment
    await run(async () => {
      if (file) {
        const body = new FormData()
        body.append('file', file)
        body.append('caption', text)
        body.append('media_type', mediaTypeForFile(file))
        const result = await api<{ message: ChatMessage }>(`/tickets/${selected.number}/messages/media`, { method: 'POST', body })
        setMessages((current) => [...current, result.message])
        clearAttachment()
      } else {
        const result = await api<{ message: ChatMessage }>(`/tickets/${selected.number}/messages`, { method: 'POST', body: JSON.stringify({ text }) })
        setMessages((current) => [...current, result.message])
      }
      setDraft('')
    }, file ? 'Файл отправлен' : 'Сообщение отправлено')
  }
  const saveEdit = async (id: number) => {
    const message = messages.find((item) => item.id === id)
    if (!message || (message.content_type === 'text' && !editText.trim()) || busy) return
    await run(async () => {
      const result = await api<{ message: ChatMessage }>(`/messages/${id}`, { method: 'PATCH', body: JSON.stringify({ text: editText }) })
      setMessages((current) => current.map((item) => item.id === id ? result.message : item)); setEditing(null)
    }, 'Сообщение изменено')
  }
  const removeMessage = async (id: number) => {
    if (!window.confirm('Удалить сообщение у сотрудника и клиента?')) return
    await run(async () => { await api(`/messages/${id}`, { method: 'DELETE' }); setMessages((current) => current.filter((item) => item.id !== id)) }, 'Сообщение удалено')
  }
  const openProfile = async () => {
    setProfileOpen(true); setProfileLoading(true); setProfileError('')
    try { setProfile(await api<Profile>('/profile')) }
    catch (reason) { setProfileError(reason instanceof Error ? reason.message : 'Не удалось загрузить профиль') }
    finally { setProfileLoading(false) }
  }
  const saveGreeting = async (greeting: string) => {
    await run(async () => {
      await api('/profile', { method: 'PATCH', body: JSON.stringify({ greeting }) })
      setProfile((current) => current ? { ...current, greeting } : current)
    }, 'Приветствие сохранено')
  }
  const selectTicket = (number: number) => { didSelect.current = true; clearAttachment(); setSelectedNumber(number) }
  const backToList = () => { didSelect.current = true; clearAttachment(); setSelectedNumber(null); setDetailsOpen(false) }

  if (!data) return <main className="app-shell flex items-center justify-center p-6">
    <div className="text-center">{error ? <><CircleHelp className="mx-auto mb-4 accent" size={36} /><h1 className="text-xl font-bold">Панель пока недоступна</h1><p className="mt-2 max-w-md break-anywhere muted">{error}</p><button onClick={() => { setError(''); void load() }} className="accent-bg mt-5 min-h-11 rounded-xl px-5 font-bold">Повторить</button></> : <><LoaderCircle className="mx-auto animate-spin accent" size={34} /><p className="mt-4 muted">Загружаем рабочее пространство…</p></>}</div>
  </main>

  const selectedStatus = data.statuses.find((item) => item.key === selected?.status)
  const isClosed = isStatusClosed(selectedStatus)
  return <main className="app-shell flex h-dvh flex-col overflow-hidden">
    <header className="safe-header panel-deep flex min-h-16 shrink-0 items-center justify-between gap-2 border-b px-3 sm:px-6">
      <div className="flex min-w-0 items-center gap-3"><div className="soft-accent flex size-10 shrink-0 items-center justify-center rounded-xl"><Headphones size={21} /></div><div className="min-w-0"><div className="truncate font-extrabold tracking-tight">Пульт поддержки</div><div className="hidden text-xs muted sm:block">Единое пространство обращений</div></div></div>
      <div className="flex shrink-0 items-center gap-1 sm:gap-2">
        <button onClick={() => void load()} className="flex size-11 items-center justify-center rounded-xl muted transition hover:bg-white/5 hover:text-white" aria-label="Обновить данные"><RefreshCw size={18} /></button>
        <button onClick={() => setQuickRepliesOpen(true)} className="flex min-h-11 items-center gap-2 rounded-xl bg-white/5 px-3 text-sm font-semibold transition hover:bg-white/10" aria-label="Быстрые ответы"><CircleHelp size={18} /><span className="hidden lg:inline">Быстрые ответы</span></button>
        {data.employee.admin && <button onClick={() => setEmployeesOpen(true)} className="flex min-h-11 items-center gap-2 rounded-xl bg-white/5 px-3 text-sm font-semibold transition hover:bg-white/10" aria-label="Сотрудники"><Users size={18} /><span className="hidden lg:inline">Сотрудники</span></button>}
        {data.employee.admin && <button onClick={() => setFaqOpen(true)} className="flex min-h-11 items-center gap-2 rounded-xl bg-white/5 px-3 text-sm font-semibold transition hover:bg-white/10" aria-label="FAQ для клиентов"><MessageSquareText size={18} /><span className="hidden lg:inline">FAQ для клиентов</span></button>}
        <button onClick={() => void openProfile()} className="flex min-h-11 min-w-11 items-center gap-2 rounded-xl px-2 text-sm font-semibold transition hover:bg-white/5 sm:px-3" aria-label="Открыть профиль"><UserRound size={18} className="shrink-0 muted" /><span className="hidden max-w-32 truncate md:inline">{data.employee.nickname}</span></button>
      </div>
    </header>

    {(error || toast) && <div aria-live="polite" className={`flex min-h-11 shrink-0 items-center justify-center gap-2 break-anywhere px-3 py-2 text-center text-sm ${error ? 'bg-red-400/15 text-red-200' : 'soft-accent'}`}><span>{error || toast}</span><button className="min-h-11 shrink-0 px-2 underline" onClick={() => { setError(''); setToast('') }}>Закрыть</button></div>}

    <div className="grid min-h-0 min-w-0 flex-1 grid-cols-1 md:grid-cols-[320px_minmax(0,1fr)] xl:grid-cols-[340px_minmax(0,1fr)_340px]">
      <aside className={`panel-deep min-h-0 min-w-0 flex-col border-r ${selected ? 'hidden md:flex' : 'flex'}`}>
        <div className="shrink-0 border-b border-white/10 p-4">
          <div className="flex items-center gap-2">
            <label className="relative block min-w-0 flex-1"><Search className="pointer-events-none absolute left-3 top-3.5 muted" size={18} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Поиск обращений" className="panel min-h-11 w-full rounded-xl border pl-10 pr-3 text-base placeholder:text-slate-500 sm:text-sm" /></label>
            <button onClick={() => void toggleSound()} className="panel flex size-11 shrink-0 items-center justify-center rounded-xl border transition hover:bg-white/10" aria-label={soundEnabled ? 'Выключить звук уведомлений' : 'Включить звук уведомлений'} aria-pressed={soundEnabled}>{soundEnabled ? <Volume2 size={19} className="accent" /> : <VolumeX size={19} className="muted" />}</button>
          </div>
          <div className="mt-3 grid grid-cols-3 rounded-xl bg-black/15 p-1">
            {([['open', 'В работе', Inbox], ['new', 'Новые', Clock3], ['closed', 'Архив', Archive]] as const).map(([key, label, Icon]) => <button key={key} onClick={() => setFilter(key)} className={`flex min-h-11 items-center justify-center gap-1.5 rounded-lg px-1 text-xs font-semibold transition ${filter === key ? 'bg-white/10 text-white' : 'muted hover:text-white'}`}><Icon size={15} />{label}<UnreadBadge count={unreadChats[key]} label={`${label}: непрочитанных чатов`} /></button>)}
          </div>
        </div>
        <div className="scrollbar flex-1 overflow-y-auto overflow-x-hidden p-2 pb-20">
          {filtered.length === 0 && <div className="px-4 py-12 text-center"><Inbox className="mx-auto mb-3 muted" /><p className="font-semibold">Здесь пока пусто</p><p className="mt-1 text-sm muted">Измените фильтр или поисковый запрос.</p></div>}
          {filtered.map((ticket) => <button key={ticket.number} onClick={() => selectTicket(ticket.number)} className={`mb-1 min-h-11 w-full rounded-xl border p-3 text-left transition ${selectedNumber === ticket.number ? 'border-teal-300/25 bg-teal-300/10' : 'border-transparent hover:bg-white/5'}`}>
            <div className="flex items-center justify-between gap-2"><span className="min-w-0 break-anywhere font-bold">{ticket.client}</span><span className="flex shrink-0 items-center gap-2"><UnreadBadge count={ticket.unread_count} label="Непрочитанных сообщений" /><span className="text-xs muted">{time(ticket.updated_at)}</span></span></div>
            <div className="mt-1 flex min-w-0 items-center gap-2"><span className="shrink-0 text-xs muted">#{ticket.number}</span><StatusPill ticket={ticket} data={data} /></div>
            <p className="mt-2 line-clamp-2 break-anywhere text-sm leading-5 muted">{ticket.title || ticket.description}</p>
          </button>)}
        </div>
      </aside>

      <section className={`min-h-0 min-w-0 flex-col bg-[oklch(0.175_0.012_255)] ${selected ? 'flex' : 'hidden md:flex'}`}>
        {selected ? <>
          <div className="panel-deep flex min-h-16 shrink-0 items-center justify-between gap-2 border-b px-2 sm:px-4">
            <div className="flex min-w-0 items-center gap-1"><button onClick={backToList} className="flex size-11 shrink-0 items-center justify-center rounded-lg muted md:hidden" aria-label="К списку"><ArrowLeft size={20} /></button><div className="min-w-0"><h1 className="truncate font-bold">{selected.title || selected.client}<span className="ml-2 font-medium muted">#{selected.number}</span></h1><p className="truncate text-xs muted">{selected.category} · {selected.employee || 'Ожидает оператора'}</p></div></div>
            <button onClick={() => setDetailsOpen(true)} className="flex size-11 shrink-0 items-center justify-center rounded-xl muted hover:bg-white/5 xl:hidden" aria-label="Параметры обращения"><MoreHorizontal size={20} /></button>
          </div>
          {!selected.employee_id ? <div className="flex flex-1 items-center justify-center overflow-y-auto p-6 text-center"><div className="min-w-0"><div className="soft-warning mx-auto mb-4 flex size-12 items-center justify-center rounded-2xl"><Clock3 /></div><h2 className="text-xl font-bold">Новое обращение</h2><p className="mx-auto mt-2 max-w-md break-anywhere whitespace-pre-wrap text-sm leading-6 muted">{selected.description}</p>{(selected.media_url || selected.media_type) && <div className="mx-auto mt-3 max-w-md text-left"><MediaAttachment mediaUrl={selected.media_url} mediaType={selected.media_type} label="Вложение к первичному обращению" /></div>}<button disabled={busy} onClick={() => void claim()} className="accent-bg mt-5 min-h-11 rounded-xl px-6 font-bold">Принять обращение</button></div></div> : <>
            <div className="scrollbar flex-1 overflow-y-auto overflow-x-hidden p-3 sm:p-6">
              <div className="mx-auto min-w-0 max-w-3xl">
                <div className="mb-6 rounded-xl border border-white/10 bg-white/[0.025] p-4"><div className="mb-2 text-xs font-bold uppercase tracking-wider muted">Первичное обращение</div><p className="break-anywhere whitespace-pre-wrap text-sm leading-6">{selected.description}</p>{(selected.media_url || selected.media_type) && <MediaAttachment mediaUrl={selected.media_url} mediaType={selected.media_type} label="Вложение к первичному обращению" />}</div>
                {loadingMessages && messages.length === 0 ? <LoaderCircle className="mx-auto animate-spin muted" /> : messages.map((message) => <article key={message.id} className={`mb-3 max-w-[92%] rounded-2xl px-4 py-3 sm:max-w-[78%] ${message.from_staff ? 'message-staff ml-auto rounded-br-md' : 'message-client rounded-bl-md'}`}>
                  <div className="mb-1 flex min-w-0 items-center justify-between gap-3 text-xs"><span className="min-w-0 break-anywhere font-bold">{message.from_staff ? selected.employee || 'Оператор' : selected.client}</span><span className="shrink-0 muted">{time(message.created_at)}</span></div>
                  {(message.media_url || message.media_label || message.content_type !== 'text') && <MediaAttachment mediaUrl={message.media_url} mediaType={message.content_type} fileName={message.file_name} mimeType={message.mime_type} size={message.file_size} label={message.media_label} legacy={message.legacy} />}
                  {editing === message.id ? <div><textarea value={editText} disabled={busy} maxLength={message.content_type === 'text' ? 4096 : 1024} aria-label={message.content_type === 'text' ? 'Текст сообщения' : 'Подпись к медиа'} onChange={(event) => setEditText(event.target.value)} rows={3} className="panel-deep w-full rounded-lg border p-3 text-base sm:text-sm" /><div className="mt-2 flex gap-2"><button disabled={busy || (message.content_type === 'text' && !editText.trim())} onClick={() => void saveEdit(message.id)} className="accent-bg min-h-11 rounded-lg px-4 text-xs font-bold">Сохранить</button><button disabled={busy} onClick={() => setEditing(null)} className="min-h-11 rounded-lg bg-white/10 px-4 text-xs">Отмена</button></div></div> : (message.body || !message.media_url) && <p className={`break-anywhere whitespace-pre-wrap text-sm leading-6 ${message.legacy ? 'italic muted' : ''}`}>{message.body}</p>}
                  {message.from_staff && !message.legacy && editing !== message.id && <div className="mt-1 flex justify-end gap-1"><button disabled={busy} onClick={() => { setEditing(message.id); setEditText(message.body) }} className="flex size-11 items-center justify-center rounded-lg muted hover:bg-white/10 hover:text-white" aria-label={message.content_type === 'text' ? 'Изменить сообщение' : 'Изменить подпись'}><Pencil size={16} /></button><button disabled={busy} onClick={() => void removeMessage(message.id)} className="flex size-11 items-center justify-center rounded-lg muted hover:bg-red-400/10 hover:text-red-200" aria-label="Удалить сообщение"><Trash2 size={16} /></button></div>}
                </article>)}
                <div ref={endRef} />
              </div>
            </div>
            <form onSubmit={send} className="composer-safe panel-deep shrink-0 border-t p-3 sm:p-4">
              {isClosed && <p className="mx-auto mb-2 max-w-3xl text-xs muted">Обращение закрыто. Смените статус, чтобы ответить клиенту.</p>}
              {attachment && <div className="composer-attachment mx-auto mb-2 max-w-3xl rounded-xl bg-white/5 p-3 text-xs">
                <LocalFilePreview file={attachment} />
                <div className="mt-2 flex flex-wrap justify-end gap-2">
                  {isEditableMedia(attachment) && <button type="button" onClick={() => setMediaEditorOpen(true)} disabled={busy} className="flex min-h-11 items-center gap-2 rounded-lg bg-white/10 px-4 font-semibold hover:bg-white/15"><Pencil size={16} />Изменить</button>}
                  <button type="button" onClick={clearAttachment} disabled={busy} className="flex min-h-11 items-center gap-2 rounded-lg bg-white/5 px-4 muted hover:bg-white/10 hover:text-white"><X size={16} />Убрать</button>
                </div>
              </div>}
              <div className="mx-auto flex max-w-3xl items-end gap-1 sm:gap-2">
                <input ref={fileInputRef} type="file" disabled={isClosed || busy} className="hidden" tabIndex={-1} onChange={(event) => {
                  const file = event.target.files?.[0] || null
                  setAttachment(file)
                  setMediaEditorOpen(Boolean(file && isEditableMedia(file)))
                }} />
                <button type="button" disabled={busy || isClosed} onClick={() => setQuickReplyPickerOpen(true)} className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-white/10 muted transition hover:bg-white/15 hover:text-white" aria-label="Быстрые ответы"><MessageSquareText size={19} /></button>
                <button type="button" disabled={busy || isClosed} onClick={() => fileInputRef.current?.click()} className="flex size-12 shrink-0 items-center justify-center rounded-xl bg-white/10 muted transition hover:bg-white/15 hover:text-white" aria-label="Прикрепить файл"><Paperclip size={19} /></button>
                <textarea ref={composerRef} aria-label={attachment ? 'Подпись к файлу' : 'Сообщение клиенту'} value={draft} disabled={isClosed || busy} maxLength={attachment ? 1024 : 4096} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => {
                  if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && window.matchMedia('(min-width: 768px) and (pointer: fine)').matches) { event.preventDefault(); void send(event) }
                }} rows={1} placeholder={isClosed ? 'Переписка недоступна' : attachment ? 'Подпись к файлу (необязательно)…' : 'Написать клиенту…'} className="panel max-h-40 min-h-12 min-w-0 flex-1 resize-none overflow-y-auto rounded-xl border px-4 py-3 text-base placeholder:text-slate-500 sm:text-sm" /><button disabled={busy || isClosed || (!attachment && !draft.trim()) || Boolean(attachment && draft.length > 1024)} type="submit" className="accent-bg flex size-12 shrink-0 items-center justify-center rounded-xl transition hover:brightness-110" aria-label="Отправить"><Send size={19} /></button>
              </div>
            </form>
          </>}
        </> : <div className="flex flex-1 items-center justify-center p-6 text-center muted"><div><FilePenLine className="mx-auto mb-3" size={32} /><p>Выберите обращение, чтобы открыть переписку</p></div></div>}
      </section>

      {selected && <aside className="panel scrollbar hidden min-h-0 min-w-0 overflow-y-auto overflow-x-hidden border-l p-5 xl:block"><TicketDetails key={selected.number} ticket={selected} data={data} onUpdate={updateTicket} onClaim={claim} onPin={pin} onReminder={setReminder} busy={busy} /></aside>}
    </div>

    {detailsOpen && selected && <Modal title={`Обращение #${selected.number}`} onClose={() => setDetailsOpen(false)}><TicketDetails key={selected.number} ticket={selected} data={data} onUpdate={updateTicket} onClaim={claim} onPin={pin} onReminder={setReminder} busy={busy} /></Modal>}
    {quickRepliesOpen && <Modal title="Быстрые ответы" wide onClose={() => setQuickRepliesOpen(false)}><QuickRepliesManager /></Modal>}
    {quickReplyPickerOpen && selected?.employee_id && <Modal title="Быстрые ответы" wide onClose={() => setQuickReplyPickerOpen(false)}><QuickReplyPicker ticketNumber={selected.number} onSent={(sentMessages) => { setMessages((current) => [...current, ...sentMessages]); setQuickReplyPickerOpen(false); notify('Быстрый ответ отправлен') }} /></Modal>}
    {employeesOpen && data.employee.admin && <Modal title="Управление сотрудниками" wide onClose={() => setEmployeesOpen(false)}><EmployeesManager onChanged={() => load(true)} currentTelegramId={data.employee.telegram_id} /></Modal>}
    {profileOpen && <Modal title="Профиль сотрудника" onClose={() => setProfileOpen(false)}>{profileLoading ? <div className="py-10 text-center"><LoaderCircle className="mx-auto animate-spin accent" /><p className="mt-3 text-sm muted">Загружаем профиль…</p></div> : profileError ? <div className="py-6 text-center"><p className="break-anywhere text-sm text-red-200">{profileError}</p><button onClick={() => void openProfile()} className="mt-4 min-h-11 rounded-xl bg-white/10 px-5 text-sm font-semibold">Повторить</button></div> : profile ? <ProfileContent key={profile.greeting} profile={profile} busy={busy} onSave={saveGreeting} /> : null}</Modal>}
    {faqOpen && <Modal title="FAQ для клиентов" wide onClose={() => setFaqOpen(false)}><FaqEditor initial={data.faq} busy={busy} onSave={async (items) => { await run(async () => { await api('/faq', { method: 'PUT', body: JSON.stringify({ faq: items }) }); setFaqOpen(false) }, 'FAQ обновлён') }} /></Modal>}
    {mediaEditorOpen && attachment && isEditableMedia(attachment) && <MediaEditorDialog file={attachment} onClose={() => setMediaEditorOpen(false)} onApply={(file) => {
      setAttachment(file)
      setMediaEditorOpen(false)
      notify('Медиа обновлено')
    }} />}
  </main>
}
