# System level imports
import os, io, json, time, tempfile, traceback
from datetime import datetime
from threading import Condition
import threading, subprocess
import argparse
import importlib.util

# Flask imports
from flask import Flask, render_template, request, jsonify, Response, send_file, abort, session, redirect, url_for
import secrets

# picamera2 imports
from picamera2 import Picamera2
from picamera2.encoders import JpegEncoder
from picamera2.encoders import MJPEGEncoder
#from picamera2.encoders import H264Encoder
from picamera2.outputs import FileOutput
from libcamera import Transform

# Image handeling imports
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

# werkzeug imports
from werkzeug.utils import secure_filename

# Plugin hooks registry
global plugin_hooks
plugin_hooks = {
    'after_image_capture': []  # List of callbacks: fn(camera_num, image_path)
}

# Helper to ensure file path is within the intended directory
def is_safe_path(basedir, path):
    return os.path.realpath(path).startswith(os.path.realpath(basedir))

# Print statement header
def print_section(title):
    print(f"\n{'=' * 10} {title} {'=' * 10}")

# Plugin loader function
def load_plugins(app, context=None, plugins_folder='plugins'):
    if not os.path.exists(plugins_folder):
        return
    for filename in os.listdir(plugins_folder):
        if filename.endswith('.py') and not filename.startswith('__'):
            plugin_path = os.path.join(plugins_folder, filename)
            spec = importlib.util.spec_from_file_location(filename[:-3], plugin_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if hasattr(module, 'init_plugin'):
                if context:
                    module.init_plugin(app, context)
                else:
                    module.init_plugin(app)

####################
# Initialize Flask 
####################

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', secrets.token_hex(16))  # Use env var if set, else random
# https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie#samesitesamesite-value
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

####################
# Initialize picamera2 
####################

# Set debug level to Warning
# Removed the debugging due to an issue https://github.com/monkeymademe/CamUI/issues/130 which prevented the server from starting
#Picamera2.set_logging(Picamera2.DEBUG)
# Ask picamera2 for what cameras are connected
global_cameras = Picamera2.global_camera_info()

##### Uncomment the line below if you want to limt the number of cameras connected (change the number to index which camera you want)
# global_cameras = [global_cameras[0]]

##### Uncomment the line below simulate having no cameras connected
# global_cameras = []

print_section("Initialize picamera2")
print(f'\nCameras Found:\n{global_cameras}\n')

####################
# Initialize default values 
####################

version = "2.0.1 - BETA"
project_title = "CamUI - for picamera2"
firmware_control = False

# Get the directory of the current script
current_dir = os.path.dirname(os.path.abspath(__file__))

# Helper function to ensure a directory exists
def ensure_directory(path):
    os.makedirs(path, exist_ok=True)
    return path

# Set and ensure the camera profiles directory
camera_profile_folder = ensure_directory(os.path.join(current_dir, 'static/camera_profiles'))
app.config['camera_profile_folder'] = camera_profile_folder

# Set and ensure the image gallery directory
upload_folder = ensure_directory(os.path.join(current_dir, 'static/gallery'))
app.config['upload_folder'] = upload_folder

# For the image gallery set items per page
items_per_page = 12

# Define the minimum required configuration
minimum_last_config = {
    "cameras": []
}

# Load the camera-module-info.json file
last_config_file_path = os.path.join(current_dir, 'camera-last-config.json')

try:
    with open(os.path.join(current_dir, 'camera-module-info.json'), 'r') as file:
        camera_module_info = json.load(file)
except (FileNotFoundError, json.JSONDecodeError) as e:
    print(f"Error loading camera-module-info.json: {e}")
    camera_module_info = {"camera_modules": []}

# Function to load or initialize configuration
def load_or_initialize_config(file_path, default_config):
    if os.path.exists(file_path):
        with open(file_path, 'r') as file:
            try:
                config = json.load(file)
                if not config:  # Check if the file is empty
                    raise ValueError("Empty configuration file")
            except (json.JSONDecodeError, ValueError):
                # If file is empty or invalid, create new config
                with open(file_path, 'w') as file:
                    json.dump(default_config, file, indent=4)
                config = default_config
    else:
        # Create the file with minimum configuration if it doesn't exist
        with open(file_path, 'w') as file:
            json.dump(default_config, file, indent=4)
        config = default_config
    return config

def list_profiles():
    profiles = []
    if not os.path.exists(camera_profile_folder):
        os.makedirs(camera_profile_folder)
    
    for filename in os.listdir(camera_profile_folder):
        if filename.endswith(".json"):
            filepath = os.path.join(camera_profile_folder, filename)
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                model = data.get("model", "Unknown")
                profiles.append({"filename": filename, "model": model})
            except Exception as e:
                print(f"Error loading {filename}: {e}")
    return profiles

def control_template():
    with open(os.path.join(current_dir, "camera_controls_db.json"), "r") as f:
        settings = json.load(f)
    return settings

# Load or initialize the configuration
camera_last_config = load_or_initialize_config(last_config_file_path, minimum_last_config)

def get_camera_info(camera_model, camera_module_info):
    return next(
        (module for module in camera_module_info["camera_modules"] if module["sensor_model"] == camera_model),
        next(module for module in camera_module_info["camera_modules"] if module["sensor_model"] == "Unknown")
    )

####################
# Streaming Class and function
####################

class StreamingOutput(io.BufferedIOBase):
    def __init__(self):
        self.buffer = io.BytesIO()
        self.condition = Condition()

    def write(self, buf):
        # Clear the buffer before writing the new frame
        self.buffer.seek(0)
        self.buffer.truncate()
        self.buffer.write(buf)
        with self.condition:
            self.condition.notify_all()

    def read_frame(self):
        self.buffer.seek(0)
        return self.buffer.read()

####################
# CameraObject that will store the itteration of 1 or more cameras
####################

class CameraObject:
    def __init__(self, camera):
        self.camera_init = True
        self.camera_info = camera
        # Generate default Camera profile
        self.camera_profile = self.generate_camera_profile()
        # Init camera to picamera2 using the camera number
        self.picam2 = Picamera2(camera['Num'])
        # Get Camera specs
        self.camera_module_spec = self.get_camera_module_spec()
        # Fetch Avaialble Sensor modes and generate available resolutions
        self.sensor_modes = self.picam2.sensor_modes
        self.camera_resolutions = self.generate_camera_resolutions()
        # Ready buffer for feed
        self.output = None
        # Initialize configs as empty dictionaries for the still and video configs
        self.init_configure_camera()
        # Compare camera controls DB flushing out settings not avaialbe from picamera2
        self.live_controls = self.initialize_controls_template(self.picam2.camera_controls)
        # Load saved camaera profile if one exists
        self.load_saved_camera_profile()
        self.camera_init = False
        # Set the Camers sensor mode 
        self.set_sensor_mode(self.camera_profile["sensor_mode"])
        # Set capture flag and set placeholder image
        self.use_placeholder = False
        self.placeholder_frame = self.generate_placeholder_frame()  # Create placeholder
        
        # Start Stream and sync metadata
        self.start_streaming()
        self.update_camera_from_metadata()

        # Final debug statements
        print_section("Available Camera Controls")
        print(f"\n{self.picam2.camera_controls}")
        print_section("Available Resolutions")
        print(f"\n{self.camera_resolutions}")
        print_section("Final Camera Profile")
        print(f"\n{self.camera_profile}")
        

    #-----
    # Camera Config Functions
    #-----

    # Init the camera config setting up still and vidoe configs
    def init_configure_camera(self):
        self.still_config = {}
        self.video_config = {}
        self.still_config = self.picam2.create_still_configuration()
        self.video_config = self.picam2.create_video_configuration()

    # Update camera settings flip and resolution settings
    def update_camera_config_OLD(self):
        # If the system is initializing no need to start stop camera so skip it 
        if not self.camera_init:
            self.picam2.stop()
        self.set_orientation()
        self.picam2.configure(self.still_config)
        self.picam2.configure(self.video_config)
        if not self.camera_init:
            self.picam2.start()

    # Update camera settings flip and resolution settings
    def update_camera_config(self, update=None):
        # Update the camera configuration.
        # Args: update (str, optional): 'video' or 'still' to update a specific config.
        # If None, both still and video configs are updated.
        print_section("Updating camera configuration")
        try:
            if not self.camera_init:
                print("Stopping camera/stream for reconfiguration")
                self.use_placeholder = True
                self.stop_streaming()
                time.sleep(0.1)
                self.picam2.stop()
                time.sleep(0.1)

            # Set flip/mirror orientation
            self.set_orientation()

            if update in (None, "still"):
                print("Applying still configuration")
                self.picam2.configure(self.still_config)

            if update in (None, "video"):
                print("Applying video configuration")
                self.picam2.configure(self.video_config)

            if not self.camera_init:
                print("Starting camera")
                time.sleep(0.1)
                self.picam2.start()
                time.sleep(0.1)
                self.start_streaming()
                self.use_placeholder = False

            print_section("Camera reconfiguration complete")

        except Exception as e:
            print(f"UNHANDLED error during camera config update: {e}")
            import traceback
            traceback.print_exc()

    # set_live_feed_resolution
    def configure_video_config(self):
        if not self.camera_init:
            self.use_placeholder = True
            self.stop_streaming()
            time.sleep(0.1)
            self.picam2.stop()
            time.sleep(0.1)
        self.set_orientation()
        self.picam2.configure(self.video_config)
        if not self.camera_init:    
            time.sleep(0.1)
            self.picam2.start()
            self.start_streaming()
            self.use_placeholder = False

    def load_saved_camera_profile(self):
        #Load the saved camera config if available.
        if self.camera_info.get("Has_Config") and self.camera_info.get("Config_Location"):
            self.load_camera_profile(self.camera_info["Config_Location"])

    def load_camera_profile(self, profile_filename):
        #Load and apply a camera profile from a given filename.
        print_section("Loading Camera Profile")
        profile_path = os.path.join(camera_profile_folder, profile_filename)
        if not os.path.exists(profile_path):
            print(f"\nProfile file not found: {profile_path}")
            return False
        try:
            with open(profile_path, "r") as f:
                profile_data = json.load(f)
            # ✅ Load the profile before applying any settings
            self.camera_profile = profile_data
            # ✅ Apply settings after loading the profile
            self.set_sensor_mode(self.camera_profile.get("sensor_mode", 0))
            self.set_orientation()
            self.update_settings('hflip', self.camera_profile['hflip'])
            self.update_settings('vflip', self.camera_profile['vflip'])
            self.update_settings('saveRAW', self.camera_profile['saveRAW'])
            self.apply_profile_controls()
            self.sync_live_controls()  # Ensure UI updates with the latest settings
            # ✅ Update camera-last-config.json
            try:
                if os.path.exists(last_config_file_path):
                    with open(last_config_file_path, "r") as f:
                        last_config = json.load(f)
                else:
                    last_config = {"cameras": []}
                # Find the matching camera entry
                camera_num = self.camera_info['Num']
                updated = False
                for camera in last_config["cameras"]:
                    if camera["Num"] == camera_num:
                        camera["Has_Config"] = True
                        camera["Config_Location"] = profile_filename
                        updated = True
                        break
                if not updated:
                    print(f"\nCamera {camera_num} not found in camera-last-config.json.")
                with open(last_config_file_path, "w") as f:
                    json.dump(last_config, f, indent=4)
                print(f"\nLoaded profile '{profile_filename}' and updated camera-last-config.json.")
            except Exception as e:
                print(f"\nError updating camera-last-config.json: {e}")
            return True
        except Exception as e:
            print(f"\nError loading camera profile '{profile_filename}': {e}")
            return False

    def generate_camera_profile(self):
        file_name = os.path.join(camera_profile_folder, 'camera-module-info.json')
        # If there is no existing config, or the file doesn't exist, create a default profile
        if not self.camera_info.get("Has_Config", False) or not os.path.exists(file_name):
            self.camera_profile = {
                "hflip": 0,
                "vflip": 0,
                "sensor_mode": 0,
                "live_preview": True,
                "model": self.camera_info.get("Model", "Unknown"),
                "resolutions": {"StillCaptureResolution": 0, "LiveFeedResolution": 0},
                "saveRAW": False,
                "controls": {}
            }
        else:
            # Load existing profile from file
            with open(file_name, 'r') as file:
                self.camera_profile = json.load(file)
        return self.camera_profile
    
    def initialize_controls_template(self, picamera2_controls):
        print_section("Initialize Controls Template")
        with open(os.path.join(current_dir, "camera_controls_db.json"), "r") as f:
            camera_json = json.load(f)
        if "sections" not in camera_json:
            print("\nError: 'sections' key not found in camera_json!")
            return camera_json  # Return unchanged if it's not structured as expected
        # Initialize empty controls in camera_profile
        self.camera_profile["controls"] = {}
        for section in camera_json["sections"]:
            if "settings" not in section:
                print(f"\nWarning: Missing 'settings' key in section: {section.get('title', 'Unknown')}")
                continue        
            section_enabled = False  # Track if any setting is enabled
            for setting in section["settings"]:
                if not isinstance(setting, dict):
                    print(f"\nWarning: Unexpected setting format: {setting}")
                    continue  # Skip if it's not a dictionary
                setting_id = setting.get("id")  # Use `.get()` to avoid crashes
                source = setting.get("source", None)  # Check if source exists
                original_enabled = setting.get("enabled", False)  # Preserve original enabled state
                
                if source == "controls":
                    if setting_id in picamera2_controls:
                        min_val, max_val, default_val = picamera2_controls[setting_id]
                        print(f"Updating {setting_id}: Min={min_val}, Max={max_val}, Default={default_val}")  # Debugging
                        setting["min"] = min_val
                        setting["max"] = max_val
                        if default_val is not None:
                            setting["default"] = default_val
                        else:
                            default_val = False if isinstance(min_val, bool) else min_val                        
                        if setting["enabled"]:
                            self.camera_profile["controls"][setting_id] = default_val
                        setting["enabled"] = original_enabled  
                        if original_enabled:
                            section_enabled = True                 
                    else:
                        print(f"Disabling {setting_id}: Not found in picamera2_controls")  # Debugging
                        setting["enabled"] = False  # Disable setting         
                elif source == "generatedresolutions":
                    resolution_options = [
                        {"value": i, "label": f"{w} x {h}", "enabled": True}
                        for i, (w, h) in enumerate(self.camera_resolutions)
                    ]
                    # Use the dynamically generated resolutions
                    setting["options"] = resolution_options
                    section_enabled = True
                    print(f"Updated {setting_id} with generated resolutions")
                else:
                    print(f"Skipping {setting_id}: No source specified, keeping existing values.")
                    section_enabled = True  
            
                if "childsettings" in setting:
                    for child in setting["childsettings"]:
                        child_id = child.get("id")
                        child_source = child.get("source", None)
                        if child_source == "controls" and child_id in picamera2_controls:
                            min_val, max_val, default_val = picamera2_controls[child_id]
                            print(f"Updating Child Setting {child_id}: Min={min_val}, Max={max_val}, Default={default_val}")  # Debugging
                            child["min"] = min_val
                            child["max"] = max_val
                            self.camera_profile["controls"][child_id] = default_val if default_val is not None else min_val
                            if default_val is not None:
                                child["default"] = default_val  
                            child["enabled"] = child.get("enabled", False)
                            if child["enabled"]:
                                section_enabled = True  
                        else:
                            print(f"Skipping or Disabling Child Setting {child_id}: Not found or no source specified")
            section["enabled"] = section_enabled
        print_section("Initialized camera_profile controls")
        print(f"\n{self.camera_profile}")
        return camera_json

    def update_settings(self, setting_id, setting_value):
        # Handle sensor mode separately
        if setting_id == "sensor_mode":
            def sensor_mode_task():
                try:
                    self.set_sensor_mode(setting_value)
                    self.camera_profile['sensor_mode'] = setting_value
                    print(f"Sensor mode {setting_value} applied")
                except ValueError as e:
                    print(f"⚠️ Error: {e}")

            # Start a thread and block until it completes
            thread = threading.Thread(target=sensor_mode_task)
            thread.start()
            thread.join()
        # Handle hflip and vflip separately
        elif setting_id in ["hflip", "vflip"]:
            try:
                self.camera_profile[setting_id] = bool(int(setting_value))
                self.update_camera_config()
                print(f"Applied transform: {setting_id} -> {setting_value} (Camera restarted)")
            except ValueError as e:
                print(f"⚠️ Error: {e}")
        elif setting_id in ["StillCaptureResolution", "LiveFeedResolution"]:
            try:
                self.camera_profile['resolutions'][setting_id] = int(setting_value)
                if setting_id == 'StillCaptureResolution':
                    self.still_config = self.picam2.create_video_configuration(main={"size": self.camera_resolutions[int(setting_value)]})
                    self.update_camera_config()
                    self.camera_profile['resolutions'][setting_id] = int(setting_value)

                if setting_id == 'LiveFeedResolution':
                    self.set_live_feed_resolution(setting_value)

                print(f"Applied transform: {setting_id} -> {setting_value} (Camera restarted)")
            except ValueError as e:
                print(f"⚠️ Error: {e}")
        elif setting_id == "saveRAW":
            try:
                self.camera_profile[setting_id] = setting_value
                print(f"Applied transform: {setting_id} -> {setting_value}")
            except ValueError as e:
                print(f"⚠️ Error: {e}")
        else:
            # Convert setting_value to correct type
            if "." in str(setting_value):
                setting_value = float(setting_value)
            else:
                setting_value = int(setting_value)
            # Apply the setting
            self.picam2.set_controls({setting_id: setting_value})
            # Store in camera_profile["controls"]
            self.camera_profile.setdefault("controls", {})[setting_id] = setting_value
        # Update live settings
        updated = False
        for section in self.live_controls.get("sections", []):
            for setting in section.get("settings", []):
                if setting["id"] == setting_id:
                    setting["value"] = setting_value  # Update main setting
                    updated = True
                    break
                # Check child settings
                for child in setting.get("childsettings", []):
                    if child["id"] == setting_id:
                        child["value"] = setting_value  # Update child setting
                        updated = True
                        break
            if updated:
                break  # Exit loop once found
        if not updated:
            print(f"⚠️ Warning: Setting {setting_id} not found in live_controls!")
        return setting_value  # Returning for confirmation

    def sync_live_controls(self):
        # Updates self.live_controls to match self.camera_profile without resetting defaults.
        for section in self.live_controls.get("sections", []):
            for setting in section.get("settings", []):
                setting_id = setting["id"]
                if setting_id in self.camera_profile["controls"]:
                    setting["value"] = self.camera_profile["controls"][setting_id]
                # Sync child settings
                for child in setting.get("childsettings", []):
                    child_id = child["id"]
                    if child_id in self.camera_profile["controls"]:
                        child["value"] = self.camera_profile["controls"][child_id]

    def apply_profile_controls(self):
        if "controls" in self.camera_profile:
            try:
                for setting_id, setting_value in self.camera_profile["controls"].items():
                    self.picam2.set_controls({setting_id: setting_value})
                    self.update_settings(setting_id, setting_value)  # ✅ Use the loop variables
                    print(f"Applied Control: {setting_id} -> {setting_value}")
                print("✅ All profile controls applied successfully")
            except Exception as e:
                print(f"⚠️ Error applying profile controls: {e}")
    
    def set_orientation(self):
        # Get current transform settings
        transform = Transform()
        # Apply hflip and vflip from camera_profile
        transform.hflip = self.camera_profile.get("hflip", False)
        transform.vflip = self.camera_profile.get("vflip", False)
        # Update both video and still configs
        self.still_config['transform'] = transform
        self.video_config['transform'] = transform
        print("Applied Orientation - hflip:", transform.hflip, "vflip:", transform.vflip)
    
    def set_sensor_mode(self, mode_index):
        try:
            # Ensure setting_value is an integer (mode index)
            mode_index = int(mode_index)
            if mode_index < 0 or mode_index >= len(self.sensor_modes):
                raise ValueError("Invalid sensor mode index")
            
            mode = self.sensor_modes[mode_index]
            print(f"\nSensor mode selected for Camera {self.camera_info['Num']}: \n\n{mode}\n")
            
            # Check if this is a high-resolution mode
            width, height = mode['size']
            if width * height > 2304 * 1296:  # If resolution is higher than 1080p
                print(f"⚠️ High resolution mode detected: {width}x{height}")
                # Add extra delay for high-res modes
                time.sleep(0.5)
            
            # Stop the camera if it's running
            if not self.camera_init:
                self.use_placeholder = True
                self.stop_streaming()
                self.picam2.stop()
                time.sleep(0.2)  # Increased delay for high-res modes
            
            self.camera_profile["sensor_mode"] = mode_index  
            
            # Print the mode for debugging
            print(f"\nSensor mode selected for Camera {self.camera_info['Num']}: \n\n{mode}\n")
            
            # Set still and video configs
            self.still_config = self.picam2.create_still_configuration(
                sensor={'output_size': mode['size'], 'bit_depth': mode['bit_depth']}
            )
            print(self.camera_resolutions[int(self.camera_profile['resolutions']['LiveFeedResolution'])])
            self.video_config = self.picam2.create_video_configuration(
                main={"size": self.camera_resolutions[int(self.camera_profile['resolutions']['LiveFeedResolution'])]}, 
                sensor={'output_size': mode['size'], 'bit_depth': mode['bit_depth']},
            )
            
            # Configure the camera
            try:
                print(f"Attempting to configure sensor mode: {mode}")
                self.picam2.configure(self.video_config)
                print("Camera configuration successful")
            except Exception as e:
                print(f"⚠️ Error configuring sensor mode: {e}")
                print(f"Error type: {type(e)}")
                print(f"Error details: {str(e)}")
                traceback.print_exc()
                raise
            
            # Restart the camera if it was running
            if not self.camera_init:
                try:
                    self.picam2.start()
                    self.start_streaming()
                    time.sleep(0.2)  # Increased delay for high-res modes
                    self.use_placeholder = False
                except Exception as e:
                    print(f"⚠️ Error restarting camera: {e}")
                    self.use_placeholder = True
                    raise
                
            print(f"✅ Sensor mode {mode_index} applied")
            
        except Exception as e:
            print(f"⚠️ Error: {e}")
            # Try to recover by restarting the camera
            if not self.camera_init:
                try:
                    self.picam2.stop()
                    self.picam2.start()
                    self.start_streaming()
                except Exception as recovery_error:
                    print(f"⚠️ Recovery failed: {recovery_error}")
                    self.use_placeholder = True
            raise ValueError(str(e))

    def set_live_feed_resolution(self, resolution_index):
        # Stop the camera if it's running
        if not self.camera_init:
            self.use_placeholder = True
            self.stop_streaming()
            self.picam2.stop()
            time.sleep(0.2)  # Increased delay for high-res modes
        # Ensure resolution_index is an integer
        resolution_index = int(resolution_index)
        if resolution_index < 0 or resolution_index >= len(self.camera_resolutions):
            raise ValueError("Invalid resolution index")
        
        resolution = self.camera_resolutions[resolution_index]
        print(f"Setting live feed resolution to: {resolution}")

        # Update video config
        self.video_config = self.picam2.create_video_configuration(main={"size": resolution})
        # Apply new configuration
        self.configure_video_config()

        # Restart the camera if it was running
        if not self.camera_init:
            try:
                self.picam2.start()
                self.start_streaming()
                time.sleep(0.2)  # Increased delay for high-res modes
                self.use_placeholder = False
            except Exception as e:
                print(f"⚠️ Error restarting camera: {e}")
                self.use_placeholder = True
                raise

    def update_camera_from_metadata(self):
        metadata = self.capture_metadata()
        if not metadata:
            print("Failed to fetch metadata")
            return
        if "sections" not in self.live_controls:
            print("Error: 'sections' key not found in live_controls!")
            return
        enabled_controls = {}
        # Extract enabled settings (including childsettings)
        for section in self.live_controls["sections"]:
            for setting in section.get("settings", []):
                if setting.get("enabled", False) and setting.get("source") == "controls":
                    enabled_controls[setting["id"]] = True
                # Check and include childsettings
                for child in setting.get("childsettings", []):
                    if child.get("enabled", False) and child.get("source") == "controls":
                        enabled_controls[child["id"]] = True
        # Update only enabled settings from metadata
        for key in enabled_controls:
            if key in metadata:
                self.camera_profile["controls"][key] = metadata[key]
                self.update_settings(key, metadata[key])
                print(f"Updated from metadata - {key}: {metadata[key]}")

    def save_profile(self, filename):
        """Save the current camera profile and update camera-last-config.json."""
        try:
            print(self.camera_profile)
            # Ensure .json is not already in the filename
            if filename.lower().endswith(".json"):
                filename = filename[:-5]
            profile_path = os.path.join(camera_profile_folder, f"{filename}.json")
            # Save the profile
            with open(profile_path, "w") as f:
                json.dump(self.camera_profile, f, indent=4)
            # ✅ Update camera-last-config.json
            try:
                if os.path.exists(last_config_file_path):
                    with open(last_config_file_path, "r") as f:
                        last_config = json.load(f)
                else:
                    last_config = {"cameras": []}  # Create an empty structure if missing
                # Find the camera entry matching the current camera number
                camera_num = self.camera_info["Num"]
                updated = False
                for camera in last_config["cameras"]:
                    if camera["Num"] == camera_num:
                        camera["Has_Config"] = True
                        camera["Config_Location"] = f"{filename}.json"  # Set the new config file
                        updated = True
                        break
                if not updated:
                    print(f"Warning: Camera {camera_num} not found in camera-last-config.json.")
                # Save the updated configuration back
                with open(last_config_file_path, "w") as f:
                    json.dump(last_config, f, indent=4)
                print(f"Updated camera-last-config.json for camera {camera_num} after saving profile.")
            except Exception as e:
                print(f"Error updating camera-last-config.json: {e}")
            return True
        except Exception as e:
            print(f"Error saving profile: {e}")
            return False

    def reset_to_default(self):
        # Resets camera settings to default and applies them.
        self.camera_profile = {
            "hflip": 0,
            "vflip": 0,
            "sensor_mode": 0,
            "live_preview": True,
            "model": self.camera_info.get("Model", "Unknown"),
            "resolutions": {"StillCaptureResolution": 0, "LiveFeedResolution": 0},
            "saveRAW": False,
            "controls": {}  # Empty controls to be updated later
        }
        # Reset key settings
        self.set_sensor_mode(self.camera_profile["sensor_mode"])
        self.set_orientation()
        # Reinitialize UI settings
        self.live_controls = self.initialize_controls_template(self.picam2.camera_controls)
        self.update_settings("saveRAW", self.camera_profile["saveRAW"])
        print(self.camera_profile["saveRAW"])
        self.update_camera_from_metadata()
        # Apply the default settings using the new function
        self.apply_profile_controls()
        print("Camera profile reset to default and settings applied.")

    #-----
    # Camera Information Functions
    #-----

    def capture_metadata(self):
        self.metadata = self.picam2.capture_metadata()
        #print(f"Metadata: {self.metadata}")
        print(self.picam2.sensor_resolution)
        return self.metadata

    def get_camera_module_spec(self):
        # Find and return the camera module details based on the sensor model.
        camera_module = next((cam for cam in camera_module_info["camera_modules"] if cam["sensor_model"] == self.camera_info["Model"]), None)
        return camera_module

    def get_sensor_mode(self):
        current_config = self.picam2.camera_configuration()
        active_mode = current_config.get('sensor', {})  # Get the currently active sensor settings
        active_mode_index = None  # Default to None if no match is found
        # Find the matching sensor mode index
        for index, mode in enumerate(self.sensor_modes):
            if mode['size'] == active_mode.get('output_size') and mode['bit_depth'] == active_mode.get('bit_depth'):
                active_mode_index = index
                break
        print(f"Active Sensor Mode: {active_mode_index}")
        return active_mode_index

    def generate_camera_resolutions(self):
        """
        Precompute a list of resolutions based on the available sensor modes.
        This list is shared between still capture and live feed resolution settings.
        """
        if not self.sensor_modes:
            print("⚠️ Warning: No sensor modes available!")
            return []

        # Extract sensor mode resolutions
        resolutions = sorted(set(mode['size'] for mode in self.sensor_modes if 'size' in mode), reverse=True)

        if not resolutions:
            print("⚠️ Warning: No valid resolutions found in sensor modes!")
            return []

        max_resolution = resolutions[0]  # Highest resolution
        aspect_ratio = max_resolution[0] / max_resolution[1]

        # Generate midpoint resolutions
        extra_resolutions = []
        for i in range(len(resolutions) - 1):
            w1, h1 = resolutions[i]
            w2, h2 = resolutions[i + 1]
            midpoint = ((w1 + w2) // 2, (h1 + h2) // 2)
            extra_resolutions.append(midpoint)

        # Add two extra smaller resolutions at the end
        last_w, last_h = resolutions[-1]
        half_res = (last_w // 2, last_h // 2)
        inbetween_res = ((last_w + half_res[0]) // 2, (last_h + half_res[1]) // 2)

        resolutions.extend(extra_resolutions)
        resolutions.append(inbetween_res)
        resolutions.append(half_res)

        # Store in camera object for later use
        self.available_resolutions = sorted(set(resolutions), reverse=True)

        return self.available_resolutions

    #-----
    # Camera Streaming Functions
    #-----

    def flush_frames(self, count=5, delay=0.05):
        for _ in range(count):
            try:
                self.picam2.capture_array("main")
                time.sleep(delay)
            except Exception as e:
                print(f"⚠️ Flush error: {e}")
                break

    def generate_stream(self):
        consecutive_timeouts = 0
        max_timeouts = 3  # Maximum number of consecutive timeouts before recovery
        
        while True:
            try:
                if self.use_placeholder:
                    frame = self.placeholder_frame
                else:
                    with self.output.condition:
                        notified = self.output.condition.wait(timeout=5.0)
                        if not notified:
                            print("⚠️ Timed out waiting for frame.")
                            consecutive_timeouts += 1
                            if consecutive_timeouts >= max_timeouts:
                                print("⚠️ Too many consecutive timeouts, attempting stream recovery...")
                                self.safe_restart_stream()
                                consecutive_timeouts = 0
                                continue
                            frame = self.placeholder_frame
                        else:
                            consecutive_timeouts = 0  # Reset counter on successful frame
                            frame = self.output.read_frame()

                if frame is None or not isinstance(frame, bytes):
                    print(f"⚠️ Invalid frame ({type(frame)}), using placeholder.")
                    frame = self.placeholder_frame
                    continue

                # Safely get camera configuration
                try:
                    config = self.picam2.camera_configuration()
                    if config is None:
                        frame = self.placeholder_frame
                        continue
                except Exception as e:
                    print(f"⚠️ Error getting camera config: {e}")
                    frame = self.placeholder_frame
                    continue

                # Check if we need to restart the stream
                try:
                    actual_res = config["main"]["size"]
                    expected_res = self.video_config["main"]["size"]

                    if actual_res != expected_res:
                        print(f"⚠️ Resolution mismatch: {actual_res} ≠ {expected_res}")
                        self.safe_restart_stream()
                        continue
                except Exception as e:
                    print(f"⚠️ Error checking resolution: {e}")
                    frame = self.placeholder_frame
                    continue

                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')

            except Exception as e:
                print(f"🚨 Stream loop error: {e}")
                traceback.print_exc()
                time.sleep(0.1)  # Prevent tight loop on error
                continue

    def safe_restart_stream(self):
        try:
            print("🔄 Restarting stream with correct config...")
            self.use_placeholder = True
            self.stop_streaming()
            self.picam2.stop()
            time.sleep(0.2)
            self.picam2.start(self.video_config, show_preview=False)
            self.start_streaming()
            self.flush_frames()
            self.use_placeholder = False
            print("✅ Stream restarted and flushed.")
        except Exception as e:
            print(f"🚨 Failed to restart stream: {e}")
            traceback.print_exc()
            self.use_placeholder = True  # Keep using placeholder if restart fails

    def generate_placeholder_frame(self):
        mode_index = int(self.camera_profile["sensor_mode"])
        if mode_index < 0 or mode_index >= len(self.sensor_modes):
            raise ValueError("Invalid sensor mode index")
        mode = self.sensor_modes[mode_index]
        img = Image.new('RGB', mode['size'], (33, 37, 41))  # Match video feed size THIS NEEDS WORK FOR THE SCALER CROP
        draw = ImageDraw.Draw(img)
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        return buf.getvalue()

    def start_streaming(self):
        self.output = StreamingOutput()
        self.picam2.start_recording(JpegEncoder(), output=FileOutput(self.output))
        print_section("Streaming started")
        time.sleep(1)

    def stop_streaming(self):
        if self.output:  # Ensure streaming was started before stopping
            self.picam2.stop_recording()
            print_section("Streaming stopped")

    #-----
    # Camera Capture Functions
    #-----

    def take_still(self, camera_num, image_name):
        try:
            self.use_placeholder = True  # Start sending placeholder frames
            time.sleep(0.5)  # Short delay to allow clients to receive the placeholder
            self.stop_streaming()
            filepath = os.path.join(app.config['upload_folder'], image_name)
            # This will be the new way to save images at max quality just need to make the save as DNG setting available
            buffers, metadata = self.picam2.switch_mode_and_capture_buffers(self.still_config, ["main", "raw"])
            self.picam2.helpers.save(self.picam2.helpers.make_image(buffers[0], self.still_config["main"]), metadata, f"{filepath}.jpg")
            if self.camera_profile["saveRAW"]:
                self.picam2.helpers.save_dng(buffers[1], metadata, self.still_config["raw"], f"{filepath}.dng")
            
            # Switch to still mode and capture the image
            #self.picam2.switch_mode_and_capture_file(self.still_config, f"{filepath}.jpg")
            print(f"Image captured successfully. Path: {filepath}")
            # Call after_image_capture hooks
            for hook in plugin_hooks.get('after_image_capture', []):
                try:
                    hook(camera_num, f"{filepath}.jpg")
                except Exception as e:
                    print(f"Error in after_image_capture hook: {e}")
            # Restart video mode
            self.start_streaming()
            print("Applied video config:", self.picam2.camera_configuration())
             
            self.use_placeholder = False
            return f'{filepath}.jpg'
        except Exception as e:
            print(f"Error capturing image: {e}")
            return None

    def take_still_from_feed(self, camera_num, image_name):
        try:
            filepath = os.path.join(app.config['upload_folder'], image_name)
            request = self.picam2.capture_request()
            request.save("main", f'{filepath}.jpg')
            print(f"Image captured successfully. Path: {filepath}")
            return f'{filepath}.jpg'
        except Exception as e:
            print(f"Error capturing image: {e}")
            return None


####################
# GPIO Class
####################

class GPIO:
    def __init__(self, config_path="gpio_map.json"):
        self.config_path = config_path
        self.gpio_pins = self.load_config()

    def load_config(self):
        try:
            with open(self.config_path, "r") as f:
                data = json.load(f)
                
                # Ensure we get a list, not an object
                if not isinstance(data, dict) or "gpio_template" not in data:
                    raise ValueError("Invalid JSON structure: Missing 'gpio_template' key.")

                gpio_template = data["gpio_template"]

                if not isinstance(gpio_template, list) or not all(isinstance(item, dict) for item in gpio_template):
                    raise ValueError("GPIO config must be a list of dictionaries.")

                return gpio_template

        except (json.JSONDecodeError, FileNotFoundError, ValueError) as e:
            print(f"Error loading GPIO config: {e}")
            return []

    def get_gpio_pins(self):
        """Return GPIO configuration as a list of dictionaries."""
        return self.gpio_pins

####################
# ImageGallery Class
####################

class ImageGallery:
    def __init__(self, upload_folder, items_per_page=10):
        self.upload_folder = upload_folder
        self.items_per_page = items_per_page
        self.items_per_page = 12

    def get_image_files(self):
         # Fetch image file details, including timestamps, resolution, and DNG presence.
        try:
            image_files = [f for f in os.listdir(self.upload_folder) if f.endswith('.jpg')]
            files_and_timestamps = []

            for image_file in image_files:
                # Extract timestamp from filename
                try:
                    unix_timestamp = int(image_file.split('_')[-1].split('.')[0])
                    timestamp = datetime.utcfromtimestamp(unix_timestamp).strftime('%Y-%m-%d %H:%M:%S')
                except ValueError:
                    print(f"Skipping file {image_file} due to incorrect timestamp format")
                    continue  # Skip files with incorrect format

                # Check if corresponding .dng file exists
                dng_file = os.path.splitext(image_file)[0] + '.dng'
                has_dng = os.path.exists(os.path.join(self.upload_folder, dng_file))

                # Get image resolution
                img_path = os.path.join(self.upload_folder, image_file)
                with Image.open(img_path) as img:
                    width, height = img.size

                # Append file details
                files_and_timestamps.append({
                    'filename': image_file,
                    'timestamp': timestamp,
                    'has_dng': has_dng,
                    'dng_file': dng_file,
                    'width': width,
                    'height': height
                })

            # Sort files by timestamp (newest first)
            files_and_timestamps.sort(key=lambda x: x['timestamp'], reverse=True)
            return files_and_timestamps

        except Exception as e:
            print(f"Error loading image files: {e}")
            return []

    def paginate_images(self, page):
        """Paginate images dynamically after an image is deleted."""
        all_images = self.get_image_files()
        
        # Recalculate total pages dynamically
        total_pages = max((len(all_images) + self.items_per_page - 1) // self.items_per_page, 1)

        # Adjust the current page if necessary
        if page > total_pages:
            page = total_pages  # Ensure we're not on a non-existent page

        start_index = (page - 1) * self.items_per_page
        end_index = start_index + self.items_per_page
        paginated_images = all_images[start_index:end_index]

        return paginated_images, total_pages
    

    def find_last_image_taken(self):
        """Find the most recent image taken."""
        all_images = self.get_image_files()
        
        if all_images:
            first_image = all_images[0]
            print(f"Filename: {first_image['filename']}")
            image = first_image['filename']
        else:
            print("No image files found.")
            image = None
        
        return image  # Extract only the filename
    
    def delete_image(self, filename):
        image_path = os.path.join(self.upload_folder, filename)

        if os.path.exists(image_path):
            try:
                
                os.remove(image_path)
                print(f"Deleted image: {filename}")
                # Check if corresponding .dng file exists
                dng_file = os.path.splitext(filename)[0] + '.dng'
                print(dng_file)
                has_dng = os.path.exists(os.path.join(self.upload_folder, dng_file))
                print(has_dng)
                if has_dng:
                    os.remove(os.path.join(self.upload_folder, dng_file))
                return True, f"Image '{filename}' deleted successfully."
            except Exception as e:
                print(f"Error deleting image {filename}: {e}")
                return False, "Failed to delete image"
        else:
            return False, "Image not found"
    
    def save_edit(self, filename, edits, save_option, new_filename=None):
        """Apply edits to an image and save it based on user selection."""
        image_path = os.path.join(self.upload_folder, filename)
        print(f"Applying edits to {filename}: {edits}")

        if not os.path.exists(image_path):
            return False, "Original image not found."

        try:
            with Image.open(image_path) as img:
                img = img.convert("RGB")  # Ensure no transparency issues

                # Reset EXIF rotation before applying new rotation
                img = ImageOps.exif_transpose(img)

                # Convert brightness and contrast from 0-200 range to 0.1-2.0
                if "brightness" in edits:
                    brightness_factor = max(0.1, float(edits["brightness"]) / 100)
                    enhancer = ImageEnhance.Brightness(img)
                    img = enhancer.enhance(brightness_factor)

                if "contrast" in edits:
                    contrast_factor = max(0.1, float(edits["contrast"]) / 100)
                    enhancer = ImageEnhance.Contrast(img)
                    img = enhancer.enhance(contrast_factor)

                # Apply absolute rotation (mod 360 to prevent stacking errors)
                if "rotation" in edits:
                    rotation_angle = int(edits["rotation"]) % 360
                    # Automatically convert the rotation to negative
                    rotation_angle = -rotation_angle
                    img = img.rotate(rotation_angle, expand=True)
                    print(f"Applied rotation: {rotation_angle}°")

                # Determine save path
                if save_option == "replace":
                    save_path = image_path
                elif save_option == "new_file" and new_filename:
                    save_path = os.path.join(self.upload_folder, new_filename)
                else:
                    return False, "Invalid save option."

                img.save(save_path)
                return True, "Image saved successfully."

        except Exception as e:
            print(f"Error applying edits to image {filename}: {e}")
            return False, "Failed to edit image."


####################
# Cycle through Cameras to create connected camera config
####################

# Template for a new config which will be the new camera-last-config
currently_connected_cameras = {'cameras': []}
# Iterate over each camera in the global_cameras list building a config model
for connected_camera in global_cameras:   
    # Check if the connected camera is a Raspberry Pi Camera Module
    matching_module = next(
        (module for module in camera_module_info["camera_modules"] 
         if module["sensor_model"] == connected_camera["Model"]), 
        None
    )
    if matching_module and matching_module.get("is_pi_cam", False) is True:
        print(f"Connected camera model '{connected_camera['Model']}' is found in the camera-module-info.json and is a Pi Camera.\n")
        is_pi_cam = True
    else:
        print(f"Connected camera model '{connected_camera['Model']}' is either NOT in the camera-module-info.json or is NOT a Pi Camera.\n")
        is_pi_cam = False
    # Build usable Connected Camera Information variable
    camera_info = {'Num':connected_camera['Num'], 'Model':connected_camera['Model'], 'Is_Pi_Cam': is_pi_cam, 'Has_Config': False, 'Config_Location': f"default_{connected_camera['Model']}.json"}
    currently_connected_cameras['cameras'].append(camera_info)

# Create a lookup for existing cameras by "Num"
existing_cameras_lookup = {cam["Num"]: cam for cam in camera_last_config["cameras"]}
# Prepare the updated list of cameras
updated_cameras = []

# Compare config generated from global_cameras with what was last connected and update the camera-last-config
for new_cam in currently_connected_cameras["cameras"]:
    cam_num = new_cam["Num"]
    if cam_num in existing_cameras_lookup:
        old_cam = existing_cameras_lookup[cam_num]  
        # If the camera model has changed, update it
        if old_cam["Model"] != new_cam["Model"]:
            print(f"Updating camera {new_cam['Model']}: Model or Pi Cam status changed.")
            updated_cameras.append(new_cam)
        else:
            # Keep existing config if nothing changed
            updated_cameras.append(old_cam)
    else:
        # If it's a new camera, add it to the list
        print(f"New camera added to config: {new_cam}")
        updated_cameras.append(new_cam)

# Save the updated configuration
new_config = {"cameras": updated_cameras}
with open(os.path.join(current_dir, 'camera-last-config.json'), "w") as file:
    json.dump(new_config, file, indent=4)

# Make sure currently_connected_cameras is the definitively list of connected cameras
currently_connected_cameras = updated_cameras

print(f"\n\n{currently_connected_cameras}\n\n ")



####################
# Cycle through connected cameras and generate camera object
####################

cameras = {}

for connected_camera in currently_connected_cameras:
    camera_obj = CameraObject(connected_camera)
    cameras[connected_camera['Num']] = camera_obj
    print(f"\n\n{cameras}\n\n ")

for key, camera in cameras.items():
    print(f"Key: {key}, Camera: {camera.camera_info}")


####################
# WebUI routes 
####################

@app.context_processor
def inject_theme():
    theme = session.get('theme', 'light')  # Default to 'light'
    return dict(version=version, title=project_title, theme=theme)

@app.context_processor
def inject_camera_list():
    camera_list = [(camera.camera_info, get_camera_info(camera.camera_info['Model'], camera_module_info)) 
                   for key, camera in cameras.items()]
    return dict(camera_list=camera_list, navbar=True)

@app.route('/set_theme/<theme>')
def set_theme(theme):
    session['theme'] = theme
    return jsonify(success=True, ok=True, message="Theme updated successfully")

# Define 'home' route
@app.route('/')
def home():
    camera_list = [(camera.camera_info, get_camera_info(camera.camera_info['Model'], camera_module_info)) for key, camera in cameras.items()]
    return render_template('home.html', active_page='home')

@app.route('/camera_info_<int:camera_num>')
def camera_info(camera_num):
    # Check if the camera number exists
    camera = cameras.get(camera_num)
    if not camera:
        return render_template('error.html', message="Error: Camera not found"), 404
    # Get camera module spec
    camera_module_spec = camera.camera_module_spec

    return render_template('camera_info.html', camera_data=camera_module_spec, camera_num=camera_num)

@app.route("/about")
def about():
    return render_template("about.html", active_page='about')

@app.route('/system_settings')
def system_settings():
    # Load camera module info
    print(camera_module_info)
    return render_template('system_settings.html', firmware_control=firmware_control, camera_modules=camera_module_info.get("camera_modules", []))

@app.route('/set_camera_config', methods=['POST'])
def set_camera_config():
    data = request.get_json()
    sensor_model = data.get('sensor_model')
    config_path = "/boot/firmware/config.txt"

    try:
        with open(config_path, "r") as f:
            lines = f.readlines()

        new_lines = []
        modified = False
        found_anchor = False
        i = 0

        while i < len(lines):
            line = lines[i]

            if "# Automatically load overlays for detected cameras" in line:
                found_anchor = True
                new_lines.append(line)
                i += 1

                # Look for camera_auto_detect line
                if i < len(lines) and lines[i].strip().startswith("camera_auto_detect="):
                    # Replace with 0
                    new_lines.append("camera_auto_detect=0\n")
                    i += 1
                else:
                    # Add camera_auto_detect=0 if missing
                    new_lines.append("camera_auto_detect=0\n")

                # Check for dtoverlay line
                if i < len(lines) and lines[i].strip().startswith("dtoverlay="):
                    # Replace this one line only
                    new_lines.append(f"dtoverlay={sensor_model}\n")
                    i += 1
                else:
                    # Insert dtoverlay line
                    new_lines.append(f"dtoverlay={sensor_model}\n")

                modified = True
                continue

            new_lines.append(line)
            i += 1

        if not found_anchor:
            return jsonify({"message": "Anchor section not found in config.txt"}), 400

        # Write to temp file
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.writelines(new_lines)
            tmp_path = tmp.name

        # Move into place with sudo
        result = subprocess.run(["sudo", "mv", tmp_path, config_path], capture_output=True)

        if result.returncode != 0:
            return jsonify({"message": f"Error writing config: {result.stderr.decode()}"}), 500

        return jsonify({"message": f"Camera '{sensor_model}' set in boot config!"})

    except Exception as e:
        return jsonify({"message": f"Error: {str(e)}"}), 500

@app.route('/reset_camera_detection', methods=['POST'])
def reset_camera_detection():
    config_path = "/boot/firmware/config.txt"
    try:
        with open(config_path, 'r') as file:
            lines = file.readlines()

        new_lines = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.strip() == "camera_auto_detect=0":
                new_lines.append("camera_auto_detect=1\n")
                # Check if the next line is a dtoverlay
                if i + 1 < len(lines) and lines[i + 1].strip().startswith("dtoverlay="):
                    i += 2  # skip both lines
                    continue
                else:
                    i += 1
                    continue
            else:
                new_lines.append(line)
                i += 1

        # Write to temp file
        with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
            tmp.writelines(new_lines)
            tmp_path = tmp.name

        # Move into place with sudo
        result = subprocess.run(["sudo", "mv", tmp_path, config_path], capture_output=True)
       
        if result.returncode != 0:
            return jsonify({"message": f"Error writing config: {result.stderr.decode()}"}), 500

        return jsonify({"message": f"Camera detection reset to automatic."})

    except Exception as e:
        return jsonify({"message": f"Error: {str(e)}"}), 500

@app.route('/shutdown', methods=['POST'])
def shutdown():
    try:
        subprocess.run(['sudo', 'shutdown', '-h', 'now'], check=True)
        return jsonify({"message": "System is shutting down."})
    except subprocess.CalledProcessError as e:
        return jsonify({"message": f"Error: {e}"}), 500

@app.route('/restart', methods=['POST'])
def restart():
    try:
        subprocess.run(['sudo', 'reboot'], check=True)
        return jsonify({"message": "System is restarting."})
    except subprocess.CalledProcessError as e:
        return jsonify({"message": f"Error: {e}"}), 500

####################
# Camera Control routes 
####################

@app.route("/camera_mobile_<int:camera_num>")
def camera_mobile(camera_num):
    try:
        camera = cameras.get(camera_num)
        if not camera:
            return render_template('camera_not_found.html', camera_num=camera_num)
        # Get camera settings
        live_controls = camera.live_controls
        print(live_controls)
        sensor_modes = camera.sensor_modes
        active_mode_index = camera.get_sensor_mode()
        # Find the last image taken by this specific camera
        last_image = None
        last_image = image_gallery_manager.find_last_image_taken()
        return render_template('camera_mobile.html', camera=camera.camera_info, settings=live_controls, sensor_modes=sensor_modes, active_mode_index=active_mode_index, last_image=last_image, profiles=list_profiles(),navbar=False, theme='dark', mode="mobile") 
    except Exception as e:
        print(f"Error loading camera view: {e}")
        return render_template('error.html', error=str(e))

@app.route("/camera_<int:camera_num>")
def camera(camera_num):
    try:
        camera = cameras.get(camera_num)
        if not camera:
            return render_template('camera_not_found.html', camera_num=camera_num)
        # Get camera settings
        live_controls = camera.live_controls
        sensor_modes = camera.sensor_modes
        active_mode_index = camera.get_sensor_mode()
        # Find the last image taken by this specific camera
        last_image = None
        last_image = image_gallery_manager.find_last_image_taken()
        return render_template('camera.html', camera=camera.camera_info, settings=live_controls, sensor_modes=sensor_modes, active_mode_index=active_mode_index, last_image=last_image, profiles=list_profiles(), mode="desktop")
    except Exception as e:
        print(f"Error loading camera view: {e}")
        return render_template('error.html', error=str(e))

# Dictionary to track the last capture time per camera
last_capture_time = {}

@app.route("/capture_still_<int:camera_num>", methods=["POST"])
def capture_still(camera_num):
    global last_capture_time

    try:
        print(f"📸 Received capture request for camera {camera_num}")

        camera = cameras.get(camera_num)
        if not camera:
            print(f"❌ Camera {camera_num} not found.")
            return jsonify(success=False, message="Camera not found"), 404

        # Rate limit: Prevent captures happening too quickly (2 seconds per camera)
        current_time = time.time()
        #if camera_num in last_capture_time and (current_time - last_capture_time[camera_num]) < 2:
        #   print(f"⚠️ Capture request too fast for camera {camera_num}. Ignoring request.")
        #   return jsonify(success=False, message="Capture request too fast"), 429  # Too Many Requests

        # Update the last capture time for this camera
        last_capture_time[camera_num] = current_time

        # Generate the new filename
        timestamp = int(time.time())  # Current Unix timestamp
        image_filename = f"pimage_camera_{camera_num}_{timestamp}"
        print(f"📁 New image filename: {image_filename}")

        # Capture and save the new image
        image_path = camera.take_still(camera_num, image_filename)

        # Add a slight delay to prevent overlapping captures
        time.sleep(0.5)

        if image_path:
            print(f"✅ Image captured successfully: {image_filename}")
            return jsonify(success=True, message="Image captured successfully", image=image_filename)
        else:
            print(f"❌ Failed to capture image for camera {camera_num}")
            return jsonify(success=False, message="Failed to capture image")

    except Exception as e:
        print(f"🔥 Error capturing still image: {e}")
        return jsonify(success=False, message=str(e)), 500
    
@app.route('/snapshot_<int:camera_num>')
def snapshot(camera_num):
    camera = cameras.get(camera_num)
    if camera:
        image_name = f"snapshot_{camera_num}"
        filepath = camera.take_still_from_feed(camera_num, image_name)
        
        if filepath:
            time.sleep(1)  # Ensure the image is saved
            return send_file(filepath, as_attachment=False, download_name="snapshot.jpg", mimetype='image/jpeg')
    else:
        abort(404)

@app.route('/video_feed_<int:camera_num>')
def video_feed(camera_num):
    camera = cameras.get(camera_num)
    if camera:
        return Response(camera.generate_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')
    else:
        abort(404)

@app.route("/toggle_video_feed", methods=["POST"])
def toggle_video_feed():
    data = request.json
    enable = data.get("enable", False)
    camera_num = data.get("camera_num")

    if camera_num is None:
        return jsonify({"success": False, "error": "Invalid camera number"}), 400

    camera_num = int(camera_num)

    if camera_num in cameras:
        if enable:
            cameras[camera_num].start_streaming()
        else:
            cameras[camera_num].stop_streaming()
        return jsonify({"success": True})
    
    return jsonify({"success": False, "error": "Camera not found"}), 404

@app.route('/preview_<int:camera_num>', methods=['POST'])
def preview(camera_num):
    try:
        camera = cameras.get(camera_num)
        if camera:
            filepath = f'snapshot/pimage_preview_{camera_num}'
            preview_path = camera.take_still(camera_num, filepath)
            return jsonify(success=True, message="Photo captured successfully", image_path=preview_path)
    except Exception as e:
        return jsonify(success=False, message=str(e))

@app.route('/update_setting', methods=['POST'])
def update_setting():
    try:
        data = request.json  # Get JSON data from the request
        camera_num = data.get("camera_num")  # New field for camera selection
        setting_id = data.get("id")
        new_value = data.get("value")
        # Debugging: Print the received values
        print(f"Received update for Camera {camera_num}: {setting_id} -> {new_value}")
        camera = cameras.get(camera_num)
        camera.update_settings(setting_id, new_value)
        # ✅ At this stage, we're just verifying the data. No changes to the camera yet.
        return jsonify({
            "success": True,
            "message": f"Received setting update for Camera {camera_num}: {setting_id} -> {new_value}"
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/camera_controls')
def redirect_to_home():
    return redirect(url_for('home'))

@app.route("/set_sensor_mode", methods=["POST"])
def set_sensor_mode():
    data = request.get_json()
    camera_num = data.get("camera_num") 
    camera = cameras.get(camera_num)
    sensor_mode = data.get("sensor_mode")

    if sensor_mode is None:
        return jsonify({"status": "error", "message": "No sensor mode provided"}), 400

    try:
        previous_mode = camera.get_sensor_mode()  # Store previous mode
        camera.set_sensor_mode(sensor_mode)  # Blocks until done
        camera.camera_profile["sensor_mode"] = sensor_mode
        return jsonify({"status": "done", "new_mode": sensor_mode})  
    except ValueError as e:
        print(f"⚠️ Error: {e}")
        return jsonify({
            "status": "error", 
            "message": str(e), 
            "previous_mode": previous_mode  # Send back the previous mode
        }), 400

####################
# Camera Profile routes 
####################

@app.route("/get_camera_profile", methods=["GET"])
def get_camera_profile():
    camera_num = request.args.get("camera_num", type=int)
    camera = cameras.get(camera_num)
    camera_profile = camera.camera_profile  # Fetch current controls
    return jsonify(success=True, camera_profile=camera_profile)

@app.route('/save_profile_<int:camera_num>', methods=['POST'])
def save_profile(camera_num):
    data = request.json
    filename = data.get("filename")

    if not filename:
        return jsonify({"error": "Filename is required"}), 400
    camera = cameras.get(camera_num)
    success = camera.save_profile(filename)

    if success:
        return jsonify({"message": f"Profile '{filename}' saved successfully"}), 200
    else:
        return jsonify({"error": "Failed to save profile"}), 500

@app.route("/reset_profile_<int:camera_num>", methods=["POST"])
def reset_profile(camera_num):
    if camera_num not in cameras:
        return jsonify({"success": False, "message": "Camera not found"}), 404

    camera = cameras[camera_num]
    camera.reset_to_default()
    return jsonify({"success": True, "message": "Profile reset to default"})

@app.route("/fetch_metadata_<int:camera_num>")
def fetch_metadata(camera_num):
    if camera_num not in cameras:
        return jsonify({"error": "Invalid camera number"}), 400
    camera = cameras[camera_num]
    metadata = camera.capture_metadata()  # Get metadata for the selected camera
    print(f"Camera {camera_num} Metadata: {metadata}")  # Log metadata
    return jsonify(metadata)  # Return as JSON

@app.route("/load_profile", methods=["POST"])
def load_profile():
    data = request.get_json()
    profile_name = data.get("profile_name")
    camera_num = data.get("camera_num")

    if not profile_name:
        return jsonify({"success": False, "error": "Profile name is missing"}), 400
    if camera_num is None:
        return jsonify({"success": False, "error": "Camera number is missing"}), 400

    if camera_num in cameras:
        success = cameras[camera_num].load_camera_profile(profile_name)
        if success:
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to load profile"}), 500
    else:
        return jsonify({"success": False, "error": "Invalid camera number"}), 400
    
@app.route("/get_profiles")
def get_profiles():
    return list_profiles()

####################
# GPIO routes 
####################

# Initialize the gallery with the upload folder
gpio = GPIO()

@app.route("/gpio_setup")
def gpio_setup():
    gpio_pins = gpio.get_gpio_pins()
    print(gpio_pins)
    return render_template("gpio_setup.html", gpio_pins = gpio.get_gpio_pins())

####################
# Image gallery routes 
####################

# Initialize the gallery with the upload folder
image_gallery_manager = ImageGallery(upload_folder)

@app.route('/image_gallery')
def image_gallery():
    page = request.args.get('page', 1, type=int)
    images, total_pages = image_gallery_manager.paginate_images(page)
    cameras_data = [(camera_num, camera) for camera_num, camera in cameras.items()]
    if not images:
        return render_template('no_files.html')
    # Define pagination bounds
    start_page = max(1, page - 2)  # Show previous 2 pages
    end_page = min(total_pages, page + 2)  # Show next 2 pages
    return render_template(
        'image_gallery.html',
        image_files=images,
        page=page,
        total_pages=total_pages,
        start_page=start_page,
        end_page=end_page,
        cameras_data=cameras_data,
        active_page='image_gallery'
    )

@app.route('/get_image_for_page')
def get_image_for_page():
    page = request.args.get('page', 1, type=int)
    images, total_pages = image_gallery_manager.paginate_images(page)
    cameras_data = [(camera_num, camera) for camera_num, camera in cameras.items()]
    if not images:
        return render_template('no_files.html')
    # Define pagination bounds
    start_page = max(1, page - 2)  # Show previous 2 pages
    end_page = min(total_pages, page + 2)  # Show next 2 pages
    response = {
        
        'image_files': images,
        'page': page,
        'total_pages': total_pages,
        'start_page': start_page,
        'end_page': end_page
    }
    return jsonify(response)
    
@app.route('/view_image/<filename>')
def view_image(filename):
    safe_filename = secure_filename(filename)
    image_path = os.path.join(app.config['upload_folder'], safe_filename)
    if not os.path.isfile(image_path) or not is_safe_path(app.config['upload_folder'], image_path):
        abort(404)
    return render_template('view_image.html', filename=safe_filename)

@app.route('/delete_image/<filename>', methods=['DELETE'])
def delete_image(filename):
    safe_filename = secure_filename(filename)
    image_path = os.path.join(app.config['upload_folder'], safe_filename)
    if not os.path.isfile(image_path) or not is_safe_path(app.config['upload_folder'], image_path):
        return jsonify({"success": False, "message": "Image not found"}), 404
    success, message = image_gallery_manager.delete_image(safe_filename)
    if success:
        return jsonify({"success": True, "message": message}), 200
    else:
        return jsonify({"success": False, "message": message}), 500

@app.route('/image_edit/<filename>')
def edit_image(filename):
    return render_template('image_edit.html', filename=filename)

@app.route("/apply_filters", methods=["POST"])
def apply_filters():
    filename = request.form["filename"]
    brightness = float(request.form["brightness"])
    contrast = float(request.form["contrast"])
    rotation = float(request.form["rotation"])

    img_path = os.path.join(app.config['upload_folder'], filename)
    img = Image.open(img_path)

    # Apply transformations
    img = img.rotate(-rotation, expand=True)
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(brightness)
    enhancer = ImageEnhance.Contrast(img)
    img = enhancer.enhance(contrast)

    edited_filename = f"edited_{filename}"
    edited_path = os.path.join(app.config['upload_folder'], edited_filename)
    img.save(edited_path)

    return edited_path

@app.route('/download_image/<filename>', methods=['GET'])
def download_image(filename):
    safe_filename = secure_filename(filename)
    image_path = os.path.join(app.config['upload_folder'], safe_filename)
    if not os.path.isfile(image_path) or not is_safe_path(app.config['upload_folder'], image_path):
        abort(404)
    try:
        return send_file(image_path, as_attachment=True)
    except Exception as e:
        print(f"\nError downloading image:\n{e}\n")
        abort(500)

@app.route('/save_edit', methods=['POST'])
def save_edit():
    try:
        data = request.json
        filename = data.get('filename')
        edits = data.get('edits', {})
        save_option = data.get('saveOption')
        new_filename = data.get('newFilename')

        success, message = image_gallery_manager.save_edit(filename, edits, save_option, new_filename)

        return jsonify({'success': success, 'message': message})

    except Exception as e:
        print(f"Error in save_edit route: {e}")
        return jsonify({'success': False, 'message': 'Error saving edit'}), 500


####################
# Misc Routes
####################

@app.route('/beta')
def beta():
    return render_template('beta.html')

@app.after_request
def add_header(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

####################
# Start Flask 
####################

if __name__ == "__main__":
    # Parse any argument passed from command line
    parser = argparse.ArgumentParser(description='PiCamera2 WebUI')
    parser.add_argument('--port', type=int, default=8080, help='Port number to run the web server on')
    parser.add_argument('--ip', type=str, default='0.0.0.0', help='IP to which the web server is bound to')
    args = parser.parse_args()
    context = {'cameras': cameras, 'plugin_hooks': plugin_hooks}
    load_plugins(app, context)
    app.run(host=args.ip, port=args.port)

@app.errorhandler(404)
def not_found_error(error):
    return render_template('error.html', message="Page not found."), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template('error.html', message="An internal error occurred."), 500