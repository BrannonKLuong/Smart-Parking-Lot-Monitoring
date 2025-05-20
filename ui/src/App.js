// Path: ui/src/App.js
// Main application component for the React frontend - now with HTTP Polling and ESLint fix.

import React, { useEffect, useState, useRef, useCallback } from 'react'; // Added useCallback
import Header        from './Header';
import Notifications from './Notifications';
import SpotTile      from './SpotTile';
import SpotsEditor   from './SpotsEditor';
import './index.css'; // Main stylesheet

const API_BASE_URL = process.env.REACT_APP_API_BASE_URL || 'http://localhost:8000';
const VIDEO_URL = `${API_BASE_URL}/webcam_feed`;
const API_SPOTS = `${API_BASE_URL}/api/spots`;


const NOTIFICATION_DURATION = 10000; // Alert duration 10 seconds
const POLLING_INTERVAL = 5000; // Fetch new data every 5 seconds (5000 ms)

export default function App() {
  const [editMode, setEditMode] = useState(false);
  const [darkMode, setDarkMode] = useState(false);
  const [spots, setSpots] = useState([]); // Holds full spot objects with bbox
  const [statuses, setStatuses] = useState({}); // { spot_id: isOccupied, ... }
  const [times, setTimes] = useState({}); // { spot_id: freeSinceTimestamp, ... }
  const [notes, setNotes] = useState([]); // [{ id: uniqueId, spot_id, timestamp }, ...]
  const [muted, setMuted] = useState(false);
  const [filterSpot, setFilterSpot] = useState(null);

  const prevStatusesRef = useRef({});

  // Function to fetch spots from the backend, wrapped in useCallback
  const fetchSpots = useCallback(() => {
    fetch(API_SPOTS)
      .then(r => {
        if (!r.ok) { // Check for HTTP errors
          throw new Error(`HTTP error ${r.status} while fetching spots`);
        }
        return r.json();
      })
      .then(data => {
        if (data && data.spots) {
          const newSpots = data.spots;
          const newStatuses = {};
          const newTimes = { ...times }; // Preserve existing times unless spot is now occupied

          newSpots.forEach(spot => {
            const spotIdStr = String(spot.id);
            const isOccupied = !spot.is_available;
            newStatuses[spotIdStr] = isOccupied;

            const wasOccupied = prevStatusesRef.current[spotIdStr];

            if (wasOccupied === true && isOccupied === false) { // Changed from occupied to free
              const nowTimestamp = new Date().toISOString();
              newTimes[spotIdStr] = nowTimestamp;
              if (!muted) { // Check muted state before creating notification
                const notificationId = `${spotIdStr}-${nowTimestamp}-${Math.random()}`;
                setNotes(prevNotes => [{ id: notificationId, spot_id: spotIdStr, timestamp: nowTimestamp }, ...prevNotes.slice(0, 4)]);
              }
            } else if (isOccupied === true) { // If occupied, ensure no 'freeSince' time
              delete newTimes[spotIdStr];
            } else if (wasOccupied === false && isOccupied === false && !newTimes[spotIdStr]) {
              // If it was free, is still free, but somehow lost its timestamp, re-add
              // This case might indicate an issue elsewhere if it happens often.
              newTimes[spotIdStr] = new Date().toISOString();
            }
          });

          setSpots(newSpots);
          setStatuses(newStatuses);
          setTimes(newTimes);

          prevStatusesRef.current = newStatuses;

        } else {
          console.error("Fetched spots data is not in the expected format:", data);
        }
      })
      .catch(error => console.error("Failed to fetch spots:", error));
  }, [muted, times]); // Add dependencies for fetchSpots: muted (for notification logic) and times (to avoid stale closures)

  // Initial fetch of spots data
  useEffect(() => {
    fetchSpots();
  }, [fetchSpots]); // Added fetchSpots to dependency array

  // HTTP Polling for spot statuses
  useEffect(() => {
    if (!editMode) { // Only poll if not in edit mode
      fetchSpots(); // Initial fetch for this polling cycle
      const intervalId = setInterval(() => {
        fetchSpots();
      }, POLLING_INTERVAL);
      return () => clearInterval(intervalId); // Cleanup interval
    }
  }, [editMode, fetchSpots]); // Added fetchSpots and editMode to dependency array

  // Effect to clear notifications after a duration
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
      body: JSON.stringify({ spots: updatedSpots }),
    })
      .then(r => {
        if (!r.ok) throw new Error(`HTTP error ${r.status}`);
        return r.json();
      })
      .then(data => {
        if (data.ok) {
          console.log('Spots saved successfully');
          setEditMode(false);
          fetchSpots(); // Refetch spots immediately after save
        } else {
          console.error('Failed to save spots:', data);
          alert('Failed to save spots.');
        }
      })
      .catch(error => {
        console.error('Error saving spots:', error);
        alert(`Error saving spots: ${error.message}`);
      });
  }, [fetchSpots]); // fetchSpots is a dependency of handleSave

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
        <div className="flex justify-center mb-6">
          <button
            onClick={() => setEditMode(e => !e)}
            className="px-6 py-3 bg-blue-600 text-white font-semibold rounded-lg shadow-md hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-75 transition duration-150 ease-in-out"
          >
            {editMode ? 'Cancel Edit Mode' : 'Edit Parking Spots'}
          </button>
        </div>

        {editMode ? (
          <SpotsEditor
            initialSpots={spots}
            videoSize={{ width: 800, height: 600 }}
            onSave={handleSave}
            setSpots={setSpots} // This prop might need careful handling if SpotsEditor directly mutates spots
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
              <div className="flex justify-center mb-8">
                <img
                  src={VIDEO_URL}
                  alt="Live parking feed"
                  className="w-full max-w-2xl rounded-lg shadow-lg border-2 border-gray-300 dark:border-gray-700"
                />
              </div>
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
//