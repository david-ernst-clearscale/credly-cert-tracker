export interface DashboardTokenPayload {
  email: string;
  expiresAt: number;
}

function decodeBase64UrlJson(segment: string): unknown {
  const base64 = segment.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64 + '='.repeat((4 - base64.length % 4) % 4);
  return JSON.parse(atob(padded));
}

export function parseDashboardToken(token: string, nowMs = Date.now()): DashboardTokenPayload | null {
  try {
    const segment = token.split('.')[1];
    if (!segment) return null;

    const payload = decodeBase64UrlJson(segment);
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;

    const { email, exp } = payload as { email?: unknown; exp?: unknown };
    if (typeof email !== 'string' || !email.trim()) return null;
    if (typeof exp !== 'number' || !Number.isFinite(exp)) return null;

    const expiresAt = exp * 1000;
    if (expiresAt <= nowMs) return null;

    return { email: email.trim(), expiresAt };
  } catch {
    return null;
  }
}
