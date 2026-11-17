import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { ApiError, request } from './api'
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
  const [loadingCampaigns, setLoadingCampaigns] = useState(true)
  const [campaignsError, setCampaignsError] = useState('')
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const locked = busy || loadingCampaigns || requestId !== null
  const creating = showForm || campaigns.length === 0
  const selectedCampaign = campaigns.find((campaign) => campaign.id === selectedId) || campaigns[0]

  async function loadCampaigns(signal?: AbortSignal) {
    setLoadingCampaigns(true)
    setCampaignsError('')
    try {
      const data = await request<Campaign[]>('/api/campaigns', { signal })
      if (signal?.aborted) return
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
          setError('Кампания пока не найдена. Проверьте результат в Keitaro перед новым созданием.')
        }
      }
    } catch (error) {
      if (!signal?.aborted) setCampaignsError(error instanceof Error ? error.message : 'Не удалось загрузить кампании.')
    } finally { if (!signal?.aborted) setLoadingCampaigns(false) }
  }

  useEffect(() => {
    const controller = new AbortController()
    void loadCampaigns(controller.signal)
    return () => controller.abort()
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    request<{ configured: boolean; missing_configuration: string[] }>('/api/health', { signal: controller.signal })
      .then((health) => {
        if (!health.configured) setConfigurationError(`Настройте backend: ${health.missing_configuration.join(', ')}.`)
      }).catch((error: unknown) => {
        if (!controller.signal.aborted) setConfigurationError(error instanceof Error ? error.message : 'Backend недоступен.')
      })
    return () => controller.abort()
  }, [])

  const offers = useMemo(() => {
    const needle = (selectedOffer ? String(selectedOffer.id) : offerText).trim().toLocaleLowerCase()
    return (allOffers || [])
      .filter((offer) => String(offer.id).includes(needle))
      .sort((a, b) => Number(String(b.id) === needle) - Number(String(a.id) === needle)
        || a.name.localeCompare(b.name))
      .slice(0, 30)
  }, [allOffers, offerText, selectedOffer])

  async function loadOffers() {
    if (allOffers !== null) return
    setLoadingOffers(true)
    setOfferError('')
    try {
      offersRequest.current ??= request<Offer[]>('/api/offers')
      const data = await offersRequest.current
      setAllOffers(data)
      setActiveIndex(-1)
    } catch (error) {
      offersRequest.current = null
      setOfferError(error instanceof Error ? error.message : 'Ошибка загрузки офферов. Выберите поле ещё раз, чтобы повторить.')
    } finally { setLoadingOffers(false) }
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
    if (!name.trim()) errors.name = 'Введите название кампании.'
    else if ([...name.trim()].length < 5) errors.name = 'Название должно содержать не меньше 5 символов.'
    else if ([...name.trim()].length > 255) errors.name = 'Название должно содержать не больше 255 символов.'
    else if (/[\u0000-\u001f\u007f-\u009f]/u.test(name)) errors.name = 'Название содержит недопустимые управляющие символы.'
    if (geoCode && /[^A-Z]/.test(geoCode)) errors.geo_code = 'GEO-код поддерживает только буквы A–Z.'
    else if (!countryCodes.has(geoCode)) errors.geo_code = 'Укажите один существующий код страны: например, DE.'
    if (!selectedOffer) errors.offer_id = 'Выберите оффер из выпадающего списка. Ввод используется только для поиска.'
    setError('')
    setFieldErrors(errors)
    if (Object.keys(errors).length) {
      document.getElementById(errors.name ? 'campaign-name' : errors.geo_code ? 'geo' : 'offer')?.focus()
      return
    }
    setBusy(true)
    if (allOffers === null) await loadOffers()
    let availableOffers = allOffers
    try { availableOffers ??= await offersRequest.current } catch { /* loadOffers displays the error. */ }
    if (!availableOffers || !availableOffers.some((offer) => offer.id === offerId)) {
      setBusy(false)
      setFieldErrors({ offer_id: availableOffers ? 'Оффер с таким ID отсутствует в списке доступных.' : 'Не удалось проверить оффер. Повторите загрузку списка.' })
      document.getElementById('offer')?.focus()
      return
    }
    const payload: CampaignInput = { name: name.trim(), geo_code: geoCode, offer_id: offerId }
    const id = crypto.randomUUID()
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ id, payload })) } catch { /* Optional recovery. */ }
    setRequestId(id)
    setError('')
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
      setError(error instanceof Error ? error.message : 'Соединение прервано. Обновите список кампаний перед новым созданием.')
    } finally { setBusy(false) }
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
    setConfirmingDelete(false)
    clearPending()
  }

  async function deleteCampaign() {
    if (!selectedCampaign || busy) return
    const campaign = selectedCampaign
    setBusy(true)
    setError('')
    try {
      await request<{ message: string }>(`/api/campaigns/${campaign.id}`, { method: 'DELETE' })
      setCampaigns((current) => current.filter((item) => item.id !== campaign.id))
      setSelectedId(null)
      setConfirmingDelete(false)
      if (requestId && campaign.alias === `tm_${requestId.replaceAll('-', '').slice(0, 16)}`) {
        setRequestId(null)
        clearPending()
      }
    } catch (error) {
      setConfirmingDelete(false)
      setError(error instanceof Error ? error.message : 'Не удалось удалить кампанию. Обновите список и проверьте результат.')
    } finally { setBusy(false) }
  }

  return (
    <main className="with-campaigns">
      <aside className="campaign-sidebar">
        <h2>Мои кампании</h2>
        <button type="button" className="secondary refresh-campaigns" disabled={busy || loadingCampaigns}
          onClick={() => void loadCampaigns()}>Обновить</button>
        {loadingCampaigns && <p className="muted" role="status">Загружаем кампании…</p>}
        {campaignsError && <p className="notice error" role="alert">{campaignsError}</p>}
        <nav aria-label="Мои кампании">
          {campaigns.map((campaign) => <button key={campaign.id} type="button"
            className={`campaign-item ${!creating && selectedCampaign?.id === campaign.id ? 'selected' : ''}`}
            aria-current={!creating && selectedCampaign?.id === campaign.id ? 'page' : undefined}
            disabled={busy || loadingCampaigns}
            onClick={() => { setSelectedId(campaign.id); setShowForm(false); setError(''); setConfirmingDelete(false) }}>
            <span>{campaign.name}</span>
            <small>ID {campaign.id}</small>
          </button>)}
        </nav>
      </aside>
      <div className="campaign-content">
        <header className="page-header">
          <h1>{creating ? 'Создание кампании' : selectedCampaign.name}</h1>
        </header>
        {configurationError && <p className="notice error" role="alert">{configurationError}</p>}
        {creating && <form onSubmit={submit} noValidate>
        <label htmlFor="campaign-name">Название кампании</label>
        <input id="campaign-name" value={name} onChange={(event) => { setName(event.target.value); clearFieldError('name') }}
          aria-invalid={Boolean(fieldErrors.name)} aria-describedby={fieldErrors.name ? 'name-error' : undefined}
          required maxLength={255} disabled={locked} placeholder="Пример: test_фамилия_campaign1" />

        {fieldErrors.name && <small id="name-error" className="field-error" role="alert">{fieldErrors.name}</small>}

        <label htmlFor="geo">GEO-коды стран</label>
        <input id="geo" value={geo} onChange={(event) => { setGeo(event.target.value.toUpperCase()); clearFieldError('geo_code') }}
          aria-invalid={Boolean(fieldErrors.geo_code)} aria-describedby={fieldErrors.geo_code ? 'geo-error' : undefined}
          required maxLength={2} pattern="[A-Za-z]{2}" disabled={locked} placeholder="DE, AU, BY, ..." />

        {fieldErrors.geo_code && <small id="geo-error" className="field-error" role="alert">{fieldErrors.geo_code}</small>}

        <label htmlFor="offer">ID оффера</label>
        <div className="offer-picker" onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false)
        }}>
          <input id="offer" role="combobox" aria-autocomplete="list" aria-expanded={focused && !locked}
            aria-controls="offer-list" aria-activedescendant={activeIndex >= 0 && offers[activeIndex] ? `offer-${offers[activeIndex].id}` : undefined}
            value={offerText} onChange={(event) => { setOfferText(event.target.value.replace(/[^0-9]/g, '')); setSelectedOffer(null); clearFieldError('offer_id'); setFocused(true); setActiveIndex(-1) }}
            onFocus={() => { setFocused(true); void loadOffers() }} onKeyDown={handleOfferKey}
            aria-invalid={Boolean(fieldErrors.offer_id)} aria-describedby={fieldErrors.offer_id ? 'offer-error' : undefined}
            required disabled={locked} placeholder="Введите ID оффера" inputMode="numeric" autoComplete="off" />
          {focused && !locked && <div className="suggestions">
            {loadingOffers && <p role="status">Загружаем офферы…</p>}
            {offerError && <p role="alert">{offerError}</p>}
            {!loadingOffers && !offerError && offers.length === 0 && <p>Офферы не найдены.</p>}
            <div id="offer-list" role="listbox" aria-label="Доступные офферы">
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
        {fieldErrors.offer_id && <small id="offer-error" className="field-error" role="alert">{fieldErrors.offer_id}</small>}
        {error && <p className="notice error" role="alert">{error}</p>}
          <div className="actions">
            <button type="submit" disabled={locked || Boolean(configurationError)}>
              {busy ? 'Создаём…' : 'Создать кампанию'}
            </button>
            {requestId && !busy && <>
              <button type="button" className="secondary" disabled={loadingCampaigns} onClick={() => void loadCampaigns()}>Обновить список кампаний</button>
              <button type="button" className="secondary" onClick={reset}>Новая кампания</button>
            </>}
          </div>
        </form>}
        {!creating && <section className={`result ${selectedCampaign.state === 'active' ? 'succeeded' : ''}`}>
          <h2>{selectedCampaign.state === 'active' ? 'Кампания активна' : 'Кампания выключена'}</h2>
          <p>ID кампании: <strong>{selectedCampaign.id}</strong></p>
          {selectedCampaign.state !== 'active' && <p>Проверьте конфигурацию кампании в Keitaro перед активацией.</p>}
          <p className="muted">GEO: {selectedCampaign.geo_code || '—'} · Оффер: {selectedCampaign.offer_id || '—'}</p>
          {fieldErrors.offer_id && <small id="offer-error" className="field-error" role="alert">{fieldErrors.offer_id}</small>}
        {error && <p className="notice error" role="alert">{error}</p>}
          <div className="actions">
            <button type="button" className="danger" disabled={busy || loadingCampaigns}
              onClick={() => { setConfirmingDelete(true); setError('') }}>Удалить кампанию</button>
          </div>
          <dialog id="delete-campaign-dialog" className="delete-dialog" aria-label="Удаление кампании"
            onCancel={(event) => { event.preventDefault(); if (!busy) setConfirmingDelete(false) }}>
            <div className="actions">
              <button type="button" className="danger" disabled={busy} onClick={() => void deleteCampaign()}>
                {busy ? 'Удаляем…' : 'Удалить'}
              </button>
              <button type="button" className="secondary" disabled={busy} onClick={() => setConfirmingDelete(false)}>Отмена</button>
            </div>
          </dialog>
        </section>}
      </div>
      {!creating && <button type="button" className="create-campaign" disabled={busy || loadingCampaigns}
        onClick={reset}>Добавить кампанию</button>}
    </main>
  )
}
