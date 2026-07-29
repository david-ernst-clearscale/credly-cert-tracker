import React from 'react';

export default function ActivityFeed({ changes }: { changes: any[] }) {
  if (!changes.length) {
    return (
      <div className="bg-gray-800 rounded-xl border border-gray-700 p-6">
        <h3 className="text-lg font-semibold text-white">📡 Live Activity</h3>
        <p className="text-gray-400 text-sm mt-4">No changes yet.</p>
      </div>
    );
  }
  return (
    <div className="bg-gray-800 rounded-xl border border-gray-700 p-6">
      <h3 className="text-lg font-semibold text-white mb-4">📡 Live Activity</h3>
      {changes.map((c, i) => (
        <div key={i} className="text-sm text-gray-300 py-1">{c.certification_name} — {c.new_status}</div>
      ))}
    </div>
  );
}
