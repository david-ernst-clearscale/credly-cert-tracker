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

function App() {
  const [user, setUser] = useState<{email: string, token: string} | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const code = params.get('code');

    if (code) {
      window.history.replaceState({}, '', '/');
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
            setUser({ email, token: data.id_token });
            localStorage.setItem('cert_token', data.id_token);
            localStorage.setItem('cert_email', email);
          }
          setLoading(false);
        })
        .catch(() => setLoading(false));
    } else {
      const token = localStorage.getItem('cert_token');
      const email = localStorage.getItem('cert_email');
      if (token && email) {
        setUser({ email, token });
      }
      setLoading(false);
    }
  }, []);

  const login = () => {
    const url = `${COGNITO_DOMAIN}/oauth2/authorize?response_type=code&client_id=${CLIENT_ID}&redirect_uri=${encodeURIComponent(REDIRECT_URI)}&scope=openid+email+profile&identity_provider=Google`;
    window.location.href = url;
  };

  const logout = () => {
    localStorage.removeItem('cert_token');
    localStorage.removeItem('cert_email');
    setUser(null);
    window.location.href = `${COGNITO_DOMAIN}/logout?client_id=${CLIENT_ID}&logout_uri=${encodeURIComponent(REDIRECT_URI)}`;
  };

  if (loading) return <div style={{display:'flex',justifyContent:'center',alignItems:'center',height:'100vh'}}>Loading...</div>;

  if (!user) {
    return (
      <div style={{display:'flex',flexDirection:'column',justifyContent:'center',alignItems:'center',height:'100vh',gap:'20px'}}>
        <h1>Certification Compliance Dashboard</h1>
        <p>Sign in with your @clearscale.com Google account</p>
        <button onClick={login} style={{padding:'12px 24px',fontSize:'16px',cursor:'pointer',borderRadius:'8px',border:'1px solid #ddd',background:'white',boxShadow:'0 2px 4px rgba(0,0,0,0.1)'}}>
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
          <button onClick={logout} style={{padding:'6px 12px',cursor:'pointer',borderRadius:'4px',border:'1px solid #ddd',background:'white'}}>Logout</button>
        </div>
      </header>
      <Dashboard token={user.token} />
    </div>
  );
}

export default App;
