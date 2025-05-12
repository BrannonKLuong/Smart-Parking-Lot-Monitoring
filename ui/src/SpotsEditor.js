import React, { useEffect, useState } from 'react';
import { Rnd } from 'react-rnd';

// Accept initialSpots and setSpots as props
export default function SpotsEditor({ initialSpots, videoSize, onSave, setSpots }) {
  // spots state is now managed by the parent (App.js)
  // const [spots, setSpots]     = useState([]); // Remove internal spots state
  // history stack for undo - keep this local to the editor
  const [history, setHistory] = useState([]);
  const [selected, setSelected] = useState(null);

  // 1) Load initial config from props and set history
  useEffect(() => {
    // Initialize history with the initial spots from props
    setHistory([initialSpots]);
    // No need to setSpots here, it's already managed by the parent
  }, [initialSpots]); // Re-initialize history if initialSpots changes

  // helper to push new state to history and update parent's spots state
  const push = newSpots => {
    setSpots(newSpots); // Update parent's spots state
    setHistory(h => [...h, newSpots]); // Update local history
  };

  // 2) Handlers
  const onDragResize = (id, x,y,w,h) => {
    // Use the spots state from props
    push(initialSpots.map(s => s.id===id ? { ...s, x,y,w,h } : s));
  };

  const addBox = () => {
    // --- Start: Robust Logic to find the lowest available UNIQUE ID ---
    const existingIds = new Set(initialSpots.map(s => s.id)); // Use a Set for efficient lookup
    let newId = 1;
    // Loop until a unique ID is found
    while (existingIds.has(newId)) {
      newId++;
    }
    // At this point, newId is guaranteed to be the lowest available unique positive integer ID
    // --- End: Robust Logic to find the lowest available UNIQUE ID ---

    // No need for the explicit `if (existingIds.includes(newId))` check here anymore
    // because the while loop guarantees newId is not in existingIds

    const defaultBox = { id:newId, x:20, y:20, w:100, h:80 };
    // Use the spots state from props when adding the new box
    push([...initialSpots, defaultBox]);
  };

  const removeBox = () => {
    if (selected == null) return;
    // Use the spots state from props when removing a box
    push(initialSpots.filter(s => s.id !== selected));
    setSelected(null); // Deselect after removing
  };

  const undo = () => {
    if (history.length < 2) return;
    const prevHistory = history.slice(0, -1);
    setHistory(prevHistory);
    // Restore the spots state from the previous history entry
    setSpots(prevHistory[prevHistory.length - 1]);
    setSelected(null); // Deselect on undo
  };

  const save = () => {
    // Pass the current spots state (managed by parent) to the onSave prop
    onSave(initialSpots);
  };

  return (
    <div>
      {/* Controls */}
      <div className="flex gap-2 mb-2">
        <button onClick={addBox} className="px-4 py-2 bg-green-600 text-white rounded-lg shadow hover:bg-green-700 focus:outline-none focus:ring-2 focus:ring-green-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Add</button>
        <button onClick={removeBox} disabled={selected==null} className="px-4 py-2 bg-red-600 text-white rounded-lg shadow hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Remove</button>
        <button onClick={undo} disabled={history.length<2} className="px-4 py-2 bg-yellow-600 text-white rounded-lg shadow hover:bg-yellow-700 disabled:opacity-50 disabled:cursor-not-allowed focus:outline-none focus:ring-2 focus:ring-yellow-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Undo</button>
        <button onClick={save} className="px-4 py-2 bg-blue-600 text-white rounded-lg shadow hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-opacity-50 transition duration-150 ease-in-out">Save</button>
      </div>

      {/* Canvas: video + draggable boxes */}
      <div
        className="relative border-2 border-gray-300 rounded-lg overflow-hidden" // Added border and overflow hidden
        style={{ width: videoSize.width, height: videoSize.height }}
      >
        <img
          src="http://localhost:8000/webcam_feed"
          alt="feed"
          className="absolute inset-0 w-full h-full object-cover"
        />

        {/* Use initialSpots from props for rendering */}
        {initialSpots.map(s => (
          <Rnd
            key={s.id}
            bounds="parent"
            size={{ width: s.w, height: s.h }}
            position={{ x: s.x, y: s.y }}
            onDragStop={(_, d) =>
              onDragResize(s.id, d.x, d.y, s.w, s.h)
            }
            onResizeStop={(_, __, ref, ___, pos) =>
              onDragResize(
                s.id,
                pos.x, pos.y,
                ref.offsetWidth, ref.offsetHeight
              )
            }
            // Added styling for the boxes
            style={{
              border: `2px solid ${selected === s.id ? 'blue' : 'rgba(255,255,255,0.7)'}`, // Highlight selected
              backgroundColor: selected === s.id ? 'rgba(0,0,255,0.2)' : 'rgba(0,0,0,0.1)', // Semi-transparent fill
              boxSizing: 'border-box', // Include border in size
              cursor: 'move', // Indicate draggable
            }}
            // Handle selection on click
            onClick={() => setSelected(s.id)}
            // Add a handle for resizing to make it easier
            // enableResizing={{
            //   top:true, right:true, bottom:true, left:true,
            //   topRight:true, bottomRight:true, bottomLeft:true, topLeft:true
            // }}
            // resizeHandleStyles={{
            //   bottomRight: { width: 10, height: 10, backgroundColor: 'blue', border: '1px solid white' },
            //   // Add styles for other handles if needed
            // }}
          >
            {/* Optional: Display spot ID inside the box */}
            <div className="flex items-center justify-center w-full h-full text-white text-sm font-bold pointer-events-none">
                {s.id}
            </div>
          </Rnd>
        ))}
      </div>
    </div>
  );
}
