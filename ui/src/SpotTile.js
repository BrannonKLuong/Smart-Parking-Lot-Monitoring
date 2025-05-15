import React, { useEffect, useState } from 'react';

export default function SpotTile({ id, isFree, freeSince, highlight }) {
  const [secs, setSecs] = useState(0);

  useEffect(() => {
    // If the spot is not free, reset the counter and clear any interval
    if (!isFree) {
      setSecs(0);
      return; 
    }

    // Check if freeSince is a valid date string before creating a Date object
    const freeDate = new Date(freeSince);
    const t0 = freeDate.getTime();

    // If t0 is NaN (invalid date), don't start the interval
    if (isNaN(t0)) {
        console.warn(`SpotTile ${id}: Invalid freeSince timestamp received: ${freeSince}`);
        setSecs(0); 
        return;
    }

    // Start the interval only if the spot is free and the timestamp is valid
    const iv = setInterval(() => {
      const delta = Math.max(0, Math.floor((Date.now() - t0) / 1000));
      setSecs(delta);
    }, 1000);

    // Clean up the interval when the component unmounts or dependencies change
    return () => {
      clearInterval(iv);
      console.log(`SpotTile ${id}: Counter interval cleared.`); // Added log
    };

  }, [isFree, freeSince, id]); 

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
        <div className="text-sm">{!isNaN(secs) ? `Free for ${secs}s` : 'Status unknown'}</div>
      ) : (
        <div className="text-sm">Occupied</div>
      )}
    </div>
  );
}
