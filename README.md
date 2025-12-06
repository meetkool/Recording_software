# Modern Python Screen Recorder
Refactored and enhanced version of the original Recording app.

## Features
- **High-Performance Recording**: Multi-threaded architecture ensures smooth UI and recording (default 60 FPS).
- **Audio Synchronization**: Automatic silence padding fixes audio sync issues caused by delayed streams.
- **System & Mic Audio**: Records both system sounds (loopback) and microphone input simultaneously.
- **Picture-in-Picture**: Webcam overlay with resizing support.
- **Cursor Tracking**: Real-time mouse cursor overlay.
- **Smart Merging**: Automatically merges video and multiple audio tracks using MoviePy.
- **Progress Tracking**: Dynamic progress bar during the processing phase.
- **Auto-Cleanup**: Automatically removes temporary files after successful processing.

## Requirements
- Python 3.8+
- Windows (for WASAPI Loopback audio support)

### Dependencies
```bash
pip install -r requirements.txt
```
*Key libraries: PyQt5, opencv-python, mss, moviepy, pyaudiowpatch, numpy*

## Usage
1. Run the application:
   ```bash
   python main.py
   ```
2. **Recording Tab**:
   - Select your Monitor.
   - Toggle Camera Preview if desired.
   - Set a Start Delay (optional).
   - Click **Start Recording**.
3. **Settings Tab**:
   - Toggle System Audio / Microphone Audio.
   - Change FPS (Default: 60).
   - Select Save Directory.

## Configuration
- **Camera Size**: Adjust `TARGET_CAM_WIDTH` and `TARGET_CAM_HEIGHT` in `main.py` to change the PIP size (default 320x240).

## Troubleshooting
- **Audio Sync**: If audio drifts, the app automatically detects start delays and pads with silence. Check logs for "Detected delay..." messages.
- **Missing Mic**: Microphone volume is boosted by 1.5x during merging to ensure visibility.

## Credits
Original by Ahmet Furkan DEMIR.
2025 Refactor & Enhancements by Meet Bhanushali.
