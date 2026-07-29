import React, { createContext, useContext, useState } from 'react';

interface WebSocketContextType {
  connected: boolean;
}

const WebSocketContext = createContext<WebSocketContextType>({ connected: false });

export function WebSocketProvider({ children }: { children: React.ReactNode }) {
  const [connected] = useState(false);
  return (
    <WebSocketContext.Provider value={{ connected }}>
      {children}
    </WebSocketContext.Provider>
  );
}

export function useWebSocket() {
  return useContext(WebSocketContext);
}
