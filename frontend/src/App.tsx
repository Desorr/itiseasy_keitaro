import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { ApiError, loadAvailableOffers, request } from './api'
import CampaignEditor from './CampaignEditor'
import { countryCodes } from './countries'
import type { Campaign, CampaignInput, Offer } from './api'

const STORAGE_KEY = 'keitaro.creation-request'

function readPending(): { id: string; payload: CampaignInput } | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || 'null')
    if (value && typeof value.id === 'string' && typeof value.payload?.name === 'string'
      && typeof value.payload?.geo_code === 'string' && Number.isInteger(value.payload?.offer_id)) return value
  } catch { /* Storage may be unavailable; the form still works. */ }
  return null
}

function clearPending() {
  try { sessionStorage.removeItem(STORAGE_KEY) } catch { /* Optional recovery. */ }
}

export default function App() {
  const [pending] = useState(readPending)
  const [name, setName] = useState(pending?.payload.name || '')
  const [geo, setGeo] = useState(pending?.payload.geo_code || '')
  const [offerText, setOfferText] = useState(pending ? String(pending.payload.offer_id) : '')
  const [selectedOffer, setSelectedOffer] = useState<Offer | null>(null)
  const [allOffers, setAllOffers] = useState<Offer[] | null>(null)
  const offersRequest = useRef<Promise<Offer[]> | null>(null)
  const [focused, setFocused] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const [loadingOffers, setLoadingOffers] = useState(false)
  const [offerError, setOfferError] = useState('')
  const [error, setError] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<keyof CampaignInput, string>>>({})

  function clearFieldError(field: keyof CampaignInput) {
    setFieldErrors((current) => ({ ...current, [field]: undefined }))
  }
  const [configurationError, setConfigurationError] = useState('')
  const [busy, setBusy] = useState(false)
  const [requestId, setRequestId] = useState<string | null>(pending?.id || null)
  const [campaigns, setCampaigns] = useState<Campaign[]>([])
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [showForm, setShowForm] = useState(true)
  const [editing, setEditing] = useState(false)
  const [loadingCampaigns, setLoadingCampaigns] = useState(true)
  const [campaignsError, setCampaignsError] = useState('')
  const [syncingCampaigns, setSyncingCampaigns] = useState(false)
  const campaignVersion = useRef(0)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const locked = busy || loadingCampaigns || requestId !== null
  const creating = showForm || campaigns.length === 0
  const selectedCampaign = campaigns.find((campaign) => campaign.id === selectedId) || campaigns[0]

  async function loadCampaigns(signal?: AbortSignal, initial = false) {
    const version = ++campaignVersion.current
    setSyncingCampaigns(true)
    if (initial) setLoadingCampaigns(true)
    setCampaignsError('')
    try {
      if (initial) {
        try {
          const cached = await request<Campaign[]>('/api/campaigns', { signal })
          if (signal?.aborted || version !== campaignVersion.current) return
          setCampaigns(cached)
        } catch { /* Synchronization below can recover a failed database read. */ }
        if (signal?.aborted || version !== campaignVersion.current) return
        setLoadingCampaigns(false)
      }
      const data = await request<Campaign[]>('/api/campaigns/sync', { method: 'POST', signal })
      if (signal?.aborted || version !== campaignVersion.current) return
      setCampaigns(data)
      setConfirmingDelete(false)
      if (requestId) {
        const alias = `tm_${requestId.replaceAll('-', '').slice(0, 16)}`
        const recovered = data.find((campaign) => campaign.alias === alias)
        if (recovered) {
          setSelectedId(recovered.id)
          setShowForm(false)
          setRequestId(null)
          setError('')
          clearPending()
        } else {
          setError('Campaign not found yet. Check Keitaro before creating another campaign.')
        }
      }
    } catch (error) {
      if (!signal?.aborted && version === campaignVersion.current) {
        setCampaignsError(error instanceof Error ? error.message : 'Failed to load campaigns.')
        try {
          const cached = await request<Campaign[]>('/api/campaigns', { signal })
          if (!signal?.aborted && version === campaignVersion.current) setCampaigns(cached)
        } catch { /* Keep the synchronization error. */ }
      }
    } finally { if (!signal?.aborted) { setLoadingCampaigns(false); setSyncingCampaigns(false) } }
  }

  useEffect(() => {
    const controller = new AbortController()
    void loadCampaigns(controller.signal, true)
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    request<{ configured: boolean; missing_configuration: string[] }>('/api/health', { signal: controller.signal })
      .then((health) => {
        if (!health.configured) setConfigurationError(`Configure the backend: ${health.missing_configuration.join(', ')}.`)
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) setConfigurationError(error instanceof Error ? error.message : 'Backend unavailable.')
      })
    return () => controller.abort()
  }, [])

  useEffect(() => {
    void request<{ ready: boolean }>('/api/campaigns/preparation').catch(() => {
      // Optional warmup; creation reports any preparation errors.
    })
  }, [])

  const offers = useMemo(() => {
    const needle = (selectedOffer ? String(selectedOffer.id) : offerText).trim().toLocaleLowerCase()
    return (allOffers || [])
      .filter((offer) => String(offer.id).includes(needle))
      .sort((a, b) => Number(String(b.id) === needle) - Number(String(a.id) === needle)
        || a.name.localeCompare(b.name))
      .slice(0, 30)
  }, [allOffers, offerText, selectedOffer])

  useEffect(() => {
    if (!creating) return
    const controller = new AbortController()
    setAllOffers(null)
    setSelectedOffer(null)
    setActiveIndex(-1)
    void loadOffers(controller.signal, true)
    return () => controller.abort()
  }, [creating])

  async function loadOffers(signal?: AbortSignal, force = false): Promise<Offer[] | null> {
    if (!force && allOffers !== null) return allOffers
    if (!force && offersRequest.current) {
      try { return await offersRequest.current } catch { return null }
    }
    setLoadingOffers(true)
    setOfferError('')
    const pendingRequest = loadAvailableOffers(signal)
    offersRequest.current = pendingRequest
    try {
      const data = await pendingRequest
      if (signal?.aborted || offersRequest.current !== pendingRequest) return null
      setAllOffers(data)
      setActiveIndex(-1)
      return data
    } catch (error) {
      if (!signal?.aborted && offersRequest.current === pendingRequest) {
        setOfferError(error instanceof Error ? error.message : 'Failed to load offers. Try again.')
      }
      return null
    } finally {
      if (offersRequest.current === pendingRequest) {
        offersRequest.current = null
        if (!signal?.aborted) setLoadingOffers(false)
      }
    }
  }

  function pickOffer(offer: Offer) {
    setSelectedOffer(offer)
    clearFieldError('offer_id')
    setOfferText(`[${offer.id}] ${offer.name}`)
    setFocused(false)
    setActiveIndex(-1)
  }

  useEffect(() => {
    const dialog = document.getElementById('delete-campaign-dialog') as HTMLDialogElement | null
    if (confirmingDelete) dialog?.showModal()
    else dialog?.close()
  }, [confirmingDelete])

  function handleOfferKey(event: KeyboardEvent<HTMLInputElement>) {
    if (selectedOffer && (event.key === 'Backspace' || event.key === 'Delete')) {
      event.preventDefault()
      setSelectedOffer(null)
      setOfferText('')
      clearFieldError('offer_id')
      setFocused(true)
      setActiveIndex(-1)
      return
    }
    if (event.key === 'Escape') { setFocused(false); return }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setFocused(true)
      setActiveIndex((index) => offers.length ? (index + (event.key === 'ArrowDown' ? 1 : -1) + offers.length) % offers.length : -1)
    }
    if (event.key === 'Enter' && focused && offers[activeIndex]) {
      event.preventDefault()
      pickOffer(offers[activeIndex])
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (locked) return
    const offerId = selectedOffer?.id || 0
    const geoCode = geo.trim().toUpperCase()
    const errors: Partial<Record<keyof CampaignInput, string>> = {}
    if (!name.trim()) errors.name = 'Enter a campaign name.'
    else if ([...name.trim()].length < 5) errors.name = 'The name must contain at least 5 characters.'
    else if ([...name.trim()].length > 255) errors.name = 'The name must contain no more than 255 characters.'
    else if (/[\u0000-\u001f\u007f-\u009f]/u.test(name)) errors.name = 'The name contains invalid control characters.'
    if (geoCode && /[^A-Z]/.test(geoCode)) errors.geo_code = 'The GEO code supports only letters A–Z.'
    else if (!countryCodes.has(geoCode)) errors.geo_code = 'Enter one valid country code, for example DE.'
    if (!selectedOffer) errors.offer_id = 'Select an offer from the dropdown. Typing is only used for search.'
    setError('')
    setFieldErrors(errors)
    if (Object.keys(errors).length) {
      document.getElementById(errors.name ? 'campaign-name' : errors.geo_code ? 'geo' : 'offer')?.focus()
      return
    }
    campaignVersion.current += 1
    setBusy(true)
    const availableOffers = allOffers ?? await loadOffers()
    if (!availableOffers || !availableOffers.some((offer) => offer.id === offerId)) {
      setBusy(false)
      setFieldErrors({ offer_id: availableOffers ? 'This offer ID is not in the available offers list.' : 'Failed to verify the offer. Reload the list.' })
      document.getElementById('offer')?.focus()
      return
    }
    const payload: CampaignInput = { name: name.trim(), geo_code: geoCode, offer_id: offerId, offer_name: selectedOffer!.name }
    const id = crypto.randomUUID()
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ id, payload })) } catch { /* Optional recovery. */ }
    setRequestId(id)
    setError('')
    campaignVersion.current += 1
    setBusy(true)
    setFocused(false)
    try {
      const campaign = await request<Campaign>('/api/campaigns', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': id },
        body: JSON.stringify(payload),
      })
      setCampaigns((current) => [campaign, ...current.filter((item) => item.id !== campaign.id)])
      setSelectedId(campaign.id)
      setShowForm(false)
      setRequestId(null)
      clearPending()
    } catch (error) {
      if (error instanceof ApiError && !error.uncertain && error.campaignId === null) {
        setRequestId(null)
        clearPending()
      }
      setError(error instanceof Error ? error.message : 'Connection interrupted. Refresh the campaign list before creating another campaign.')
    } finally { setBusy(false) }
  }

  function clearCreationForm() {
    setName('')
    setGeo('')
    setOfferText('')
    setSelectedOffer(null)
    setAllOffers(null)
    setFieldErrors({})
    setOfferError('')
    setError('')
    setFocused(false)
    setActiveIndex(-1)
    setLoadingOffers(false)
    offersRequest.current = null
  }

  function reset() {
    setFieldErrors({})
    setRequestId(null)
    setError('')
    setName('')
    setGeo('')
    setOfferText('')
    setSelectedOffer(null)
    setFocused(false)
    setShowForm(true)
    setEditing(false)
    setConfirmingDelete(false)
    clearPending()
  }

  async function deleteCampaign() {
    if (!selectedCampaign || busy) return
    const campaign = selectedCampaign
    campaignVersion.current += 1
    setBusy(true)
    setError('')
    try {
      await request<{ message: string }>(`/api/campaigns/${campaign.id}`, { method: 'DELETE' })
      setCampaigns((current) => current.filter((item) => item.id !== campaign.id))
      setSelectedId(null)
      setConfirmingDelete(false)
      setName('')
      setGeo('')
      setOfferText('')
      setSelectedOffer(null)
      setFieldErrors({})
      setFocused(false)
      setActiveIndex(-1)
      if (requestId && campaign.alias === `tm_${requestId.replaceAll('-', '').slice(0, 16)}`) {
        setRequestId(null)
        clearPending()
      }
    } catch (error) {
      setConfirmingDelete(false)
      setError(error instanceof Error ? error.message : 'Failed to archive the campaign. Refresh the list and check the result.')
    } finally { setBusy(false) }
  }

  return (
    <main className="with-campaigns">
      <aside className="campaign-sidebar">
        <div className="campaign-sidebar-header">
          <h2>My campaigns</h2>
          <button type="button" className="secondary refresh-campaigns" disabled={busy || loadingCampaigns || syncingCampaigns}
            onClick={() => void loadCampaigns()}>Refresh</button>
        </div>
        {loadingCampaigns && <p className="muted" role="status">Loading campaigns…</p>}
        {!loadingCampaigns && syncingCampaigns && <p className="muted" role="status">Syncing with Keitaro…</p>}
        {campaignsError && <p className="notice error" role="alert">{campaignsError}</p>}
        <nav aria-label="My campaigns">
          {campaigns.map((campaign) => <button key={campaign.id} type="button"
            className={`campaign-item ${!creating && selectedCampaign?.id === campaign.id ? 'selected' : ''}`}
            aria-current={!creating && selectedCampaign?.id === campaign.id ? 'page' : undefined}
            disabled={busy || loadingCampaigns}
            onClick={() => { clearCreationForm(); setSelectedId(campaign.id); setShowForm(false); setEditing(false); setConfirmingDelete(false) }}>
            <span>{campaign.name}</span>
            <small>ID {campaign.id}</small>
          </button>)}
        </nav>
      </aside>
      <div className="campaign-content">
        <header className="page-header">
          <h1>{creating ? 'Create campaign' : `${editing ? 'Editing: ' : ''}${selectedCampaign.name}`}</h1>
        </header>
        {configurationError && <p className="notice error" role="alert">{configurationError}</p>}
        {creating && <form onSubmit={submit} noValidate>
        <label htmlFor="campaign-name">Campaign name</label>
        <input id="campaign-name" value={name} onChange={(event) => { setName(event.target.value); clearFieldError('name') }}
          aria-invalid={Boolean(fieldErrors.name)} aria-describedby={fieldErrors.name ? 'name-error' : undefined}
          required maxLength={255} disabled={locked} placeholder="Example: test_secondname_campaign1" />

        {fieldErrors.name && <small id="name-error" className="field-error" role="alert">{fieldErrors.name}</small>}

        <label htmlFor="geo">Country GEO code</label>
        <input id="geo" value={geo} onChange={(event) => { setGeo(event.target.value.toUpperCase()); clearFieldError('geo_code') }}
          aria-invalid={Boolean(fieldErrors.geo_code)} aria-describedby={fieldErrors.geo_code ? 'geo-error' : undefined}
          required maxLength={2} pattern="[A-Za-z]{2}" disabled={locked} placeholder="DE, AU, BY, ..." />

        {fieldErrors.geo_code && <small id="geo-error" className="field-error" role="alert">{fieldErrors.geo_code}</small>}

        <label htmlFor="offer">Offer ID</label>
        <div className="offer-picker" onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false)
        }}>
          <input id="offer" role="combobox" aria-autocomplete="list" aria-expanded={focused && !locked}
            aria-controls="offer-list" aria-activedescendant={activeIndex >= 0 && offers[activeIndex] ? `offer-${offers[activeIndex].id}` : undefined}
            value={offerText} onChange={(event) => { setOfferText(selectedOffer ? '' : event.target.value.replace(/[^0-9]/g, '')); setSelectedOffer(null); clearFieldError('offer_id'); setFocused(true); setActiveIndex(-1) }}
            onFocus={() => { setFocused(true); void loadOffers() }} onKeyDown={handleOfferKey}
            aria-invalid={Boolean(fieldErrors.offer_id)} aria-describedby={fieldErrors.offer_id ? 'offer-error' : undefined}
            required disabled={locked} placeholder="Enter offer ID" inputMode="numeric" autoComplete="off" />
          {focused && !locked && <div className="suggestions">
            {loadingOffers && <p role="status">Loading offers…</p>}
            {!loadingOffers && !offerError && offers.length === 0 && <p>No offers found.</p>}
            <div id="offer-list" role="listbox" aria-label="Available offers">
              {offers.map((offer, index) => <button key={offer.id} id={`offer-${offer.id}`} type="button" role="option"
                aria-selected={selectedOffer?.id === offer.id || activeIndex === index} className={activeIndex === index ? 'active' : ''}
                onPointerDown={(event) => {
                  if (event.button === 0 && event.pointerType === 'mouse') {
                    event.preventDefault()
                    pickOffer(offer)
                  }
                }} onClick={() => pickOffer(offer)}>
                [{offer.id}] {offer.name}
              </button>)}
            </div>
          </div>}
        </div>
        {offerError && <div className="field-error" role="alert">
          {offerError} <button type="button" className="secondary" disabled={locked || loadingOffers}
            onClick={() => void loadOffers(undefined, true)}>Retry</button>
        </div>}
        {fieldErrors.offer_id && <small id="offer-error" className="field-error" role="alert">{fieldErrors.offer_id}</small>}
        {error && <p className="notice error" role="alert">{error}</p>}
          <div className="actions">
            <button type="submit" disabled={locked || Boolean(configurationError)}>
              {busy ? 'Creating…' : 'Create campaign'}
            </button>
            {requestId && !busy && <>
              <button type="button" className="secondary" disabled={busy || loadingCampaigns || syncingCampaigns} onClick={() => void loadCampaigns()}>Refresh campaign list</button>
              <button type="button" className="secondary" onClick={reset}>New campaign</button>
            </>}
          </div>
        </form>}
        {!creating && editing && <CampaignEditor key={selectedCampaign.id} campaignId={selectedCampaign.id}
          onBusy={setBusy} onBack={() => {
            setEditing(false)
            void request<Campaign[]>('/api/campaigns').then(setCampaigns).catch((error: unknown) => {
              setCampaignsError(error instanceof Error ? error.message : 'Failed to refresh campaign details.')
            })
          }} />}
        {!creating && !editing && <section className={`result ${selectedCampaign.state === 'active' ? 'succeeded' : ''}`}>
          <h2>{selectedCampaign.state === 'active' ? 'Campaign active' : 'Campaign disabled'}</h2>
          <p>Campaign ID: <strong>{selectedCampaign.id}</strong></p>
          {selectedCampaign.state !== 'active' && <p>Check the campaign configuration in Keitaro before enabling it.</p>}
        {error && <p className="notice error" role="alert">{error}</p>}
          <div className="actions">
            <button type="button" disabled={busy || loadingCampaigns}
              onClick={() => { setEditing(true); setError('') }}>Edit</button>
            <button type="button" className="danger" disabled={busy || loadingCampaigns}
              onClick={() => { setConfirmingDelete(true); setError('') }}>Delete campaign</button>
          </div>
          <dialog id="delete-campaign-dialog" className="delete-dialog" aria-label="Delete campaign"
            onCancel={(event) => { event.preventDefault(); if (!busy) setConfirmingDelete(false) }}>
            <div className="actions">
              <button type="button" className="danger" disabled={busy} onClick={() => void deleteCampaign()}>
                {busy ? 'Deleting…' : 'Remove'}
              </button>
              <button type="button" className="secondary" disabled={busy} onClick={() => setConfirmingDelete(false)}>Cancel</button>
            </div>
          </dialog>
        </section>}
      </div>
      {!creating && !editing && <button type="button" className="create-campaign" disabled={busy || loadingCampaigns}
        onClick={reset}>Add campaign</button>}
    </main>
  )
}
