import http
import json
import os
import ssl
import stat
import time
import urllib.request

# write-only file that the driver reads (but never writes) to get user-specified control flags
CONTROL_FLAGS_FILE_PATH = '/dev/shm/xr_driver_control'

# read-only file that the driver writes (but never reads) to with its current state
DRIVER_STATE_FILE_PATH = '/dev/shm/xr_driver_state'

CONTROL_FLAGS = [
    'recenter_screen', 
    'recalibrate', 
    'calibrate_magnet',
    'disable_magnet',
    'sbs_mode', 
    'enable_breezy_desktop_smooth_follow',
    'toggle_breezy_desktop_smooth_follow',
    'breezy_desktop_display_distance',
    'breezy_desktop_follow_threshold',
    'force_quit',
    'request_features'
]
SBS_MODE_VALUES = ['unset', 'enable', 'disable']
BASE_EXTERNAL_MODES = ['none']
VR_LITE_OUTPUT_MODES = ['mouse', 'joystick']

def parse_boolean(value, default):
    if not value:
        return default

    return value.lower() == 'true'


def parse_int(value, default):
    return int(value) if value.isdigit() else default

def parse_float(value, default):
    try:
        return float(value)
    except ValueError:
        return default

def parse_string(value, default):
    return value if value else default

def parse_array(value, default):
    return value.split(",") if value else default

def parse_json_string(value, default):
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


CONFIG_PARSER_INDEX = 0
CONFIG_DEFAULT_VALUE_INDEX = 1
CONFIG_ENTRIES = {
    'disabled': [parse_boolean, True],
    'gamescope_reshade_wayland_disabled': [parse_boolean, False],
    'output_mode': [parse_string, 'mouse'],
    'external_mode': [parse_array, ['none']],
    'vr_lite_invert_x': [parse_boolean, False],
    'vr_lite_invert_y': [parse_boolean, False],
    'mouse_sensitivity': [parse_int, 30],
    'display_zoom': [parse_float, 1.0],
    'look_ahead': [parse_int, 0],
    'sbs_display_size': [parse_float, 1.0],
    'sbs_display_distance': [parse_float, 1.0],
    'sbs_content': [parse_boolean, False],
    'sbs_mode_stretched': [parse_boolean, True],
    'sideview_position': [parse_string, 'center'],
    'sideview_display_size': [parse_float, 1.0],
    'virtual_display_smooth_follow_enabled': [parse_boolean, False],
    'sideview_smooth_follow_enabled': [parse_boolean, False],
    'sideview_follow_threshold': [parse_float, 0.5],
    'curved_display': [parse_boolean, False],
    'multi_tap_enabled': [parse_boolean, False],
    'smooth_follow_track_roll': [parse_boolean, False],
    'smooth_follow_track_pitch': [parse_boolean, True],
    'smooth_follow_track_yaw': [parse_boolean, True],
    'neck_saver_horizontal_multiplier': [parse_float, 1.0],
    'neck_saver_vertical_multiplier': [parse_float, 1.0],
    'opentrack_app_ip': [parse_string, '127.0.0.1'],
    'opentrack_app_port': [parse_int, 4242],
    'opentrack_listener_enabled': [parse_boolean, False],
    'opentrack_listen_ip': [parse_string, '0.0.0.0'],
    'opentrack_listen_port': [parse_int, 4242],
    'debug': [parse_array, []],
}

STATE_ENTRIES = {
    'heartbeat': [parse_int, 0],
    'hardware_id': [parse_string, None],
    'connected_device_brand': [parse_string, None],
    'connected_device_model': [parse_string, None],
    'magnet_supported': [parse_boolean, False],
    'magnet_calibration_type': [parse_string, 'UNSUPPORTED'],
    'using_magnet': [parse_boolean, False],
    'magnet_stale': [parse_boolean, False],
    'magnet_calibrating': [parse_boolean, False],
    'gyro_calibrating': [parse_boolean, False],
    'accel_calibrating': [parse_boolean, False],
    'sbs_mode_enabled': [parse_boolean, False],
    'sbs_mode_supported': [parse_boolean, False],
    'firmware_update_recommended': [parse_boolean, False],
    'breezy_desktop_smooth_follow_enabled': [parse_boolean, False],
    'is_gamescope_reshade_ipc_connected': [parse_boolean, False],
}

class Logger:
    def info(self, message):
        print(message)

    def error(self, message):
        print(message)

class XRDriverIPC:
    _instance = None

    @staticmethod
    def set_instance(ipc):
        XRDriverIPC._instance = ipc

    @staticmethod
    def get_instance():
        if not XRDriverIPC._instance:
            XRDriverIPC._instance = XRDriverIPC()

        return XRDriverIPC._instance

    def __init__(self, logger=Logger(), config_home=None, supported_output_modes=[]):
        self.breezy_installed = False
        self.breezy_installing = False
        if not config_home:
            config_home = os.path.join(os.path.expanduser("~"), ".config")
        self.config_file_path = os.path.join(config_home, "xr_driver", "config.ini")
        self.supported_output_modes = supported_output_modes + BASE_EXTERNAL_MODES
        self.logger = logger
        self.request_context = ssl._create_unverified_context()

    def retrieve_config(self, include_ui_view = True):
        config = {}
        for key, value in CONFIG_ENTRIES.items():
            config[key] = value[CONFIG_DEFAULT_VALUE_INDEX]

        try:
            with open(self.config_file_path, 'r') as f:
                for line in f:
                    try:
                        if not line.strip():
                            continue

                        key, value = line.strip().split('=')
                        if key in CONFIG_ENTRIES:
                            parser = CONFIG_ENTRIES[key][CONFIG_PARSER_INDEX]
                            default_val = CONFIG_ENTRIES[key][CONFIG_DEFAULT_VALUE_INDEX]
                            config[key] = parser(value, default_val)
                    except Exception as e:
                        self.logger.error(f"Error parsing line {line}: {e}")
        except FileNotFoundError as e:
            pass

        if include_ui_view: config['ui_view'] = self.build_config_ui_view(config)

        return config

    def write_config(self, config):
        try:
            output = ""

            # remove the UI's "view" data, translate back to config values, and merge them in
            view = config.pop('ui_view', None)
            if view:
                # Retrieve the previous configs to preserve any external modes not specifically supported by the app using this library.
                old_config = self.retrieve_config()

                config.update(self.headset_mode_to_config(view.get('headset_mode'), view.get('is_joystick_mode'), old_config.get('external_mode')))

            if len(config['external_mode']) == 0:
                config['external_mode'].append("none")

            for key, value in config.items():
                if key != "updated":
                    if isinstance(value, bool):
                        output += f'{key}={str(value).lower()}\n'
                    elif isinstance(value, int):
                        output += f'{key}={value}\n'
                    elif isinstance(value, list):
                        output += f'{key}={",".join(value)}\n'
                    else:
                        output += f'{key}={value}\n'

            temp_file = "temp.txt"

            # Write to a temporary file
            with open(temp_file, 'w') as f:
                f.write(output)

            # Atomically replace the old config file with the new one
            os.replace(temp_file, self.config_file_path)
            os.chmod(self.config_file_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH)

            config['ui_view'] = self.build_config_ui_view(config)

            return config
        except Exception as e:
            self.logger.error(f"Error writing config {e}")
            raise e

    # like a SQL "view," these are computed values that are commonly used in the UI
    def build_config_ui_view(self, config):
        view = {}
        view['headset_mode'] = self.config_to_headset_mode(config)
        view['is_joystick_mode'] = config.get('output_mode') == 'joystick'
        return view

    def filter_to_other_external_modes(self, external_modes):
        return [mode for mode in external_modes if mode not in self.supported_output_modes]

    def headset_mode_to_config(self, headset_mode, joystick_mode, old_external_modes):
        new_external_modes = self.filter_to_other_external_modes(old_external_modes)

        config = {}
        if headset_mode in self.supported_output_modes:
            # TODO - uncomment this when the driver can support multiple external_mode values
            # new_external_modes.append(headset_mode)
            new_external_modes = [headset_mode]
            config['output_mode'] = "external_only"
            config['disabled'] = False
        elif headset_mode == "vr_lite":
            config['output_mode'] = "joystick" if joystick_mode else "mouse"
            config['disabled'] = False
        elif len(new_external_modes) == 0:
            config["disabled"] = True
        else:
            config['output_mode'] = "external_only"

        config['external_mode'] = new_external_modes

        return config

    def config_to_headset_mode(self, config):
        if not config or config['disabled']:
            return "disabled"

        if config['output_mode'] in VR_LITE_OUTPUT_MODES:
            return "vr_lite"

        supported_mode = next((mode for mode in self.supported_output_modes if mode in config['external_mode']), None)
        if supported_mode and supported_mode != "none":
            return supported_mode

        return "disabled"

    def write_control_flags(self, control_flags):
        try:
            output = ""
            for key, value in control_flags.items():
                if key in CONTROL_FLAGS:
                    if key == 'sbs_mode':
                        if value not in SBS_MODE_VALUES:
                            self.logger.error(f"Invalid value {value} for sbs_mode flag")
                            continue
                    elif key == 'request_features':
                        if not isinstance(value, list):
                            self.logger.error(f"Invalid value {value} for request_features flag, expected list")
                            continue
                        value = ",".join(value)
                    output += f'{key}={str(value).lower()}\n'

            fd = os.open(CONTROL_FLAGS_FILE_PATH, os.O_WRONLY | os.O_CREAT, 0o777)
            with os.fdopen(fd, 'w') as f:
                f.write(output)
        except Exception as e:
            self.logger.error(f"Error writing control flags {e}")

    def build_state_ui_view(self, state):
        ui_view = {
            'driver_running': state['heartbeat'] != 0 and (time.time() - state['heartbeat']) < 5
        }

        return ui_view

    def retrieve_driver_state(self):
        state = {}
        
        for key, value in STATE_ENTRIES.items():
            state[key] = value[CONFIG_DEFAULT_VALUE_INDEX]

        try:
            with open(DRIVER_STATE_FILE_PATH, 'r') as f:
                for line in f:
                    try:
                        if not line.strip():
                            continue

                        key, value = line.strip().split('=')
                        if key in STATE_ENTRIES:
                            parser = STATE_ENTRIES[key][CONFIG_PARSER_INDEX]
                            default_val = STATE_ENTRIES[key][CONFIG_DEFAULT_VALUE_INDEX]
                            state[key] = parser(value, default_val)
                    except Exception as e:
                        self.logger.error(f"Error parsing line {line}: {e}")
        except FileNotFoundError as e:
            pass

        state['ui_view'] = self.build_state_ui_view(state)

        # state is stale, just send the ui_view
        if not state['ui_view']['driver_running']:
            return {
                'heartbeat': state['heartbeat'],
                'hardware_id': state['hardware_id'],
                'ui_view': state['ui_view']
            }
        
        return state
