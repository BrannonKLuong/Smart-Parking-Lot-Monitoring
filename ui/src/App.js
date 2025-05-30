// Path: ui/src/App.js (V11.18 - with restored notification logic)
import React, { useEffect, useState, useRef, useCallback } from 'react';
import Header from './Header';
import Notifications from './Notifications';
import SpotTile from './SpotTile';
import SpotsEditor from './SpotsEditor';
import WebcamStreamer from './WebcamStreamer';
import './index.css';

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL || 'http://localhost:8000';
const PROCESSED_VIDEO_URL = `${API_BASE_URL}/webcam_feed`; 

// Using the V10/V11 test routes from your App.js V11.18
const API_SPOTS_SAVE_ROUTE = `${API_BASE_URL}/api/nuke_test_save`; 
const API_SPOTS_GET_ROUTE = `${API_BASE_URL}/api/spots_v10_get`; 


const NOTIFICATION_DURATION = 10000;
const POLLING_INTERVAL = 5000; // ms

export default function App() {
  const [editMode, setEditMode] = useState(false);
  const [darkMode, setDarkMode] = useState(false);
  const [spots, setSpots] = useState([]);
  const [statuses, setStatuses] = useState({}); // Stores { spotId: isOccupiedBoolean }
  const [times, setTimes] = useState({}); // Stores { spotId: freeSinceTimestamp }
  const [notes, setNotes] = useState([]); // Stores notification objects { id, spot_id, timestamp }
  const [muted, setMuted] = useState(false);
  const [filterSpot, setFilterSpot] = useState(null);
  const [streamSource, setStreamSource] = useState('processed');
  const [processedFeedKey, setProcessedFeedKey] = useState(Date.now());
  
  const prevStatusesRef = useRef({}); // Stores the previous statuses object

  const getWebSocketUrl = useCallback(() => {
    if (!API_BASE_URL) { console.error("[App.js] API_BASE_URL is not set."); return null; }
    if (API_BASE_URL.startsWith('https://')) return `wss://${API_BASE_URL.substring(8)}/ws/video_stream_upload`;
    if (API_BASE_URL.startsWith('http://')) return `ws://${API_BASE_URL.substring(7)}/ws/video_stream_upload`;
    console.error("[App.js] Cannot determine WebSocket protocol from API_BASE_URL:", API_BASE_URL);
    return null;
  }, []); 

  const webcamWebSocketUrl = getWebSocketUrl();

  const fetchSpots = useCallback(() => {
    console.log("[App.js] Fetching spots from:", API_SPOTS_GET_ROUTE);
    fetch(API_SPOTS_GET_ROUTE) 
      .then(r => {
        if (!r.ok) {
          // Improved error logging for fetch
          const errorDetail = `HTTP error ${r.status} (${r.statusText}) while fetching spots from ${API_SPOTS_GET_ROUTE}`;
          console.error(errorDetail, r);
          throw new Error(errorDetail);
        }
        return r.json();
      })
      .then(data => {
        if (data && data.spots) {
          const newApiSpots = data.spots; // raw spots from API: {id, x, y, w, h, is_available}
          
          // Prepare new spots configuration and new statuses
          const newSpotConfigs = [];
          const newStatuses = {};
          newApiSpots.forEach(spot => {
            const spotIdStr = String(spot.id);
            newSpotConfigs.push({
                id: spotIdStr, 
                x: spot.x, y: spot.y, w: spot.w, h: spot.h,
                // is_available is directly from backend, already reflects live status
            });
            newStatuses[spotIdStr] = !spot.is_available; // is_available: true means free (not occupied)
          });

          // Update times and generate notifications (this logic is from your Codebase 1 App.js)
          setTimes(prevTimes => {
            const updatedTimes = { ...prevTimes };
            const newNotes = [];

            newSpotConfigs.forEach(spotConfig => {
              const spotIdStr = spotConfig.id;
              const isNowOccupied = newStatuses[spotIdStr]; // Current occupancy
              const wasPreviouslyOccupied = prevStatusesRef.current[spotIdStr]; // Previous occupancy

              if (wasPreviouslyOccupied === true && isNowOccupied === false) { // Spot became free
                const nowTimestamp = new Date().toISOString();
                updatedTimes[spotIdStr] = nowTimestamp; // Set freeSince timestamp
                if (!muted) {
                  const notificationId = `${spotIdStr}-${nowTimestamp}-${Math.random()}`;
                  newNotes.push({ id: notificationId, spot_id: spotIdStr, timestamp: nowTimestamp });
                }
              } else if (isNowOccupied === true) { // Spot is occupied or became occupied
                delete updatedTimes[spotIdStr]; // Remove freeSince timestamp
              } else if (wasPreviouslyOccupied === false && isNowOccupied === false && !updatedTimes[spotIdStr]) {
                // Spot was free, is still free, but lost its timestamp (e.g. initial load)
                // Try to set a timestamp; if not available, SpotTile handles it.
                // This might happen if the spot was free before the app loaded.
                // For an already free spot, we might not have an exact 'became free' time from polling.
                // We could try to use a timestamp from the backend if available, or default.
                 updatedTimes[spotIdStr] = new Date().toISOString(); // Default to now if no better data
              }
            });

            if (newNotes.length > 0) {
              setNotes(prevNotes => [...newNotes, ...prevNotes.slice(0, 5 - newNotes.length)]);
            }
            return updatedTimes;
          });

          setSpots(newSpotConfigs); 
          setStatuses(newStatuses);
          
          // Update prevStatusesRef *after* all processing for this fetch is done
          prevStatusesRef.current = { ...newStatuses };

        } else { 
            console.error("Fetched spots data malformed (is_available missing or spots array not found):", data); 
        }
      })
      .catch(error => { 
          // Error logging already improved in the first .then block
          // You might want to set an error state here to display to the user
          console.error("Failed to fetch spots (network or parsing error):", error); 
      });
  }, [muted]); // Only muted is a direct dependency for notification generation

  // Initial fetch and polling
  useEffect(() => {
    if (!editMode) {
      fetchSpots(); // Initial fetch
      const intervalId = setInterval(fetchSpots, POLLING_INTERVAL);
      return () => clearInterval(intervalId); // Cleanup interval on unmount or when editMode changes
    }
  }, [editMode, fetchSpots]); // fetchSpots is memoized

  // Notification timeout handling
  useEffect(() => {
    if (notes.length === 0) return;
    const timers = notes.map(note => {
      const timer = setTimeout(() => {
        setNotes(prevNotes => prevNotes.filter(n => n.id !== note.id));
      }, NOTIFICATION_DURATION);
      return timer;
    });
    return () => {
      timers.forEach(timer => clearTimeout(timer));
    };
  }, [notes]);

  // Save spots handler (uses V11.18 logic, checks exact backend message)
  const handleSave = useCallback((updatedSpotsFromEditor) => {
    console.log("[App.js] handleSave called with (raw from editor):", updatedSpotsFromEditor);
    
    const spotsPayloadToSend = {
        spots: updatedSpotsFromEditor.map(s => ({
            id: String(s.id),
            x: Math.round(s.x),
            y: Math.round(s.y),
            w: Math.round(s.w),
            h: Math.round(s.h)
        }))
    };

    console.log("[App.js] handleSave - Sending FULL SPOTS PAYLOAD to /api/nuke_test_save:", spotsPayloadToSend);

    fetch(API_SPOTS_SAVE_ROUTE, { 
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(spotsPayloadToSend),
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
        // This check now relies on the backend sending "Spots saved to DB successfully!"
        if (data.message && data.message.includes("Spots saved to DB successfully!")) { 
          console.log('Spots save successful (matched backend message):', data);
          setEditMode(false);
          fetchSpots(); 
        } else {
          const errTxt = data.detail ? JSON.stringify(data.detail, null, 2) : (data.message || 'Save failed: Unknown structure from backend.');
          console.error('Failed to save spots (backend response issue - unexpected structure or message):', errTxt, data);
          alert(`Failed to save spots: ${errTxt}`);
        }
      })
      .catch(error => {
        console.error('Error saving spots (fetch catch):', error);
        alert(`Error saving spots: ${error.message}`); 
      });
  }, [fetchSpots]); 

  const handleWebcamStreamingActive = useCallback((isActive) => {
    if (isActive) {
        console.log("App.js: Webcam streaming reported as active. Refreshing processed feed key.");
        setProcessedFeedKey(Date.now()); 
    } else {
        console.log("App.js: Webcam streaming reported as inactive.");
    }
  }, []);

  const visibleNotes = filterSpot 
    ? notes.filter(n => String(n.spot_id) === String(filterSpot)) 
    : notes;
  const totalSpotsCount = spots.length;
  // statuses has { spotId: isOccupied }, so freeSpotsCount is where isOccupied is false
  const freeSpotsCount = Object.values(statuses).filter(isOccupied => !isOccupied).length;

  return (
    <div className={`App min-h-screen ${darkMode ? 'dark bg-gray-900 text-white' : 'bg-gray-100 text-gray-900'}`}>
      <Header darkMode={darkMode} toggleDarkMode={() => setDarkMode(prev => !prev)} freeSpots={freeSpotsCount} totalSpots={totalSpotsCount}/>
      <div className="container mx-auto px-4 py-8">
        <div className="flex justify-center mb-6 space-x-4">
          <button onClick={() => setEditMode(e => !e)} className="px-6 py-3 bg-purple-600 text-white font-semibold rounded-lg shadow-md hover:bg-purple-700 focus:outline-none focus:ring-2 focus:ring-purple-500 focus:ring-opacity-75 transition duration-150 ease-in-out">
            {editMode ? 'Cancel Spot Editing' : 'Edit Parking Spots'}
          </button>
          {!editMode && ( 
            <div className="flex space-x-2"> 
              <button 
                onClick={() => setStreamSource('processed')} 
                className={`px-4 py-3 rounded-lg font-semibold shadow-md transition duration-150 ease-in-out ${streamSource === 'processed' ? 'bg-indigo-600 text-white hover:bg-indigo-700 focus:ring-indigo-500' : 'bg-gray-300 dark:bg-gray-700 text-gray-800 dark:text-gray-200 hover:bg-gray-400 dark:hover:bg-gray-600 focus:ring-gray-400'} focus:outline-none focus:ring-2 focus:ring-opacity-75`}
              > 
                Processed Feed (File/Other) 
              </button> 
              <button 
                onClick={() => { setStreamSource('webcam'); setProcessedFeedKey(Date.now()); }} 
                className={`px-4 py-3 rounded-lg font-semibold shadow-md transition duration-150 ease-in-out ${streamSource === 'webcam' ? 'bg-teal-600 text-white hover:bg-teal-700 focus:ring-teal-500' : 'bg-gray-300 dark:bg-gray-700 text-gray-800 dark:text-gray-200 hover:bg-gray-400 dark:hover:bg-gray-600 focus:ring-gray-400'} focus:outline-none focus:ring-2 focus:ring-opacity-75`}
              > 
                Use My Webcam (Live Processed) 
              </button> 
            </div> 
          )}
        </div>

        {editMode ? ( 
          <SpotsEditor 
            initialSpots={spots} 
            videoSize={{ width: 800, height: 600 }} 
            onSave={handleSave} 
            setSpots={setSpots} /* This allows SpotsEditor to directly update spots for undo, if needed */
            apiBaseUrl={API_BASE_URL} 
          /> 
        ) : ( 
        <> 
          <Notifications 
            notes={visibleNotes} 
            onFilter={setFilterSpot} 
            muted={muted} 
            toggleMute={() => setMuted(m => !m)}
          /> 
          <main className="p-4"> 
            {streamSource === 'processed' && ( 
              <div className="flex flex-col items-center mb-8"> 
                <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">
                  Processed feed (e.g., backend `VIDEO_SOURCE_TYPE` might be "FILE").
                </p> 
                <img 
                  key={processedFeedKey} 
                  src={PROCESSED_VIDEO_URL} 
                  alt="Parking feed (processed by backend)" 
                  className="w-full max-w-2xl rounded-lg shadow-lg border-2 border-gray-300 dark:border-gray-700" 
                  onError={(e) => { e.target.alt = "Feed unavailable. Check backend."; e.target.src="https://placehold.co/640x480/2d3748/cbd5e0?text=Feed+Unavailable"; }}
                /> 
              </div> 
            )} 
            {streamSource === 'webcam' && webcamWebSocketUrl && ( 
              <div className="flex flex-col items-center mb-8 space-y-6"> 
                <p className="text-sm text-gray-600 dark:text-gray-400">
                  Ensure backend `VIDEO_SOURCE_TYPE` is set to `WEBSOCKET_STREAM`.
                </p> 
                <WebcamStreamer 
                  webSocketUrl={webcamWebSocketUrl} 
                  onStreamingActive={handleWebcamStreamingActive}
                /> 
                <div className="w-full max-w-2xl"> 
                  <h3 className="text-lg font-semibold mb-2 text-center text-gray-700 dark:text-gray-300">Processed Webcam Feed</h3> 
                  <img 
                    key={processedFeedKey} 
                    src={PROCESSED_VIDEO_URL} 
                    alt="Parking feed (processed from your webcam by backend)" 
                    className="w-full rounded-lg shadow-lg border-2 border-gray-300 dark:border-gray-700" 
                    onError={(e) => { e.target.alt = "Processed webcam feed unavailable. Ensure streaming and backend processing."; e.target.src="https://placehold.co/640x480/2d3748/cbd5e0?text=Processed+Feed+Unavailable"; }}
                  /> 
                </div> 
              </div> 
            )} 
            {streamSource === 'webcam' && !webcamWebSocketUrl && ( 
              <div className="flex justify-center mb-8">
                <div className="p-4 bg-red-100 text-red-700 rounded-lg shadow">Error: Could not derive WebSocket URL. API_BASE_URL might be misconfigured.</div>
              </div>
            )} 
            <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-6"> 
              {spots.map(spot => ( 
                <SpotTile 
                  key={spot.id} 
                  id={spot.id} 
                  isFree={!statuses[spot.id]} // Pass current occupancy status
                  freeSince={times[spot.id]}  // Pass freeSince timestamp
                  highlight={String(filterSpot) === String(spot.id)}
                /> 
              ))} 
            </div> 
          </main> 
        </> 
        )}
      </div>
    </div>
  );
}
