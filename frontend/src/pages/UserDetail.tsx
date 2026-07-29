import React from 'react';
import { useParams, Link } from 'react-router-dom';

export default function UserDetail() {
  const { employeeId } = useParams<{ employeeId: string }>();

  return (
    <div className="space-y-6">
      <Link to="/" className="text-gray-400 hover:text-white text-sm">← Back to Dashboard</Link>
      <h2 className="text-2xl font-bold text-white">Certifications for {employeeId}</h2>
      <p className="text-gray-400">Connect WebSocket to load user data.</p>
    </div>
  );
}
