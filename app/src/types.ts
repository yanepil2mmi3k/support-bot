export type Status = {
  key: string
  title: string
  code: string
  description?: string
  closed?: boolean
  archive?: boolean
  pauses_reminders?: boolean
}

export type TicketReminder = {
  active: boolean
  next_run_at: string | null
}

export type Ticket = {
  id: number
  number: number
  user_id: number | null
  username: string | null
  client: string
  category: string
  description: string
  note: string
  title: string | null
  status: string
  thread_id: number | null
  employee_id: number | null
  employee: string | null
  created_at: string
  updated_at: string
  last_user_message_at: string | null
  last_support_message_at: string | null
  unread_count: number
  media_url: string | null
  media_type: string | null
  reminder: TicketReminder | null
}

export type ChatMessage = {
  id: number
  body: string
  content_type: string
  media_label: string | null
  media_url: string | null
  thumbnail_url: string | null
  file_name: string | null
  mime_type: string | null
  file_size: number | null
  from_staff: boolean
  created_at: string
  legacy: boolean
}

export type FaqItem = { id: string; question: string; answer: string }
export type QuickReplyItem = {
  id: number
  position: number
  text: string
  content_type: string
  media_url: string | null
  file_name: string | null
  mime_type: string | null
  file_size: number | null
}
export type QuickReply = {
  id: number
  title: string
  text: string
  items: QuickReplyItem[]
}
export type EmployeeSummary = {
  id: number
  name: string
  nickname: string
  active?: boolean
}
export type Employee = EmployeeSummary & {
  telegram_id: number
  gender: string
  greeting: string
  active: boolean
  schedule: string
}

export type Profile = {
  name: string
  nickname: string
  gender: string
  schedule: string
  greeting: string
  ratings_visible: boolean
  reviews: {
    ticket_number: number
    rating: number
    comment: string | null
    created_at: string
  }[]
}

export type Bootstrap = {
  employee: EmployeeSummary & { telegram_id: number; admin: boolean }
  tickets: Ticket[]
  statuses: Status[]
  employees: EmployeeSummary[]
  faq: FaqItem[]
}
