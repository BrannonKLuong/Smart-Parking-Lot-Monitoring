import React, { useEffect, useState } from 'react'
import SpotTile from './SpotTile'
import './App.css'

function App() {
  const [spots,     setSpots   ] = useState([])
  const [statuses, setStatuses] = useState({})
  const [times,    setTimes   ] = useState({})
  const [notes,    setNotes   ] = useState([])

  // 1) Load spot IDs
  useEffect(() => {
    fetch('/spots.json')
      .then(r => r.json())
      .then(data => {
        const ids = data.spots.map(s => s.id)
        setSpots(ids)
        setStatuses(Object.fromEntries(ids.map(id => [id, true])))
      })
  }, [])

  // 2) WebSocket handler for vacancy + occupancy
  useEffect(() => {
    const ws = new WebSocket('ws://localhost:8000/ws')

    ws.onopen = () => console.log('✅ WS connected')
    ws.onmessage = ev => {
      console.log('📬 Got event', ev.data)
      let { spot_id, timestamp, status } = JSON.parse(ev.data)

      // ensure JS parses as UTC
      if (!timestamp.endsWith('Z')) timestamp = timestamp + 'Z'

      if (status === 'occupied') {
        // spot has become occupied again
        setStatuses(s => ({ ...s, [spot_id]: true }))
      } else {
        // vacancy event
        setStatuses(s => ({ ...s, [spot_id]: false }))
        setTimes   (t => ({ ...t, [spot_id]: timestamp }))

        // notification toast
        setNotes(n => [...n, { spot_id, timestamp }])
        setTimeout(() => {
          setNotes(n => n.filter(x => x.spot_id !== spot_id))
        }, 5000)
      }
    }
    ws.onerror = e => console.error('❌ WS error', e)
    ws.onclose = () => console.warn('⚠️ WS closed')

    return () => ws.close()
  }, [])

  return (
    <div style={{ fontFamily: 'Arial, sans-serif', textAlign: 'center' }}>
      <h1>Smart Parking Monitor</h1>

      {/* Live MJPEG feed */}
      <div style={{ position: 'relative', display: 'inline-block' }}>
        <img
          src="http://localhost:8000/webcam_feed"
          alt="Parking Feed"
          style={{ width: 800, maxWidth: '100%' }}
        />
        <ul style={{
          position: 'absolute', top: 10, right: 10,
          listStyle: 'none', padding: 0, margin: 0
        }}>
          {notes.map((n,i) => (
            <li key={i} style={{
              background:'rgba(255,255,255,0.8)',
              marginBottom:5, padding:'8px 12px',
              borderRadius:4
            }}>
              Spot {n.spot_id} freed at {new Date(n.timestamp).toLocaleTimeString()}
            </li>
          ))}
        </ul>
      </div>

      {/* Spot grid */}
      <div style={{
        display:'flex', flexWrap:'wrap',
        justifyContent:'center', marginTop:20
      }}>
        {spots.map(id => (
          <SpotTile
            key={id}
            id={id}
            isFree={!statuses[id]}
            freeSince={times[id]}
          />
        ))}
      </div>
    </div>
  )
}

export default App
