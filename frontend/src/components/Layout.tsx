import React from 'react';

export default function Layout({ children }: { children: React.ReactNode }) {
  return <div className="min-h-screen bg-gray-900 text-white">{children}</div>;
}
