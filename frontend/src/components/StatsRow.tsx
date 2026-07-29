import React from 'react';

export default function StatsRow({ totalCerts, totalUsers }: { totalCerts: number; totalUsers: number }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <span className="text-xs text-gray-400">Total Certs</span>
        <p className="text-2xl font-bold text-white">{totalCerts}</p>
      </div>
      <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
        <span className="text-xs text-gray-400">Users</span>
        <p className="text-2xl font-bold text-white">{totalUsers}</p>
      </div>
    </div>
  );
}
