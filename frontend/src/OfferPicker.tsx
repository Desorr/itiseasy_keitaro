import { useId, useMemo, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { loadAvailableOffers } from './api'
import type { Offer } from './api'

type Props = { disabled: boolean; value: Offer | null; onChange: (offer: Offer | null) => void; error?: string }

export default function OfferPicker({ disabled, value, onChange, error }: Props) {
  const id = useId()
  const [text, setText] = useState('')
  const [all, setAll] = useState<Offer[]>([])
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [active, setActive] = useState(-1)
  const offers = useMemo(() => all.filter((offer) => String(offer.id).includes(value ? String(value.id) : text))
    .sort((a, b) => Number(String(b.id) === text) - Number(String(a.id) === text) || a.name.localeCompare(b.name)).slice(0, 30), [all, text, value])

  async function focus() {
    setOpen(true)
    setLoading(true)
    setLoadError('')
    try { setAll(await loadAvailableOffers()) } catch (error) {
      setLoadError(error instanceof Error ? error.message : 'Failed to load offers.')
    } finally { setLoading(false) }
  }
  function pick(offer: Offer) { onChange(offer); setText(''); setOpen(false); setActive(-1) }
  function keydown(event: KeyboardEvent<HTMLInputElement>) {
    if (value && (event.key === 'Backspace' || event.key === 'Delete')) {
      event.preventDefault()
      onChange(null)
      setText('')
      setOpen(true)
      setActive(-1)
      return
    }
    if (event.key === 'Escape') setOpen(false)
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true)
      setActive((index) => offers.length ? (index + (event.key === 'ArrowDown' ? 1 : -1) + offers.length) % offers.length : -1)
    }
    if (event.key === 'Enter' && open && offers[active]) { event.preventDefault(); pick(offers[active]) }
  }
  return <div className="offer-picker" onBlur={(event) => {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false)
  }}>
    <label htmlFor={id}>Offer ID</label>
    <input id={id} role="combobox" aria-autocomplete="list" aria-expanded={open && !disabled}
      aria-controls={`${id}-list`} aria-activedescendant={active >= 0 && offers[active] ? `${id}-${offers[active].id}` : undefined}
      disabled={disabled} value={value ? `[${value.id}] ${value.name}` : text}
      placeholder="Enter offer ID" inputMode="numeric" autoComplete="off"
      aria-invalid={Boolean(error)} aria-describedby={error ? `${id}-error` : undefined}
      onChange={(event) => { setText(value ? '' : event.target.value.replace(/[^0-9]/g, '')); onChange(null); setOpen(true); setActive(-1) }}
      onFocus={() => void focus()} onKeyDown={keydown} />
    {open && !disabled && <div className="suggestions">
      {loading && <p role="status">Loading offers…</p>}
      {loadError && <p role="alert">{loadError}</p>}
      {!loading && !loadError && offers.length === 0 && <p>No offers found.</p>}
      <div id={`${id}-list`} role="listbox" aria-label="Available offers">
        {offers.map((offer, index) => <button key={offer.id} id={`${id}-${offer.id}`} type="button" role="option"
          aria-selected={value?.id === offer.id || active === index} className={active === index ? 'active' : ''}
          onPointerDown={(event) => {
            if (event.button === 0 && event.pointerType === 'mouse') { event.preventDefault(); pick(offer) }
          }} onClick={() => pick(offer)}>[{offer.id}] {offer.name}</button>)}
      </div>
    </div>}
    {error && <small id={`${id}-error`} className="field-error" role="alert">{error}</small>}
  </div>
}
