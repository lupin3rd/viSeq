import dearpygui.dearpygui as dpg
import threading
import time
import json
import io
import queue
import numpy as np
import random
import sounddevice as sd
import essentia
import essentia.standard as es
from pythonosc import udp_client
from pythonosc import dispatcher
from pythonosc import osc_server
from PIL import Image

# --- HARD CAPS ON NETWORK-FED DATA (viOSC replies) ---
# Bound memory use and block PIL decompression bombs (audit MED-6).
Image.MAX_IMAGE_PIXELS = 25_000_000            # PIL's hard ceiling (~25 MP)
MAX_THUMBNAIL_PIXELS = 3_000_000              # explicit cap; real thumbs are ~58k px
MAX_THUMBNAIL_BLOB_BYTES = 8 * 1024 * 1024    # per-blob cap
MAX_STATE_JSON_BYTES = 1 * 1024 * 1024        # per-replydata cap

# --- OSC CONFIGURATION ---
# viseq talks exclusively to viOSC: /vimix/* messages are forwarded by viOSC
# to Vimix (port 7000), replies come back on viOSC's output port 6667.
VIOSC_IP = "127.0.0.1"
VIOSC_PORT = 6666
osc_client = udp_client.SimpleUDPClient(VIOSC_IP, VIOSC_PORT)

viosc_client = None
local_osc_server = None
local_server_thread = None
is_server_running = False

# --- COMMUNICATION QUEUES ---
ui_state_queue = queue.Queue()      
blob_queue = queue.Queue()          
texture_queue = queue.Queue()       
log_queue = queue.Queue()  
ui_task_queue = queue.Queue()       # UI mutations from worker threads, drained on the main thread

def ui_task(fn):
    """Run a UI mutation on the main thread via the task queue."""
    ui_task_queue.put(fn)

def enqueue_set_value(tag, value):
    """Queue a dpg.set_value(tag, value) for the main thread, if the item exists."""
    def _set():
        if dpg.does_item_exist(tag):
            dpg.set_value(tag, value)
    ui_task(_set)

def log_error(context, message):
    t = time.strftime("%H:%M:%S")
    log_queue.put(f"[{t}] ERROR: {context}: {message}")

# --- GLOBAL VIMIX STATE ---
global_vimix_state = {"current_source": None, "sources": {}}

ALL_PROPERTIES = [
    "index", "name", "lock", "failed", "play", "pause", "blending", "alpha",
    "transparency", "depth", "position", "size", "corner", "angle",
    "seek", "speed", "brightness", "contrast", "saturation", "hue",
    "threshold", "gamma", "color", "posterize", "invert", "uri"
]

last_ui_signature = ""
last_num_cols = 4  
osc_log_history = [] 

# --- SEQUENCER STATE ---
NUM_STEPS = 8
NUM_TRACKS = 8
current_step = -1
is_playing = False
phase_nudge = 0.0
sync_event_seq = threading.Event()
sync_event_led = threading.Event()

# La struttura dati del sequencer con il nuovo parametro "msgs"
tracks_data = []
for r in range(NUM_TRACKS):
    track = {
        "target_id": None,
        "base_address": "", 
        "active_fade": {"active": False}, 
        "steps": []
    }
    for c in range(NUM_STEPS):
        track["steps"].append({
            "active": False,
            "type": "NONE",   
            "v1": 0.0,        
            "v2": 1.0,        
            "frames": 4,      
            "msgs": 1,        # NEW: number of messages to send in a single step
            "color": [255, 255, 255],
            "last_rand_v1": 0.0,           
            "last_rand_color": [0, 0, 0]   
        })
    tracks_data.append(track)

# --- AUDIO STATE ---
samplerate = 44100
is_audio_analyzing = False
is_beat_tracking = False
lowpass_enabled = True          # mirrors the "Use Low-Pass Filter" checkbox (read on worker threads)
audio_stream = None
audio_buffer = np.zeros(samplerate * 6, dtype=np.float32)
current_bpm = 120.0
beat_confidence = 0.0
rhythm_extractor = es.RhythmExtractor2013(method="multifeature")
lowpass_filter = es.LowPass(cutoffFrequency=250.0)

thumbnails_data = {}        
request_timestamps = {}     

def get_input_devices():
    devices = sd.query_devices()
    inputs = [f"{i}: {d['name']}" for i, d in enumerate(devices) if d['max_input_channels'] > 0]
    return inputs if inputs else ["No input device found"]

def turn_off_led():
    def _turn_off():
        if dpg.does_item_exist("beat_led"):
            dpg.configure_item("beat_led", fill=(50, 50, 50, 255))
    ui_task(_turn_off)

def flash_beat_led():
    def _flash():
        if dpg.does_item_exist("beat_led"):
            dpg.configure_item("beat_led", fill=(255, 50, 50, 255))
            threading.Timer(0.1, turn_off_led).start()
    ui_task(_flash)

def append_log(direction, address):
    t = time.strftime("%H:%M:%S")
    log_msg = f"[{t}] {direction}: {address}"
    log_queue.put(log_msg)

# ==============================================================================
# SEQUENCER UI & CLIP ASSIGNMENT
# ==============================================================================

def assign_clip_to_track(sender, app_data, user_data):
    row = user_data
    current_source = global_vimix_state.get("current_source")
    
    if current_source is not None:
        data_dict = global_vimix_state.get("sources", {})
        target_id = None
        
        for k, props in data_dict.items():
            if str(k) == str(current_source):
                name = props.get("name")
                target_id = str(name) if name else str(k)
                break
        
        if target_id:
            tracks_data[row]["target_id"] = target_id
            tracks_data[row]["base_address"] = f"/vimix/{target_id}"
            update_track_slot_ui(row)

def update_track_slot_ui(row):
    slot_tag = f"seq_slot_{row}"
    if not dpg.does_item_exist(slot_tag): return
    
    dpg.delete_item(slot_tag, children_only=True)
    target_id = tracks_data[row].get("target_id")
    
    with dpg.group(parent=slot_tag, indent=4):
        dpg.add_spacer(height=3)
        if target_id:
            if target_id in thumbnails_data:
                tex_tag = thumbnails_data[target_id]
                dpg.add_image_button(texture_tag=tex_tag, width=110, height=70, callback=assign_clip_to_track, user_data=row)
            else:
                dpg.add_button(label=f"{target_id[:10]}\n(Waiting...)", width=110, height=70, callback=assign_clip_to_track, user_data=row)
        else:
            dpg.add_button(label="ASSIGN\nCLIP", width=110, height=70, callback=assign_clip_to_track, user_data=row)

def set_step_type(sender, app_data, user_data):
    row, col, step_type = user_data
    tracks_data[row]["steps"][col]["type"] = step_type
    update_step_ui(row, col)

def toggle_step_active(sender, app_data, user_data):
    row, col = user_data
    tracks_data[row]["steps"][col]["active"] = app_data
    update_step_theme(row, col)

def update_step_val(sender, app_data, user_data):
    row, col, param_name = user_data
    if param_name == "color":
        tracks_data[row]["steps"][col][param_name] = app_data[:3]
    else:
        tracks_data[row]["steps"][col][param_name] = app_data

def update_step_theme(row, col, is_head=False):
    # Runs on any thread: capture state here, apply the theme on the main thread.
    cell_tag = f"seq_cell_{row}_{col}"
    is_active = tracks_data[row]["steps"][col]["active"]
    ui_task(lambda ct=cell_tag, ia=is_active, h=is_head: _apply_step_theme(ct, ia, h))

def _apply_step_theme(cell_tag, is_active, is_head):
    if not dpg.does_item_exist(cell_tag): return
    if is_head:
        dpg.bind_item_theme(cell_tag, theme_cell_play_on if is_active else theme_cell_play_off)
    else:
        dpg.bind_item_theme(cell_tag, theme_cell_on if is_active else theme_cell_off)

def update_step_ui(row, col):
    cell_tag = f"seq_cell_{row}_{col}"
    step_data = tracks_data[row]["steps"][col]
    
    if not dpg.does_item_exist(cell_tag): return
    
    dpg.delete_item(cell_tag, children_only=True)
    
    with dpg.group(horizontal=True, parent=cell_tag):
        cb = dpg.add_checkbox(default_value=step_data["active"], callback=toggle_step_active, user_data=(row, col))
        dpg.add_text(step_data["type"] if step_data["type"] != "NONE" else "", color=(200, 200, 200, 255))
        
        with dpg.popup(cb, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(label="Empty", callback=set_step_type, user_data=(row, col, "NONE"))
            dpg.add_separator()
            dpg.add_menu_item(label="Alpha Value", callback=set_step_type, user_data=(row, col, "AlphaV"))
            dpg.add_menu_item(label="Alpha Random", callback=set_step_type, user_data=(row, col, "AlphaR"))
            dpg.add_menu_item(label="Alpha Fade", callback=set_step_type, user_data=(row, col, "AlphaF"))
            dpg.add_separator()
            dpg.add_menu_item(label="Color Value", callback=set_step_type, user_data=(row, col, "ColorV"))
            dpg.add_menu_item(label="Color Random", callback=set_step_type, user_data=(row, col, "ColorR"))

    if step_data["type"] == "AlphaV":
        dpg.add_spacer(parent=cell_tag, height=5)
        dpg.add_drag_float(parent=cell_tag, width=70, default_value=step_data["v1"], min_value=0.0, max_value=1.0, speed=0.01, format="%.2f", callback=update_step_val, user_data=(row, col, "v1"))
    
    elif step_data["type"] == "AlphaR":
        dpg.add_spacer(parent=cell_tag, height=5)
        dpg.add_text(f"{step_data['last_rand_v1']:.2f}", color=(150, 255, 150, 255), tag=f"rand_v1_{row}_{col}", parent=cell_tag, indent=20)
        
    elif step_data["type"] == "AlphaF":
        dpg.add_spacer(parent=cell_tag, height=2)
        # NEW UI: split into two compact rows to fit the intermediate messages
        with dpg.group(horizontal=True, parent=cell_tag):
            dpg.add_drag_float(width=34, default_value=step_data["v1"], min_value=0.0, max_value=1.0, speed=0.01, format="%.1f", callback=update_step_val, user_data=(row, col, "v1"))
            dpg.add_drag_float(width=34, default_value=step_data["v2"], min_value=0.0, max_value=1.0, speed=0.01, format="%.1f", callback=update_step_val, user_data=(row, col, "v2"))
        with dpg.group(horizontal=True, parent=cell_tag):
            dpg.add_drag_int(width=34, default_value=step_data["frames"], min_value=1, max_value=32, speed=1, format="%ds", callback=update_step_val, user_data=(row, col, "frames"))
            dpg.add_drag_int(width=34, default_value=step_data["msgs"], min_value=1, max_value=32, speed=1, format="%dm", callback=update_step_val, user_data=(row, col, "msgs"))
        
    elif step_data["type"] == "ColorV":
        dpg.add_spacer(parent=cell_tag, height=5)
        norm_color = [c/255.0 for c in step_data["color"]]
        dpg.add_color_edit(parent=cell_tag, default_value=norm_color, no_alpha=True, no_inputs=True, width=70, height=25, callback=update_step_val, user_data=(row, col, "color"))

    elif step_data["type"] == "ColorR":
        dpg.add_spacer(parent=cell_tag, height=5)
        # last_rand_color is stored normalized (0..1); no_alpha color_edit expects 3 components
        dpg.add_color_edit(parent=cell_tag, default_value=list(step_data["last_rand_color"]), no_alpha=True, no_inputs=True, no_picker=True, no_tooltip=True, width=70, height=25, tag=f"rand_color_{row}_{col}")

    update_step_theme(row, col, is_head=(is_playing and current_step == col))


def regen_thumb_callback(sender, app_data, user_data):
    target_id = user_data
    if viosc_client:
        msg_addr = f"/viosc/regen_thumb/{target_id}"
        viosc_client.send_message(msg_addr, [])
        append_log("OUT", msg_addr)
        
    with dpg.mutex():
        if target_id in thumbnails_data:
            thumbnails_data.pop(target_id)
        if dpg.does_item_exist(f"img_{target_id}"): dpg.delete_item(f"img_{target_id}")
        if dpg.does_item_exist(f"tex_{target_id}"): dpg.delete_item(f"tex_{target_id}")
            
        container_tag = f"thumb_container_{target_id}"
        loading_tag = f"loading_txt_{target_id}"
        if dpg.does_item_exist(container_tag) and not dpg.does_item_exist(loading_tag):
            dpg.add_text("  [ Rigenero... ]", color=(255, 200, 50, 255), tag=loading_tag, parent=container_tag)
            with dpg.popup(loading_tag, mousebutton=dpg.mvMouseButton_Right):
                dpg.add_menu_item(label="Regenerate Thumbnail (Random)", callback=regen_thumb_callback, user_data=target_id)

def update_vimix_sources_ui(json_string):
    global global_vimix_state, last_ui_signature, last_num_cols
    try:
        payload = json.loads(json_string)
        if not isinstance(payload, dict):
            raise ValueError("replydata payload is not a JSON object")
        sources = payload.get("sources", {})
        if sources is None:
            sources = {}
        if not isinstance(sources, dict):
            raise ValueError("replydata 'sources' is not an object")
        # Drop malformed entries so a single bad source can't crash the UI/main loop
        sources = {k: v for k, v in sources.items() if isinstance(v, dict)}
        global_vimix_state["current_source"] = payload.get("current_source")
        global_vimix_state["sources"] = sources

        # L-1: prune cached state for sources that no longer exist (main thread only),
        # so thumbnails_data / request_timestamps / registry textures do not grow across churn.
        live_ids = set()
        for k, props in sources.items():
            name = props.get("name")
            live_ids.add(str(name) if name else str(k))
        for target_id in list(thumbnails_data):
            if target_id not in live_ids:
                thumbnails_data.pop(target_id)
                tex_tag = f"tex_{target_id}"
                if dpg.does_item_exist(tex_tag):
                    dpg.delete_item(tex_tag)
        for key in list(request_timestamps):
            if key.startswith("thumb_"):
                target_id = key[len("thumb_"):]
                if target_id not in live_ids:
                    request_timestamps.pop(key)
        
        current_source = global_vimix_state["current_source"]
        data_dict = global_vimix_state["sources"]
        
        def get_sort_index(k):
            idx_val = data_dict[k].get("index")
            if idx_val is not None:
                try:
                    return int(idx_val)
                except (TypeError, ValueError):
                    pass
            try:
                return int(k)
            except (TypeError, ValueError):
                return 0
            
        sorted_keys = sorted(data_dict.keys(), key=get_sort_index)
        current_signature = f"cols:{last_num_cols}_" + str([(k, data_dict[k].get("name"), data_dict[k].get("index")) for k in sorted_keys])
        
        if current_signature != last_ui_signature:
            if dpg.does_item_exist("vimix_table"): dpg.delete_item("vimix_table")
            if dpg.does_item_exist("media_grid"): dpg.delete_item("media_grid")

            t_raw = dpg.add_table(parent="vimix_raw_group", tag="vimix_table", header_row=True, borders_innerH=True, borders_innerV=True, row_background=True, scrollX=True, scrollY=True, freeze_columns=2, height=180)
            for prop in ALL_PROPERTIES:
                dpg.add_table_column(label=prop.capitalize(), parent=t_raw)
                
            for idx in sorted_keys:
                r_id = dpg.add_table_row(parent=t_raw)
                props_i = data_dict[idx]
                for prop in ALL_PROPERTIES:
                    tag_name = f"raw_{idx}_{prop}"
                    if dpg.does_item_exist(tag_name): dpg.delete_item(tag_name)
                    if dpg.does_alias_exist(tag_name): dpg.remove_alias(tag_name)
                    val = props_i.get(prop)
                    if prop == "index" and val is None: val = idx 
                    if isinstance(val, float): val_str = f"{val:.2f}"
                    elif val is None: val_str = "---"
                    else: val_str = str(val)
                    dpg.add_text(val_str, parent=r_id, tag=tag_name)
            
            num_cols = last_num_cols
            t_grid = dpg.add_table(parent="vimix_media_group", tag="media_grid", header_row=False, borders_innerH=False, borders_innerV=False, policy=dpg.mvTable_SizingFixedFit)
            for i in range(num_cols): 
                dpg.add_table_column(parent=t_grid)
                
            for i in range(0, len(sorted_keys), num_cols):
                row_indices = sorted_keys[i:i+num_cols]
                r_id = dpg.add_table_row(parent=t_grid)
                for idx in row_indices:
                    name = data_dict[idx].get("name")
                    target_id = str(name) if name else str(idx)
                    tile_tag = f"tile_{target_id}"
                    
                    if dpg.does_item_exist(tile_tag): dpg.delete_item(tile_tag)
                    cw = dpg.add_child_window(parent=r_id, width=135, height=160, border=True, tag=tile_tag)
                    
                    title_tag = f"tile_title_{target_id}"
                    if dpg.does_item_exist(title_tag): dpg.delete_item(title_tag)
                    dpg.add_text("---", parent=cw, wrap=125, color=(255, 255, 255, 255), tag=title_tag)
                    with dpg.popup(title_tag, mousebutton=dpg.mvMouseButton_Right):
                        dpg.add_menu_item(label="Regenerate Thumbnail (Random)", callback=regen_thumb_callback, user_data=target_id)
                    
                    dpg.add_spacer(parent=cw, height=5)
                    container_tag = f"thumb_container_{target_id}"
                    if dpg.does_item_exist(container_tag): dpg.delete_item(container_tag)
                    
                    g_id = dpg.add_group(parent=cw, tag=container_tag, indent=4)
                    img_tag = f"img_{target_id}"
                    if target_id in thumbnails_data:
                        tex_tag = thumbnails_data[target_id]
                        if dpg.does_item_exist(img_tag): dpg.delete_item(img_tag)
                        dpg.add_image(texture_tag=tex_tag, parent=g_id, tag=img_tag, width=110, height=80)
                        with dpg.popup(img_tag, mousebutton=dpg.mvMouseButton_Right):
                            dpg.add_menu_item(label="Regenerate Thumbnail (Random)", callback=regen_thumb_callback, user_data=target_id)
                    else:
                        loading_tag = f"loading_txt_{target_id}"
                        if dpg.does_item_exist(loading_tag): dpg.delete_item(loading_tag)
                        dpg.add_text(" [ Loading... ]", parent=g_id, color=(150, 150, 150, 255), tag=loading_tag)
                        with dpg.popup(loading_tag, mousebutton=dpg.mvMouseButton_Right):
                            dpg.add_menu_item(label="Regenerate Thumbnail (Random)", callback=regen_thumb_callback, user_data=target_id)
                        
                for _ in range(num_cols - len(row_indices)):
                    dpg.add_text("", parent=r_id)
                    
            last_ui_signature = current_signature
            
        for idx in sorted_keys:
            props = data_dict[idx]
            name = props.get("name")
            is_selected = (str(idx) == str(current_source))
            target_id = str(name) if name else str(idx)
            display_name = str(name) if name else f"Idx: {idx}"
            tile_tag = f"tile_{target_id}"
            
            if dpg.does_item_exist(tile_tag):
                dpg.bind_item_theme(tile_tag, theme_selected_clip if is_selected else theme_normal_clip)
            if dpg.does_item_exist(f"tile_title_{target_id}"):
                dpg.set_value(f"tile_title_{target_id}", f"{display_name}")
            
            for prop in ALL_PROPERTIES:
                val = props.get(prop)
                if prop == "index" and val is None: val = idx 
                if isinstance(val, float): val_str = f"{val:.2f}"
                elif val is None: val_str = "---"
                else: val_str = str(val)
                txt_tag = f"raw_{idx}_{prop}"
                if dpg.does_item_exist(txt_tag):
                    dpg.set_value(txt_tag, val_str)
                    
    except Exception as e:
        log_error("UI update", e)

def thumbnail_decoder_worker():
    while True:
        name, t_idx, blob_bytes = blob_queue.get()
        try:
            image = Image.open(io.BytesIO(blob_bytes))
            width, height = image.size
            if width * height > MAX_THUMBNAIL_PIXELS:
                raise ValueError(f"thumbnail too large: {width}x{height} px")
            image = image.convert('RGBA')
            img_data = np.array(image, dtype=np.float32) / 255.0
            texture_queue.put((name, img_data.flatten(), width, height))
        except Exception as e:
            print(f"[viseq Decoder Error] Unable to decode '{name}': {e}")
        blob_queue.task_done()

# ==============================================================================
# MONITOR PLAYERS
# ==============================================================================
monitor_players = []          # each: {"id", "tag", "target_id", "props"}
monitor_player_counter = 0

def get_current_target_id():
    """Return the name (or index) of the source currently selected in vimix."""
    current_source = global_vimix_state.get("current_source")
    if current_source is None:
        return None
    for k, props in global_vimix_state.get("sources", {}).items():
        if str(k) == str(current_source):
            name = props.get("name")
            return str(name) if name else str(k)
    return None

def find_source_by_name(name):
    for idx, props in global_vimix_state.get("sources", {}).items():
        if str(props.get("name")) == str(name):
            return idx, props
    return None, None

def find_player_index(player_id):
    for i, p in enumerate(monitor_players):
        if p["id"] == player_id:
            return i
    return None

def send_monitor_command(player_id):
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    addr = f"/viosc/monitor/{target_id}"
    props = player.get("props", [])
    if props:
        osc_client.send_message(addr, list(props))
        append_log("OUT", f"{addr} {props}")
    else:
        osc_client.send_message(addr, [])
        append_log("OUT", f"{addr} (stop)")

def new_monitor_player(sender=None, app_data=None, user_data=None):
    global monitor_player_counter
    monitor_player_counter += 1
    player_id = monitor_player_counter
    tag = f"monitor_player_{player_id}"
    player = {"id": player_id, "tag": tag, "target_id": None, "props": ["seek"]}
    monitor_players.append(player)
    pos = (10 + 280 * ((player_id - 1) % 4), 30 + 260 * ((player_id - 1) // 4))
    with dpg.window(label=f"Monitor Player {player_id}", tag=tag, width=270, height=265, pos=pos):
        head_tag = f"mon_head_{player_id}"
        dpg.add_text("Click the box below to assign the current source.", tag=head_tag, wrap=250)
        with dpg.popup(head_tag, mousebutton=dpg.mvMouseButton_Right):
            dpg.add_menu_item(label="Monitor Properties...", callback=lambda s, a, u: open_monitor_props(player_id), user_data=player_id)
            dpg.add_separator()
            dpg.add_menu_item(label="Remove Player", callback=lambda s, a, u: remove_monitor_player(player_id), user_data=player_id)
        dpg.add_spacer(height=6)
        with dpg.child_window(width=160, height=120, tag=f"mon_box_{player_id}", border=True, no_scrollbar=True):
            with dpg.group(indent=4, tag=f"mon_box_content_{player_id}"):
                dpg.add_spacer(height=3)
                dpg.add_button(label="CLICK TO ASSIGN", width=150, height=110, callback=assign_monitor_player, user_data=player_id)
        dpg.add_spacer(height=6)
        with dpg.group(tag=f"mon_vals_{player_id}"):
            dpg.add_text("No monitoring yet.")
        dpg.add_spacer(height=6)
        with dpg.group(horizontal=True):
            dpg.add_button(label="Properties...", width=120, callback=lambda s, a, u: open_monitor_props(player_id), user_data=player_id)
            dpg.add_button(label="Remove", width=90, callback=lambda s, a, u: remove_monitor_player(player_id), user_data=player_id)

def update_monitor_player_ui(player_id):
    try:
        idx = find_player_index(player_id)
        if idx is None:
            return
        player = monitor_players[idx]
        tag = player["tag"]
        if not dpg.does_item_exist(tag):
            return
        target_id = player["target_id"]
        head = f"mon_head_{player_id}"
        if dpg.does_item_exist(head):
            if target_id:
                dpg.set_value(head, f"Source: {target_id}")
            else:
                dpg.set_value(head, "Click the box below to assign the current source.")
        box = f"mon_box_{player_id}"
        content = f"mon_box_content_{player_id}"
        if dpg.does_item_exist(box):
            if dpg.does_item_exist(content):
                dpg.delete_item(content)
            with dpg.group(parent=box, indent=4, tag=content):
                dpg.add_spacer(height=3)
                if target_id:
                    if target_id in thumbnails_data:
                        dpg.add_image(texture_tag=thumbnails_data[target_id], width=150, height=110)
                    else:
                        dpg.add_text("Loading thumbnail...", color=(150, 150, 150, 255), wrap=140)
                else:
                    dpg.add_button(label="CLICK TO ASSIGN", width=150, height=110, callback=assign_monitor_player, user_data=player_id)
        rebuild_monitor_player_values(player_id)
    except Exception as e:
        print(f"[viseq Monitor UI] Error updating player {player_id}: {e}")

def rebuild_monitor_player_values(player_id):
    try:
        idx = find_player_index(player_id)
        if idx is None:
            return
        player = monitor_players[idx]
        vals = f"mon_vals_{player_id}"
        if not dpg.does_item_exist(vals):
            return
        dpg.delete_item(vals, children_only=True)
        if not player["target_id"]:
            dpg.add_text("No monitoring yet.", parent=vals)
            return
        props = player.get("props", [])
        if not props:
            dpg.add_text("Monitoring stopped (no properties selected).", parent=vals, color=(200, 200, 200, 255))
            return
        for prop in props:
            dpg.add_text(f"{prop}: ---", parent=vals, tag=f"mon_val_{player_id}_{prop}", wrap=250)
    except Exception as e:
        print(f"[viseq Monitor UI] Error rebuilding values of player {player_id}: {e}")

def refresh_monitor_player_values(player_id):
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    _, props = find_source_by_name(target_id)
    for prop in player.get("props", []):
        t = f"mon_val_{player_id}_{prop}"
        if not dpg.does_item_exist(t):
            continue
        val = props.get(prop) if props else None
        if isinstance(val, float):
            s = f"{val:.3f}"
        elif val is None:
            s = "---"
        else:
            s = str(val)
        dpg.set_value(t, f"{prop}: {s}")

def assign_monitor_player(sender, app_data, user_data):
    player_id = user_data
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = get_current_target_id()
    if not target_id:
        if dpg.does_item_exist(f"mon_head_{player_id}"):
            dpg.set_value(f"mon_head_{player_id}", "No source selected in the media library.")
        return
    for other in monitor_players:
        if other["id"] != player_id and other.get("target_id") == target_id:
            if dpg.does_item_exist(f"mon_head_{player_id}"):
                dpg.set_value(f"mon_head_{player_id}", f"Already monitored in Player {other['id']}.")
            return
    player["target_id"] = target_id
    player["props"] = ["seek"]
    send_monitor_command(player_id)
    update_monitor_player_ui(player_id)

def open_monitor_props(player_id):
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    target_id = player["target_id"]
    if not target_id:
        return
    modal_tag = f"mon_props_modal_{player_id}"
    if dpg.does_item_exist(modal_tag):
        dpg.delete_item(modal_tag)
    with dpg.window(label=f"Monitor Properties - {target_id}", tag=modal_tag, modal=True, width=270, height=400, no_resize=True):
        dpg.add_text("Select the properties to monitor:", wrap=240)
        dpg.add_separator()
        with dpg.child_window(height=310, border=True):
            for prop in ALL_PROPERTIES:
                dpg.add_checkbox(label=prop, default_value=(prop in player["props"]), tag=f"mon_cb_{player_id}_{prop}", callback=on_monitor_prop_toggle, user_data=player_id)

def on_monitor_prop_toggle(sender, app_data, user_data):
    player_id = user_data
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    new_props = []
    for prop in ALL_PROPERTIES:
        cb_tag = f"mon_cb_{player_id}_{prop}"
        if dpg.does_item_exist(cb_tag) and dpg.get_value(cb_tag):
            new_props.append(prop)
    player["props"] = new_props
    send_monitor_command(player_id)
    rebuild_monitor_player_values(player_id)

def remove_monitor_player(player_id):
    idx = find_player_index(player_id)
    if idx is None:
        return
    player = monitor_players[idx]
    if player.get("target_id"):
        addr = f"/viosc/monitor/{player['target_id']}"
        osc_client.send_message(addr, [])
        append_log("OUT", f"{addr} (stop)")
    tag = player["tag"]
    if dpg.does_item_exist(tag):
        dpg.delete_item(tag)
    del monitor_players[idx]

def incoming_osc_handler(address, *args):
    append_log("IN ", address)
    try:
        if address == "/viosc/replydata" and args:
            if len(args[0]) <= MAX_STATE_JSON_BYTES:
                ui_state_queue.put(args[0])
        elif address.startswith("/viosc/replythumb/") and args:
            if len(args[0]) <= MAX_THUMBNAIL_BLOB_BYTES:
                parts = address.split('/')
                blob_queue.put((parts[-2], parts[-1], args[0]))
    except Exception as e:
        log_error("OSC input", e)

def toggle_local_server():
    global local_osc_server, local_server_thread, is_server_running
    if is_server_running:
        if local_osc_server:
            local_osc_server.shutdown()
            local_server_thread.join(timeout=1.0)
            local_osc_server = None
        is_server_running = False
        dpg.set_item_label("btn_server_toggle", "Start Server")
        dpg.set_value("server_status", "Server Status: Stopped")
    else:
        ip = dpg.get_value("listen_ip")
        port = dpg.get_value("listen_port")
        try:
            disp = dispatcher.Dispatcher()
            disp.set_default_handler(incoming_osc_handler)
            local_osc_server = osc_server.ThreadingOSCUDPServer((ip, port), disp)
            local_server_thread = threading.Thread(target=local_osc_server.serve_forever, daemon=True)
            local_server_thread.start()
            is_server_running = True
            dpg.set_item_label("btn_server_toggle", "Stop Server")
            dpg.set_value("server_status", f"Server Status: Listening on {ip}:{port}")
        except Exception as e:
            dpg.set_value("server_status", f"Server Status: ERROR ({e})")

def connect_to_viosc():
    global viosc_client
    ip = dpg.get_value("viosc_ip")
    port = dpg.get_value("viosc_port")
    try:
        viosc_client = udp_client.SimpleUDPClient(ip, port)
        dpg.set_value("viosc_status", f"Client Status: Ready on {ip}:{port}")
    except Exception:
        dpg.set_value("viosc_status", f"Client Status: Initialization error")

def callback_resync():
    global current_step
    current_step = -1
    for r in range(NUM_TRACKS):
        tracks_data[r]["active_fade"]["active"] = False
    sync_event_seq.set()
    sync_event_led.set()

def callback_nudge_backward():
    global phase_nudge
    phase_nudge += 0.05 

def callback_nudge_forward():
    global phase_nudge
    phase_nudge -= 0.05

def audio_callback(indata, frames, time_info, status):
    global audio_buffer
    if status: print(status)
    samples = indata[:, 0].astype(np.float32)
    audio_buffer = np.roll(audio_buffer, -frames)
    audio_buffer[-frames:] = samples
    if is_audio_analyzing:
        enqueue_set_value("vu_meter", float(np.max(np.abs(samples))))

def essentia_analyzer_loop():
    global current_bpm, beat_confidence
    last_error = ""
    while True:
        if is_beat_tracking:
            try:
                audio_slice = essentia.array(audio_buffer.copy())
                if np.max(np.abs(audio_slice)) > 0.005:
                    if lowpass_enabled: audio_slice = lowpass_filter(audio_slice)
                    bpm, beats, confidence, estimates, intervals = rhythm_extractor(audio_slice)
                    if confidence > 0.2 or beat_confidence == 0.0:
                        current_bpm = float(bpm)
                        beat_confidence = float(confidence)
                        enqueue_set_value("testo_bpm", f"BPM: {current_bpm:.1f} (Conf: {beat_confidence:.2f})")
            except Exception as e:
                # Log each distinct failure once, not every second
                err = f"{type(e).__name__}: {e}"
                if err != last_error:
                    last_error = err
                    log_error("BPM analysis", err)
        time.sleep(1.0)

def visual_metronome_loop():
    global phase_nudge
    while True:
        if is_beat_tracking and current_bpm > 0 and not is_playing:
            base_sleep = 60.0 / current_bpm
            actual_sleep = max(0.0, base_sleep + phase_nudge)
            flash_beat_led()
            sync_event_led.wait(actual_sleep)
            if sync_event_led.is_set(): sync_event_led.clear()
            phase_nudge = 0.0
        else:
            time.sleep(0.1)

# ==============================================================================
# NEW ASYNC THREAD FOR HIGH-RESOLUTION FADES
# ==============================================================================
def fade_tick_loop():
    while True:
        if is_playing:
            current_time = time.time()
            for r, track in enumerate(tracks_data):
                fade = track.get("active_fade", {})
                if fade and fade.get("active"):
                    elapsed = current_time - fade["start_time"]
                    expected_msg_index = int(elapsed / fade["msg_interval"])

                    # If we fell behind, or it is time for the next tick
                    if expected_msg_index > fade["last_msg_index"]:
                        max_msg = min(expected_msg_index, fade["total_msgs"] - 1)
                        
                        # Send all the accumulated intermediate messages
                        for i in range(fade["last_msg_index"] + 1, max_msg + 1):
                            progress = i / float(fade["total_msgs"] - 1) if fade["total_msgs"] > 1 else 1.0
                            val = fade["start_val"] + (fade["end_val"] - fade["start_val"]) * progress
                            try:
                                osc_client.send_message(fade["address"], float(val))
                                append_log("OUT", f"{fade['address']} [FADE: {val:.2f}]")
                            except Exception: pass

                        fade["last_msg_index"] = max_msg

                        # Deactivate when the fade is finished
                        if fade["last_msg_index"] >= fade["total_msgs"] - 1:
                            fade["active"] = False
        time.sleep(0.01) # 100 FPS check loop for smooth fades

def sequencer_tick():
    global current_step, phase_nudge
    while True:
        if is_playing:
            base_sleep = 60.0 / current_bpm if current_bpm > 0 else 0.5
            actual_sleep = max(0.0, base_sleep + phase_nudge)
            phase_nudge = 0.0 
            
            prev_step = current_step
            current_step = (current_step + 1) % NUM_STEPS
            
            for r, track in enumerate(tracks_data):
                if prev_step != -1: update_step_theme(r, prev_step, is_head=False)
                update_step_theme(r, current_step, is_head=True)
                
                step_data = track["steps"][current_step]
                
                if step_data["active"]:
                    # A new step cancels any pending fade unless it starts its own (audit HIGH-2)
                    track["active_fade"]["active"] = False
                    base_addr = track["base_address"]
                    if base_addr and base_addr.strip():
                        try:
                            if step_data["type"] == "AlphaV":
                                target_addr = f"{base_addr}/alpha"
                                osc_client.send_message(target_addr, float(step_data["v1"]))
                                append_log("OUT", f"{target_addr} [{step_data['v1']:.2f}]")
                            
                            elif step_data["type"] == "AlphaR":
                                target_addr = f"{base_addr}/alpha"
                                rand_val = random.uniform(0.0, 1.0)
                                osc_client.send_message(target_addr, float(rand_val))
                                append_log("OUT", f"{target_addr} [{rand_val:.2f}]")
                                
                                step_data["last_rand_v1"] = rand_val
                                tag_v1 = f"rand_v1_{r}_{current_step}"
                                enqueue_set_value(tag_v1, f"{rand_val:.2f}")
                                    
                            elif step_data["type"] == "AlphaF":
                                target_addr = f"{base_addr}/alpha"
                                total_msgs = step_data["frames"] * step_data["msgs"]
                                
                                # Start the asynchronous state machine
                                track["active_fade"] = {
                                    "active": True,
                                    "address": target_addr,
                                    "start_val": step_data["v1"],
                                    "end_val": step_data["v2"],
                                    "total_msgs": total_msgs,
                                    "msg_interval": base_sleep / step_data["msgs"] if step_data["msgs"] > 0 else base_sleep,
                                    "start_time": time.time(),
                                    "last_msg_index": 0
                                }
                                # The sequencer sends the FIRST value immediately
                                osc_client.send_message(target_addr, float(step_data["v1"]))
                                append_log("OUT", f"{target_addr} [FADE START: {step_data['v1']:.2f}]")
                                
                            elif step_data["type"] == "ColorV":
                                target_addr = f"{base_addr}/color"
                                r_val, g_val, b_val = [float(c/255.0) for c in step_data["color"]]
                                osc_client.send_message(target_addr, [r_val, g_val, b_val])
                                append_log("OUT", f"{target_addr} [{r_val:.2f}, {g_val:.2f}, {b_val:.2f}]")
                                
                            elif step_data["type"] == "ColorR":
                                target_addr = f"{base_addr}/color"
                                r_val, g_val, b_val = random.uniform(0.0, 1.0), random.uniform(0.0, 1.0), random.uniform(0.0, 1.0)
                                osc_client.send_message(target_addr, [r_val, g_val, b_val])
                                append_log("OUT", f"{target_addr} [{r_val:.2f}, {g_val:.2f}, {b_val:.2f}]")
                                
                                step_data["last_rand_color"] = [r_val, g_val, b_val]
                                tag_color = f"rand_color_{r}_{current_step}"
                                enqueue_set_value(tag_color, list(step_data["last_rand_color"]))

                        except Exception as e: print(f"[viseq OSC Error] {e}")
                        
            flash_beat_led()
            sync_event_seq.wait(actual_sleep)
            if sync_event_seq.is_set(): sync_event_seq.clear() 
        else:
            time.sleep(0.1)

def on_lowpass_toggle(sender, app_data, user_data):
    global lowpass_enabled
    lowpass_enabled = bool(app_data)

def toggle_audio_stream(sender, app_data, user_data):
    global audio_stream, is_audio_analyzing, is_beat_tracking
    if user_data == "vu_meter": is_audio_analyzing = app_data
    elif user_data == "beat_tracking": is_beat_tracking = app_data
    needs_stream = is_audio_analyzing or is_beat_tracking

    if needs_stream and audio_stream is None:
        device_string = dpg.get_value("combo_devices")
        if "No input device" in device_string:
            dpg.set_value(sender, False)
            return
        device_id = int(device_string.split(":")[0])
        try:
            audio_stream = sd.InputStream(device=device_id, channels=1, samplerate=samplerate, dtype=np.float32, callback=audio_callback)
            audio_stream.start()
        except Exception as e:
            dpg.set_value(sender, False)
    elif not needs_stream and audio_stream is not None:
        audio_stream.stop()
        audio_stream.close()
        audio_stream = None
        dpg.set_value("vu_meter", 0.0)
        dpg.set_value("testo_bpm", "BPM: ---")
        turn_off_led()

def toggle_play():
    global is_playing, current_step
    is_playing = not is_playing
    if not is_playing:
        for r in range(NUM_TRACKS):
            for c in range(NUM_STEPS):
                update_step_theme(r, c, is_head=False)
            tracks_data[r]["active_fade"]["active"] = False
        current_step = -1
        dpg.set_item_label("btn_play", "PLAY")
    else:
        dpg.set_item_label("btn_play", "STOP")
        sync_event_seq.set()


dpg.create_context()

with dpg.texture_registry(tag="texture_registry"): pass

with dpg.theme() as theme_selected_clip:
    with dpg.theme_component(dpg.mvChildWindow):
        dpg.add_theme_color(dpg.mvThemeCol_Border, (50, 255, 50, 255))
        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (30, 80, 30, 255))
        dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_normal_clip:
    with dpg.theme_component(dpg.mvChildWindow):
        dpg.add_theme_color(dpg.mvThemeCol_Border, (80, 80, 80, 255))
        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (40, 40, 40, 255))
        dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_compact_table:
    with dpg.theme_component(dpg.mvTable):
        dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 1, 1)

with dpg.theme() as theme_cell_off:
    with dpg.theme_component(dpg.mvChildWindow):
        dpg.add_theme_color(dpg.mvThemeCol_Border, (80, 80, 80, 255))
        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (40, 40, 40, 255))
        dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_on:
    with dpg.theme_component(dpg.mvChildWindow):
        dpg.add_theme_color(dpg.mvThemeCol_Border, (50, 255, 50, 255))
        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (30, 80, 30, 255))
        dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_play_off:
    with dpg.theme_component(dpg.mvChildWindow):
        dpg.add_theme_color(dpg.mvThemeCol_Border, (255, 255, 255, 255))
        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (80, 80, 80, 255))
        dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)

with dpg.theme() as theme_cell_play_on:
    with dpg.theme_component(dpg.mvChildWindow):
        dpg.add_theme_color(dpg.mvThemeCol_Border, (255, 255, 255, 255))
        dpg.add_theme_color(dpg.mvThemeCol_ChildBg, (80, 220, 80, 255))
        dpg.add_theme_style(dpg.mvStyleVar_ChildRounding, 5)


# WINDOW 1: SEQUENCER
with dpg.window(label="Step Sequencer", width=1050, height=800, pos=(10, 10), no_close=True):
    with dpg.group(horizontal=True):
        dpg.add_button(label="PLAY", tag="btn_play", callback=toggle_play, width=100, height=40)
        dpg.add_spacer(width=20)
        dpg.add_button(label="<", callback=callback_nudge_backward, width=40, height=40)
        dpg.add_button(label="RESYNC", callback=callback_resync, width=80, height=40)
        dpg.add_button(label=">", callback=callback_nudge_forward, width=40, height=40)
        
    dpg.add_spacer(height=10)
    
    with dpg.table(header_row=False, borders_innerH=False, borders_innerV=False, borders_outerH=False, borders_outerV=False, scrollX=True, scrollY=True, policy=dpg.mvTable_SizingFixedFit, tag="seq_table"):
        
        for i in range(NUM_STEPS): 
            dpg.add_table_column(width_fixed=True, init_width_or_weight=90)
        dpg.add_table_column(width_fixed=True, init_width_or_weight=135)
            
        for row in range(NUM_TRACKS):
            with dpg.table_row():
                
                # THE 8 PADS
                for step in range(NUM_STEPS):
                    cell_tag = f"seq_cell_{row}_{step}"
                    with dpg.child_window(width=90, height=90, tag=cell_tag, no_scrollbar=True):
                        pass
                    update_step_ui(row, step)
                    
                # ASSIGNABLE THUMBNAIL SLOT
                with dpg.child_window(width=135, height=90, border=True, tag=f"seq_slot_{row}", no_scrollbar=True):
                    pass
                update_track_slot_ui(row)

    dpg.bind_item_theme("seq_table", theme_compact_table)

# WINDOW 2: AUDIO ANALYZER
input_devices_list = get_input_devices()

with dpg.window(label="Audio analyzer", width=350, height=220, pos=(10, 820), no_close=True):
    dpg.add_text("Select Audio Source:")
    dpg.add_combo(items=input_devices_list, default_value=input_devices_list[0], tag="combo_devices", width=-1)
    dpg.add_spacer(height=10)
    dpg.add_checkbox(label="Enable Level Analysis (VU)", callback=toggle_audio_stream, user_data="vu_meter")
    dpg.add_progress_bar(tag="vu_meter", default_value=0.0, width=-1, height=15, overlay="")
    dpg.add_spacer(height=10)
    dpg.add_checkbox(label="Enable BPM Analysis (Essentia)", callback=toggle_audio_stream, user_data="beat_tracking")
    dpg.add_checkbox(label="Use Low-Pass Filter (kick only)", default_value=True, tag="cb_lowpass", callback=on_lowpass_toggle)
    
    with dpg.group(horizontal=True):
        dpg.add_text("BPM: ---", tag="testo_bpm")
        with dpg.drawlist(width=30, height=30):
            dpg.draw_circle(center=[15, 15], radius=10, color=(0,0,0,255), fill=(50, 50, 50, 255), tag="beat_led")

# WINDOW 3: viOSC
with dpg.window(label="viOSC", width=340, height=320, pos=(370, 820), no_close=True):
    dpg.add_text("1. Setup Client (to viOSC):")
    with dpg.group(horizontal=True):
        dpg.add_input_text(default_value="127.0.0.1", tag="viosc_ip", width=120)
        dpg.add_input_int(default_value=6666, tag="viosc_port", width=80, step=0)
        dpg.add_button(label="Connect Client", callback=connect_to_viosc)
    dpg.add_text("Client Status: Waiting", tag="viosc_status", color=(150, 150, 150, 255))
    dpg.add_separator()
    dpg.add_spacer(height=5)
    dpg.add_text("2. Setup Server (Listening):")
    with dpg.group(horizontal=True):
        dpg.add_input_text(default_value="127.0.0.1", tag="listen_ip", width=120)
        dpg.add_input_int(default_value=6667, tag="listen_port", width=80, step=0)
        dpg.add_button(label="Start Server", tag="btn_server_toggle", callback=toggle_local_server)
    dpg.add_text("Server Status: Stopped", tag="server_status", color=(150, 150, 150, 255))
    dpg.add_separator()
    dpg.add_spacer(height=5)
    
    with dpg.group(tag="vimix_raw_group"):
        pass

# WINDOW 4: VIMIX MEDIA
with dpg.window(label="Vimix Media", width=550, height=690, pos=(1100, 10), no_close=True, tag="vimix_media_window"):
    dpg.add_text("Media Library:")
    dpg.add_separator()
    with dpg.group(tag="vimix_media_group"):
        pass

# WINDOW 5: OSC LOGS
with dpg.window(label="OSC Logs", width=950, height=150, pos=(720, 820), no_close=True):
    dpg.add_text("Waiting for OSC traffic...", tag="osc_log_text")

# NEW THREAD FOR HIGH-FREQUENCY FADES
threading.Thread(target=fade_tick_loop, daemon=True).start()

threading.Thread(target=sequencer_tick, daemon=True).start()
threading.Thread(target=visual_metronome_loop, daemon=True).start()
threading.Thread(target=essentia_analyzer_loop, daemon=True).start()
threading.Thread(target=thumbnail_decoder_worker, daemon=True).start()

dpg.create_viewport(title='viseq - Audio-Reactive VJ Controller', width=1700, height=1000)
with dpg.viewport_menu_bar():
    with dpg.menu(label="Monitor"):
        dpg.add_menu_item(label="New Monitor Player", callback=new_monitor_player)
dpg.setup_dearpygui()
dpg.show_viewport()

try:
    while dpg.is_dearpygui_running():
    
        if dpg.does_item_exist("vimix_media_window"):
            w = dpg.get_item_width("vimix_media_window")
            current_cols = max(1, int((w - 20) / 145))
            if current_cols != last_num_cols:
                last_num_cols = current_cols
                if global_vimix_state.get("sources"):
                    update_vimix_sources_ui(json.dumps(global_vimix_state))

        has_new_logs = False
        while not log_queue.empty():
            osc_log_history.append(log_queue.get())
            has_new_logs = True
        
        if has_new_logs:
            if len(osc_log_history) > 25:
                del osc_log_history[:-25]
            if dpg.does_item_exist("osc_log_text"):
                dpg.set_value("osc_log_text", "\n".join(osc_log_history))

        # Run queued UI mutations from worker threads on the main thread (audit HIGH-1)
        while not ui_task_queue.empty():
            task = ui_task_queue.get()
            try:
                task()
            except Exception as e:
                log_error("UI task", e)

        latest_json = None
        while not ui_state_queue.empty():
            latest_json = ui_state_queue.get()
    
        if latest_json:
            update_vimix_sources_ui(latest_json)

        while not texture_queue.empty():
            name, img_data, w, h = texture_queue.get()
            target_id = name
            tex_tag = f"tex_{target_id}"
            img_tag = f"img_{target_id}"
            container_tag = f"thumb_container_{target_id}"
            loading_tag = f"loading_txt_{target_id}"
        
            if dpg.does_item_exist(tex_tag):
                dpg.delete_item(tex_tag)
            
            dpg.add_static_texture(width=w, height=h, default_value=img_data, tag=tex_tag, parent="texture_registry")
            
            thumbnails_data[target_id] = tex_tag
        
            if not dpg.does_item_exist(img_tag) and dpg.does_item_exist(container_tag):
                if dpg.does_item_exist(loading_tag): 
                    dpg.delete_item(loading_tag)
                dpg.add_image(texture_tag=tex_tag, tag=img_tag, width=110, height=80, parent=container_tag)
            
                with dpg.popup(img_tag, mousebutton=dpg.mvMouseButton_Right):
                    dpg.add_menu_item(label="Regenerate Thumbnail (Random)", callback=regen_thumb_callback, user_data=target_id)
                
            for r, track in enumerate(tracks_data):
                if track.get("target_id") == target_id:
                    update_track_slot_ui(r)
            for p in monitor_players:
                if p.get("target_id") == target_id:
                    update_monitor_player_ui(p["id"])

        if viosc_client:
            current_time = time.time()
            for idx, props in global_vimix_state.get("sources", {}).items():
                name = props.get("name")
                uri = props.get("uri")
                target_id = str(name) if name else str(idx)
                    
                if uri and target_id not in thumbnails_data:
                    last_thumb = request_timestamps.get(f"thumb_{target_id}", 0)
                    if current_time - last_thumb > 3.0:
                        msg_addr = f"/viosc/thumb/{target_id}"
                        viosc_client.send_message(msg_addr, ["all"])
                        append_log("OUT", msg_addr)
                        request_timestamps[f"thumb_{target_id}"] = current_time

        # monitor players: cleanup closed windows and refresh values
        for p in list(monitor_players):
            if not dpg.does_item_exist(p["tag"]):
                if p.get("target_id"):
                    addr = f"/viosc/monitor/{p['target_id']}"
                    osc_client.send_message(addr, [])
                    append_log("OUT", f"{addr} (stop)")
                monitor_players.remove(p)
                continue
            refresh_monitor_player_values(p["id"])

        dpg.render_dearpygui_frame()
        time.sleep(0.016)
finally:
    # L-4 clean exit: stop audio, shut down the OSC server, destroy the context
    if audio_stream is not None:
        try:
            audio_stream.stop()
            audio_stream.close()
        except Exception:
            pass
    if local_osc_server is not None:
        try:
            local_osc_server.shutdown()
        except Exception:
            pass
    dpg.destroy_context()
