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
      const delta = Math.floor((Date.now() - t0) / 1000);
      setSecs(Math.max(0, delta));
    }, 1000);
    return () => clearInterval(iv);
  }, [isFree, freeSince]);

  return (
    <div
      className={`p-4 m-2 min-w-[100px] border rounded-lg ${
        isFree
          ? 'bg-green-100 dark:bg-green-900'
          : 'bg-red-100 dark:bg-red-900'
      } ${highlight ? 'ring-4 ring-blue-500' : ''}`}
    >
      <div className="font-semibold">Spot {id}</div>
      {isFree ? (
        <div className="text-sm">Free for {secs}s</div>
      ) : (
        <div className="text-sm">Occupied</div>
      )}
    </div>
  );
}
