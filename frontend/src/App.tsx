import { useState, useEffect } from 'react';
import Dashboard from './pages/Dashboard';

const COGNITO_DOMAIN = import.meta.env.VITE_COGNITO_DOMAIN;
const CLIENT_ID = import.meta.env.VITE_USER_POOL_CLIENT_ID;
const REDIRECT_URI = window.location.origin;

function decodeJwt(token: string): Record<string, unknown> {
  const seg = token.split('.').at(1)!;
  
  const base64 = seg.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64 + '='.repeat((4 - base64.length % 4) % 4);
  return JSON.parse(atob(padded));
}

function storeSession(idToken: string, email: string, expiresIn: number, refreshToken?: string) {
  localStorage.setItem('cert_token', idToken);
  localStorage.setItem('cert_email', email);
  localStorage.setItem('cert_token_expires_at', String(Date.now() + expiresIn * 1000));
  if (refreshToken) localStorage.setItem('cert_refresh_token', refreshToken);
}

function clearSession() {
  localStorage.removeItem('cert_token');
  localStorage.removeItem('cert_email');
  localStorage.removeItem('cert_token_expires_at');
  localStorage.removeItem('cert_refresh_token');
}

// Exchanges a stored refresh token for a fresh id_token, so a session doesn't just
// die with a raw 401 once the 1-hour id_token expires. Returns null if the refresh
// token itself is invalid/expired (e.g. revoked, or past Cognito's refresh-token TTL),
// in which case the caller should fall back to a full sign-in.
async function refreshAccessToken(refreshToken: string): Promise<{ id_token: string; expires_in: number } | null> {
  try {
    const r = await fetch(`${COGNITO_DOMAIN}/oauth2/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'refresh_token',
        client_id: CLIENT_ID,
        refresh_token: refreshToken,
      }),
    });
    const data = await r.json();
    if (r.ok && data.id_token) {
      return { id_token: data.id_token, expires_in: data.expires_in ?? 3600 };
    }
    return null;
  } catch {
    return null;
  }
}

function App() {
  const [user, setUser] = useState<{email: string, token: string} | null>(null);
  const [loading, setLoading] = useState(true);

  const signOut = () => {
    clearSession();
    setUser(null);
  };

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get('code');
    const returnedState = params.get('state');

    if (code) {
      const expectedState = sessionStorage.getItem('oauth_state');
      sessionStorage.removeItem('oauth_state');
      window.history.replaceState({}, '', '/');

      if (!expectedState || returnedState !== expectedState) {
        // Missing/mismatched state — this login redirect wasn't the one we initiated.
        // Don't exchange the code; bail back to the sign-in screen.
        setLoading(false);
        return;
      }

      fetch(`${COGNITO_DOMAIN}/oauth2/token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'authorization_code',
          client_id: CLIENT_ID,
          code,
          redirect_uri: REDIRECT_URI,
        }),
      })
        .then(r => r.json())
        .then(data => {
          if (data.id_token) {
            const payload = decodeJwt(data.id_token);
            const email = (payload.email as string) || '';
            storeSession(data.id_token, email, data.expires_in ?? 3600, data.refresh_token);
            setUser({ email, token: data.id_token });
          }
          setLoading(false);
        })
        .catch(() => setLoading(false));
    } else {
      const token = localStorage.getItem('cert_token');
      const email = localStorage.getItem('cert_email');
      const expiresAt = Number(localStorage.getItem('cert_token_expires_at') || 0);
      const refreshToken = localStorage.getItem('cert_refresh_token');

      if (!token || !email) {
        setLoading(false);
        return;
      }

      // Refresh a little early (60s buffer) instead of waiting for the exact expiry
      // instant — avoids a near-miss where the token dies mid-request.
      if (Date.now() < expiresAt - 60_000) {
        setUser({ email, token });
        setLoading(false);
      } else if (refreshToken) {
        refreshAccessToken(refreshToken).then(result => {
          if (result) {
            storeSession(result.id_token, email, result.expires_in, refreshToken);
            setUser({ email, token: result.id_token });
          } else {
            // Refresh token is dead too (revoked / past its own TTL) — back to sign-in.
            clearSession();
          }
          setLoading(false);
        });
      } else {
        clearSession();
        setLoading(false);
      }
    }
  }, []);

  const login = () => {
    const state = crypto.randomUUID();
    sessionStorage.setItem('oauth_state', state);
    const url = `${COGNITO_DOMAIN}/oauth2/authorize?response_type=code&client_id=${CLIENT_ID}&redirect_uri=${encodeURIComponent(REDIRECT_URI)}&scope=openid+email+profile&identity_provider=Google&state=${state}`;
    window.location.href = url;
  };

  const logout = () => {
    signOut();
    window.location.href = `${COGNITO_DOMAIN}/logout?client_id=${CLIENT_ID}&logout_uri=${encodeURIComponent(REDIRECT_URI)}`;
  };

  if (loading) return <div style={{display:'flex',justifyContent:'center',alignItems:'center',height:'100vh'}}>Loading...</div>;

  if (!user) {
    return (
      <div style={{display:'flex',flexDirection:'column',justifyContent:'center',alignItems:'center',height:'100vh',gap:'20px'}}>
        <h1>Certification Compliance Dashboard</h1>
        <p>Sign in with your @clearscale.com Google account</p>
        <button onClick={login} style={{padding:'12px 24px',fontSize:'16px',cursor:'pointer',borderRadius:'8px',border:'1px solid #ddd',background:'white',color:'#000',boxShadow:'0 2px 4px rgba(0,0,0,0.1)'}}>
          Sign in with Google
        </button>
      </div>
    );
  }

  return (
    <div>
      <header style={{display:'flex',justifyContent:'space-between',alignItems:'center',padding:'12px 24px',borderBottom:'1px solid #eee'}}>
        <h2 style={{margin:0}}>Certification Compliance Dashboard</h2>
        <div style={{display:'flex',alignItems:'center',gap:'12px'}}>
          <span>{user.email}</span>
          <button onClick={logout} style={{padding:'6px 12px',cursor:'pointer',borderRadius:'4px',border:'1px solid #ddd',background:'white',color:'#000'}}>Logout</button>
        </div>
      </header>
      <Dashboard token={user.token} onAuthError={signOut} />
    </div>
  );
}

export default App;
