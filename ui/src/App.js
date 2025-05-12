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

// Duration for notifications to stay visible (in milliseconds)
const NOTIFICATION_DURATION = 10000; // 10 seconds

export default function App() {
  const [editMode,  setEditMode ]  = useState(false);
  const [darkMode,  setDarkMode ]  = useState(false);
  // spots state now holds the full spot objects with bbox
  const [spots,     setSpots    ]  = useState([]);
  const [statuses,  setStatuses ]  = useState({}); // { spot_id: isOccupied, ... }
  const [times,     setTimes    ]  = useState({}); // { spot_id: freeSinceTimestamp, ... }
  // notes state now includes a unique id for each notification for clearing
  const [notes,     setNotes    ]  = useState([]); // [{ id: uniqueId, spot_id, timestamp }, ...]
  const [muted,     setMuted    ]  = useState(false);
  const [filterSpot,setFilterSpot] = useState(null);

  // Function to fetch spots and update both spots (for editor) and statuses (for tiles)
  const fetchSpots = () => {
    fetch(API_SPOTS)
      .then(r => r.json())
      .then(data => {
        // Update spots state with full objects for the editor
        setSpots(data.spots);
        // Update statuses state based on is_available from the backend
        setStatuses(Object.fromEntries(data.spots.map(s => [s.id, !s.is_available]))); // isOccupied is opposite of is_available
      })
      .catch(error => console.error("Failed to fetch spots:", error)); // Add error handling
  };

  // Load spots.json (IDs + initial status) on initial mount
  useEffect(() => {
    fetchSpots(); // Use the new fetchSpots function
  }, []); // Empty dependency array means this runs once on mount

  // WebSocket connection for real-time status updates
  useEffect(() => {
    const ws = new WebSocket(WS_URL);

    ws.onopen = () => {
      console.log('WebSocket connected');
    };

    ws.onmessage = (event) => {
      // console.log('WebSocket message received:', event.data); // Log received messages
      try {
        const message = JSON.parse(event.data);
        if (message.type === 'spot_status_update') {
          const { spot_id, status, timestamp } = message;
          // Update statuses based on WebSocket messages
          setStatuses(prevStatuses => ({
            ...prevStatuses,
            [spot_id]: status === 'occupied' // true if occupied, false if free
          }));
          // Update times for free spots
          if (status === 'free') {
              setTimes(prevTimes => ({
                  ...prevTimes,
                  [spot_id]: timestamp
              }));
               // Add notification for free spots if not muted
              if (!muted) {
                   // Assign a unique ID to each notification
                   const notificationId = `${spot_id}-${timestamp}-${Math.random()}`; // Simple unique ID
                   setNotes(prevNotes => [...prevNotes, { id: notificationId, spot_id, timestamp }]);
              }
          } else {
               // Clear time for occupied spots
               setTimes(prevTimes => {
                   const newTimes = { ...prevTimes };
                   delete newTimes[spot_id];
                   return newTimes;
               });
          }
        } else if (message.type === 'config_update') {
            // If config changes (spots added/removed), refetch spots and statuses
            console.log('Config update received, refetching spots and statuses');
            fetchSpots(); // Refetch spots and statuses
        }
      } catch (error) {
        console.error("Error parsing WebSocket message:", error);
      }
    };

    ws.onclose = (event) => {
      console.log('WebSocket disconnected', event.code, event.reason);
      // Attempt to reconnect after a delay
      setTimeout(() => {
        console.log('Attempting to reconnect WebSocket...');
        // This useEffect will run again and create a new WebSocket
      }, 5000); // Reconnect after 5 seconds
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      ws.close(); // Close the connection on error to trigger reconnect
    };

    // Clean up WebSocket connection on component unmount
    return () => {
      console.log('Cleaning up WebSocket connection');
      ws.close();
    };
  }, [muted]); // Reconnect WebSocket if muted state changes (optional, but good practice)

  // Effect to clear notifications after a duration
  useEffect(() => {
      if (notes.length === 0) return; // No notes to clear

      const timers = notes.map(note => {
          // Set a timer for each note
          const timer = setTimeout(() => {
              setNotes(prevNotes => prevNotes.filter(n => n.id !== note.id));
          }, NOTIFICATION_DURATION);
          return timer; // Return the timer ID
      });

      // Clean up timers when the notes list changes or component unmounts
      return () => {
          timers.forEach(timer => clearTimeout(timer));
      };

  }, [notes]); // Re-run this effect whenever the notes state changes


  // Handle saving spots from the editor
  const handleSave = (updatedSpots) => {
    // updatedSpots is the array of spot objects with bbox
    fetch(API_SPOTS, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ spots: updatedSpots }),
    })
      .then(r => r.json())
      .then(data => {
        if (data.ok) {
          console.log('Spots saved successfully');
          setEditMode(false); // Exit edit mode after saving

          // --- IMPORTANT: Refetch spots and statuses after saving ---
          // This ensures the UI tiles reflect the actual initial status of newly added spots
          fetchSpots();
          // --- End of IMPORTANT ---

        } else {
          console.error('Failed to save spots:', data);
          alert('Failed to save spots.'); // Basic error notification
        }
      })
      .catch(error => {
        console.error('Error saving spots:', error);
        alert(`Error saving spots: ${error.message}`); // Basic error notification
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
    <div className={`App ${darkMode ? 'dark' : ''}`}>
      {/* Pass freeSpots and totalSpots to the Header component */}
      <Header
        darkMode={darkMode}
        setDarkMode={setDarkMode}
        freeSpots={freeSpots}
        totalSpots={totalSpots}
      />

      {/* Main content area */}
      <div className="container mx-auto px-4 py-8">
        {/* Edit Mode Toggle */}
        <div className="flex justify-center mb-4">
          <button
            onClick={() => setEditMode(e => !e)}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg shadow hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50 transition duration-150 ease-in-out"
          >
            {editMode ? 'Cancel Edit' : 'Edit Spots'}
          </button>
        </div>

        {/* Conditional Rendering based on Edit Mode */}
        {editMode ? (
          // Spots Editor View
          <SpotsEditor
            // Pass the current list of spot objects to the editor
            initialSpots={spots}
            videoSize={{ width: 800, height: 600 }} // Adjust video size as needed
            onSave={handleSave}
            // Pass setSpots to SpotsEditor so it can manage its local state
            setSpots={setSpots}
          />
        ) : (
          // Live View (Video Feed + Spot Tiles)
          <>
            {/* Notifications Component */}
            <Notifications
              notes={visibleNotes}
              onFilter={setFilterSpot}
              muted={muted}
              toggleMute={() => setMuted(m => !m)}
            />

            <main className="p-4">
              {/* Video Feed */}
              <div className="flex justify-center mb-8">
                <img
                  src={VIDEO_URL}
                  alt="Live feed"
                  className="w-full max-w-xl rounded-lg shadow" // Adjust max-w-xl as needed
                />
              </div>

              {/* Spot Tiles Grid */}
              <div className="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
                {/* Map over the spot objects to render tiles */}
                {spots.map(spot => (
                  <SpotTile
                    key={spot.id} // Use spot.id as the key
                    id={spot.id}
                    // Use the status from the statuses state
                    isFree={!statuses[spot.id]} // isFree is opposite of isOccupied
                    freeSince={times[spot.id]}
                    highlight={filterSpot === spot.id}
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
