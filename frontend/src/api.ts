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
  offer_ids: number[]
  google_stream_id: number | null
  offer_stream_id: number | null
  tracking_url: string | null
  admin_url: string
}

export class ApiError extends Error {
  constructor(
    message: string, public status: number, public uncertain = false, public campaignId: number | null = null,
    public code: string | null = null,
  ) { super(message) }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const mutating = Boolean(init?.method && !['GET', 'HEAD'].includes(init.method))
  let response: Response
  try { response = await fetch(path, init) } catch (error) {
    if (init?.signal?.aborted) throw error
    throw new ApiError('Connection interrupted. Refresh the data before continuing.', 0, mutating)
  }
  let data
  try { data = await response.json() } catch {
    throw new ApiError('The server returned an invalid response.', response.status, mutating)
  }
  if (!response.ok) {
    const detail = data?.detail
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map((item: { msg: string }) => item.msg).join(' ')
      : detail?.message || 'Request failed.'
    throw new ApiError(message, response.status, detail?.uncertain ?? (mutating && response.status >= 500), detail?.campaign_id ?? null, detail?.code ?? null)
  }
  return data as T
}

export type StreamOffer = {
  offer_id: number; name: string; share: number; state: string; pinned: boolean; removed: boolean
}
export type Stream = {
  id: number; name: string; position: number; revision: number; editable: boolean; missing: boolean
  dirty: boolean; conflict: boolean; publication: 'idle' | 'publishing' | 'uncertain'; offers: StreamOffer[]
  allocation_error: string | null
}
export type DraftAction = 'add' | 'remove' | 'restore' | 'toggle_pin'

let availableOffers: Promise<Offer[]> | null = null
export function loadAvailableOffers(): Promise<Offer[]> {
  availableOffers ??= request<Offer[]>('/api/offers').catch((error: unknown) => {
    availableOffers = null
    throw error
  })
  return availableOffers
}
