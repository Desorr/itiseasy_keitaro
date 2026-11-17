export type CampaignInput = { name: string; geo_code: string; offer_id: number }
export type Offer = { id: number; name: string }
export type Campaign = {
  id: number
  name: string
  alias: string
  state: string
  domain_id: number | null
  group_id: number | null
  traffic_source_id: number | null
  geo_code: string | null
  offer_id: number | null
  google_stream_id: number | null
  offer_stream_id: number | null
  tracking_url: string | null
  admin_url: string
}

export class ApiError extends Error {
  constructor(
    message: string, public status: number, public uncertain = false, public campaignId: number | null = null,
  ) { super(message) }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  let data
  try { data = await response.json() } catch {
    throw new ApiError('Сервер вернул некорректный ответ.', response.status, init?.method === 'POST')
  }
  if (!response.ok) {
    const detail = data?.detail
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map((item: { msg: string }) => item.msg).join(' ')
      : detail?.message || 'Не удалось выполнить запрос.'
    throw new ApiError(message, response.status, detail?.uncertain ?? (init?.method === 'POST' && response.status >= 500), detail?.campaign_id ?? null)
  }
  return data as T
}
