import React, { useState, useRef, useEffect, useCallback } from 'react';

// Configuration
// const WEBSOCKET_URL = 'wss://your-app-runner-url/ws/video_stream_upload'; // Now passed as a prop
const FRAME_RATE = 5; // Frames per second
const IMAGE_QUALITY = 0.7; // JPEG quality (0.1 to 1.0)

const WebcamStreamer = ({ webSocketUrl }) => { // webSocketUrl is now a prop
  const [isCameraActive, setIsCameraActive] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState(null);
  const [wsStatus, setWsStatus] = useState('Disconnected');
  const videoRef = useRef(null);
  const canvasRef = useRef(null);
  const webSocketRef = useRef(null);
  const streamRef = useRef(null);
  const streamIntervalRef = useRef(null);

  // Function to start the camera
  const startCamera = async () => {
    setError(null);
    try {
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        const stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play(); 
        }
        setIsCameraActive(true);
        console.log('Camera started');
      } else {
        throw new Error('getUserMedia not supported in this browser.');
      }
    } catch (err) {
      console.error('Error starting camera:', err);
      setError(`Error starting camera: ${err.message}. Please ensure you have a webcam and grant permission.`);
      setIsCameraActive(false);
    }
  };

  // Function to stop the camera
  const stopCamera = useCallback(() => {
    if (streamIntervalRef.current) { // Stop sending frames if camera stops
        clearInterval(streamIntervalRef.current);
        streamIntervalRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(track => track.stop());
      streamRef.current = null;
    }
    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }
    setIsCameraActive(false);
    // Do not automatically disconnect WebSocket here, let user control streaming explicitly
    // setIsStreaming(false); // User might want to stop camera but keep WS for a moment or vice-versa
    console.log('Camera stopped');
  }, []);

  // Function to connect to WebSocket
  const connectWebSocket = useCallback(() => {
    if (!webSocketUrl) {
        setError("WebSocket URL is not provided to WebcamStreamer component.");
        setWsStatus('Error: URL not configured');
        console.error("WebSocket URL is missing in props.");
        return;
    }
    if (webSocketRef.current && webSocketRef.current.readyState === WebSocket.OPEN) {
      console.log('WebSocket already connected.');
      setWsStatus('Connected');
      // setIsStreaming(true); // Set isStreaming only after successful connection and user intent
      return;
    }

    webSocketRef.current = new WebSocket(webSocketUrl);
    setWsStatus('Connecting...');

    webSocketRef.current.onopen = () => {
      console.log('WebSocket connected to', webSocketUrl);
      setWsStatus('Connected');
      setIsStreaming(true); // Now we are officially streaming
    };

    webSocketRef.current.onclose = (event) => {
      console.log('WebSocket disconnected:', event.reason, `Code: ${event.code}`);
      setWsStatus(`Disconnected: ${event.reason || 'Connection closed'}`);
      setIsStreaming(false);
      if (streamIntervalRef.current) { // Ensure interval is cleared if WS closes
        clearInterval(streamIntervalRef.current);
        streamIntervalRef.current = null;
      }
    };

    webSocketRef.current.onerror = (err) => {
      console.error('WebSocket error:', err);
      setError(`WebSocket error connecting to ${webSocketUrl}. Check console and backend.`);
      setWsStatus('Error');
      setIsStreaming(false);
    };

    webSocketRef.current.onmessage = (event) => {
      console.log('WebSocket message received:', event.data);
    };
  }, [webSocketUrl]);

  // Function to disconnect WebSocket
  const disconnectWebSocket = useCallback(() => {
    if (streamIntervalRef.current) {
      clearInterval(streamIntervalRef.current);
      streamIntervalRef.current = null;
    }
    if (webSocketRef.current) {
      webSocketRef.current.close();
      // webSocketRef.current = null; // onclose will handle state updates
    }
    // setIsStreaming(false); // onclose will set this
    // setWsStatus('Disconnected'); // onclose will set this
    console.log('WebSocket disconnected by user.');
  }, []);


  // Function to capture a frame and send it
  const captureAndSendFrame = useCallback(() => {
    if (!videoRef.current || !canvasRef.current || !webSocketRef.current || webSocketRef.current.readyState !== WebSocket.OPEN || !isCameraActive) {
      return;
    }

    const video = videoRef.current;
    const canvas = canvasRef.current;

    if (video.videoWidth === 0 || video.videoHeight === 0) {
        return;
    }

    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const context = canvas.getContext('2d');
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const frameDataUrl = canvas.toDataURL('image/jpeg', IMAGE_QUALITY);
    webSocketRef.current.send(frameDataUrl);

  }, [isCameraActive]); // Added isCameraActive dependency


  // Effect to start/stop streaming frames
  useEffect(() => {
    if (isStreaming && isCameraActive && webSocketRef.current && webSocketRef.current.readyState === WebSocket.OPEN) {
      if (streamIntervalRef.current) {
        clearInterval(streamIntervalRef.current); 
      }
      streamIntervalRef.current = setInterval(captureAndSendFrame, 1000 / FRAME_RATE);
      console.log(`Started streaming frames at ${FRAME_RATE} FPS`);
    } else {
      if (streamIntervalRef.current) {
        clearInterval(streamIntervalRef.current);
        streamIntervalRef.current = null;
        console.log('Stopped streaming frames');
      }
    }

    return () => {
      if (streamIntervalRef.current) {
        clearInterval(streamIntervalRef.current);
      }
    };
  }, [isStreaming, isCameraActive, captureAndSendFrame]);


  // Cleanup on component unmount
  useEffect(() => {
    return () => {
      stopCamera();
      disconnectWebSocket();
    };
  }, [stopCamera, disconnectWebSocket]);


  const handleStartStreaming = async () => {
    setError(null);
    if (!isCameraActive) {
      await startCamera(); 
      // It's important startCamera sets isCameraActive to true for the useEffect to pick up streaming.
      // A small delay might still be needed if state updates are not immediate enough for connectWebSocket.
      // However, connectWebSocket itself checks for camera readiness implicitly.
      await new Promise(resolve => setTimeout(resolve, 100)); // Small delay for camera to be fully ready
    }
     // Check if camera is active before connecting. startCamera updates isCameraActive.
    if (streamRef.current) { // Check if stream is actually available
        connectWebSocket();
    } else if (!error) { 
        setError("Camera could not be started or is not yet ready. Cannot start streaming.");
        console.log("Attempted to start streaming, but camera stream is not available.");
    }
  };

  const handleStopStreaming = () => {
    disconnectWebSocket(); // This will set isStreaming to false via onclose
    // Optionally stop the camera as well
    // stopCamera(); 
  };


  return (
    <div className="p-4 bg-gray-800 text-white rounded-lg shadow-xl w-full max-w-2xl mx-auto my-8 font-sans">
      <h3 className="text-xl font-semibold mb-4 text-center text-teal-400">Stream Your Webcam</h3>

      <div className="mb-4 bg-black rounded-md overflow-hidden shadow-inner">
        <video ref={videoRef} muted autoPlay playsInline className="w-full h-auto aspect-video" />
      </div>
      
      <canvas ref={canvasRef} style={{ display: 'none' }} />

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
        {!isCameraActive ? (
          <button
            onClick={startCamera}
            className="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2 px-3 rounded-lg shadow transition duration-150 ease-in-out focus:outline-none focus:ring-2 focus:ring-blue-400"
          >
            Start Camera
          </button>
        ) : (
          <button
            onClick={stopCamera}
            className="w-full bg-red-600 hover:bg-red-700 text-white font-semibold py-2 px-3 rounded-lg shadow transition duration-150 ease-in-out focus:outline-none focus:ring-2 focus:ring-red-400"
          >
            Stop Camera
          </button>
        )}

        {isCameraActive && !isStreaming ? (
          <button
            onClick={handleStartStreaming}
            disabled={!isCameraActive || wsStatus === 'Connecting...'}
            className="w-full bg-green-600 hover:bg-green-700 text-white font-semibold py-2 px-3 rounded-lg shadow transition duration-150 ease-in-out disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-green-400"
          >
            {wsStatus === 'Connecting...' ? 'Connecting WS...' : 'Start Streaming to Backend'}
          </button>
        ) : isCameraActive && isStreaming ? (
          <button
            onClick={handleStopStreaming}
            className="w-full bg-yellow-500 hover:bg-yellow-600 text-black font-semibold py-2 px-3 rounded-lg shadow transition duration-150 ease-in-out focus:outline-none focus:ring-2 focus:ring-yellow-300"
          >
            Stop Streaming
          </button>
        ) : (
            <div className="w-full bg-gray-700 text-gray-400 font-semibold py-2 px-3 rounded-lg shadow text-center text-sm">
                Start camera to enable streaming controls
            </div>
        )}
      </div>
      
      <div className="space-y-2 text-xs sm:text-sm">
        {error && (
          <div className="bg-red-700 border border-red-900 text-white px-3 py-2 rounded-lg" role="alert">
            <strong className="font-bold">Error: </strong>
            <span className="block sm:inline">{error}</span>
          </div>
        )}
        <div className={`p-2 rounded-lg shadow ${isCameraActive ? 'bg-green-700 border-green-900' : 'bg-gray-700 border-gray-900'}`}>
          <span className="font-semibold">Camera:</span> {isCameraActive ? 'Active' : 'Inactive'}
        </div>
        <div className={`p-2 rounded-lg shadow ${wsStatus === 'Connected' ? 'bg-green-700 border-green-900' : wsStatus.startsWith('Error') ? 'bg-red-700 border-red-900' : 'bg-gray-700 border-gray-900'}`}>
          <span className="font-semibold">Backend WS:</span> {wsStatus}
        </div>
         <div className={`p-2 rounded-lg shadow ${isStreaming ? 'bg-green-700 border-green-900' : 'bg-gray-700 border-gray-900'}`}>
          <span className="font-semibold">Streaming to Backend:</span> {isStreaming ? `Active (${FRAME_RATE} FPS)` : 'Not Streaming'}
        </div>
      </div>
    </div>
  );
};

export default WebcamStreamer;