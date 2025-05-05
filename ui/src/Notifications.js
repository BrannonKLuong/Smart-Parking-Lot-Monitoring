import React, { useState } from 'react';

export default function Notifications({
  notes,          // array of { spot_id, timestamp }
  onFilter,       // function to call when a note is clicked
  muted,          // boolean
  toggleMute      // function to mute/unmute
}) {
  const [collapsed, setCollapsed] = useState(false);

  return (
    <div className="fixed top-4 right-4 w-64 z-50">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">Alerts</h2>
        <button
          onClick={() => setCollapsed(c => !c)}
          className="text-sm text-blue-600 dark:text-blue-400"
        >
          {collapsed ? 'Expand' : 'Collapse'}
        </button>
      </div>

      {!collapsed && (
        <ul className="bg-white dark:bg-gray-800 shadow rounded overflow-auto max-h-60">
          {notes.map((n, i) => (
            <li
              key={i}
              onClick={() => onFilter(n.spot_id)}
              className="p-2 border-b last:border-b-0 hover:bg-gray-100 dark:hover:bg-gray-700 cursor-pointer"
            >
              <div className="font-medium text-gray-900 dark:text-gray-100">
                Spot {n.spot_id} freed
              </div>
              <div className="text-sm text-gray-500 dark:text-gray-400">
                {new Date(n.timestamp).toLocaleTimeString()}
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-2 text-right">
        <button
          onClick={toggleMute}
          className="text-sm text-red-500 dark:text-red-300"
        >
          {muted ? 'Unmute' : 'Mute'}
        </button>
      </div>
    </div>
  );
}
