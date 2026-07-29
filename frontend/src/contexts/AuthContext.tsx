import React, { createContext, useContext, useState } from 'react';

interface AuthContextType {
  user: { id: string; email: string; role: string } | null;
}

const AuthContext = createContext<AuthContextType>({ user: null });

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user] = useState(null);
  return <AuthContext.Provider value={{ user }}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  return useContext(AuthContext);
}
