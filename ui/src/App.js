import React, { useEffect, useState } from 'react';
import Header from './Header';
import Notifications from './Notifications';
import SpotTile from './SpotTile';
import './index.css';

function App() {
  const [darkMode, setDarkMode]     = useState(false);
  const [spots, setSpots]           = useState([]);
  const [statuses, setStatuses]     = useState({});
  const [times, setTimes]           = useState({});
  const [notes, setNotes]           = useState([]);
  const [muted, setMuted]           = useState(false);
  const [filterSpot, setFilterSpot] = useState(null);

  // Load spot IDs
  useEffect(() => {
    fetch('/spots.json')
      .then(r => r.json())
      .then(data => {
        const ids = data.spots.map(s => s.id);
        setSpots(ids);
        setStatuses(Object.fromEntries(ids.map(id => [id, true])));
      });
  }, []);

  // WebSocket
  useEffect(() => {
    const ws = new WebSocket('ws://localhost:8000/ws');
    ws.onopen    = () => console.log('WS connected');
    ws.onmessage = ev => {
      const { spot_id, timestamp, status } = JSON.parse(ev.data);
      const t = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z';
      if (status === 'occupied') {
        setStatuses(s => ({ ...s, [spot_id]: true }));
      } else {
        setStatuses(s => ({ ...s, [spot_id]: false }));
        setTimes   (tms => ({ ...tms, [spot_id]: t }));
        if (!muted) {
          setNotes(n => [...n, { spot_id, timestamp: t }]);
          setTimeout(() => {
            setNotes(n => n.filter(x => x.spot_id !== spot_id));
          }, 5000);
        }
      }
    };
    ws.onerror = e => console.error('WS error', e);
    ws.onclose = () => console.warn('WS closed');
    return () => ws.close();
  }, [muted]);

  const visibleNotes = filterSpot
    ? notes.filter(n => n.spot_id === filterSpot)
    : notes;

  return (
    <div className={darkMode ? 'dark' : ''}>
      <div className="min-h-screen bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-gray-100">
        <Header
          darkMode={darkMode}
          toggleDarkMode={() => setDarkMode(dm => !dm)}
          totalSpots={spots.length}
          freeSpots={spots.filter(id => !statuses[id]).length}
        />
        <Notifications
          notes={visibleNotes}
          onFilter={setFilterSpot}
          muted={muted}
          toggleMute={() => setMuted(m => !m)}
        />

        <main className="p-4">
          {/* Live MJPEG feed */}
          <div className="flex justify-center">
            <img
              src="http://localhost:8000/webcam_feed"
              alt="Live feed"
              className="w-full max-w-xl rounded-lg shadow"
            />
          </div>

          {/* Responsive grid of SpotTiles */}
          <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 mt-6">
            {spots.map(id => (
              <SpotTile
                key={id}
                id={id}
                isFree={!statuses[id]}
                freeSince={times[id]}
                highlight={filterSpot === id}
              />
            ))}
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;
