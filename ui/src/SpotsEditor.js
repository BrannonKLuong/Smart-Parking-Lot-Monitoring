// Path: ui/src/SpotsEditor.js
// Component for visually editing parking spot bounding boxes on a video feed.

import React, { useEffect, useState, useMemo } from 'react';
import { Rnd } from 'react-rnd'; 

export default function SpotsEditor({ initialSpots, videoSize, onSave, setSpots, apiBaseUrl }) {
  const [currentSpots, setCurrentSpots] = useState([]); 
  const [history, setHistory] = useState([]);
  const [selectedBoxId, setSelectedBoxId] = useState(null); 

  // Construct the full video feed URL
  const videoFeedUrl = useMemo(() => `${apiBaseUrl}/webcam_feed`, [apiBaseUrl]);

  // Initialize currentSpots and history when initialSpots prop changes
  useEffect(() => {
    setCurrentSpots(initialSpots.map(s => ({...s})));
    setHistory([initialSpots.map(s => ({...s}))]);
    setSelectedBoxId(null);
  }, [initialSpots]);

  // Function to update spots and push to history capped at 20
  const updateSpotsAndHistory = (newSpots) => {
    setCurrentSpots(newSpots);
    setHistory(h => [...h.slice(0, 20), newSpots]);
  };

  // Handler for dragging or resizing a spot box
  const onDragResize = (id, x, y, w, h) => {
    const updatedSpots = currentSpots.map(s =>
      String(s.id) === String(id) ? { ...s, x, y, w, h } : s 
    );
    updateSpotsAndHistory(updatedSpots);
  };

  // Handler to add a new parking spot box
  const addBox = () => {
    const newId = currentSpots.length > 0 ? Math.max(0, ...currentSpots.map(s => parseInt(s.id, 10))) + 1 : 1;
    const defaultBox = { id: String(newId), x: 20, y: 20, w: 100, h: 80, is_available: true };
    updateSpotsAndHistory([...currentSpots, defaultBox]);
  };

  // Handler to remove the selected parking spot box
  const removeBox = () => {
    if (selectedBoxId == null) return;
    const filteredSpots = currentSpots.filter(s => String(s.id) !== String(selectedBoxId));
    updateSpotsAndHistory(filteredSpots);
    setSelectedBoxId(null);
  };

  // Handler for the undo action
  const undo = () => {
    if (history.length < 2) return;
    const prevHistory = history.slice(0, -1);
    const previousSpots = prevHistory[prevHistory.length - 1];
    setHistory(prevHistory);
    setCurrentSpots(previousSpots); 
    setSpots(previousSpots); 
    setSelectedBoxId(null); 
  };

  // Handler to save the current spot configurations
  const handleSave = () => {
    onSave(currentSpots); 
  };

  return (
    <div className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg shadow-inner">
      {/* Control buttons for adding, removing, undoing, and saving */}
      <div className="flex flex-wrap gap-3 mb-4">
        <button onClick={addBox} className="px-4 py-2 bg-green-600 text-white rounded-lg shadow hover:bg-green-700 focus:outline-none focus:ring-2 focus:ring-green-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Add Spot</button>
        <button onClick={removeBox} disabled={selectedBoxId == null} className="px-4 py-2 bg-red-600 text-white rounded-lg shadow hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Remove Selected</button>
        <button onClick={undo} disabled={history.length < 2} className="px-4 py-2 bg-yellow-500 text-white rounded-lg shadow hover:bg-yellow-600 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-yellow-400 focus:ring-opacity-50 transition duration-150 ease-in-out">Undo</button>
        <button onClick={handleSave} className="px-4 py-2 bg-blue-600 text-white rounded-lg shadow hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Save Configuration</button>
      </div>

      {/* Canvas area for video feed and draggable/resizable boxes */}
      <div
        className="relative border-2 border-gray-400 dark:border-gray-600 rounded-lg overflow-hidden bg-black" 
        style={{ width: videoSize.width, height: videoSize.height }}
        onClick={(e) => { if (e.target === e.currentTarget) setSelectedBoxId(null);}}
      >
        <img
          src={videoFeedUrl} 
          alt="Live feed for spot configuration"
          className="absolute inset-0 w-full h-full object-contain" 
        />

        {/* Render draggable and resizable boxes for each spot */}
        {currentSpots.map(spot => (
          <Rnd
            key={spot.id}
            bounds="parent"
            size={{ width: spot.w, height: spot.h }}
            position={{ x: spot.x, y: spot.y }}
            onDragStop={(_, d) =>
              onDragResize(spot.id, d.x, d.y, spot.w, spot.h)
            }
            onResizeStop={(_, __, ref, ___, pos) =>
              onDragResize(
                spot.id,
                pos.x, pos.y,
                ref.offsetWidth, ref.offsetHeight
              )
            }
            onClick={(e) => {
                e.stopPropagation(); // Prevent canvas click from deselecting
                setSelectedBoxId(spot.id);
            }}
            style={{ // Styling for the boxes
              border: `2px dashed ${String(selectedBoxId) === String(spot.id) ? 'blue' : 'rgba(255,255,255,0.7)'}`,
              backgroundColor: String(selectedBoxId) === String(spot.id) ? 'rgba(0,0,255,0.2)' : 'rgba(255,255,255,0.1)',
              boxSizing: 'border-box',
              cursor: 'move',
            }}
            className="transition-all duration-100 ease-in-out" 
          >
            {/* Display spot ID inside the box */}
            <div className="flex items-center justify-center w-full h-full text-white text-sm font-bold pointer-events-none select-none">
              {spot.id}
            </div>
          </Rnd>
        ))}
      </div>
    </div>
  );
}
