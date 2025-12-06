# Ahmet Furkan DEMIR (Original)
# Refactored 2025 - Multi-threaded Architecture & Logging
# Fixes: UI Freeze, Crashes on startup, Thread safety, Audio Crash via Multiprocessing

import sys
import os
import time
import datetime
import threading
import multiprocessing
import logging
import shutil
import traceback

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QFileDialog, QMessageBox
from PyQt5.QtCore import QTimer, QThread, pyqtSignal, pyqtSlot, Qt

import numpy as np
import cv2
import mss
import ctypes

class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

def get_cursor_pos():
    pt = POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y

# Import audio worker from separate file to ensure clean process state
from audio_service import run_audio_service

# --- Logging Setup ---
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(threadName)s] %(levelname)s: %(message)s',
    handlers=[
        logging.FileHandler("recorder_debug.log", mode='w'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Recorder")

# --- MoviePy Import ---
try:
    from moviepy import VideoFileClip, AudioFileClip, CompositeAudioClip
    from proglog import ProgressBarLogger
    logger.info("MoviePy imported successfully.")
except ImportError:
    try:
        from moviepy.editor import VideoFileClip, AudioFileClip, CompositeAudioClip
        from proglog import ProgressBarLogger
        logger.info("MoviePy (legacy) imported successfully.")
    except ImportError:
        logger.error("MoviePy not found. Merging will fail.")
        VideoFileClip = None
        ProgressBarLogger = object # Dummy class to prevent crashes if import fails

# --- Custom Logger for Progress Bar ---
class QtProgressBarLogger(ProgressBarLogger):
    def __init__(self, signal):
        super().__init__()
        self.signal = signal
    
    def bars_callback(self, bar, attr, value, old_value=None):
        # 'bar' is usually 't' for time (rendering video)
        # 'value' is current frame/time, 'total' is total frames/duration
        if attr == 'total':
             # We might need to store totals if we want percentage
             pass
        else:
            # attr is 'index' (current value) or 'total'
            # For simple percentage:
            percentage = (value / self.bars[bar]['total']) * 100
            self.signal.emit(int(percentage))

# --- Worker Threads ---

class VideoRecorderThread(QThread):
    """Background thread for recording video frames."""
    error_occurred = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.running = False
        self.writer = None
        self.fps = 30
        self.include_camera = False
        self.frame_desktop = None
        self.frame_camera = None
        self.width = 1920
        self.height = 1080
        self.filepath = ""
        
    def setup(self, filepath, fps, width, height):
        self.fps = fps
        self.width = width
        self.height = height
        self.filepath = filepath
        return True
        
    def run(self):
        logger.info(f"Starting Video Thread. Target: {self.filepath}")
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        try:
            self.writer = cv2.VideoWriter(self.filepath, fourcc, self.fps, (self.width, self.height))
            if not self.writer.isOpened():
                msg = f"Error: Could not open video writer for {self.filepath}"
                logger.error(msg)
                self.error_occurred.emit(msg)
                return
        except Exception as e:
            msg = f"VideoWriter Init Error: {e}"
            logger.error(msg)
            self.error_occurred.emit(msg)
            return

        frame_duration = 1.0 / self.fps
        while self.running:
            frame_start = time.time()
            try:
                if self.frame_desktop is not None:
                    # Use a copy to avoid race conditions if possible, though simple assignment is atomic-ish in Python
                    frame = self.frame_desktop.copy()
                    
                    if frame.shape[0] != self.height or frame.shape[1] != self.width:
                        frame = cv2.resize(frame, (self.width, self.height))
                    
                    # Overlay Camera
                    if self.include_camera and self.frame_camera is not None:
                        cam_h, cam_w = self.frame_camera.shape[:2]
                        # Check bounds
                        if cam_h < self.height and cam_w < self.width:
                            # Resize Camera for PIP (Small)
                            # You can adjust these values to change the camera size
                            # TARGET_CAM_WIDTH = 160 
                            # TARGET_CAM_HEIGHT = 120
                            TARGET_CAM_WIDTH = 320 
                            TARGET_CAM_HEIGHT = 240
                            
                            try:
                                self.frame_camera = cv2.resize(self.frame_camera, (TARGET_CAM_WIDTH, TARGET_CAM_HEIGHT))
                                cam_h, cam_w = self.frame_camera.shape[:2]
                            except Exception:
                                pass # Keep original if resize fails

                            y_start = self.height - cam_h - 20
                            y_end = self.height - 20
                            x_start = self.width - cam_w - 20
                            x_end = self.width - 20
                            
                            # Ensure coordinates are valid
                            if y_start >= 0 and x_start >= 0:
                                frame[y_start:y_end, x_start:x_end] = self.frame_camera
                    
                    # Write Frame
                    frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    self.writer.write(frame_bgr)
            except Exception as e:
                logger.error(f"Video write loop error: {e}")
            
            # Maintain FPS
            elapsed = time.time() - frame_start
            sleep_time = frame_duration - elapsed
            if sleep_time > 0:
                time.sleep(sleep_time)
        
        if self.writer is not None:
            self.writer.release()
            self.writer = None
        logger.info("Video Thread Stopped")
    
    def stop(self):
        self.running = False
        self.wait()


class CameraPreviewThread(QThread):
    """Handles camera capture in a separate thread to prevent UI blocking."""
    frame_ready = pyqtSignal(object)
    camera_error = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.running = False
        self.cap = None

    def run(self):
        logger.info("Camera thread starting...")
        try:
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW) # CAP_DSHOW is faster on Windows
            if not self.cap.isOpened():
                # Fallback
                self.cap = cv2.VideoCapture(0)
            
            if not self.cap.isOpened():
                logger.error("Could not open camera")
                self.camera_error.emit("Could not open camera")
                return

            self.running = True
            logger.info("Camera opened successfully")

            while self.running:
                ret, frame = self.cap.read()
                if ret:
                    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    self.frame_ready.emit(frame_rgb)
                else:
                    time.sleep(0.1)
                time.sleep(0.03) # Approx 30 FPS for preview
                
        except Exception as e:
            logger.error(f"Camera thread error: {e}")
            self.camera_error.emit(str(e))
        finally:
            if self.cap and self.cap.isOpened():
                self.cap.release()
            logger.info("Camera thread finished")

    def stop(self):
        self.running = False
        self.wait()


class DesktopPreviewThread(QThread):
    """Handles screen capture for preview in a separate thread."""
    frame_ready = pyqtSignal(object)

    def __init__(self, monitor_index=0):
        super().__init__()
        self.running = False
        self.monitor_index = monitor_index

    def set_monitor(self, index):
        self.monitor_index = index

    def run(self):
        logger.info(f"Desktop preview thread starting for monitor {self.monitor_index}")
        # Create mss instance inside the thread to be safe
        with mss.mss() as sct:
            self.running = True
            while self.running:
                try:
                    if self.monitor_index < len(sct.monitors):
                        monitor = sct.monitors[self.monitor_index]
                        # Ensure even dimensions
                        w = monitor['width']
                        h = monitor['height']
                        if w % 2 != 0: w -= 1
                        if h % 2 != 0: h -= 1
                        
                        # Create a rect that MSS accepts
                        rect = {'top': monitor['top'], 'left': monitor['left'], 'width': w, 'height': h}
                        
                        img = np.array(sct.grab(rect))
                        
                        # --- Draw Cursor ---
                        try:
                            cur_x, cur_y = get_cursor_pos()
                            rel_x = cur_x - monitor['left']
                            rel_y = cur_y - monitor['top']
                            
                            # Check if cursor is within this monitor
                            if 0 <= rel_x < w and 0 <= rel_y < h:
                                # Simple Arrow Cursor
                                # Tip at (rel_x, rel_y)
                                pts = np.array([
                                    [rel_x, rel_y], 
                                    [rel_x, rel_y + 16], 
                                    [rel_x + 5, rel_y + 11], 
                                    [rel_x + 11, rel_y + 16], # Tail
                                    [rel_x + 13, rel_y + 14], 
                                    [rel_x + 7, rel_y + 9], 
                                    [rel_x + 11, rel_y + 9]
                                ], np.int32)
                                pts = pts.reshape((-1, 1, 2))
                                
                                # Fill White (BGRA)
                                cv2.fillPoly(img, [pts], (255, 255, 255, 255))
                                # Outline Black
                                cv2.polylines(img, [pts], True, (0, 0, 0, 255), 1)
                        except Exception as e:
                            # logger.debug(f"Cursor draw error: {e}")
                            pass
                        # -------------------

                        frame_rgb = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
                        self.frame_ready.emit(frame_rgb)
                    time.sleep(0.05) # Cap preview FPS to ~20 to save resources
                except Exception as e:
                    logger.error(f"Desktop preview error: {e}")
                    time.sleep(1)
        logger.info("Desktop preview thread finished")

    def stop(self):
        self.running = False
        self.wait()


class ProcessingThread(QThread):
    """Handles the merging process in the background."""
    finished = pyqtSignal(str) # Emits final path
    error = pyqtSignal(str)
    progress = pyqtSignal(int)

    def __init__(self, video_path, audio_sys_path, audio_mic_path, output_path):
        super().__init__()
        self.video_path = video_path
        self.audio_sys_path = audio_sys_path
        self.audio_mic_path = audio_mic_path
        self.output_path = output_path

    def run(self):
        logger.info("Starting merge process...")
        clips_to_close = []
        final_clip = None
        
        # Setup progress logger
        progress_logger = QtProgressBarLogger(self.progress)

        try:
            # Allow file handles to close
            time.sleep(1.0)
            
            if not os.path.exists(self.video_path):
                raise Exception("Video file not found!")

            video_clip = VideoFileClip(self.video_path)
            clips_to_close.append(video_clip)
            
            audio_tracks = []
            
            # Load System Audio
            if os.path.exists(self.audio_sys_path):
                logger.info(f"Found system audio file: {self.audio_sys_path}")
                try:
                    sys_audio = AudioFileClip(self.audio_sys_path)
                    # Normalize or adjust volume if needed
                    clips_to_close.append(sys_audio)
                    audio_tracks.append(sys_audio)
                except Exception as e: logger.warning(f"Sys audio load error: {e}")
            else:
                logger.warning(f"System audio file NOT found: {self.audio_sys_path}")
                
            # Load Mic Audio
            if os.path.exists(self.audio_mic_path):
                logger.info(f"Found mic audio file: {self.audio_mic_path}")
                try:
                    mic_audio = AudioFileClip(self.audio_mic_path)
                    
                    # Boost Mic Volume (User reported missing mic audio)
                    try:
                        # Try MoviePy v1 style
                        mic_audio = mic_audio.volumex(1.5)
                    except AttributeError:
                        # Try MoviePy v2 style
                        try: mic_audio = mic_audio.multiply_volume(1.5)
                        except: pass # standard volume
                    
                    clips_to_close.append(mic_audio)
                    audio_tracks.append(mic_audio)
                except Exception as e: logger.warning(f"Mic audio load error: {e}")
            else:
                logger.warning(f"Mic audio file NOT found: {self.audio_mic_path}")
            
            # Combine
            if audio_tracks:
                logger.info(f"Merging {len(audio_tracks)} audio tracks")
                duration = video_clip.duration
                final_audio_tracks = []
                
                # Ensure all tracks are valid and trimmed to video length
                for track in audio_tracks:
                    if track.duration > duration:
                        try: track = track.subclipped(0, duration)
                        except: track = track.subclip(0, duration)
                    final_audio_tracks.append(track)
                    
                if len(final_audio_tracks) > 1:
                    # "Equally merged" - CompositeAudioClip mixes them
                    mixed_audio = CompositeAudioClip(final_audio_tracks)
                else:
                    mixed_audio = final_audio_tracks[0]
                
                # Explicitly set audio to the video clip
                try: 
                    final_clip = video_clip.with_audio(mixed_audio)
                except AttributeError: 
                    final_clip = video_clip.set_audio(mixed_audio)
                    
                logger.info(f"Audio duration: {mixed_audio.duration}, Video duration: {video_clip.duration}")
            else:
                final_clip = video_clip
            
            clips_to_close.append(final_clip)
            
            logger.info(f"Writing final video to {self.output_path}")
            # Check MoviePy version compatibility for verbose argument
            try:
                final_clip.write_videofile(
                    self.output_path, 
                    codec='libx264', 
                    audio_codec='aac', 
                    verbose=False, 
                    logger=progress_logger
                )
            except TypeError:
                # Fallback for newer MoviePy versions
                logger.info("Retrying write_videofile without verbose argument...")
                final_clip.write_videofile(
                    self.output_path, 
                    codec='libx264', 
                    audio_codec='aac',
                    logger=progress_logger
                )
            self.finished.emit(self.output_path)
            
        except Exception as e:
            logger.error(f"Merge failed: {traceback.format_exc()}")
            # Fallback copy if merge fails
            if os.path.exists(self.video_path) and not os.path.exists(self.output_path):
                shutil.copy2(self.video_path, self.output_path)
            self.error.emit(str(e))
            
        finally:
            for clip in clips_to_close:
                try: clip.close()
                except: pass
            
            # Cleanup temps
            time.sleep(0.5)
            # Delete temp files
            for f in [self.video_path, self.audio_sys_path, self.audio_mic_path]:
                try:
                    if os.path.exists(f): 
                        os.remove(f)
                        logger.info(f"Deleted temp file: {f}")
                except Exception as e:
                    logger.warning(f"Could not delete temp file {f}: {e}")


# --- Main UI ---

class Ui_Dialog(object):
    def setupUi(self, Dialog):
        self.dialog = Dialog
        Dialog.setObjectName("Dialog")
        Dialog.resize(650, 450)

        self.tabWidget = QtWidgets.QTabWidget(Dialog)
        self.tabWidget.setGeometry(QtCore.QRect(10, 10, 630, 430))
        self.tabWidget.setObjectName("tabWidget")
        
        # --- Tab 1: Desktop Recording ---
        self.tab = QtWidgets.QWidget()
        self.tab.setObjectName("tab")
        
        self.label_desktop_preview = QtWidgets.QLabel(self.tab)
        self.label_desktop_preview.setGeometry(QtCore.QRect(10, 10, 300, 180))
        self.label_desktop_preview.setStyleSheet("border: 1px solid gray; background: black;")
        self.label_desktop_preview.setScaledContents(True)
        
        self.label_camera_preview = QtWidgets.QLabel(self.tab)
        self.label_camera_preview.setGeometry(QtCore.QRect(320, 10, 300, 180))
        self.label_camera_preview.setStyleSheet("border: 1px solid gray; background: black;")
        self.label_camera_preview.setScaledContents(True)

        self.checkBox_camera = QtWidgets.QCheckBox(self.tab)
        self.checkBox_camera.setGeometry(QtCore.QRect(10, 210, 491, 23))
        font = QtGui.QFont()
        font.setPointSize(10)
        self.checkBox_camera.setFont(font)
        self.checkBox_camera.setObjectName("checkBox_camera")
        
        self.label_monitor = QtWidgets.QLabel(self.tab)
        self.label_monitor.setGeometry(QtCore.QRect(10, 240, 80, 25))
        self.label_monitor.setText("Monitor:")
        self.label_monitor.setFont(font)
        
        self.monitorComboBox = QtWidgets.QComboBox(self.tab)
        self.monitorComboBox.setGeometry(QtCore.QRect(100, 240, 500, 25))

        self.label_delay = QtWidgets.QLabel(self.tab)
        self.label_delay.setGeometry(QtCore.QRect(10, 280, 251, 21))
        self.label_delay.setFont(font)
        
        self.spinBox_delay = QtWidgets.QSpinBox(self.tab)
        self.spinBox_delay.setGeometry(QtCore.QRect(270, 280, 49, 26))
        self.spinBox_delay.setMaximum(180)
        
        self.label_seconds = QtWidgets.QLabel(self.tab)
        self.label_seconds.setGeometry(QtCore.QRect(330, 280, 71, 21))
        self.label_seconds.setFont(font)

        self.btn_record = QtWidgets.QPushButton(self.tab)
        self.btn_record.setGeometry(QtCore.QRect(480, 280, 140, 40))
        self.btn_record.setStyleSheet('QPushButton {background-color: #A3C1DA; color: green; font-weight: bold;}')
        self.btn_record.setFont(font)
        self.btn_record.setCursor(QtGui.QCursor(QtCore.Qt.OpenHandCursor))

        # Progress Bar (Initially Hidden)
        self.progressBar = QtWidgets.QProgressBar(self.tab)
        self.progressBar.setGeometry(QtCore.QRect(480, 330, 140, 20))
        self.progressBar.setProperty("value", 0)
        self.progressBar.setTextVisible(True)
        self.progressBar.hide()

        self.tabWidget.addTab(self.tab, "")
        
        # --- Tab 2: Settings ---
        self.tab_2 = QtWidgets.QWidget()
        self.tab_2.setObjectName("tab_2")
        
        self.checkBox_sys_audio = QtWidgets.QCheckBox(self.tab_2)
        self.checkBox_sys_audio.setGeometry(QtCore.QRect(10, 70, 321, 31))
        self.checkBox_sys_audio.setFont(font)
        
        self.checkBox_mic_audio = QtWidgets.QCheckBox(self.tab_2)
        self.checkBox_mic_audio.setGeometry(QtCore.QRect(10, 120, 351, 23))
        self.checkBox_mic_audio.setFont(font)
        
        self.label_fps = QtWidgets.QLabel(self.tab_2)
        self.label_fps.setGeometry(QtCore.QRect(13, 30, 61, 31))
        self.label_fps.setFont(font)
        
        self.spinBox_fps = QtWidgets.QSpinBox(self.tab_2)
        self.spinBox_fps.setGeometry(QtCore.QRect(60, 30, 49, 26))
        self.spinBox_fps.setMinimum(15)
        self.spinBox_fps.setMaximum(60)
        self.spinBox_fps.setSingleStep(15)
        self.spinBox_fps.setValue(30)
        
        self.btn_change_dir = QtWidgets.QPushButton(self.tab_2)
        self.btn_change_dir.setGeometry(QtCore.QRect(10, 170, 241, 31))
        self.btn_change_dir.setFont(font)
        
        self.label_save_path = QtWidgets.QLabel(self.tab_2)
        self.label_save_path.setGeometry(QtCore.QRect(10, 210, 600, 60))
        self.label_save_path.setWordWrap(True)
        self.label_save_path.setStyleSheet("color: #666;")
        self.label_save_path.setText(f"Save location: {os.getcwd()}")
        
        self.label_credits = QtWidgets.QLabel(self.tab_2)
        self.label_credits.setGeometry(QtCore.QRect(10, 350, 500, 21))
        self.label_credits.setText("Credits: Ahmet Furkan DEMIR | 2025 Update: Meet Bhanushali")
        
        self.tabWidget.addTab(self.tab_2, "")

        self.retranslateUi(Dialog)
        self.tabWidget.setCurrentIndex(0)
        QtCore.QMetaObject.connectSlotsByName(Dialog)
        
        # --- Logic Initialization ---
        self.is_recording = False
        self.folder = ""
        self.sct = mss.mss()
        
        # Workers
        self.desktop_thread = DesktopPreviewThread()
        self.desktop_thread.frame_ready.connect(self.update_desktop_preview_slot)
        
        self.camera_thread = CameraPreviewThread()
        self.camera_thread.frame_ready.connect(self.update_camera_preview_slot)
        self.camera_thread.camera_error.connect(lambda e: logger.error(f"Camera Error: {e}"))
        
        self.recorder_thread = VideoRecorderThread()
        self.audio_processes = []
        self.stop_event = None
        self.processing_thread = None
        
        # Connect UI
        self.btn_record.clicked.connect(self.toggle_recording)
        self.btn_change_dir.clicked.connect(self.change_directory)
        self.monitorComboBox.currentIndexChanged.connect(self.change_monitor)
        self.checkBox_camera.stateChanged.connect(self.toggle_camera_preview)
        
        # Init
        self.init_monitors()
        self.desktop_thread.start()
        
        # Timer for countdown
        self.countdown_timer = QTimer()
        self.countdown_timer.timeout.connect(self.countdown_tick)
        self.countdown_val = 0

    def retranslateUi(self, Dialog):
        _translate = QtCore.QCoreApplication.translate
        Dialog.setWindowTitle(_translate("Dialog", "Recorder App"))
        self.checkBox_camera.setText(_translate("Dialog", "Include camera in recording?"))
        self.label_delay.setText(_translate("Dialog", "Start delay:"))
        self.btn_record.setText(_translate("Dialog", "Start recording"))
        self.label_seconds.setText(_translate("Dialog", "seconds"))
        self.label_desktop_preview.setText(_translate("Dialog", "Desktop Preview"))
        self.label_camera_preview.setText(_translate("Dialog", "Camera Preview (Check box below)"))
        self.tabWidget.setTabText(self.tabWidget.indexOf(self.tab), _translate("Dialog", "Recording"))
        self.checkBox_sys_audio.setText(_translate("Dialog", "Record System Audio"))
        self.checkBox_mic_audio.setText(_translate("Dialog", "Record Microphone"))
        self.label_fps.setText(_translate("Dialog", "FPS"))
        self.btn_change_dir.setText(_translate("Dialog", "Change Save Directory"))
        self.tabWidget.setTabText(self.tabWidget.indexOf(self.tab_2), _translate("Dialog", "Settings"))
        
        self.checkBox_sys_audio.setChecked(True)
        self.checkBox_mic_audio.setChecked(True)

    def init_monitors(self):
        self.monitorComboBox.clear()
        monitors = self.sct.monitors
        for i, mon in enumerate(monitors):
            if i == 0: name = f"All Screens ({mon['width']}x{mon['height']})"
            else: name = f"Monitor {i} ({mon['width']}x{mon['height']})"
            self.monitorComboBox.addItem(name, i)
        
        if len(monitors) > 1:
            self.monitorComboBox.setCurrentIndex(1) # Default to primary monitor if exists
        else:
            self.monitorComboBox.setCurrentIndex(0)

    def change_monitor(self):
        index = self.monitorComboBox.currentData()
        if index is not None:
            self.desktop_thread.set_monitor(index)

    def toggle_camera_preview(self, state):
        if state == Qt.Checked:
            if not self.camera_thread.isRunning():
                self.camera_thread.start()
        else:
            if self.camera_thread.isRunning():
                self.camera_thread.stop()
                self.label_camera_preview.clear()
                self.label_camera_preview.setText("Camera Off")

    # @pyqtSlot(object)
    def update_desktop_preview_slot(self, frame):
        # Pass frame to recorder if recording
        if self.is_recording and self.recorder_thread.isRunning():
            self.recorder_thread.frame_desktop = frame
            
        # Update UI
        try:
            h, w, ch = frame.shape
            bytes_per_line = ch * w
            qimg = QtGui.QImage(frame.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
            pixmap = QtGui.QPixmap.fromImage(qimg)
            self.label_desktop_preview.setPixmap(pixmap.scaled(300, 180, Qt.KeepAspectRatio))
        except Exception as e: pass

    # @pyqtSlot(object)
    def update_camera_preview_slot(self, frame):
        # Pass frame to recorder if recording
        if self.is_recording and self.recorder_thread.isRunning():
            self.recorder_thread.frame_camera = frame
            
        # Update UI
        try:
            h, w, ch = frame.shape
            bytes_per_line = ch * w
            qimg = QtGui.QImage(frame.data, w, h, bytes_per_line, QtGui.QImage.Format_RGB888)
            pixmap = QtGui.QPixmap.fromImage(qimg)
            self.label_camera_preview.setPixmap(pixmap.scaled(300, 180, Qt.KeepAspectRatio))
        except Exception as e: pass

    def change_directory(self):
        folder = str(QFileDialog.getExistingDirectory(None, "Select Directory"))
        if folder:
            self.folder = folder
            self.label_save_path.setText(f"Save location: {folder}")

    def toggle_recording(self):
        if not self.is_recording:
            # Start Countdown
            delay = self.spinBox_delay.value()
            if delay > 0:
                self.countdown_val = delay
                self.btn_record.setEnabled(False)
                self.countdown_timer.start(1000)
                self.btn_record.setText(f"Starting {delay}...")
            else:
                self.start_recording()
        else:
            self.stop_recording()

    def countdown_tick(self):
        self.countdown_val -= 1
        self.btn_record.setText(f"Starting {self.countdown_val}...")
        if self.countdown_val <= 0:
            self.countdown_timer.stop()
            self.start_recording()

    def start_recording(self):
        logger.info("Initiating recording...")
        self.is_recording = True
        self.btn_record.setEnabled(True)
        self.btn_record.setText("STOP RECORDING")
        self.btn_record.setStyleSheet('QPushButton {background-color: #FFCCCC; color: red; font-weight: bold;}')
        self.monitorComboBox.setEnabled(False)
        self.btn_change_dir.setEnabled(False)
        
        # Paths
        now = datetime.datetime.now()
        ts = now.strftime("%Y-%m-%d_%H-%M-%S")
        base = self.folder if self.folder else os.getcwd()
        
        self.temp_vid = os.path.join(base, f"temp_vid_{ts}.mp4")
        self.temp_aud_sys = os.path.join(base, f"temp_aud_sys_{ts}.wav")
        self.temp_aud_mic = os.path.join(base, f"temp_aud_mic_{ts}.wav")
        self.final_out = os.path.join(base, f"Recording_{ts}.mp4")
        
        # Start Audio (Processes)
        self.audio_processes = []
        self.stop_event = multiprocessing.Event()
        self.start_event = multiprocessing.Event() # Sync start
        ready_events = []
        
        if self.checkBox_sys_audio.isChecked():
            ready_evt = multiprocessing.Event()
            p = multiprocessing.Process(
                target=run_audio_service, 
                args=('system', self.temp_aud_sys, self.stop_event, ready_evt, self.start_event)
            )
            p.start()
            self.audio_processes.append(p)
            ready_events.append(ready_evt)
            
        if self.checkBox_mic_audio.isChecked():
            ready_evt = multiprocessing.Event()
            p = multiprocessing.Process(
                target=run_audio_service, 
                args=('mic', self.temp_aud_mic, self.stop_event, ready_evt, self.start_event)
            )
            p.start()
            self.audio_processes.append(p)
            ready_events.append(ready_evt)
            
        # Wait for audio devices to be ready
        if ready_events:
            logger.info(f"Waiting for {len(ready_events)} audio devices to initialize...")
            all_ready = True
            
            # Wait for devices with a timeout
            for evt in ready_events:
                if not evt.wait(timeout=15): # 15s timeout for init
                    all_ready = False
                    break
            
            if not all_ready:
                logger.error("Timeout waiting for audio devices. Aborting recording.")
                
                # Cleanup
                self.stop_event.set()
                for p in self.audio_processes:
                    p.terminate()
                
                self.is_recording = False
                self.btn_record.setText("Start recording")
                self.btn_record.setStyleSheet('QPushButton {background-color: #A3C1DA; color: green; font-weight: bold;}')
                self.monitorComboBox.setEnabled(True)
                self.btn_change_dir.setEnabled(True)
                
                QMessageBox.critical(self.dialog, "Error", "Audio devices failed to initialize in time.\nCheck logs.")
                return
            logger.info("Audio devices ready.")
            
        # Start Video
        monitor_idx = self.monitorComboBox.currentData()
        monitor_bbox = self.sct.monitors[monitor_idx]
        
        self.recorder_thread.setup(
            self.temp_vid,
            self.spinBox_fps.value(),
            monitor_bbox['width'] - (1 if monitor_bbox['width']%2!=0 else 0),
            monitor_bbox['height'] - (1 if monitor_bbox['height']%2!=0 else 0)
        )
        self.recorder_thread.include_camera = self.checkBox_camera.isChecked()
        self.recorder_thread.running = True
        self.recorder_thread.start()
        
        # Signal audio to start NOW
        if self.start_event:
            self.start_event.set()
        
        logger.info("Recording started (Video & Audio Synced).")

    def stop_recording(self):
        logger.info("Stopping recording...")
        self.is_recording = False
        self.btn_record.setEnabled(False)
        self.btn_record.setText("Processing...")
        
        # Show Progress
        self.progressBar.show()
        self.progressBar.setValue(0)
        
        # Stop threads
        self.recorder_thread.stop()
        
        # Stop Audio Processes
        if self.stop_event:
            self.stop_event.set()
            logger.info("Stop event set for audio processes")
        
        # Wait for audio processes to finish saving
        # Give them plenty of time since they need to concatenate and write large audio buffers
        for p in self.audio_processes:
            logger.info(f"Waiting for audio process {p.pid} to finish...")
            p.join(timeout=15)
            if p.is_alive():
                logger.warning(f"Audio process {p.pid} still alive, waiting more...")
                p.join(timeout=10)  # Extra time
                if p.is_alive():
                    logger.warning(f"Audio process {p.pid} hung, forcing termination")
                    p.terminate()
        
        # Additional wait for files to be written to disk
        time.sleep(1.0)
        logger.info(f"Checking for audio files...")
        logger.info(f"  System audio exists: {os.path.exists(self.temp_aud_sys)}")
        logger.info(f"  Mic audio exists: {os.path.exists(self.temp_aud_mic)}")
            
        # Start Processing
        self.processing_thread = ProcessingThread(self.temp_vid, self.temp_aud_sys, self.temp_aud_mic, self.final_out)
        self.processing_thread.finished.connect(self.on_processing_finished)
        self.processing_thread.error.connect(self.on_processing_error)
        self.processing_thread.progress.connect(self.on_processing_progress)
        self.processing_thread.start()

    def on_processing_progress(self, val):
        self.progressBar.setValue(val)

    def on_processing_finished(self, path):
        logger.info("Processing finished.")
        self.progressBar.hide()
        self.btn_record.setEnabled(True)
        self.btn_record.setText("Start recording")
        self.btn_record.setStyleSheet('QPushButton {background-color: #A3C1DA; color: green; font-weight: bold;}')
        self.monitorComboBox.setEnabled(True)
        self.btn_change_dir.setEnabled(True)
        QMessageBox.information(self.dialog, "Success", f"Recording saved:\n{path}")

    def on_processing_error(self, err):
        logger.error(f"Processing error: {err}")
        self.progressBar.hide()
        self.btn_record.setEnabled(True)
        self.btn_record.setText("Start recording")
        self.btn_record.setStyleSheet('QPushButton {background-color: #A3C1DA; color: green; font-weight: bold;}')
        self.monitorComboBox.setEnabled(True)
        self.btn_change_dir.setEnabled(True)
        QMessageBox.warning(self.dialog, "Warning", f"Processing issue:\n{err}\n\nCheck log file.")

    def cleanup(self):
        logger.info("App closing, cleaning up...")
        self.desktop_thread.stop()
        self.camera_thread.stop()
        if self.recorder_thread.isRunning():
            self.recorder_thread.stop()
        
        if self.stop_event:
            self.stop_event.set()
        for p in self.audio_processes:
            p.terminate()

if __name__ == "__main__":
    # Support for PyInstaller/Multiprocessing on Windows
    multiprocessing.freeze_support()
    
    app = QtWidgets.QApplication(sys.argv)
    Dialog = QtWidgets.QDialog()
    ui = Ui_Dialog()
    ui.setupUi(Dialog)
    Dialog.show()
    app.aboutToQuit.connect(ui.cleanup)
    sys.exit(app.exec_())
