import React, { useEffect, useState } from 'react';

export default function SpotTile({ id, isFree, freeSince }) {
  const [secs, setSecs] = useState(0);

  useEffect(() => {
    if (!isFree) {
      setSecs(0);
      return;
    }
    // parse freeSince as UTC
    const t0 = new Date(freeSince).getTime();
    const iv = setInterval(() => {
      const delta = Math.floor((Date.now() - t0) / 1000);
      setSecs(Math.max(0, delta));  // clamp to ≥ 0
    }, 1000);
    return () => clearInterval(iv);
  }, [isFree, freeSince]);

  return (
    <div style={{
      border: '1px solid #666', borderRadius: 4,
      padding: 8, margin: 4, minWidth: 100,
      background: isFree ? '#d4f8d4' : '#f8d4d4'
    }}>
      <div>Spot {id}</div>
      {isFree
        ? <div>Free for {secs}s</div>
        : <div>Occupied</div>
      }
    </div>
  );
}
