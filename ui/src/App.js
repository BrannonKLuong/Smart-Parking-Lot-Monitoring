// ui/src/App.js
import React, { useEffect, useState } from 'react';
import Header        from './Header';
import Notifications from './Notifications';
import SpotTile      from './SpotTile';
import SpotsEditor   from './SpotsEditor';
import './index.css';

const API_SPOTS = '/api/spots';
const WS_URL     = 'ws://localhost:8000/ws';
const VIDEO_URL  = 'http://localhost:8000/webcam_feed';

export default function App() {
  const [editMode,  setEditMode ]  = useState(false);
  const [darkMode,  setDarkMode ]  = useState(false);
  const [spots,     setSpots    ]  = useState([]);
  const [statuses,  setStatuses ]  = useState({});
  const [times,     setTimes    ]  = useState({});
  const [notes,     setNotes    ]  = useState([]);
  const [muted,     setMuted    ]  = useState(false);
  const [filterSpot,setFilterSpot] = useState(null);

  // Load spots.json (IDs + initial occupied=true)
  useEffect(() => {
    fetch(API_SPOTS)
      .then(r => r.json())
      .then(data => {
        setSpots(data.spots.map(s => s.id));
        setStatuses(Object.fromEntries(data.spots.map(s => [s.id, true])));
      });
  }, []);

  // WebSocket for live vacancy/occupancy (only in live mode)
  useEffect(() => {
    if (editMode) return;
    const ws = new WebSocket(WS_URL);
    ws.onopen    = () => console.log('✅ WS connected');
    ws.onmessage = ev => {
      const { spot_id, timestamp, status } = JSON.parse(ev.data);
      const ts = timestamp.endsWith('Z') ? timestamp : timestamp + 'Z';
      if (status === 'occupied') {
        setStatuses(s => ({ ...s, [spot_id]: true }));
      } else {
        setStatuses(s => ({ ...s, [spot_id]: false }));
        setTimes   (t => ({ ...t, [spot_id]: ts }));
        if (!muted) {
          setNotes(n => [...n, { spot_id, timestamp: ts }]);
          setTimeout(() => setNotes(n => n.filter(x => x.spot_id !== spot_id)), 5000);
        }
      }
    };
    ws.onerror = e => console.error('❌ WS error', e);
    ws.onclose = () => console.warn('⚠️ WS closed');
    return () => ws.close();
  }, [muted, editMode]);

  const visibleNotes = filterSpot
    ? notes.filter(n => n.spot_id === filterSpot)
    : notes;

  // Save handler for SpotsEditor
  const handleSave = async config => {
    const res = await fetch(API_SPOTS, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(config),
    });
    if (res.ok) {
      setEditMode(false);
      // reload spots
      const data = await res.json().then(() => fetch(API_SPOTS).then(r=>r.json()));
      setSpots(data.spots.map(s => s.id));
      setStatuses(Object.fromEntries(data.spots.map(s => [s.id, true])));
    } else {
      alert('Failed to save layout');
    }
  };

  return (
    <div className={darkMode ? 'dark' : ''}>
      <div className="min-h-screen bg-gray-50 dark:bg-gray-900 text-gray-900 dark:text-gray-100">
        <Header
          darkMode={darkMode}
          toggleDarkMode={()=>setDarkMode(d=>!d)}
          totalSpots={spots.length}
          freeSpots={spots.filter(id=>!statuses[id]).length}
        />

        <div className="p-4">
          <button
            onClick={()=>setEditMode(e=>!e)}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg"
          >
            {editMode ? 'Cancel Edit' : 'Edit Spots'}
          </button>
        </div>

        {editMode
          ? <SpotsEditor
              videoSize={{width:800, height:600}}
              onSave={handleSave}
            />
          : <>
              <Notifications
                notes={visibleNotes}
                onFilter={setFilterSpot}
                muted={muted}
                toggleMute={()=>setMuted(m=>!m)}
              />
              <main className="p-4">
                <div className="flex justify-center mb-8">
                  <img
                    src={VIDEO_URL}
                    alt="Live feed"
                    className="w-full max-w-xl rounded-lg shadow"
                  />
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
                  {spots.map(id => (
                    <SpotTile
                      key={id}
                      id={id}
                      isFree={!statuses[id]}
                      freeSince={times[id]}
                      highlight={filterSpot===id}
                    />
                  ))}
                </div>
              </main>
            </>
        }
      </div>
    </div>
  );
}
