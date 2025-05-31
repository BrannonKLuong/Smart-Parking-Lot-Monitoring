// Path: ui/src/App.js (Polling Re-enabled)
import React, { useEffect, useState, useRef, useCallback } from 'react';
import Header from './Header'; 
import Notifications from './Notifications';
import SpotTile from './SpotTile';
import SpotsEditor from './SpotsEditor';
import WebcamStreamer from './WebcamStreamer';
import './index.css';

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL || 'http://localhost:8000';
const PROCESSED_VIDEO_URL = `${API_BASE_URL}/webcam_feed`; 

const API_SPOTS_SAVE_ROUTE = `${API_BASE_URL}/api/nuke_test_save`; 
const API_SPOTS_GET_ROUTE = `${API_BASE_URL}/api/spots_v10_get`; 

const NOTIFICATION_DURATION = 10000;
const POLLING_INTERVAL = 5000; // ms (Re-enabled)

export default function App() {
  const [editMode, setEditMode] = useState(false);
  const [spots, setSpots] = useState([]);
  const [statuses, setStatuses] = useState({}); 
  const [times, setTimes] = useState({});     
  const [notes, setNotes] = useState([]);     
  const [muted, setMuted] = useState(false);
  const [filterSpot, setFilterSpot] = useState(null);
  const [streamSource, setStreamSource] = useState('webcam'); 
  const [processedFeedKey, setProcessedFeedKey] = useState(Date.now());
  
  const prevStatusesRef = useRef({}); 

  const getWebSocketUrl = useCallback(() => {
    if (!API_BASE_URL) { console.error("[App.js] API_BASE_URL is not set."); return null; }
    if (API_BASE_URL.startsWith('https://')) return `wss://${API_BASE_URL.substring(8)}/ws/video_stream_upload`;
    if (API_BASE_URL.startsWith('http://')) return `ws://${API_BASE_URL.substring(7)}/ws/video_stream_upload`;
    console.error("[App.js] Cannot determine WebSocket protocol from API_BASE_URL:", API_BASE_URL);
    return null;
  }, []); 

  const webcamWebSocketUrl = getWebSocketUrl();
  // console.log("[App.js] Derived webcamWebSocketUrl:", webcamWebSocketUrl, "from API_BASE_URL:", API_BASE_URL);


  const fetchSpots = useCallback(() => {
    // console.log(`[App.js ${new Date().toLocaleTimeString()}] Fetching spots... Current muted state: ${muted}`);
    fetch(API_SPOTS_GET_ROUTE) 
      .then(r => {
        if (!r.ok) {
          const errorDetail = `HTTP error ${r.status} (${r.statusText}) while fetching spots from ${API_SPOTS_GET_ROUTE}`;
          console.error(errorDetail, r);
          throw new Error(errorDetail);
        }
        return r.json();
      })
      .then(data => {
        if (data && data.spots) {
          const newApiSpots = data.spots; 
          const newSpotConfigs = [];
          const currentFetchStatuses = {}; 
          newApiSpots.forEach(spot => {
            const spotIdStr = String(spot.id);
            newSpotConfigs.push({
                id: spotIdStr, 
                x: spot.x, y: spot.y, w: spot.w, h: spot.h,
            });
            currentFetchStatuses[spotIdStr] = !spot.is_available; 
          });

          const newNotesToGenerate = [];
          setTimes(prevTimes => {
            const newTimesData = {...prevTimes}; 
            newSpotConfigs.forEach(spotConfig => {
              const spotIdStr = spotConfig.id;
              const isNowOccupied = currentFetchStatuses[spotIdStr];
              const wasPreviouslyOccupied = prevStatusesRef.current[spotIdStr]; 

              if (wasPreviouslyOccupied === true && isNowOccupied === false) { 
                const nowTimestamp = new Date().toISOString();
                newTimesData[spotIdStr] = nowTimestamp; 
                if (!muted) {
                  const notificationId = `${spotIdStr}-${nowTimestamp}-${Math.random()}`;
                  newNotesToGenerate.push({ id: notificationId, spot_id: spotIdStr, timestamp: nowTimestamp });
                }
              } else if (isNowOccupied === true) { 
                delete newTimesData[spotIdStr]; 
              } else if (isNowOccupied === false && newTimesData[spotIdStr] === undefined) {
                 newTimesData[spotIdStr] = new Date().toISOString(); 
              }
            });
            return newTimesData;
          });
          
          setSpots(newSpotConfigs);
          setStatuses(currentFetchStatuses);

          if (newNotesToGenerate.length > 0) {
            setNotes(prevNotesState => {
              const combinedNotes = [...newNotesToGenerate, ...prevNotesState];
              return combinedNotes.slice(0, 5); 
            });
          }
          prevStatusesRef.current = { ...currentFetchStatuses };
        } else { 
            console.error("[App.js] Fetched spots data malformed:", data); 
        }
      })
      .catch(error => { 
          console.error("[App.js] Failed to fetch spots (network or parsing error):", error); 
      });
  }, [muted]); 

  useEffect(() => {
    // console.log("[App.js] Effect for initial fetchSpots and Polling. editMode:", editMode);
    if (!editMode) {
      const timerId = setTimeout(() => {
        // console.log("[App.js] Executing delayed initial fetchSpots.");
        fetchSpots();
      }, 1000); 

      const intervalId = setInterval(fetchSpots, POLLING_INTERVAL); // Polling re-enabled
      
      return () => {
        clearTimeout(timerId);
        clearInterval(intervalId); // Cleanup polling interval
      };
    }
  }, [editMode, fetchSpots]); 

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

  const handleSave = useCallback((updatedSpotsFromEditor) => {
    const spotsPayloadToSend = {
        spots: updatedSpotsFromEditor.map(s => ({
            id: String(s.id),
            x: Math.round(s.x),
            y: Math.round(s.y),
            w: Math.round(s.w),
            h: Math.round(s.h)
        }))
    };
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
        if (data.message && data.message.includes("Spots saved to DB successfully!")) { 
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
        setProcessedFeedKey(Date.now()); 
    }
  }, []);

  const visibleNotes = filterSpot 
    ? notes.filter(n => String(n.spot_id) === String(filterSpot)) 
    : notes;
  const totalSpotsCount = spots.length;
  const freeSpotsCount = Object.values(statuses).filter(isOccupied => !isOccupied).length;

  return (
    <div className={`App min-h-screen bg-gray-100 text-gray-900`}> 
      <Header 
        freeSpots={freeSpotsCount} 
        totalSpots={totalSpotsCount}
      />
      <div className="container mx-auto px-4 py-8">
        <div className="flex justify-center mb-6 space-x-4">
          <button 
            onClick={() => setEditMode(e => !e)} 
            className="px-6 py-3 bg-purple-600 text-white font-semibold rounded-lg shadow-md hover:bg-purple-700 focus:outline-none focus:ring-2 focus:ring-purple-500 focus:ring-opacity-75 transition duration-150 ease-in-out"
          >
            {editMode ? 'Cancel Spot Editing' : 'Edit Parking Spots'}
          </button>
          {!editMode && ( 
            <button 
              onClick={() => { 
                if (streamSource !== 'webcam') setStreamSource('webcam'); 
                setProcessedFeedKey(Date.now()); 
              }} 
              className={`px-4 py-3 rounded-lg font-semibold shadow-md transition duration-150 ease-in-out bg-teal-600 text-white hover:bg-teal-700 focus:ring-teal-500 focus:outline-none focus:ring-2 focus:ring-opacity-75`}
            > 
              Activate/Refresh Webcam Feed
            </button> 
          )}
        </div>

        {streamSource === 'webcam' && webcamWebSocketUrl && (
          <div className={editMode ? "hidden" : "block mb-6"}> 
            <WebcamStreamer
              webSocketUrl={webcamWebSocketUrl}
              onStreamingActive={handleWebcamStreamingActive} 
            />
          </div>
        )}
        {streamSource === 'webcam' && !webcamWebSocketUrl && !editMode && ( 
          <div className="flex justify-center mb-8">
            <div className="p-4 bg-red-100 text-red-700 rounded-lg shadow">Error: Could not derive WebSocket URL. API_BASE_URL might be misconfigured.</div>
          </div>
        )}

        {editMode ? ( 
          <SpotsEditor 
            initialSpots={spots} 
            videoSize={{ width: 800, height: 600 }} 
            onSave={handleSave} 
            setSpots={setSpots} 
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
              {streamSource === 'webcam' && webcamWebSocketUrl && (
                <div className="flex flex-col items-center mb-8">
                  <h3 className="text-lg font-semibold mb-2 text-center text-gray-700">Processed Webcam Feed</h3> 
                  <img 
                    key={processedFeedKey} 
                    src={PROCESSED_VIDEO_URL} 
                    alt="Parking feed (processed from your webcam by backend)" 
                    className="w-full max-w-2xl rounded-lg shadow-lg border-2 border-gray-300" 
                    onError={(e) => { e.target.alt = "Processed webcam feed unavailable. Ensure streaming and backend processing."; e.target.src="https://placehold.co/640x480/2d3748/cbd5e0?text=Processed+Feed+Unavailable"; }}
                  /> 
                </div>
              )}
              
              <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-6"> 
                {spots.map(spot => ( 
                  <SpotTile 
                    key={spot.id} 
                    id={spot.id} 
                    isFree={!statuses[spot.id]} 
                    freeSince={times[spot.id]}  
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
