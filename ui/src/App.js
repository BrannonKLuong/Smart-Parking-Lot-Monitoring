// Path: ui/src/App.js
// Main application component - Now shows WebcamStreamer and Processed Feed simultaneously in webcam mode,
// and attempts to refresh processed feed when webcam streaming starts.

import React, { useEffect, useState, useRef, useCallback } from 'react';
import Header from './Header';
import Notifications from './Notifications';
import SpotTile from './SpotTile';
import SpotsEditor from './SpotsEditor';
import WebcamStreamer from './WebcamStreamer'; // Import the WebcamStreamer component
import './index.css'; // Main stylesheet

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL || 'http://localhost:8000';
const PROCESSED_VIDEO_URL = `${API_BASE_URL}/webcam_feed`; // MJPEG feed from backend processing
const API_SPOTS = `${API_BASE_URL}/api/spots`;

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

  const [streamSource, setStreamSource] = useState('processed'); // 'processed' or 'webcam'
  const [processedFeedKey, setProcessedFeedKey] = useState(Date.now()); // Key to force refresh <img>

  const prevStatusesRef = useRef({});

  const getWebSocketUrl = useCallback(() => {
    // Log API_BASE_URL for debugging
    // console.log("[App.js] API_BASE_URL inside getWebSocketUrl:", API_BASE_URL); // Reduced logging

    if (!API_BASE_URL) { // Check if API_BASE_URL is falsy (e.g., empty string)
        console.error("[App.js] API_BASE_URL is not set or empty. Cannot derive WebSocket URL.");
        return null;
    }
    if (API_BASE_URL.startsWith('https://')) {
      return `wss://${API_BASE_URL.substring('https://'.length)}/ws/video_stream_upload`;
    } else if (API_BASE_URL.startsWith('http://')) {
      return `ws://${API_BASE_URL.substring('http://'.length)}/ws/video_stream_upload`;
    }
    console.error("[App.js] Cannot determine WebSocket protocol from API_BASE_URL:", API_BASE_URL, "It must start with http:// or https://");
    return null;
  }, []);

  const webcamWebSocketUrl = getWebSocketUrl();

  useEffect(() => {
    console.log("[App.js] Initial API_BASE_URL:", API_BASE_URL);
    console.log("[App.js] Derived webcamWebSocketUrl:", webcamWebSocketUrl);
  }, [webcamWebSocketUrl]);


  const fetchSpots = useCallback(() => {
    fetch(API_SPOTS)
      .then(r => {
        if (!r.ok) {
          throw new Error(`HTTP error ${r.status} while fetching spots`);
        }
        return r.json();
      })
      .then(data => {
        if (data && data.spots) {
          const newSpots = data.spots;
          const newStatuses = {};
          
          // Use functional update for setTimes to avoid depending on 'times' in useCallback
          setTimes(prevTimes => {
            const updatedTimes = { ...prevTimes };
            newSpots.forEach(spot => {
              const spotIdStr = String(spot.id);
              const isOccupied = !spot.is_available;
              newStatuses[spotIdStr] = isOccupied; // Populate newStatuses here

              const wasOccupied = prevStatusesRef.current[spotIdStr];

              if (wasOccupied === true && isOccupied === false) {
                const nowTimestamp = new Date().toISOString();
                updatedTimes[spotIdStr] = nowTimestamp;
                if (!muted) { // muted state is from App component's scope, safe here
                  const notificationId = `${spotIdStr}-${nowTimestamp}-${Math.random()}`;
                  setNotes(prevNotes => [{ id: notificationId, spot_id: spotIdStr, timestamp: nowTimestamp }, ...prevNotes.slice(0, 4)]);
                }
              } else if (isOccupied === true) {
                delete updatedTimes[spotIdStr];
              } else if (wasOccupied === false && isOccupied === false && !updatedTimes[spotIdStr]) {
                // If it was free, is still free, but somehow lost its timestamp, re-add
                // This case might indicate an issue elsewhere if it happens often.
                updatedTimes[spotIdStr] = new Date().toISOString(); 
              }
            });
            return updatedTimes;
          });

          setSpots(newSpots);
          setStatuses(newStatuses);
          // setTimes is now handled by its functional update above

          prevStatusesRef.current = newStatuses;

        } else {
          console.error("Fetched spots data is not in the expected format:", data);
        }
      })
      .catch(error => {
        // Only log fetch errors if they are not resource exhaustion errors,
        // as those might be symptomatic of other issues.
        if (error.message && !error.message.includes('ERR_INSUFFICIENT_RESOURCES')) {
            console.error("Failed to fetch spots:", error);
        }
      });
  }, [muted]); // Removed 'times' from dependency array. 'muted' is fine as it's a boolean.

  useEffect(() => {
    if (!editMode) {
      fetchSpots();
    }
  }, [fetchSpots, editMode]);

  useEffect(() => {
    let intervalId;
    if (!editMode) {
      // fetchSpots(); // fetchSpots is already called by the useEffect above when editMode changes or fetchSpots changes.
      intervalId = setInterval(fetchSpots, POLLING_INTERVAL);
    }
    return () => {
      if (intervalId) clearInterval(intervalId);
    };
  }, [editMode, fetchSpots]); // This is correct

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

  const handleSave = useCallback((updatedSpots) => {
    fetch(API_SPOTS, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ spots: updatedSpots.map(s => ({id: s.id, x: s.x, y: s.y, w: s.w, h: s.h })) }),
    })
      .then(r => {
        if (!r.ok) {
            return r.json().then(errData => {
                const detail = errData.detail || `HTTP error ${r.status}`;
                throw new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
            }).catch(() => {
                throw new Error(`HTTP error ${r.status} with non-JSON response`);
            });
        }
        return r.json();
      })
      .then(data => {
        if (data.message && data.message.toLowerCase().includes("success")) {
          console.log('Spots saved successfully');
          setEditMode(false);
          fetchSpots();
        } else {
          const errorMessage = data.detail ? JSON.stringify(data.detail) : (data.message || 'Failed to save spots. Unknown error.');
          console.error('Failed to save spots:', errorMessage);
          alert(`Failed to save spots: ${errorMessage}`);
        }
      })
      .catch(error => {
        console.error('Error saving spots:', error);
        alert(`Error saving spots: ${error.message}`);
      });
  }, [fetchSpots]);

  const handleWebcamStreamingActive = useCallback((isActive) => {
    if (isActive) {
      console.log("App.js: Webcam streaming reported as active. Refreshing processed feed key.");
      setProcessedFeedKey(Date.now()); // Update key to force <img> reload
    } else {
      console.log("App.js: Webcam streaming reported as inactive.");
    }
  }, []);

  const visibleNotes = filterSpot
    ? notes.filter(n => String(n.spot_id) === String(filterSpot))
    : notes;

  const totalSpots = spots.length;
  const freeSpots = Object.values(statuses).filter(isOccupied => !isOccupied).length;

  return (
    <div className={`App min-h-screen ${darkMode ? 'dark bg-gray-900 text-white' : 'bg-gray-100 text-gray-900'}`}>
      <Header
        darkMode={darkMode}
        toggleDarkMode={() => setDarkMode(prev => !prev)}
        freeSpots={freeSpots}
        totalSpots={totalSpots}
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
            <div className="flex space-x-2">
                 <button
                    onClick={() => setStreamSource('processed')}
                    className={`px-4 py-3 rounded-lg font-semibold shadow-md transition duration-150 ease-in-out ${streamSource === 'processed' ? 'bg-indigo-600 text-white hover:bg-indigo-700 focus:ring-indigo-500' : 'bg-gray-300 dark:bg-gray-700 text-gray-800 dark:text-gray-200 hover:bg-gray-400 dark:hover:bg-gray-600 focus:ring-gray-400'} focus:outline-none focus:ring-2 focus:ring-opacity-75`}
                >
                    Processed Feed (from File/Other)
                </button>
                <button
                    onClick={() => {
                        setStreamSource('webcam');
                        setProcessedFeedKey(Date.now());
                    }}
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
              {/* Video Display Area */}
              {streamSource === 'processed' && (
                <div className="flex flex-col items-center mb-8">
                   <p className="text-sm text-gray-600 dark:text-gray-400 mb-2">
                    Showing processed feed from backend (e.g., from a file if `VIDEO_SOURCE_TYPE` is "FILE").
                  </p>
                  <img
                    key={processedFeedKey}
                    src={PROCESSED_VIDEO_URL}
                    alt="Live parking feed (processed by backend - e.g. from file)"
                    className="w-full max-w-2xl rounded-lg shadow-lg border-2 border-gray-300 dark:border-gray-700"
                    onError={(e) => { e.target.alt = "Processed video feed is currently unavailable. Check backend."; e.target.src="https://placehold.co/640x480/2d3748/cbd5e0?text=Feed+Unavailable"; }}
                  />
                </div>
              )}

              {streamSource === 'webcam' && webcamWebSocketUrl && (
                <div className="flex flex-col items-center mb-8 space-y-6">
                   <p className="text-sm text-gray-600 dark:text-gray-400">
                    Ensure backend `VIDEO_SOURCE_TYPE` is set to `WEBSOCKET_STREAM`. Use controls below to start your camera & stream.
                  </p>
                  <WebcamStreamer
                    webSocketUrl={webcamWebSocketUrl}
                    onStreamingActive={handleWebcamStreamingActive}
                  />
                  <div className="w-full max-w-2xl">
                    <h3 className="text-lg font-semibold mb-2 text-center text-gray-700 dark:text-gray-300">Processed Webcam Feed (from Backend)</h3>
                    <img
                        key={processedFeedKey}
                        src={PROCESSED_VIDEO_URL}
                        alt="Live parking feed (processed from your webcam by backend)"
                        className="w-full rounded-lg shadow-lg border-2 border-gray-300 dark:border-gray-700"
                        onError={(e) => { e.target.alt = "Processed webcam feed is currently unavailable. Ensure you are streaming and backend is processing."; e.target.src="https://placehold.co/640x480/2d3748/cbd5e0?text=Processed+Feed+Unavailable"; }}
                    />
                  </div>
                </div>
              )}
              {streamSource === 'webcam' && !webcamWebSocketUrl && (
                 <div className="flex justify-center mb-8">
                    <div className="p-4 bg-red-100 text-red-700 rounded-lg shadow">
                        Error: Could not derive WebSocket URL. `API_BASE_URL` might be misconfigured. Current value: '{String(API_BASE_URL)}'. It should start with http:// or https://.
                    </div>
                </div>
              )}

              {/* Spot Tiles Display Area */}
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
