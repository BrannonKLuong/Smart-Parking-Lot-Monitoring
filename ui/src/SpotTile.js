import React, { useEffect, useState } from 'react';

export default function SpotTile({ id, isFree, freeSince, highlight }) {
  const [secs, setSecs] = useState(0);

  useEffect(() => {
    if (!isFree) {
      setSecs(0);
      return;
    }
    const t0 = new Date(freeSince).getTime();
    const iv = setInterval(() => {
      const delta = Math.max(0, Math.floor((Date.now() - t0) / 1000));
      setSecs(delta);
    }, 1000);
    return () => clearInterval(iv);
  }, [isFree, freeSince]);

  return (
    <div
      className={`
        p-4 flex flex-col items-center justify-center 
        rounded-lg shadow cursor-pointer
        ${isFree 
          ? 'bg-green-200 text-green-800' 
          : 'bg-red-200 text-red-800'}
        ${highlight ? 'ring-4 ring-blue-500' : ''}
      `}
      onClick={() => {/* optional: filter by this spot */}}
    >
      <div className="font-semibold mb-2">Spot {id}</div>
      {isFree ? (
        <div className="text-sm">Free for {secs}s</div>
      ) : (
        <div className="text-sm">Occupied</div>
      )}
    </div>
  );
}
