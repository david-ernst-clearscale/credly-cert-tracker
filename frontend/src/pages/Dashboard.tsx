import { useState, useEffect } from 'react'
interface CertEntry { name: string; employee: string; expires_at: string; status: string }
interface TierData { current: number; required: number | null; percentage: number | null; certifications: CertEntry[] }
interface LeaderEntry { employee: string; count: number; rank: number }
interface ApnPerson { name: string; email: string; apn_cert_count: number; certs?: { name: string; level: string; award_date: string; expiration_date: string }[]; credly_username?: string }
interface ApnRedactedRow { tier: string; count: number }
interface CredlyOnlyPerson { name: string; email: string; credly_username?: string }
interface ApnNetwork { matched: ApnPerson[]; missing: ApnPerson[]; credly_only?: CredlyOnlyPerson[]; redacted_count: number; redacted_breakdown?: ApnRedactedRow[]; total_named: number }
interface ComplianceData { timestamp: string; aws_tiers: Record<string, TierData>; claude_tiers: Record<string, TierData>; leaderboard: { aws: LeaderEntry[]; claude: LeaderEntry[] }; apn_network?: ApnNetwork }
const API_URL = import.meta.env.VITE_API_URL || ''
// Epoch millis for an expiry date, for sorting. Missing / "no-expiry" (never expires) → -Infinity.
function expTime(d?: string): number { if (!d || d === 'no-expiry') return -Infinity; const s = d.indexOf('T') > -1 ? d.substring(0, d.indexOf('T')) : d; const t = Date.parse(s); return isNaN(t) ? -Infinity : t }
// Soonest-expiring first; a missing / "no-expiry" (never) date always sinks to the bottom.
function cmpSoonest(ta: number, tb: number): number { if (ta === -Infinity && tb === -Infinity) return 0; if (ta === -Infinity) return 1; if (tb === -Infinity) return -1; return ta - tb }
function byExpirySoonest<T>(get: (x: T) => string | undefined) { return (a: T, b: T) => cmpSoonest(expTime(get(a)), expTime(get(b))) }
// Whole days from now until an expiry date; null if it never expires / no date. Negative = already past.
function daysUntil(d?: string): number | null { const t = expTime(d); if (t === -Infinity) return null; return Math.ceil((t - Date.now()) / 86400000) }
// Next (earliest upcoming) expiry among a person's certs — orders the APN people tables.
function nextCertExp(p: ApnPerson): number { const ts = (p.certs ?? []).map(x => expTime(x.expiration_date)).filter(t => t !== -Infinity); return ts.length ? Math.min(...ts) : -Infinity }
function nextCertExpDate(p: ApnPerson): string { let best = ''; let bestT = Infinity; for (const x of (p.certs ?? [])) { const t = expTime(x.expiration_date); if (t !== -Infinity && t < bestT) { bestT = t; best = x.expiration_date } } return best }
// APN dates come from the uploaded CSV, which may be ISO (YYYY-MM-DD) or not; pretty-print ISO, else show as-is.
function formatExp(d: string): string { if (!d) return '—'; if (d === 'no-expiry') return 'Never'; return /^\d{4}-\d{2}-\d{2}/.test(d) ? formatDate(d) : d }
function formatName(id: string) { return id.split('.').map(s => s.charAt(0).toUpperCase() + s.slice(1)).join(' ') }
function formatDate(d: string): string { if (!d || d === "no-expiry") return "Never"; const dateStr = d.indexOf("T") > -1 ? d.substring(0, d.indexOf("T")) : d; const [y, m, day] = dateStr.split("-"); const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]; return months[parseInt(m)-1] + " " + parseInt(day) + ", " + y; }
function TierCard({ name, data }: { name: string; data: TierData | null }) {
  const current = data?.current ?? 0; const required = data?.required; const pct = data?.percentage ?? 0
  let color = '#ef4444'
  if (required === null || required === 0) color = '#6b7280'
  else if (pct !== null && pct >= 100) color = '#22c55e'
  else if (pct !== null && pct >= 80) color = '#eab308'
  return (<div style={{ border: `2px solid ${color}`, borderRadius: '12px', padding: '24px', textAlign: 'center', minWidth: '180px', flex: 1, backgroundColor: `${color}11` }}><h3 style={{ margin: '0 0 8px 0', fontSize: '14px', color: '#9ca3af', textTransform: 'uppercase' }}>{name}</h3><div style={{ fontSize: '36px', fontWeight: 'bold', color }}>{current}{required ? <span style={{ fontSize: '18px', color: '#9ca3af' }}> / {required}</span> : null}</div>{pct !== null && required ? <div style={{ fontSize: '14px', color: '#9ca3af', marginTop: '4px' }}>{pct}%</div> : null}</div>)
}
function TierDetail({ name, data }: { name: string; data: TierData | null }) {
  const certs = [...(data?.certifications ?? [])].sort(byExpirySoonest(c => c.expires_at)); const current = data?.current ?? 0; const required = data?.required; const pct = data?.percentage ?? 0
  let color = '#ef4444'
  if (required === null || required === 0) color = '#6b7280'
  else if (pct !== null && pct >= 100) color = '#22c55e'
  else if (pct !== null && pct >= 80) color = '#eab308'
  return (<div style={{ marginBottom: '32px' }}><div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px', borderBottom: `3px solid ${color}`, paddingBottom: '8px' }}><h3 style={{ margin: 0, fontSize: '16px', color: '#e5e7eb' }}>{name}</h3><span style={{ fontSize: '14px', color, fontWeight: 'bold' }}>{current}{required ? ` / ${required}` : ''}</span></div>{certs.length > 0 ? (<table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '14px' }}><thead><tr style={{ borderBottom: '1px solid #374151', backgroundColor: '#1f2937' }}><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Certification</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Person</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Expires</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Status</th></tr></thead><tbody>{certs.map((cert, i) => { const d = daysUntil(cert.expires_at); const soon = cert.status === 'active' && d !== null && d >= 0 && d <= 183; return (<tr key={i} style={{ borderBottom: '1px solid #374151', ...(soon ? { outline: '2px solid #ef4444', outlineOffset: '-2px', backgroundColor: '#ef44441a' } : {}) }}><td style={{ padding: '10px', color: '#e5e7eb' }}>{cert.name}</td><td style={{ padding: '10px', color: '#e5e7eb' }}>{formatName(cert.employee)}</td><td style={{ padding: '10px', color: soon ? '#fca5a5' : '#e5e7eb' }}>{formatDate(cert.expires_at)}{soon && <span style={{ marginLeft: '8px', padding: '2px 8px', borderRadius: '9999px', border: '1px solid #ef4444', color: '#ef4444', fontSize: '11px', fontWeight: 700, whiteSpace: 'nowrap' }}>⚠ {d} {d === 1 ? 'day' : 'days'} left</span>}</td><td style={{ padding: '10px' }}><span style={{ padding: '3px 10px', borderRadius: '9999px', backgroundColor: cert.status === 'active' ? '#dcfce7' : '#fef9c3', color: cert.status === 'active' ? '#166534' : '#854d0e', fontSize: '12px' }}>{cert.status}</span></td></tr>) })}</tbody></table>) : (<p style={{ color: '#6b7280', fontStyle: 'italic' }}>No certifications in this tier yet.</p>)}</div>)
}
function groupByRank(entries: LeaderEntry[]) {
  const groups: { rank: number; count: number; employees: string[] }[] = []
  for (const e of entries) {
    const last = groups[groups.length - 1]
    if (last && last.rank === e.rank) { last.employees.push(e.employee) }
    else { groups.push({ rank: e.rank, count: e.count, employees: [e.employee] }) }
  }
  return groups
}
function LeaderboardTable({ title, entries, medal }: { title: string; entries: LeaderEntry[]; medal: string }) {
  return (<div style={{ flex: 1, minWidth: '300px' }}><h3 style={{ color: '#e5e7eb', fontSize: '16px', marginBottom: '12px' }}>{medal} {title}</h3>{entries.length > 0 ? (<table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '14px' }}><thead><tr style={{ borderBottom: '1px solid #374151', backgroundColor: '#1f2937' }}><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db', width: '50px' }}>Rank</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Person</th><th style={{ textAlign: 'right', padding: '10px', color: '#d1d5db' }}>Certs</th></tr></thead><tbody>{groupByRank(entries).map((g, i) => (<tr key={i} style={{ borderBottom: '1px solid #374151' }}><td style={{ padding: '10px', color: '#e5e7eb', fontSize: '18px', verticalAlign: 'top' }}>{`#${g.rank}`}</td><td style={{ padding: '10px', color: '#e5e7eb' }}>{g.employees.map((emp, j) => (<div key={j}>{formatName(emp)}</div>))}</td><td style={{ padding: '10px', color: '#e5e7eb', textAlign: 'right', fontSize: '20px', fontWeight: 'bold', verticalAlign: 'top' }}>{g.count}</td></tr>))}</tbody></table>) : (<p style={{ color: '#6b7280', fontStyle: 'italic' }}>No certifications tracked yet.</p>)}</div>)
}
function ApnNotTrackable({ total, breakdown }: { total: number; breakdown: ApnRedactedRow[] }) {
  const accent = '#6b7280'
  return (<div style={{ marginBottom: '32px' }}><div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px', borderBottom: `3px solid ${accent}`, paddingBottom: '8px' }}><h3 style={{ margin: 0, fontSize: '16px', color: '#e5e7eb' }}>Not Trackable — Redacted APN Records</h3><span style={{ fontSize: '14px', color: accent, fontWeight: 'bold' }}>{total}</span></div><p style={{ color: '#9ca3af', fontSize: '13px', marginTop: 0, marginBottom: '14px' }}>{total} APN certification record(s) in the export have redacted identities (name and work email shown as “XXXXXXX”), so they can't be matched to a Credly account. These are AWS-side cert records whose owner the export withheld — distinct headcount is unknown. Subtotal below shows what APN is reporting by certification.</p>{breakdown.length > 0 && (<table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '14px' }}><thead><tr style={{ borderBottom: '1px solid #374151', backgroundColor: '#1f2937' }}><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Tier</th><th style={{ textAlign: 'right', padding: '10px', color: '#d1d5db' }}>Count</th></tr></thead><tbody>{breakdown.map((r, i) => (<tr key={i} style={{ borderBottom: '1px solid #374151' }}><td style={{ padding: '10px', color: '#e5e7eb' }}>{r.tier}</td><td style={{ padding: '10px', color: '#e5e7eb', textAlign: 'right', fontWeight: 'bold' }}>{r.count}</td></tr>))}<tr style={{ borderTop: `2px solid ${accent}` }}><td style={{ padding: '10px', color: '#e5e7eb', fontWeight: 'bold' }}>Subtotal</td><td style={{ padding: '10px', color: '#e5e7eb', textAlign: 'right', fontWeight: 'bold' }}>{total}</td></tr></tbody></table>)}</div>)
}
function CredlyOnlyTable({ people }: { people: CredlyOnlyPerson[] }) {
  const accent = '#ef4444'
  return (<div style={{ marginBottom: '32px' }}><div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px', borderBottom: `3px solid ${accent}`, paddingBottom: '8px' }}><h3 style={{ margin: 0, fontSize: '16px', color: '#e5e7eb' }}>In App, Not on APN List</h3><span style={{ fontSize: '14px', color: accent, fontWeight: 'bold' }}>{people.length}</span></div><p style={{ color: '#9ca3af', fontSize: '13px', marginTop: 0, marginBottom: '14px' }}>Credly users who hold an AWS certification but don't appear on the uploaded APN export (Anthropic-only holders are excluded).</p>{people.length > 0 ? (<table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '14px' }}><thead><tr style={{ borderBottom: '1px solid #374151', backgroundColor: '#1f2937' }}><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Person</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Work Email</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Credly Username</th></tr></thead><tbody>{people.map((p, i) => (<tr key={i} style={{ borderBottom: '1px solid #374151' }}><td style={{ padding: '10px', color: '#e5e7eb' }}>{p.name}</td><td style={{ padding: '10px', color: '#9ca3af' }}>{p.email || '—'}</td><td style={{ padding: '10px', color: '#9ca3af' }}>{p.credly_username || '—'}</td></tr>))}</tbody></table>) : (<p style={{ color: '#6b7280', fontStyle: 'italic' }}>Every tracked Credly user is on the APN list.</p>)}</div>)
}
function ApnStat({ label, value, color }: { label: string; value: number; color: string }) {
  return (<div style={{ border: `2px solid ${color}`, borderRadius: '12px', padding: '24px', textAlign: 'center', minWidth: '180px', flex: 1, backgroundColor: `${color}11` }}><h3 style={{ margin: '0 0 8px 0', fontSize: '14px', color: '#9ca3af', textTransform: 'uppercase' }}>{label}</h3><div style={{ fontSize: '36px', fontWeight: 'bold', color }}>{value}</div></div>)
}
function ApnTable({ title, people, kind }: { title: string; people: ApnPerson[]; kind: 'missing' | 'matched' }) {
  const isMissing = kind === 'missing'
  const accent = isMissing ? '#ef4444' : '#22c55e'
  const icon = isMissing ? '✗' : '✓'
  const rows = [...people].sort((a, b) => cmpSoonest(nextCertExp(a), nextCertExp(b)))
  return (<div style={{ marginBottom: '32px' }}><div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '12px', borderBottom: `3px solid ${accent}`, paddingBottom: '8px' }}><h3 style={{ margin: 0, fontSize: '16px', color: '#e5e7eb' }}>{title}</h3><span style={{ fontSize: '14px', color: accent, fontWeight: 'bold' }}>{people.length}</span></div>{people.length > 0 ? (<table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '14px' }}><thead><tr style={{ borderBottom: '1px solid #374151', backgroundColor: '#1f2937' }}><th style={{ textAlign: 'center', padding: '10px', color: '#d1d5db', width: '50px' }}>Credly</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Person</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Work Email</th><th style={{ textAlign: 'left', padding: '10px', color: '#d1d5db' }}>Next Expiry</th><th style={{ textAlign: 'right', padding: '10px', color: '#d1d5db' }}>APN Certs</th></tr></thead><tbody>{rows.map((p, i) => (<tr key={i} style={{ borderBottom: '1px solid #374151' }}><td style={{ padding: '10px', textAlign: 'center', color: accent, fontSize: '18px', fontWeight: 'bold' }}>{icon}</td><td style={{ padding: '10px', color: '#e5e7eb' }}>{p.name}</td><td style={{ padding: '10px', color: '#9ca3af' }}>{p.email}</td><td style={{ padding: '10px', color: '#9ca3af' }}>{formatExp(nextCertExpDate(p))}</td><td style={{ padding: '10px', color: '#e5e7eb', textAlign: 'right' }}>{p.apn_cert_count}</td></tr>))}</tbody></table>) : (<p style={{ color: '#6b7280', fontStyle: 'italic' }}>{isMissing ? 'Everyone on the APN roster has a Credly account. 🎉' : 'No matched accounts yet.'}</p>)}</div>)
}
function Tab({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (<button onClick={onClick} style={{ padding: '12px 24px', fontSize: '15px', fontWeight: active ? 700 : 400, color: active ? '#f9fafb' : '#6b7280', backgroundColor: active ? '#374151' : 'transparent', border: 'none', borderBottom: active ? '3px solid #3b82f6' : '3px solid transparent', cursor: 'pointer', transition: 'all 0.2s' }}>{label}</button>)
}
function ApnUpload({ token, onAuthError, onUploaded }: { token: string; onAuthError?: () => void; onUploaded: () => void }) {
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  async function handleFile(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    e.target.value = '' // allow re-selecting the same filename later
    if (!file) return
    setBusy(true); setMsg(null)
    try {
      const text = await file.text()
      const r = await fetch(`${API_URL}/apn-roster`, { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'text/csv' }, body: text })
      if (r.status === 401 || r.status === 403) { onAuthError?.(); return }
      const j = await r.json().catch(() => ({}))
      if (!r.ok) { setMsg({ kind: 'err', text: j.error || `Upload failed (HTTP ${r.status})` }); return }
      setMsg({ kind: 'ok', text: `Loaded ${j.named_people} named people · ${j.redacted_count} redacted records. Refreshing…` })
      onUploaded()
    } catch (err) {
      setMsg({ kind: 'err', text: err instanceof Error ? err.message : 'Upload failed' })
    } finally {
      setBusy(false)
    }
  }
  return (<div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap', marginBottom: '20px', padding: '14px 18px', border: '1px solid #374151', borderRadius: '10px', backgroundColor: '#1f293733' }}><label style={{ display: 'inline-flex', alignItems: 'center', gap: '8px', padding: '8px 16px', borderRadius: '8px', border: '1px solid #3b82f6', background: busy ? '#1f2937' : '#3b82f6', color: busy ? '#9ca3af' : '#fff', cursor: busy ? 'default' : 'pointer', fontSize: '14px', fontWeight: 600 }}>{busy ? 'Uploading…' : 'Upload APN CSV'}<input type="file" accept=".csv,text/csv" disabled={busy} onChange={handleFile} style={{ display: 'none' }} /></label><span style={{ fontSize: '13px', color: '#6b7280' }}>Upload a fresh APN certification export to refresh this tab.</span>{msg && <span style={{ fontSize: '13px', color: msg.kind === 'ok' ? '#22c55e' : '#ef4444' }}>{msg.text}</span>}</div>)
}
interface CredlyUser { employee_id: string; credly_username: string; email: string; consent_status: string }
function UsersTab({ token, onAuthError }: { token: string; onAuthError?: () => void }) {
  const [users, setUsers] = useState<CredlyUser[]>([])
  const [isAdmin, setIsAdmin] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState<CredlyUser>({ employee_id: '', credly_username: '', email: '', consent_status: 'opted_in' })
  const blankAdd: CredlyUser = { employee_id: '', credly_username: '', email: '', consent_status: 'opted_in' }
  const [addRow, setAddRow] = useState<CredlyUser>(blankAdd)

  const load = () => {
    setLoading(true)
    fetch(`${API_URL}/users`, { headers: { Authorization: `Bearer ${token}` } })
      .then(r => { if (r.status === 401 || r.status === 403) { onAuthError?.(); throw new Error('Session expired') } if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then(j => { setUsers(j.users || []); setIsAdmin(!!j.is_admin); setError(null); setLoading(false) })
      .catch(e => { setError(e.message); setLoading(false) })
  }
  useEffect(() => { load() }, [])

  async function post(url: string, body?: unknown) {
    const r = await fetch(url, { method: 'POST', headers: { Authorization: `Bearer ${token}`, ...(body ? { 'Content-Type': 'application/json' } : {}) }, body: body ? JSON.stringify(body) : undefined })
    if (r.status === 401) { onAuthError?.(); throw new Error('Session expired') }
    const j = await r.json().catch(() => ({}))
    if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`)
    return j
  }
  async function saveUser(user: CredlyUser, isNew: boolean) {
    if (!user.employee_id.trim()) { setMsg({ kind: 'err', text: 'Employee ID is required.' }); return }
    setBusy(true); setMsg(null)
    try {
      await post(`${API_URL}/users`, user)
      setMsg({ kind: 'ok', text: isNew ? `Added ${user.employee_id}. Use “Sync now” to pull their badges.` : `Updated ${user.employee_id}.` })
      setEditingId(null)
      if (isNew) setAddRow(blankAdd)
      load()
    } catch (e) { setMsg({ kind: 'err', text: e instanceof Error ? e.message : 'Save failed' }) } finally { setBusy(false) }
  }
  async function syncNow() {
    setBusy(true); setMsg(null)
    try { const j = await post(`${API_URL}/sync`); setMsg({ kind: 'ok', text: j.message || 'Sync started.' }) }
    catch (e) { setMsg({ kind: 'err', text: e instanceof Error ? e.message : 'Sync failed' }) } finally { setBusy(false) }
  }

  const inputStyle: React.CSSProperties = { padding: '6px 8px', fontSize: '13px', background: '#111827', color: '#e5e7eb', border: '1px solid #374151', borderRadius: '6px', width: '100%', boxSizing: 'border-box' }
  const th: React.CSSProperties = { textAlign: 'left', padding: '10px', color: '#d1d5db' }
  const td: React.CSSProperties = { padding: '8px 10px', color: '#e5e7eb', borderBottom: '1px solid #374151' }
  const btn = (bg: string): React.CSSProperties => ({ padding: '6px 12px', fontSize: '13px', fontWeight: 600, color: '#fff', background: bg, border: 'none', borderRadius: '6px', cursor: busy ? 'default' : 'pointer' })

  if (loading) return <p style={{ textAlign: 'center', color: '#9ca3af' }}>Loading…</p>
  if (error) return <p style={{ textAlign: 'center', color: '#ef4444' }}>Error: {error}</p>
  return (<>
    <div style={{ display: 'flex', alignItems: 'center', gap: '14px', flexWrap: 'wrap', marginBottom: '16px' }}>
      <p style={{ color: '#6b7280', fontSize: '13px', margin: 0, flex: 1, minWidth: '260px' }}>{users.length} tracked Credly user(s). {isAdmin ? 'Add or edit entries below, then “Sync now” to pull badges.' : 'Read-only — ask an admin to make changes.'}</p>
      {isAdmin && <button onClick={syncNow} disabled={busy} style={btn('#3b82f6')}>{busy ? 'Working…' : '↻ Sync now'}</button>}
    </div>
    {msg && <p style={{ fontSize: '13px', color: msg.kind === 'ok' ? '#22c55e' : '#ef4444', marginTop: 0 }}>{msg.text}</p>}
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '14px' }}>
      <thead><tr style={{ borderBottom: '1px solid #374151', backgroundColor: '#1f2937' }}>
        <th style={th}>Employee ID</th><th style={th}>Credly Username</th><th style={th}>Email</th><th style={th}>Consent</th>{isAdmin && <th style={{ ...th, textAlign: 'right' }}>Actions</th>}
      </tr></thead>
      <tbody>
        {isAdmin && (<tr style={{ backgroundColor: '#1f293733' }}>
          <td style={td}><input style={inputStyle} placeholder="first.last" value={addRow.employee_id} onChange={e => setAddRow({ ...addRow, employee_id: e.target.value })} /></td>
          <td style={td}><input style={inputStyle} placeholder="credly-handle" value={addRow.credly_username} onChange={e => setAddRow({ ...addRow, credly_username: e.target.value })} /></td>
          <td style={td}><input style={inputStyle} placeholder="name@clearscale.com" value={addRow.email} onChange={e => setAddRow({ ...addRow, email: e.target.value })} /></td>
          <td style={td}><select style={inputStyle} value={addRow.consent_status} onChange={e => setAddRow({ ...addRow, consent_status: e.target.value })}><option value="opted_in">opted_in</option><option value="opted_out">opted_out</option></select></td>
          <td style={{ ...td, textAlign: 'right' }}><button onClick={() => saveUser(addRow, true)} disabled={busy} style={btn('#22c55e')}>Add</button></td>
        </tr>)}
        {users.map(u => {
          const editing = editingId === u.employee_id
          return (<tr key={u.employee_id}>
            <td style={td}>{u.employee_id}</td>
            <td style={td}>{editing ? <input style={inputStyle} value={draft.credly_username} onChange={e => setDraft({ ...draft, credly_username: e.target.value })} /> : (u.credly_username || <span style={{ color: '#6b7280' }}>—</span>)}</td>
            <td style={td}>{editing ? <input style={inputStyle} value={draft.email} onChange={e => setDraft({ ...draft, email: e.target.value })} /> : (u.email || <span style={{ color: '#6b7280' }}>—</span>)}</td>
            <td style={td}>{editing ? <select style={inputStyle} value={draft.consent_status} onChange={e => setDraft({ ...draft, consent_status: e.target.value })}><option value="opted_in">opted_in</option><option value="opted_out">opted_out</option></select> : <span style={{ color: u.consent_status === 'opted_in' ? '#22c55e' : '#9ca3af' }}>{u.consent_status}</span>}</td>
            {isAdmin && <td style={{ ...td, textAlign: 'right', whiteSpace: 'nowrap' }}>{editing ? (<><button onClick={() => saveUser(draft, false)} disabled={busy} style={{ ...btn('#22c55e'), marginRight: '6px' }}>Save</button><button onClick={() => setEditingId(null)} disabled={busy} style={btn('#4b5563')}>Cancel</button></>) : (<button onClick={() => { setEditingId(u.employee_id); setDraft(u); setMsg(null) }} style={btn('#374151')}>Edit</button>)}</td>}
          </tr>)
        })}
      </tbody>
    </table>
  </>)
}
export default function Dashboard({ token, onAuthError }: { token: string; onAuthError?: () => void }) {
  const [data, setData] = useState<ComplianceData | null>(null); const [loading, setLoading] = useState(true); const [error, setError] = useState<string | null>(null); const [tab, setTab] = useState<'aws' | 'anthropic' | 'leaderboard' | 'apn' | 'users'>('aws')
  const loadData = () => { fetch(`${API_URL}/compliance`, { headers: { Authorization: `Bearer ${token}` } }).then(r => { if (r.status === 401 || r.status === 403) { onAuthError?.(); throw new Error('Session expired'); } if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() }).then(j => { setData(j); setError(null); setLoading(false) }).catch(e => { setError(e.message); setLoading(false) }) }
  useEffect(() => { loadData() }, [])
  return (<div style={{ maxWidth: '1100px', margin: '0 auto', padding: '32px', fontFamily: 'system-ui' }}><div style={{ display: 'flex', justifyContent: 'center', gap: '4px', marginBottom: '32px', borderBottom: '1px solid #374151' }}><Tab label="AWS" active={tab === 'aws'} onClick={() => setTab('aws')} /><Tab label="Anthropic" active={tab === 'anthropic'} onClick={() => setTab('anthropic')} /><Tab label="Leaderboard" active={tab === 'leaderboard'} onClick={() => setTab('leaderboard')} /><Tab label="AWS APN Network" active={tab === 'apn'} onClick={() => setTab('apn')} /><Tab label="Users" active={tab === 'users'} onClick={() => setTab('users')} /></div>{loading && tab !== 'users' && <p style={{ textAlign: 'center', color: '#9ca3af' }}>Loading...</p>}{error && tab !== 'users' && <p style={{ textAlign: 'center', color: '#ef4444' }}>Error: {error}</p>}{tab === 'users' && <UsersTab token={token} onAuthError={onAuthError} />}{data && tab === 'aws' && (<><p style={{ color: '#6b7280', marginBottom: '20px', fontSize: '13px' }}>APN Premier tier: 10 Foundational, 25 Technical, 10 Professional/Specialty</p><div style={{ display: 'flex', gap: '16px', justifyContent: 'center', flexWrap: 'wrap', marginBottom: '32px' }}><TierCard name="Foundational" data={data.aws_tiers?.['Foundational'] ?? null} /><TierCard name="Technical" data={data.aws_tiers?.['Technical'] ?? null} /><TierCard name="Professional / Specialty" data={data.aws_tiers?.['Professional/Specialty'] ?? null} /></div><TierDetail name="Foundational" data={data.aws_tiers?.['Foundational'] ?? null} /><TierDetail name="Technical (Associate)" data={data.aws_tiers?.['Technical'] ?? null} /><TierDetail name="Professional / Specialty" data={data.aws_tiers?.['Professional/Specialty'] ?? null} /></>)}{data && tab === 'anthropic' && (<><p style={{ color: '#6b7280', marginBottom: '20px', fontSize: '13px' }}>Claude Partner Network — requirement: 10 CCAR-F (Architect Foundations)</p><div style={{ display: 'flex', gap: '16px', justifyContent: 'center', flexWrap: 'wrap', marginBottom: '32px' }}><TierCard name="CCAR-F" data={data.claude_tiers?.['CCAR-F'] ?? null} /><TierCard name="CCAR-P" data={data.claude_tiers?.['CCAR-P'] ?? null} /><TierCard name="CCDV-F" data={data.claude_tiers?.['CCDV-F'] ?? null} /><TierCard name="CCAO-F" data={data.claude_tiers?.['CCAO-F'] ?? null} /></div><TierDetail name="CCAR-F — Architect Foundations (Required: 10)" data={data.claude_tiers?.['CCAR-F'] ?? null} /><TierDetail name="CCAR-P — Architect Professional" data={data.claude_tiers?.['CCAR-P'] ?? null} /><TierDetail name="CCDV-F — Developer Foundations" data={data.claude_tiers?.['CCDV-F'] ?? null} /><TierDetail name="CCAO-F — Associate Foundations" data={data.claude_tiers?.['CCAO-F'] ?? null} /></>)}{data && tab === 'leaderboard' && (<><p style={{ color: '#6b7280', marginBottom: '24px', fontSize: '13px' }}>Top certified team members (active certs only)</p><div style={{ display: 'flex', gap: '40px', flexWrap: 'wrap' }}><LeaderboardTable title="AWS Certifications" entries={data.leaderboard?.aws ?? []} medal="☁️" /><LeaderboardTable title="Anthropic Certifications" entries={data.leaderboard?.claude ?? []} medal="🤖" /></div></>)}{data && tab === 'apn' && (<><ApnUpload token={token} onAuthError={onAuthError} onUploaded={loadData} /><p style={{ color: '#6b7280', marginBottom: '20px', fontSize: '13px' }}>AWS APN network members with certifications, cross-referenced against Credly. <span style={{ color: '#ef4444' }}>✗ = no Credly account (needs to connect)</span> · <span style={{ color: '#22c55e' }}>✓ = Credly account connected</span></p><div style={{ display: 'flex', gap: '16px', justifyContent: 'center', flexWrap: 'wrap', marginBottom: '32px' }}><ApnStat label="On Credly & APN" value={data.apn_network?.matched?.length ?? 0} color="#22c55e" /><ApnStat label="In App, Not on APN" value={data.apn_network?.credly_only?.length ?? 0} color="#ef4444" /><ApnStat label="Redacted / Not Trackable" value={data.apn_network?.redacted_count ?? 0} color="#6b7280" /></div>{(data.apn_network?.redacted_count ?? 0) > 0 && (<ApnNotTrackable total={data.apn_network?.redacted_count ?? 0} breakdown={data.apn_network?.redacted_breakdown ?? []} />)}<CredlyOnlyTable people={data.apn_network?.credly_only ?? []} /><ApnTable title="On Credly & APN" people={data.apn_network?.matched ?? []} kind="matched" /></>)}{data?.timestamp && <p style={{ textAlign: 'center', color: '#6b7280', fontSize: '12px', marginTop: '32px' }}>Last updated: {new Date(data.timestamp).toLocaleString()}</p>}</div>)
}
