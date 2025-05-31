// Path: ui/src/WebcamStreamer.js (Ultra-Detailed Logging for getUserMedia)
import React, { useState, useRef, useEffect, useCallback } from 'react';

// Configuration
const FRAME_RATE = 5; // Frames per second
const IMAGE_QUALITY = 0.7; // JPEG quality (0.1 to 1.0)

const WebcamStreamer = ({ webSocketUrl, onStreamingActive }) => {
  const [isCameraActive, setIsCameraActive] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [error, setError] = useState(null);
  const [wsStatus, setWsStatus] = useState('Disconnected');
  const videoRef = useRef(null);
  const canvasRef = useRef(null);
  const webSocketRef = useRef(null);
  const streamRef = useRef(null);
  const streamIntervalRef = useRef(null);

  useEffect(() => {
    console.log("[WebcamStreamer] Component Mounted or Updated. Props: webSocketUrl:", webSocketUrl);
    return () => {
        console.log("[WebcamStreamer] Component Unmounting. Cleaning up...");
    };
  }, [webSocketUrl]);


  const startCamera = async () => {
    console.log("[WebcamStreamer] Attempting to start camera...");
    setError(null);
    try {
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        console.log("[WebcamStreamer] navigator.mediaDevices.getUserMedia is available.");
        console.log("[WebcamStreamer] --- Calling getUserMedia NOW ---");
        const getUserMediaPromise = navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 } });
        console.log("[WebcamStreamer] getUserMedia promise created:", getUserMediaPromise);

        const stream = await getUserMediaPromise; // Wait for the promise to resolve

        console.log("[WebcamStreamer] --- getUserMedia call has completed/resolved ---");
        if (stream) {
            console.log("[WebcamStreamer] getUserMedia stream obtained:", stream);
            streamRef.current = stream;
            if (videoRef.current) {
              console.log("[WebcamStreamer] videoRef is available. Setting srcObject.");
              videoRef.current.srcObject = stream;
              await videoRef.current.play();
              console.log("[WebcamStreamer] videoRef.current.play() successful.");
            } else {
                console.warn("[WebcamStreamer] videoRef.current is null when trying to set srcObject.");
            }
            setIsCameraActive(true);
            console.log('[WebcamStreamer] Camera started successfully. isCameraActive: true');
        } else {
            // This case should ideally not happen if getUserMedia resolves without error but returns no stream.
            // Usually, it would throw an error if no stream is available.
            console.warn("[WebcamStreamer] getUserMedia resolved but stream is null or undefined.");
            throw new Error("getUserMedia resolved but no stream was returned.");
        }
      } else {
        console.error('[WebcamStreamer] getUserMedia not supported in this browser.');
        throw new Error('getUserMedia not supported in this browser.');
      }
    } catch (err) {
      console.error('[WebcamStreamer] Error in startCamera (could be from getUserMedia or play):', err.name, err.message, err);
      setError(`Cam Error: ${err.name} - ${err.message}. Chk perm/hw.`);
      setIsCameraActive(false);
      if (onStreamingActive) onStreamingActive(false);
    }
  };

  const stopCamera = useCallback(() => {
    console.log("[WebcamStreamer] Stopping camera...");
    if (streamIntervalRef.current) {
        clearInterval(streamIntervalRef.current);
        streamIntervalRef.current = null;
        console.log("[WebcamStreamer] Frame sending interval cleared due to stopCamera.");
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(track => track.stop());
      streamRef.current = null;
      console.log("[WebcamStreamer] Media stream tracks stopped.");
    }
    if (videoRef.current) {
      videoRef.current.srcObject = null;
    }
    setIsCameraActive(false);
    if (isStreaming) { 
        setIsStreaming(false);
        if (onStreamingActive) onStreamingActive(false);
    }
    console.log('[WebcamStreamer] Camera stopped. isCameraActive: false');
  }, [isStreaming, onStreamingActive]);

  const connectWebSocket = useCallback(() => {
    console.log("[WebcamStreamer] Attempting to connect WebSocket. URL:", webSocketUrl);
    if (!webSocketUrl) {
        const msg = "WebSocket URL is not provided to WebcamStreamer component.";
        console.error("[WebcamStreamer]", msg);
        setError(msg);
        setWsStatus('Error: URL not configured');
        if (onStreamingActive) onStreamingActive(false);
        return;
    }
    if (webSocketRef.current && webSocketRef.current.readyState === WebSocket.OPEN) {
      console.log('[WebcamStreamer] WebSocket already connected.');
      setWsStatus('Connected');
      if (isCameraActive) {
        setIsStreaming(true);
        if (onStreamingActive) onStreamingActive(true);
      }
      return;
    }

    webSocketRef.current = new WebSocket(webSocketUrl);
    setWsStatus('Connecting...');
    console.log("[WebcamStreamer] WebSocket instance created. Status: Connecting...");

    webSocketRef.current.onopen = () => {
      console.log('[WebcamStreamer] WebSocket connected to', webSocketUrl);
      setWsStatus('Connected');
      if (isCameraActive) { 
        setIsStreaming(true); 
        if (onStreamingActive) onStreamingActive(true);
      } else {
        console.log("[WebcamStreamer] WebSocket opened, but camera is not active. Not setting isStreaming to true yet.");
      }
    };

    webSocketRef.current.onclose = (event) => {
      console.log(`[WebcamStreamer] WebSocket disconnected: ${event.reason || 'Connection closed'}, Code: ${event.code}`);
      setWsStatus(`Disconnected: ${event.reason || 'Connection closed'}`);
      setIsStreaming(false);
      if (onStreamingActive) onStreamingActive(false);
      if (streamIntervalRef.current) { 
        clearInterval(streamIntervalRef.current);
        streamIntervalRef.current = null;
        console.log("[WebcamStreamer] Frame sending interval cleared due to WS close.");
      }
    };

    webSocketRef.current.onerror = (err) => {
      console.error('[WebcamStreamer] WebSocket error:', err);
      setError(`WebSocket error. Check console and backend. URL: ${webSocketUrl}`);
      setWsStatus('Error');
      setIsStreaming(false);
      if (onStreamingActive) onStreamingActive(false);
    };
  }, [webSocketUrl, isCameraActive, onStreamingActive]); 

  const disconnectWebSocket = useCallback(() => {
    console.log("[WebcamStreamer] Disconnecting WebSocket...");
    if (streamIntervalRef.current) {
      clearInterval(streamIntervalRef.current);
      streamIntervalRef.current = null;
      console.log("[WebcamStreamer] Frame sending interval cleared due to disconnectWebSocket call.");
    }
    if (webSocketRef.current) {
      webSocketRef.current.close();
      console.log("[WebcamStreamer] WebSocket.close() called.");
    }
  }, []);

  const captureAndSendFrame = useCallback(() => {
    if (!videoRef.current || !canvasRef.current || !webSocketRef.current || webSocketRef.current.readyState !== WebSocket.OPEN || !isCameraActive) {
      if (webSocketRef.current && webSocketRef.current.readyState !== WebSocket.OPEN) {
          setIsStreaming(false);
          if(onStreamingActive) onStreamingActive(false);
      }
      return;
    }
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (video.videoWidth === 0 || video.videoHeight === 0 || video.paused || video.ended) return;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const context = canvas.getContext('2d');
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const frameDataUrl = canvas.toDataURL('image/jpeg', IMAGE_QUALITY);
    if (webSocketRef.current.bufferedAmount < 1024 * 1024) { // Check if buffer is less than 1MB (arbitrary limit)
        webSocketRef.current.send(frameDataUrl);
    } else {
        console.log("[WebcamStreamer] WebSocket buffer high, skipping frame. Buffered amount:", webSocketRef.current.bufferedAmount);
    }
  }, [isCameraActive, onStreamingActive]);

  useEffect(() => {
    console.log(`[WebcamStreamer] Effect for streaming interval. isStreaming: ${isStreaming}, isCameraActive: ${isCameraActive}, WS_ReadyState: ${webSocketRef.current?.readyState}`);
    if (isStreaming && isCameraActive && webSocketRef.current && webSocketRef.current.readyState === WebSocket.OPEN) {
      if (streamIntervalRef.current) clearInterval(streamIntervalRef.current); 
      streamIntervalRef.current = setInterval(captureAndSendFrame, 1000 / FRAME_RATE);
      console.log(`[WebcamStreamer] Started streaming frames at ${FRAME_RATE} FPS`);
    } else {
      if (streamIntervalRef.current) {
        clearInterval(streamIntervalRef.current);
        streamIntervalRef.current = null;
        console.log('[WebcamStreamer] Stopped streaming frames (condition not met or interval cleared).');
      }
    }
    return () => {
      if (streamIntervalRef.current) {
        clearInterval(streamIntervalRef.current);
        console.log("[WebcamStreamer] Cleaned up streaming interval from effect.");
      }
    };
  }, [isStreaming, isCameraActive, captureAndSendFrame]);

  useEffect(() => {
    return () => {
      console.log("[WebcamStreamer] Component will unmount - calling stopCamera and disconnectWebSocket.");
      stopCamera();
      disconnectWebSocket();
    };
  }, [stopCamera, disconnectWebSocket]);

  const handleStartStreaming = async () => {
    console.log("[WebcamStreamer] 'Start Streaming to Backend' button clicked.");
    setError(null);
    if (!isCameraActive) {
      console.log("[WebcamStreamer] Camera not active, calling startCamera first.");
      await startCamera(); 
      if (!streamRef.current) { 
          const msg = "Camera failed to start. Cannot initiate streaming.";
          console.error("[WebcamStreamer]", msg);
          setError(msg);
          return; 
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    if (streamRef.current && isCameraActive) { 
        console.log("[WebcamStreamer] Camera is active, proceeding to connect WebSocket.");
        connectWebSocket();
    } else { 
        const msg = "Camera is not active after attempt. Cannot start streaming.";
        console.error("[WebcamStreamer]", msg);
        if (!error) setError(msg); 
    }
  };

  const handleStopStreaming = () => {
    console.log("[WebcamStreamer] 'Stop Streaming' button clicked.");
    disconnectWebSocket(); 
  };

  return (
    <div className="p-4 bg-gray-800 text-white rounded-lg shadow-xl w-full max-w-2xl mx-auto my-8 font-sans">
      <h3 className="text-xl font-semibold mb-4 text-center text-teal-400">Stream Your Webcam</h3>
      <div className="mb-2 text-center text-xs">
        (Console logs in DevTools will show detailed steps)
      </div>
      <div className="mb-4 bg-black rounded-md overflow-hidden shadow-inner">
        <video ref={videoRef} muted autoPlay playsInline className="w-full h-auto aspect-video" />
      </div>
      <canvas ref={canvasRef} style={{ display: 'none' }} />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
        {!isCameraActive ? (
          <button onClick={startCamera} className="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2 px-3 rounded-lg shadow transition duration-150 ease-in-out focus:outline-none focus:ring-2 focus:ring-blue-400">
            Start Camera
          </button>
        ) : (
          <button onClick={stopCamera} className="w-full bg-red-600 hover:bg-red-700 text-white font-semibold py-2 px-3 rounded-lg shadow transition duration-150 ease-in-out focus:outline-none focus:ring-2 focus:ring-red-400">
            Stop Camera
          </button>
        )}
        {isCameraActive && !isStreaming ? (
          <button onClick={handleStartStreaming} disabled={!isCameraActive || wsStatus === 'Connecting...'} className="w-full bg-green-600 hover:bg-green-700 text-white font-semibold py-2 px-3 rounded-lg shadow transition duration-150 ease-in-out disabled:opacity-50 focus:outline-none focus:ring-2 focus:ring-green-400">
            {wsStatus === 'Connecting...' ? 'Connecting WS...' : 'Start Streaming to Backend'}
          </button>
        ) : isCameraActive && isStreaming ? (
          <button onClick={handleStopStreaming} className="w-full bg-yellow-500 hover:bg-yellow-600 text-black font-semibold py-2 px-3 rounded-lg shadow transition duration-150 ease-in-out focus:outline-none focus:ring-2 focus:ring-yellow-300">
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