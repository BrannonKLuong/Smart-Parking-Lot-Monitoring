// Path: ui/src/WebcamStreamer.js (HTTP POST for Frames)
import React, { useState, useRef, useEffect, useCallback, useMemo } from 'react';

// Configuration
const FRAME_RATE = 5; // Frames per second to send to backend
const IMAGE_QUALITY = 0.7; // JPEG quality (0.1 to 1.0)
const VIDEO_CONSTRAINTS = { video: { width: 640, height: 480 } };

const WebcamStreamer = ({ apiBaseUrl, onStreamingActive }) => { 
  const [isCameraActive, setIsCameraActive] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false); 
  const [error, setError] = useState(null);
  const [statusMessage, setStatusMessage] = useState('Idle'); 

  const videoRef = useRef(null);
  const canvasRef = useRef(null);
  const streamRef = useRef(null);
  const streamIntervalRef = useRef(null);

  const uploadFrameEndpoint = useMemo(() => {
    if (!apiBaseUrl) {
        console.warn("[WebcamStreamer] apiBaseUrl is null/undefined, uploadFrameEndpoint will be null.");
        return null;
    }
    return `${apiBaseUrl}/api/upload_frame`;
  }, [apiBaseUrl]);

  useEffect(() => {
    // console.log("[WebcamStreamer] Component Mounted/Updated. Upload Endpoint:", uploadFrameEndpoint);
    return () => {
        // console.log("[WebcamStreamer] Component Unmounting. Cleaning up...");
        if (streamIntervalRef.current) {
            clearInterval(streamIntervalRef.current);
        }
    };
  }, [uploadFrameEndpoint]);


  const startCamera = useCallback(async () => {
    // console.log("[WebcamStreamer] Attempting to start camera...");
    setError(null);
    setStatusMessage("Starting camera...");
    try {
      if (navigator.mediaDevices && navigator.mediaDevices.getUserMedia) {
        const stream = await navigator.mediaDevices.getUserMedia(VIDEO_CONSTRAINTS);
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play();
        }
        setIsCameraActive(true); 
        setStatusMessage("Camera active.");
        console.log('[WebcamStreamer] Camera started successfully.');
      } else {
        throw new Error('getUserMedia not supported.');
      }
    } catch (err) {
      console.error('[WebcamStreamer] Error starting camera:', err.name, err.message);
      setError(`Cam Error: ${err.name} - ${err.message}.`);
      setStatusMessage("Camera error.");
      setIsCameraActive(false);
      if (onStreamingActive) onStreamingActive(false);
    }
  }, [onStreamingActive]); 
  
  const stopCamera = useCallback(() => {
    // console.log("[WebcamStreamer] Stopping camera...");
    if (streamIntervalRef.current) {
        clearInterval(streamIntervalRef.current);
        streamIntervalRef.current = null;
    }
    if (streamRef.current) {
      streamRef.current.getTracks().forEach(track => track.stop());
      streamRef.current = null;
    }
    if (videoRef.current) videoRef.current.srcObject = null;
    
    setIsStreaming(false); 
    setIsCameraActive(false); 
    
    if (onStreamingActive) onStreamingActive(false); 
    setStatusMessage("Camera stopped.");
    // console.log('[WebcamStreamer] Camera stopped.');
  }, [onStreamingActive]);

  const captureAndSendFrame = useCallback(async () => {
    // console.log("[WebcamStreamer] captureAndSendFrame called"); 

    if (!videoRef.current || !canvasRef.current || !isCameraActive || !uploadFrameEndpoint) {
      // console.log("[WebcamStreamer] captureAndSendFrame: Preconditions not met. isCameraActive:", isCameraActive, "uploadFrameEndpoint:", uploadFrameEndpoint);
      return;
    }
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (video.videoWidth === 0 || video.videoHeight === 0 || video.paused || video.ended) {
      // console.log("[WebcamStreamer] captureAndSendFrame: Video not ready or ended.");
      return;
    }

    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const context = canvas.getContext('2d');
    context.drawImage(video, 0, 0, canvas.width, canvas.height);
    const frameDataUrl = canvas.toDataURL('image/jpeg', IMAGE_QUALITY);

    try {
      const response = await fetch(uploadFrameEndpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', },
        body: JSON.stringify({ frame: frameDataUrl }),
      });
      if (!response.ok) {
        const errorData = await response.text();
        console.error(`[WebcamStreamer] HTTP error sending frame: ${response.status} ${response.statusText}`, errorData);
        setError(`HTTP error: ${response.status}. Check console.`);
        // Consider stopping streaming on persistent errors
        // setIsStreaming(false); 
      } else {
         // console.log("[WebcamStreamer] Frame send success (HTTP)");
      }
    } catch (err) {
      console.error("[WebcamStreamer] Network error sending frame via HTTP POST:", err);
      setError("Network error sending frame. Check console.");
      // setIsStreaming(false); // Optionally stop streaming on network error
    }
  }, [isCameraActive, uploadFrameEndpoint]); 

  useEffect(() => {
    // console.log(`[WebcamStreamer] Frame sending effect triggered. isStreaming: ${isStreaming}, isCameraActive: ${isCameraActive}`);
    if (isStreaming && isCameraActive) {
      if (streamIntervalRef.current) clearInterval(streamIntervalRef.current); 
      streamIntervalRef.current = setInterval(captureAndSendFrame, 1000 / FRAME_RATE);
      setStatusMessage(`Streaming ${FRAME_RATE} FPS via HTTP`);
      console.log(`[WebcamStreamer] Started HTTP frame sending at ${FRAME_RATE} FPS`);
      if (onStreamingActive) onStreamingActive(true);
    } else {
      if (streamIntervalRef.current) {
        clearInterval(streamIntervalRef.current);
        streamIntervalRef.current = null;
        setStatusMessage(isCameraActive ? "Camera active, not streaming." : "Streaming stopped.");
        // console.log('[WebcamStreamer] Stopped HTTP frame sending.');
      }
      // Ensure parent is notified if streaming stops for any reason leading to this else block
      if (onStreamingActive && isStreaming === false) onStreamingActive(false); 
    }
    return () => { if (streamIntervalRef.current) clearInterval(streamIntervalRef.current); };
  }, [isStreaming, isCameraActive, captureAndSendFrame, onStreamingActive]);

  useEffect(() => {
    return () => { stopCamera(); };
  }, [stopCamera]);

  const handleStartStreaming = async () => {
    // console.log("[WebcamStreamer] 'Start HTTP Streaming' button clicked.");
    setError(null);
    if (!isCameraActive) { 
      // console.log("[WebcamStreamer] Camera not active, calling startCamera first.");
      await startCamera(); 
      if (!streamRef.current) {
          // Error handling is now within startCamera, which sets error state
          return; 
      }
      // Give a moment for isCameraActive state to reflect
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    
    // Check isCameraActive state directly after attempting to start camera
    // This relies on the fact that startCamera will set isCameraActive
    // For more robustnes, ensure startCamera completes before checking isCameraActive for setIsStreaming
    if (isCameraActive || streamRef.current) { // Check both, streamRef.current is more direct after await
        console.log("[WebcamStreamer] Camera is active, setting isStreaming to true for HTTP.");
        setIsStreaming(true);
    } else {
        const msg = "Camera could not be activated. Cannot start streaming.";
        console.error("[WebcamStreamer]", msg);
        if (!error) setError(msg); 
        setStatusMessage("Camera not ready.");
    }
  };

  const handleStopStreaming = () => {
    // console.log("[WebcamStreamer] 'Stop HTTP Streaming' button clicked.");
    setIsStreaming(false); // This will trigger the useEffect to clear interval and update status
  };

  return (
    <div className="p-4 bg-gray-800 text-white rounded-lg shadow-xl w-full max-w-2xl mx-auto my-8 font-sans">
      <h3 className="text-xl font-semibold mb-4 text-center text-teal-400">Webcam (HTTP Upload Mode)</h3>
      <div className="mb-4 bg-black rounded-md overflow-hidden shadow-inner">
        <video ref={videoRef} muted autoPlay playsInline className="w-full h-auto aspect-video" />
      </div>
      <canvas ref={canvasRef} style={{ display: 'none' }} />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
        {!isCameraActive ? (
          <button onClick={startCamera} className="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2 px-3 rounded-lg shadow">
            Start Camera
          </button>
        ) : (
          <button onClick={stopCamera} className="w-full bg-red-600 hover:bg-red-700 text-white font-semibold py-2 px-3 rounded-lg shadow">
            Stop Camera
          </button>
        )}
        {isCameraActive && !isStreaming ? (
          <button onClick={handleStartStreaming} disabled={!isCameraActive} className="w-full bg-green-600 hover:bg-green-700 text-white font-semibold py-2 px-3 rounded-lg shadow disabled:opacity-50">
            Start HTTP Streaming
          </button>
        ) : isCameraActive && isStreaming ? (
          <button onClick={handleStopStreaming} className="w-full bg-yellow-500 hover:bg-yellow-600 text-black font-semibold py-2 px-3 rounded-lg shadow">
            Stop HTTP Streaming
          </button>
        ) : (
          <div className="w-full bg-gray-700 text-gray-400 font-semibold py-2 px-3 rounded-lg shadow text-center text-sm">
            Start camera to enable streaming
          </div>
        )}
      </div>
      <div className="space-y-2 text-xs sm:text-sm">
        {error && (<div className="bg-red-700 border border-red-900 text-white px-3 py-2 rounded-lg" role="alert"><strong>Error: </strong><span className="block sm:inline">{error}</span></div>)}
        <div className={`p-2 rounded-lg shadow ${isCameraActive ? 'bg-green-700' : 'bg-gray-700'}`}><span className="font-semibold">Camera:</span> {isCameraActive ? 'Active' : 'Inactive'}</div>
        <div className={`p-2 rounded-lg shadow ${isStreaming ? 'bg-green-700' : 'bg-gray-700'}`}><span className="font-semibold">HTTP Streaming:</span> {isStreaming ? `Active (${FRAME_RATE} FPS)` : 'Inactive'} ({statusMessage})</div>
      </div>
    </div>
  );
};
export default WebcamStreamer;
