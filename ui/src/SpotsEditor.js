import React, { useEffect, useState } from 'react';
import { Rnd } from 'react-rnd';

export default function SpotsEditor({ videoSize, onSave }) {
  // spots: [{ id, x, y, w, h }, …]
  const [spots, setSpots]     = useState([]);
  // history stack for undo
  const [history, setHistory] = useState([]);
  const [selected, setSelected] = useState(null);

  // 1) Load initial config
  useEffect(() => {
    fetch('/api/spots')
      .then(r=>r.json())
      .then(data => {
        setSpots(data.spots);
        setHistory([data.spots]);
      });
  }, []);

  // helper to push new state to history
  const push = newSpots => {
    setSpots(newSpots);
    setHistory(h => [...h, newSpots]);
  };

  // 2) Handlers
  const onDragResize = (id, x,y,w,h) => {
    push(spots.map(s => s.id===id ? { ...s, x,y,w,h } : s));
  };
  const addBox = () => {
    const newId = Math.max(0, ...spots.map(s=>s.id)) + 1;
    const defaultBox = { id:newId, x:20, y:20, w:100, h:80 };
    push([...spots, defaultBox]);
    setSelected(newId);
  };
  const removeBox = () => {
    if (selected == null) return;
    push(spots.filter(s=>s.id!==selected));
    setSelected(null);
  };
  const undo = () => {
    if (history.length < 2) return;
    const newHist = [...history];
    newHist.pop();
    setHistory(newHist);
    setSpots(newHist[newHist.length-1]);
    setSelected(null);
  };
  const save = () => onSave({ spots });

  return (
    <div>
      {/* Controls */}
      <div className="flex gap-2 mb-2">
        <button onClick={addBox}>Add</button>
        <button onClick={removeBox} disabled={selected==null}>Remove</button>
        <button onClick={undo} disabled={history.length<2}>Undo</button>
        <button onClick={save}>Save</button>
      </div>

      {/* Canvas: video + draggable boxes */}
      <div
        className="relative"
        style={{ width: videoSize.width, height: videoSize.height }}
      >
        <img
          src="http://localhost:8000/webcam_feed"
          alt="feed"
          className="absolute inset-0 w-full h-full object-cover"
        />

        {spots.map(s => (
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
            style={{
              border: s.id===selected ? '2px solid #00f' : '2px dashed #fff',
              background: 'rgba(255,255,255,0.2)',
              cursor: 'move'
            }}
            onClick={() => setSelected(s.id)}
          />
        ))}
      </div>
    </div>
  );
}
