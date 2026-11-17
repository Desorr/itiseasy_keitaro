import { useState } from 'react'
import { ApiError, request } from './api'
import type { DraftAction, Offer, Stream } from './api'
import OfferPicker from './OfferPicker'

type Props = { campaignId: number; onBack: () => void; onBusy: (busy: boolean) => void }

export default function CampaignEditor({ campaignId, onBack, onBusy }: Props) {
  const [streams, setStreams] = useState<Stream[]>([])
  const [loading, setLoading] = useState(false)
  const [hasLoaded, setHasLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [errors, setErrors] = useState<Record<number, string>>({})
  const [picked, setPicked] = useState<Record<number, Offer | null>>({})
  const [offerErrors, setOfferErrors] = useState<Record<number, string>>({})
  const base = `/api/campaigns/${campaignId}/streams`
  const orderedStreams = [...streams].sort((a, b) => Number(b.offers.length > 0) - Number(a.offers.length > 0)
    || a.position - b.position || a.id - b.id)

  async function refresh() {
    setBusy(true); setLoading(true); onBusy(true); setError(''); setErrors({})
    try {
      setStreams(await request<Stream[]>(`${base}/sync`, { method: 'POST' }))
      setHasLoaded(true)
    }
    catch (error) { setError(error instanceof Error ? error.message : 'Failed to fetch flows.') }
    finally { setBusy(false); setLoading(false); onBusy(false) }
  }

  async function change(stream: Stream, action: DraftAction | 'cancel' | 'publish', offerId?: number) {
    if (busy) return
    if (action === 'add' && !picked[stream.id]) {
      setOfferErrors((current) => ({ ...current, [stream.id]: 'Select an offer from the dropdown.' })); return
    }
    setBusy(true); onBusy(true)
    setErrors((current) => ({ ...current, [stream.id]: '' }))
    setOfferErrors((current) => ({ ...current, [stream.id]: '' }))
    try {
      const result = await request<Stream>(`${base}/${stream.id}/${action === 'publish' ? 'publish' : 'draft'}`, {
        method: action === 'publish' ? 'POST' : action === 'cancel' ? 'DELETE' : 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: stream.revision,
          ...(action === 'cancel' || action === 'publish' ? {} : { action, offer_id: offerId ?? picked[stream.id]?.id }) }),
      })
      setStreams((current) => current.map((item) => item.id === result.id ? result : item))
      if (action === 'add') setPicked((current) => ({ ...current, [stream.id]: null }))
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Failed to save the change.'
      if (action === 'add') setOfferErrors((current) => ({ ...current, [stream.id]: message }))
      else setErrors((current) => ({ ...current, [stream.id]: message }))
      // Read only: never replay an uncertain write. Refresh revisions after any failed mutation.
      try { setStreams(await request<Stream[]>(base)) } catch { /* Keep the original error and visible draft. */ }
      if (error instanceof ApiError && error.uncertain) {
        setErrors((current) => ({ ...current, [stream.id]: `${message} Fetch flows from Keitaro to check the result.` }))
      }
    } finally { setBusy(false); onBusy(false) }
  }

  return <section className="campaign-editor" aria-label="Flow editor">
    <div className="editor-toolbar">
      <button type="button" className="secondary" onClick={onBack} disabled={busy}>Back</button>
      <button type="button" className="secondary" onClick={() => void refresh()} disabled={busy || loading}>
        {loading ? 'Loading…' : busy ? 'Saving…' : 'Fetch streams from KT'}
      </button>
    </div>
    {loading && <p role="status">Loading flows…</p>}
    {error && <p className="notice error" role="alert">{error}</p>}
    {hasLoaded && !loading && !error && streams.length === 0 && <p className="muted">This campaign has no flows.</p>}
    {orderedStreams.map((stream) => {
      const blocked = busy || loading || stream.publication !== 'idle'
      return <article key={stream.id} className={`stream-card${stream.dirty ? ' dirty' : ''}`}>
        <h2>{stream.name || `Flow ${stream.position}`} <small>ID {stream.id}</small></h2>
        {stream.missing && <p className="notice error">This flow was deleted in Keitaro. Cancel the draft and fetch flows again.</p>}
        {stream.conflict && <p className="notice error" role="alert">This flow changed in Keitaro. Your draft is saved. Cancel it to accept the current version.</p>}
        {stream.allocation_error && <p className="notice error" role="alert">{stream.allocation_error}</p>}
        {stream.publication !== 'idle' && <p className="notice" role="status">Publication is not confirmed yet. Fetch flows from Keitaro in a minute.</p>}
        {stream.dirty && <div className="actions stream-actions">
          <button type="button" disabled={blocked || stream.conflict || stream.missing || Boolean(stream.allocation_error)}
            onClick={() => void change(stream, 'publish')}>Push to KT</button>
          <button type="button" className="secondary" disabled={blocked}
            onClick={() => void change(stream, 'cancel')}>Cancel</button>
        </div>}
        {stream.offers.length > 0 && <ul className="stream-offers">
          {[...stream.offers].sort((a, b) => Number(a.removed) - Number(b.removed)
            || Number(b.share > 0) - Number(a.share > 0)).map((offer) => <li key={offer.offer_id} className={offer.removed ? 'removed' : ''}>
            <div className="stream-offer-name">[{offer.offer_id}] {offer.name}
              {offer.removed && <small>Removed</small>}
              {!offer.removed && offer.state !== 'active' && <small>Disabled in Keitaro</small>}
            </div>
            <div className="stream-offer-share"><strong>{offer.share}%</strong>
              {!offer.removed && offer.state === 'active' && <button type="button" className={`secondary pin${offer.pinned ? ' pinned' : ''}`}
                aria-pressed={offer.pinned} disabled={blocked || stream.conflict || !stream.editable}
                onClick={() => void change(stream, 'toggle_pin', offer.offer_id)}>
                {offer.pinned ? 'Unpin' : 'Pin'}</button>}
            </div>
            <button type="button" className="secondary" disabled={blocked || stream.conflict || !stream.editable}
              onClick={() => void change(stream, offer.removed ? 'restore' : 'remove', offer.offer_id)}>
              {offer.removed ? 'Bring back' : 'Remove'}</button>
          </li>)}
        </ul>}
        {stream.editable && <div className="editor-add-offer">
          <OfferPicker disabled={blocked || stream.conflict} value={picked[stream.id] ?? null} error={offerErrors[stream.id]}
            onChange={(offer) => { setPicked((current) => ({ ...current, [stream.id]: offer })); setOfferErrors((current) => ({ ...current, [stream.id]: '' })) }} />
          <button type="button" disabled={blocked || stream.conflict} onClick={() => void change(stream, 'add')}>Add</button>
        </div>}
        {errors[stream.id] && <p className="notice error" role="alert">{errors[stream.id]}</p>}
      </article>
    })}
  </section>
}
