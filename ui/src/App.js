// Path: ui/src/App.js (V11.18 - Send Full SpotsPayload to New Route)
import React, { useEffect, useState, useRef, useCallback } from 'react';
import Header from './Header';
import Notifications from './Notifications';
import SpotTile from './SpotTile';
import SpotsEditor from './SpotsEditor';
import WebcamStreamer from './WebcamStreamer';
import './index.css';

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL || 'http://localhost:8000';
const PROCESSED_VIDEO_URL = `${API_BASE_URL}/webcam_feed`; 

// Using the V10/V11 test routes
const API_SPOTS_SAVE_ROUTE = `${API_BASE_URL}/api/nuke_test_save`; 
const API_SPOTS_GET_ROUTE = `${API_BASE_URL}/api/spots_v10_get`; 


const NOTIFICATION_DURATION = 10000;
const POLLING_INTERVAL = 5000;

export default function App() {
  const [editMode, setEditMode] = useState(false);
  const [darkMode, setDarkMode] = useState(false);
  const [spots, setSpots] = useState([]);
  const [statuses, setStatuses] = useState({});
  const [times, setTimes] = useState({});
  const [notes, setNotes] = useState([]);
  const [muted, setMuted] = useState(false);
  const [filterSpot, setFilterSpot] = useState(null);
  const [streamSource, setStreamSource] = useState('processed');
  const [processedFeedKey, setProcessedFeedKey] = useState(Date.now());
  const prevStatusesRef = useRef({});

  const getWebSocketUrl = useCallback(() => {
    if (!API_BASE_URL) { console.error("[App.js V11.18] API_BASE_URL is not set."); return null; }
    if (API_BASE_URL.startsWith('https://')) return `wss://${API_BASE_URL.substring(8)}/ws/video_stream_upload`;
    if (API_BASE_URL.startsWith('http://')) return `ws://${API_BASE_URL.substring(7)}/ws/video_stream_upload`;
    console.error("[App.js V11.18] Cannot determine WebSocket protocol from API_BASE_URL:", API_BASE_URL);
    return null;
  }, []); 

  const webcamWebSocketUrl = getWebSocketUrl();

  const fetchSpots = useCallback(() => {
    console.log("[App.js V11.18] Fetching spots from:", API_SPOTS_GET_ROUTE);
    fetch(API_SPOTS_GET_ROUTE) 
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status} fetching spots from ${API_SPOTS_GET_ROUTE}`);
        return r.json();
      })
      .then(data => {
        if (data && data.spots) {
          const newSpotsFromApi = data.spots;
          const newSpotConfigs = newSpotsFromApi.map(s => ({id: String(s.id), x:s.x, y:s.y, w:s.w, h:s.h, is_available: s.is_available !== undefined ? s.is_available : true }));
          const newStatuses = {}; 
          setTimes(prevTimes => { /* ... time update logic ... */ return prevTimes; }); 
          console.log("[App.js V11.18] fetchSpots - Setting spots from API:", newSpotConfigs);
          setSpots(newSpotConfigs); 
          setStatuses(newStatuses);
          prevStatusesRef.current = newStatuses;
        } else { console.error("Fetched spots data malformed (V11.18 GET):", data); }
      })
      .catch(error => { console.error("Failed to fetch spots (V11.18 GET):", error); });
  }, [muted]); 

  useEffect(() => {
    if (!editMode) { fetchSpots(); const iid = setInterval(fetchSpots, POLLING_INTERVAL); return () => clearInterval(iid); }
  }, [editMode, fetchSpots]);

  useEffect(() => {
    if (notes.length === 0) return;
    const t = notes.map(n => setTimeout(() => setNotes(p => p.filter(i => i.id !== n.id)), NOTIFICATION_DURATION));
    return () => t.forEach(clearTimeout);
  }, [notes]);

  const handleSave = useCallback((updatedSpotsFromEditor) => {
    console.log("[App.js V11.18] handleSave called with (raw from editor):", updatedSpotsFromEditor);
    
    // Sending the full spots payload, matching SpotsUpdateRequest on backend
    const spotsPayloadToSend = {
        spots: updatedSpotsFromEditor.map(s => ({
            id: String(s.id),
            x: Math.round(s.x),
            y: Math.round(s.y),
            w: Math.round(s.w),
            h: Math.round(s.h)
        }))
    };

    console.log("[App.js V11.18] handleSave - Sending FULL SPOTS PAYLOAD to /api/nuke_test_save:", spotsPayloadToSend);

    fetch(API_SPOTS_SAVE_ROUTE, { 
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(spotsPayloadToSend), // Send the full spots payload
    })
      .then(r => {
        if (!r.ok) {
            return r.json().then(errData => { 
                const detail = errData.detail || `HTTP error ${r.status}`;
                const errTxt = Array.isArray(detail) ? JSON.stringify(detail, null, 2) : String(detail);
                throw new Error(`Error from ${API_SPOTS_SAVE_ROUTE}: ${errTxt}`);
            }).catch((jsonParseError) => { 
                throw new Error(`HTTP error ${r.status} from ${API_SPOTS_SAVE_ROUTE}. Response not JSON. Check backend logs. Error: ${jsonParseError.message}`);
            });
        }
        return r.json();
      })
      .then(data => {
        // Match the success message from main.py V11.18 backend
        if (data.message && data.message.includes("Spots saved to DB successfully!")) { 
          console.log('V11.18 Spots save (full payload) successful:', data);
          setEditMode(false);
          fetchSpots(); // Refresh spots from DB after successful save
        } else {
          const errTxt = data.detail ? JSON.stringify(data.detail, null, 2) : (data.message || 'Save failed: Unknown structure from V11.18 backend.');
          console.error('Failed to save spots (V11.18 backend response issue):', errTxt);
          alert(`Failed to save spots (V11.18): ${errTxt}`);
        }
      })
      .catch(error => {
        console.error('Error saving spots (V11.18 fetch catch):', error);
        alert(`Error saving spots (V11.18): ${error.message}`); 
      });
  }, [fetchSpots]); 

  const handleWebcamStreamingActive = useCallback((isActive) => {
    if (isActive) setProcessedFeedKey(Date.now());
  }, []);

  const visibleNotes = filterSpot ? notes.filter(n => String(n.spot_id) === String(filterSpot)) : notes;
  const totalSpotsCount = spots.length;
  const freeSpotsCount = Object.values(statuses).filter(isOccupied => !isOccupied).length;

  return (
    <div className={`App min-h-screen ${darkMode ? 'dark bg-gray-900 text-white' : 'bg-gray-100 text-gray-900'}`}>
      <Header darkMode={darkMode} toggleDarkMode={() => setDarkMode(prev => !prev)} freeSpots={freeSpotsCount} totalSpots={totalSpotsCount}/>
      <div className="container mx-auto px-4 py-8">
        <div className="flex justify-center mb-6 space-x-4">
          <button onClick={() => setEditMode(e => !e)} className="px-6 py-3 bg-purple-600 text-white font-semibold rounded-lg shadow-md hover:bg-purple-700 focus:outline-none focus:ring-2 focus:ring-purple-500 focus:ring-opacity-75 transition duration-150 ease-in-out">
            {editMode ? 'Cancel Spot Editing' : 'Edit Parking Spots'}
          </button>
          {!editMode && ( <div className="flex space-x-2"> <button onClick={() => setStreamSource('processed')} className={`px-4 py-3 rounded-lg font-semibold shadow-md transition duration-150 ease-in-out ${streamSource === 'processed' ? 'bg-indigo-600 text-white hover:bg-indigo-700 focus:ring-indigo-500' : 'bg-gray-300 dark:bg-gray-700 text-gray-800 dark:text-gray-200 hover:bg-gray-400 dark:hover:bg-gray-600 focus:ring-gray-400'} focus:outline-none focus:ring-2 focus:ring-opacity-75`}> Processed Feed (File/Other) </button> <button onClick={() => { setStreamSource('webcam'); setProcessedFeedKey(Date.now()); }} className={`px-4 py-3 rounded-lg font-semibold shadow-md transition duration-150 ease-in-out ${streamSource === 'webcam' ? 'bg-teal-600 text-white hover:bg-teal-700 focus:ring-teal-500' : 'bg-gray-300 dark:bg-gray-700 text-gray-800 dark:text-gray-200 hover:bg-gray-400 dark:hover:bg-gray-600 focus:ring-gray-400'} focus:outline-none focus:ring-2 focus:ring-opacity-75`}> Use My Webcam (Live Processed) </button> </div> )}
        </div>
        {editMode ? ( <SpotsEditor initialSpots={spots} videoSize={{ width: 800, height: 600 }} onSave={handleSave} setSpots={setSpots} apiBaseUrl={API_BASE_URL} /> ) : ( <> <Notifications notes={visibleNotes} onFilter={setFilterSpot} muted={muted} toggleMute={() => setMuted(m => !m)}/> <main className="p-4"> {streamSource === 'processed' && ( <div className="flex flex-col items-center mb-8"> <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">Processed feed (backend `VIDEO_SOURCE_TYPE`="FILE").</p> <img key={processedFeedKey} src={PROCESSED_VIDEO_URL} alt="Parking feed (processed)" className="w-full max-w-2xl rounded-lg shadow-lg border-2 border-gray-300 dark:border-gray-700" onError={(e) => { e.target.alt = "Feed unavailable."; e.target.src="https://placehold.co/640x480/2d3748/cbd5e0?text=Feed+Unavailable"; }}/> </div> )} {streamSource === 'webcam' && webcamWebSocketUrl && ( <div className="flex flex-col items-center mb-8 space-y-6"> <p className="text-sm text-gray-600 dark:text-gray-400">Backend `VIDEO_SOURCE_TYPE` must be `WEBSOCKET_STREAM`.</p> <WebcamStreamer webSocketUrl={webcamWebSocketUrl} onStreamingActive={handleWebcamStreamingActive}/> <div className="w-full max-w-2xl"> <h3 className="text-lg font-semibold mb-2 text-center text-gray-700 dark:text-gray-300">Processed Webcam Feed</h3> <img key={processedFeedKey} src={PROCESSED_VIDEO_URL} alt="Parking feed (processed webcam)" className="w-full rounded-lg shadow-lg border-2 border-gray-300 dark:border-gray-700" onError={(e) => { e.target.alt = "Processed webcam feed unavailable."; e.target.src="https://placehold.co/640x480/2d3748/cbd5e0?text=Processed+Feed+Unavailable"; }}/> </div> </div> )} {streamSource === 'webcam' && !webcamWebSocketUrl && ( <div className="flex justify-center mb-8"><div className="p-4 bg-red-100 text-red-700 rounded-lg shadow">Error: Could not derive WebSocket URL.</div></div>)} <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-6"> {spots.map(spot => ( <SpotTile key={spot.id} id={spot.id} isFree={!statuses[spot.id]} freeSince={times[spot.id]} highlight={String(filterSpot) === String(spot.id)}/> ))} </div> </main> </> )}
      </div>
    </div>
  );
}
