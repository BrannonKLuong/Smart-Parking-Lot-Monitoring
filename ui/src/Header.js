// Path: ui/src/Header.js (No Dark Mode)
import React from 'react';

// Removed darkMode and toggleDarkMode props
export default function Header({ totalSpots, freeSpots }) {
  return (
    // Removed dark mode specific classes like dark:bg-gray-800, dark:text-gray-100
    <header className="flex items-center justify-between p-4 bg-white shadow">
      <h1 className="text-2xl font-bold text-gray-900">
        Smart Parking ({freeSpots}/{totalSpots} free)
      </h1>
      {/* Dark mode toggle button REMOVED */}
    </header>
  );
}
