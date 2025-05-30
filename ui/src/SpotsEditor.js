// Path: ui/src/SpotsEditor.js
import React, { useEffect, useState, useMemo } from 'react';
import { Rnd } from 'react-rnd'; 

export default function SpotsEditor({ initialSpots, videoSize, onSave, setSpots, apiBaseUrl }) {
  const [currentSpots, setCurrentSpots] = useState([]); 
  const [history, setHistory] = useState([]); 
  const [selectedBoxId, setSelectedBoxId] = useState(null); 

  const videoFeedUrl = useMemo(() => {
    if (!apiBaseUrl) return ''; 
    return `${apiBaseUrl}/webcam_feed`;
  }, [apiBaseUrl]);

  useEffect(() => {
    console.log("[SpotsEditor] Initializing with initialSpots:", initialSpots);
    const processedInitialSpots = initialSpots.map(s => ({
        ...s,
        id: String(s.id), 
        x: Math.round(s.x || 0), 
        y: Math.round(s.y || 0),
        w: Math.round(s.w || 50), 
        h: Math.round(s.h || 50)  
    }));
    setCurrentSpots(processedInitialSpots);
    setHistory([processedInitialSpots]); 
    setSelectedBoxId(null); 
  }, [initialSpots]);

  const updateSpotsAndHistory = (newSpots) => {
    setCurrentSpots(newSpots);
    setHistory(h => [...h.slice(-19), newSpots]); 
  };

  const onDragResize = (id, x, y, w, h) => {
    const updatedSpots = currentSpots.map(s =>
      String(s.id) === String(id) ? { 
        ...s, 
        x: Math.round(x), 
        y: Math.round(y), 
        w: Math.round(w), 
        h: Math.round(h)  
      } : s 
    );
    updateSpotsAndHistory(updatedSpots);
  };

  const addBox = () => {
    const newId = currentSpots.length > 0 ? Math.max(0, ...currentSpots.map(s => parseInt(s.id, 10))) + 1 : 1;
    const defaultBox = { 
        id: String(newId), 
        x: 20, y: 20, w: 100, h: 80, 
        is_available: true 
    };
    console.log("[SpotsEditor] Adding new box:", defaultBox);
    const newSpotList = [...currentSpots, defaultBox];
    updateSpotsAndHistory(newSpotList);
  };

  const removeBox = () => {
    if (selectedBoxId == null) return; 
    const filteredSpots = currentSpots.filter(s => String(s.id) !== String(selectedBoxId));
    console.log("[SpotsEditor] Removing box, new list:", filteredSpots);
    updateSpotsAndHistory(filteredSpots);
    setSelectedBoxId(null); 
  };

  const undo = () => {
    if (history.length < 2) return; 
    const prevHistory = history.slice(0, -1);
    const previousSpots = prevHistory[prevHistory.length - 1];
    setHistory(prevHistory);
    setCurrentSpots(previousSpots); 
    if (setSpots) { 
        setSpots(previousSpots); 
    }
    setSelectedBoxId(null); 
  };

  const handleSave = () => {
    console.log("[SpotsEditor] handleSave called. currentSpots:", JSON.parse(JSON.stringify(currentSpots))); // Deep copy for logging
    const spotsToSave = currentSpots.map(s => ({
      id: String(s.id), 
      x: Math.round(s.x),
      y: Math.round(s.y),
      w: Math.round(s.w),
      h: Math.round(s.h)
    }));
    console.log("[SpotsEditor] spotsToSave (passed to App.js onSave):", spotsToSave);
    onSave(spotsToSave); 
  };

  return (
    <div className="p-4 bg-gray-50 dark:bg-gray-800 rounded-lg shadow-inner">
      <div className="flex flex-wrap gap-3 mb-4">
        <button onClick={addBox} className="px-4 py-2 bg-green-600 text-white rounded-lg shadow hover:bg-green-700 focus:outline-none focus:ring-2 focus:ring-green-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Add Spot</button>
        <button onClick={removeBox} disabled={selectedBoxId == null} className="px-4 py-2 bg-red-600 text-white rounded-lg shadow hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Remove Selected</button>
        <button onClick={undo} disabled={history.length < 2} className="px-4 py-2 bg-yellow-500 text-white rounded-lg shadow hover:bg-yellow-600 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-yellow-400 focus:ring-opacity-50 transition duration-150 ease-in-out">Undo</button>
        <button onClick={handleSave} className="px-4 py-2 bg-blue-600 text-white rounded-lg shadow hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Save Configuration</button>
      </div>

      <div
        className="relative border-2 border-gray-400 dark:border-gray-600 rounded-lg overflow-hidden bg-black" 
        style={{ width: videoSize.width, height: videoSize.height }}
        onClick={(e) => { if (e.target === e.currentTarget) setSelectedBoxId(null);}}
      >
        {videoFeedUrl ? (
            <img
                src={videoFeedUrl} 
                alt="Live feed for spot configuration"
                className="absolute inset-0 w-full h-full object-contain" 
                onError={(e) => { e.target.alt = "Video feed unavailable. Check backend."; e.target.style.display='none'; }}
            />
        ) : (
            <div className="absolute inset-0 w-full h-full flex items-center justify-center text-white bg-gray-700">
                Video feed URL not configured.
            </div>
        )}

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
                e.stopPropagation(); 
                setSelectedBoxId(spot.id);
            }}
            style={{ 
              border: `2px dashed ${String(selectedBoxId) === String(spot.id) ? 'blue' : 'rgba(255,255,255,0.7)'}`,
              backgroundColor: String(selectedBoxId) === String(spot.id) ? 'rgba(0,0,255,0.2)' : 'rgba(255,255,255,0.1)',
              boxSizing: 'border-box',
              cursor: 'move',
            }}
            className="transition-all duration-100 ease-in-out" 
          >
            <div className="flex items-center justify-center w-full h-full text-white text-sm font-bold pointer-events-none select-none">
              {spot.id}
            </div>
          </Rnd>
        ))}
      </div>
    </div>
  );
}
