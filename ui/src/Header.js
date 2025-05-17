import React from 'react';

export default function Header({ darkMode, toggleDarkMode, totalSpots, freeSpots }) {
  return (
    <header className="flex items-center justify-between p-4 bg-white dark:bg-gray-800 shadow">
      <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
        Smart Parking ({freeSpots}/{totalSpots} free)
      </h1>
      <button
        onClick={toggleDarkMode}
        className="p-2 rounded bg-gray-200 dark:bg-gray-700 text-gray-800 dark:text-gray-200"
      >
        {darkMode ? '🌙 Dark' : '☀️ Light'}
      </button>
    </header>
  );
}