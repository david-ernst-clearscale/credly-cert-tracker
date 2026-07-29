import React from 'react';

export default function ComplianceBanner({ compliant }: { compliant: boolean }) {
  if (compliant) {
    return (
      <div className="bg-green-500/10 border border-green-500/30 rounded-xl p-6">
        <h2 className="text-xl font-bold text-green-400">✅ All Tiers Compliant</h2>
      </div>
    );
  }
  return (
    <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-6">
      <h2 className="text-xl font-bold text-red-400">🚨 Compliance Breach</h2>
    </div>
  );
}
