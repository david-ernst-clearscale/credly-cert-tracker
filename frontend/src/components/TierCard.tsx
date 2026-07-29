import React from 'react';

interface TierCardProps {
  name: string;
  current: number;
  required: number;
  riskLevel: 'GREEN' | 'YELLOW' | 'RED';
}

export default function TierCard({ name, current, required, riskLevel }: TierCardProps) {
  const colors = {
    GREEN: 'border-green-500 text-green-400',
    YELLOW: 'border-yellow-500 text-yellow-400',
    RED: 'border-red-500 text-red-400',
  };

  return (
    <div className={`rounded-xl border ${colors[riskLevel]} bg-gray-800 p-6`}>
      <h3 className="text-lg font-semibold text-white">{name}</h3>
      <p className={`text-4xl font-bold mt-2 ${colors[riskLevel]}`}>
        {current} <span className="text-gray-400 text-lg">/ {required}</span>
      </p>
    </div>
  );
}
