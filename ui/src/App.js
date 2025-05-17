import React, { useEffect, useState } from 'react';
import Header        from './Header';
import Notifications from './Notifications';
import SpotTile      from './SpotTile';
import SpotsEditor   from './SpotsEditor';
import './index.css';
// Test Push
const API_BASE_URL = process.env.REACT_APP_API_BASE_URL || 'http://localhost:8000';
const WS_BASE_URL  = process.env.REACT_APP_WS_BASE_URL || 'ws://localhost:8000';

const API_SPOTS = `${API_BASE_URL}/api/spots`;
const WS_URL    = `${WS_BASE_URL}/ws`;
const VIDEO_URL = `${API_BASE_URL}/webcam_feed`;

const NOTIFICATION_DURATION = 10000; // Alert duration 10 seconds

export default function App() {
  const [editMode, setEditMode] = useState(false);
  const [darkMode, setDarkMode] = useState(false);
  const [spots, setSpots] = useState([]); // Holds full spot objects with bbox
  const [statuses, setStatuses] = useState({}); // { spot_id: isOccupied, ... }
  const [times, setTimes] = useState({}); // { spot_id: freeSinceTimestamp, ... }
  const [notes, setNotes] = useState([]); // [{ id: uniqueId, spot_id, timestamp }, ...]
  const [muted, setMuted] = useState(false);
  const [filterSpot, setFilterSpot] = useState(null);

  // Function to fetch spots and update both spots (for editor) and statuses (for tiles)
  const fetchSpots = () => {
    fetch(API_SPOTS)
      .then(r => r.json())
      .then(data => {
        if (data && data.spots) {
          setSpots(data.spots);
          setStatuses(Object.fromEntries(data.spots.map(s => [s.id, !s.is_available])));
        } else {
          console.error("Fetched spots data is not in the expected format:", data);
          setSpots([]);
          setStatuses({});
        }
      })
      .catch(error => console.error("Failed to fetch spots:", error));
  };

  // Initial fetch of spots data
  useEffect(() => {
    fetchSpots(); 
  }, []); 

  // WebSocket connection for real-time status updates
  useEffect(() => {
    const ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      console.log('WebSocket connected');
    };

    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'spot_status_update') {
          const { spot_id, status, timestamp } = message;
          setStatuses(prevStatuses => ({
            ...prevStatuses,
            [spot_id]: status === 'occupied' 
          }));

          // Update times for free spots
          if (status === 'free') {
              setTimes(prevTimes => ({
                  ...prevTimes,
                  [spot_id]: timestamp
              }));
               // Add notification for free spots if not muted
              if (!muted) {
                   const notificationId = `${spot_id}-${timestamp}-${Math.random()}`;
                   setNotes(prevNotes => [...prevNotes, { id: notificationId, spot_id, timestamp }]);
              }
          } else {
               setTimes(prevTimes => {
                   const newTimes = { ...prevTimes };
                   delete newTimes[spot_id];
                   return newTimes;
               });
          }
        } else if (message.type === 'config_update') {
            console.log('Config update received, refetching spots and statuses');
            fetchSpots();
        }
      } catch (error) {
        console.error("Error parsing WebSocket message:", error);
      }
    };

    ws.onclose = (event) => {
      console.log('WebSocket disconnected', event.code, event.reason);
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      ws.close();
    };

    return () => {
      console.log('Cleaning up WebSocket connection');
      ws.close();
    };
  }, [muted]);

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


  // Handle saving spots from the editor
  const handleSave = (updatedSpots) => {
    fetch(API_SPOTS, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ spots: updatedSpots }),
    })
      .then(r => {
        if (!r.ok) { // Check for HTTP errors
          throw new Error(`HTTP error ${r.status}`);
        }
        return r.json();
      })
      .then(data => {
        if (data.ok) {
          console.log('Spots saved successfully');
          setEditMode(false);
          fetchSpots();

        } else {
          console.error('Failed to save spots:', data);
          alert('Failed to save spots.');
        }
      })
      .catch(error => {
        console.error('Error saving spots:', error);
        alert(`Error saving spots: ${error.message}`);
      });
  };

  // Filter notes based on selected spot
  const visibleNotes = filterSpot
    ? notes.filter(n => n.spot_id === filterSpot)
    : notes;

  // Calculate free and total spot counts
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
        {/* Edit Mode Toggle Button */}
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
            videoSize={{ width: 800, height: 600 }} // Define video preview size for the editor
            onSave={handleSave}
            setSpots={setSpots} 
            apiBaseUrl={API_BASE_URL} 
          />
        ) : (
          // Live View (Video Feed + Spot Tiles)
          <>
            <Notifications
              notes={visibleNotes}
              onFilter={setFilterSpot} // Allow clicking a notification to filter/highlight a spot
              muted={muted}
              toggleMute={() => setMuted(m => !m)} // Allow muting/unmuting notifications
            />

            <main className="p-4">
              {/* Live Video Feed */}
              <div className="flex justify-center mb-8">
                <img
                  src={VIDEO_URL}
                  alt="Live parking feed"
                  className="w-full max-w-2xl rounded-lg shadow-lg border-2 border-gray-300 dark:border-gray-700"
                />
              </div>

              {/* Grid of Parking Spot Tiles */}
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