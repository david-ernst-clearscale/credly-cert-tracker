import { useState, useEffect } from 'react'

interface TierData {
  current: number
  required: number
  percentage: number
}

interface CertEntry {
  name: string
  employee_id: string
  expires_at: string
  status: string
  tier: string
}

interface ComplianceData {
  timestamp: string
  tiers: Record<string, TierData>
  certifications: CertEntry[]
}

const API_URL = import.meta.env.VITE_API_URL || ''

function TierCard({ name, data }: { name: string; data: TierData | null }) {
  const current = data?.current ?? '—'
  const required = data?.required ?? '—'
  const pct = data?.percentage ?? 0

  let color = '#ef4444' // red
  if (pct >= 100) color = '#22c55e' // green
  else if (pct >= 80) color = '#eab308' // yellow

  return (
    <div style={{
      border: `2px solid ${color}`,
      borderRadius: '12px',
      padding: '24px',
      textAlign: 'center',
      minWidth: '200px',
      backgroundColor: `${color}11`,
    }}>
      <h3 style={{ margin: '0 0 8px 0', fontSize: '16px', color: '#374151' }}>{name}</h3>
      <div style={{ fontSize: '32px', fontWeight: 'bold', color }}>
        {current} / {required}
      </div>
      <div style={{ fontSize: '14px', color: '#6b7280', marginTop: '4px' }}>
        {data ? `${pct}%` : '—'}
      </div>
    </div>
  )
}

function CertTable({ certs }: { certs: CertEntry[] }) {
  if (!certs.length) return null

  return (
    <div style={{ marginTop: '32px', overflowX: 'auto' }}>
      <h3 style={{ color: '#374151' }}>Certifications</h3>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '14px' }}>
        <thead>
          <tr style={{ borderBottom: '2px solid #e5e7eb' }}>
            <th style={{ textAlign: 'left', padding: '8px' }}>Name</th>
            <th style={{ textAlign: 'left', padding: '8px' }}>Employee</th>
            <th style={{ textAlign: 'left', padding: '8px' }}>Tier</th>
            <th style={{ textAlign: 'left', padding: '8px' }}>Expires</th>
            <th style={{ textAlign: 'left', padding: '8px' }}>Status</th>
          </tr>
        </thead>
        <tbody>
          {certs.map((cert, i) => (
            <tr key={i} style={{ borderBottom: '1px solid #f3f4f6' }}>
              <td style={{ padding: '8px' }}>{cert.name}</td>
              <td style={{ padding: '8px' }}>{cert.employee_id}</td>
              <td style={{ padding: '8px' }}>{cert.tier}</td>
              <td style={{ padding: '8px' }}>{cert.expires_at === 'no-expiry' ? 'Never' : cert.expires_at?.split('T')}</td>
              <td style={{ padding: '8px' }}>
                <span style={{
                  padding: '2px 8px',
                  borderRadius: '9999px',
                  backgroundColor: cert.status === 'active' ? '#dcfce7' : '#fef9c3',
                  color: cert.status === 'active' ? '#166534' : '#854d0e',
                  fontSize: '12px',
                }}>{cert.status}</span>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function Dashboard({ token }: { token: string }) {
  const [data, setData] = useState<ComplianceData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    fetch(`${API_URL}/compliance`, { headers: { Authorization: `Bearer ${token}` } })
      .then(res => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        return res.json()
      })
      .then(json => {
        setData(json)
        setLoading(false)
      })
      .catch(err => {
        setError(err.message)
        setLoading(false)
      })
  }, [])

  return (
    <div style={{ maxWidth: '1000px', margin: '0 auto', padding: '32px', fontFamily: 'system-ui' }}>
      <h1 style={{ textAlign: 'center', color: '#111827' }}>AWS Cert Tracker Dashboard</h1>

      {loading && <p style={{ textAlign: 'center' }}>Loading compliance data...</p>}
      {error && <p style={{ textAlign: 'center', color: '#ef4444' }}>Error: {error}</p>}

      <div style={{ display: 'flex', gap: '24px', justifyContent: 'center', flexWrap: 'wrap', marginTop: '24px' }}>
        <TierCard name="Foundational" data={data?.tiers?.['Foundational'] ?? null} />
        <TierCard name="Technical" data={data?.tiers?.['Technical'] ?? null} />
        <TierCard name="Professional/Specialty" data={data?.tiers?.['Professional/Specialty'] ?? null} />
      </div>

      {data?.timestamp && (
        <p style={{ textAlign: 'center', color: '#9ca3af', fontSize: '12px', marginTop: '16px' }}>
          Last updated: {new Date(data.timestamp).toLocaleString()}
        </p>
      )}

      {data?.certifications && <CertTable certs={data.certifications} />}
    </div>
  )
}
