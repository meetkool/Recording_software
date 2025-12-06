import time
import traceback
import wave
import os

def run_audio_service(device_type, filename, stop_event, ready_event, start_event):
    """
    Audio recording using PyAudioWPatch with Callback Mode for non-blocking operation.
    Includes synchronization logic to pad silence if recording starts late (e.g. Loopback waiting for audio).
    """
    print(f"[{device_type}] Audio Process Started. Target: {filename}", flush=True)
    
    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        print(f"[{device_type}] ERROR: pyaudiowpatch not installed!", flush=True)
        if ready_event: ready_event.set()
        return
    
    # Default settings
    chunk = 1024
    sample_format = pyaudio.paInt16
    channels = 2
    sample_rate = 44100
    
    p = pyaudio.PyAudio()
    stream = None
    frames = []
    
    # Sync variables
    first_frame_time = None
    
    def callback(in_data, frame_count, time_info, status):
        nonlocal first_frame_time
        if first_frame_time is None:
            first_frame_time = time.time()
        frames.append(in_data)
        return (in_data, pyaudio.paContinue)
    
    try:
        # --- Device Setup ---
        if device_type == 'system':
            # WASAPI Loopback
            try:
                wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
                default_speakers = p.get_device_info_by_index(wasapi_info['defaultOutputDevice'])
                print(f"[{device_type}] Default speakers: {default_speakers['name']}", flush=True)
                
                loopback_device = None
                for i in range(p.get_device_count()):
                    dev_info = p.get_device_info_by_index(i)
                    if (dev_info.get('isLoopbackDevice', False) and 
                        default_speakers['name'] in dev_info['name']):
                        loopback_device = dev_info
                        print(f"[{device_type}] Found matching loopback: {dev_info['name']}", flush=True)
                        break
                
                if loopback_device is None:
                    # Fallback
                    for i in range(p.get_device_count()):
                        dev_info = p.get_device_info_by_index(i)
                        if dev_info.get('isLoopbackDevice', False):
                            loopback_device = dev_info
                            print(f"[{device_type}] Using fallback loopback: {dev_info['name']}", flush=True)
                            break
                
                if loopback_device is None:
                    print(f"[{device_type}] ERROR: No loopback device found!", flush=True)
                    if ready_event: ready_event.set()
                    p.terminate()
                    return
                
                # Loopback requires exact device settings
                sample_rate = int(loopback_device['defaultSampleRate'])
                channels = int(loopback_device['maxInputChannels']) or 2
                
                print(f"[{device_type}] Opening loopback stream: {sample_rate}Hz, {channels}ch", flush=True)
                
                stream = p.open(
                    format=sample_format,
                    channels=channels,
                    rate=sample_rate,
                    frames_per_buffer=chunk,
                    input=True,
                    input_device_index=loopback_device['index'],
                    stream_callback=callback,
                    start=False
                )
                
            except Exception as e:
                print(f"[{device_type}] Loopback setup error: {e}", flush=True)
                traceback.print_exc()
                if ready_event: ready_event.set()
                p.terminate()
                return
                
        elif device_type == 'mic':
            try:
                default_mic = p.get_default_input_device_info()
                print(f"[{device_type}] Default mic: {default_mic['name']}", flush=True)
                
                sample_rate = int(default_mic['defaultSampleRate'])
                # Force stereo if possible, or mono
                channels = min(2, int(default_mic['maxInputChannels'])) or 1
                
                print(f"[{device_type}] Opening mic stream: {sample_rate}Hz, {channels}ch", flush=True)
                
                stream = p.open(
                    format=sample_format,
                    channels=channels,
                    rate=sample_rate,
                    frames_per_buffer=chunk,
                    input=True,
                    stream_callback=callback,
                    start=False
                )
                
            except Exception as e:
                print(f"[{device_type}] Mic setup error: {e}", flush=True)
                if ready_event: ready_event.set()
                p.terminate()
                return
        
        if stream is None:
            print(f"[{device_type}] Failed to open stream!", flush=True)
            if ready_event: ready_event.set()
            p.terminate()
            return
            
        # --- Ready & Sync ---
        print(f"[{device_type}] Device ready. Waiting for start signal...", flush=True)
        if ready_event: ready_event.set()
        
        if start_event:
            if not start_event.wait(timeout=30):
                print(f"[{device_type}] Timeout waiting for start.", flush=True)
                p.terminate()
                return
        
        # --- Recording Start ---
        # Capture T0 right before starting stream
        start_timestamp = time.time()
        
        print(f"[{device_type}] Starting stream...", flush=True)
        stream.start_stream()
        print(f"[{device_type}] Recording started!", flush=True)
        
        last_report = time.time()
        
        # --- Main Loop ---
        while not stop_event.is_set():
            if not stream.is_active():
                # For loopback, this might mean device lost or just stopped? 
                # Usually loopback stays active but callback stops.
                pass
                
            if time.time() - last_report > 5:
                # Estimate duration
                duration = len(frames) * chunk / sample_rate
                print(f"[{device_type}] Recording... {duration:.1f}s ({len(frames)} chunks)", flush=True)
                last_report = time.time()
            
            time.sleep(0.1)
        
        # --- Stop & Save ---
        print(f"[{device_type}] Stopping stream...", flush=True)
        stream.stop_stream()
        stream.close()
        stream = None
        
        # --- Synchronization / Padding Logic ---
        # Calculate if we need padding
        sample_width = p.get_sample_size(sample_format)
        
        if first_frame_time is None:
            # No audio was recorded at all. Pad with silence for the full duration?
            # Or maybe the loopback never fired.
            total_duration = time.time() - start_timestamp
            print(f"[{device_type}] WARNING: No frames received. Padding full duration ({total_duration:.1f}s) with silence.", flush=True)
            
            pad_frames = int(total_duration * sample_rate)
            silence_bytes = b'\x00' * pad_frames * channels * sample_width
            frames.append(silence_bytes)
            
        else:
            # Calculate delay
            delay = first_frame_time - start_timestamp
            if delay > 0.05: # Tolerance of 50ms
                print(f"[{device_type}] Detected delay of {delay:.3f}s. Padding start...", flush=True)
                pad_frames = int(delay * sample_rate)
                
                # Create silence buffer
                # Ensure alignment to frame size
                bytes_per_frame = channels * sample_width
                pad_bytes_count = pad_frames * bytes_per_frame
                silence_bytes = b'\x00' * pad_bytes_count
                
                frames.insert(0, silence_bytes)
            else:
                print(f"[{device_type}] Sync OK (delay {delay:.3f}s)", flush=True)
        
        print(f"[{device_type}] Saving {len(frames)} chunks...", flush=True)
        
        if frames:
            try:
                wf = wave.open(filename, 'wb')
                wf.setnchannels(channels)
                wf.setsampwidth(sample_width)
                wf.setframerate(sample_rate)
                wf.writeframes(b''.join(frames))
                wf.close()
                
                file_size = os.path.getsize(filename)
                final_duration = (file_size - 44) / (sample_rate * channels * sample_width) # Approx
                print(f"[{device_type}] SAVED: {filename} ({file_size} bytes, ~{final_duration:.1f}s)", flush=True)
            except Exception as e:
                print(f"[{device_type}] Save error: {e}", flush=True)
                traceback.print_exc()
        
    except Exception as e:
        print(f"[{device_type}] Critical error: {e}", flush=True)
        traceback.print_exc()
            
    finally:
        if stream:
            try:
                stream.stop_stream()
                stream.close()
            except: pass
        p.terminate()
        print(f"[{device_type}] Process finished", flush=True)
