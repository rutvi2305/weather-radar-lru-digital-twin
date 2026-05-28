import sys, os, time, threading
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
from flask_cors import CORS

import backend.database as db
from core.arinc429         import ARINC429Bus
from simulation.live_radar import LiveRadarEngine
from faults.fault_injector import FaultInjector, FaultType

BASE_DIR = os.path.join(os.path.dirname(__file__), '..', 'visualization')
app      = Flask(__name__, static_folder=BASE_DIR, static_url_path='')
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

bus    = ARINC429Bus(speed="high")
fi     = FaultInjector()
engine = LiveRadarEngine(bus, fi)
session_id = None

@app.route('/')
def index():
    return send_from_directory(BASE_DIR, 'dashboard.html')

@app.route('/api/cell', methods=['POST'])
def add_cell():
    data = request.json
    try:
        cid = db.upsert_cell(
            az       = float(data['azimuth_deg']),
            rng      = float(data['range_nm']),
            dbz      = float(data['dbz']),
            width    = float(data.get('width_deg', 15.0)),
            alt      = float(data.get('altitude_ft', 25000.0)),
            name     = data.get('cell_name', 'Cell'),
            added_by = data.get('added_by', 'operator'),
            cell_id  = data.get('id'),
        )
        cells = db.get_active_cells()
        engine.update_cells(cells)
        socketio.emit('db_update', {
            'type': 'cell_added', 'cell_id': cid, 'cells': cells,
            'message': f"Cell added by {data.get('added_by','operator')}"
        })
        return jsonify({'ok': True, 'id': cid, 'cells': cells})
    except (KeyError, ValueError) as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

@app.route('/api/cell/<int:cell_id>', methods=['DELETE'])
def remove_cell(cell_id):
    db.deactivate_cell(cell_id)
    cells = db.get_active_cells()
    engine.update_cells(cells)
    socketio.emit('db_update', {
        'type': 'cell_removed', 'cell_id': cell_id, 'cells': cells,
        'message': f"Cell #{cell_id} removed"
    })
    return jsonify({'ok': True, 'cells': cells})

@app.route('/api/aircraft', methods=['POST'])
def set_aircraft():
    data = request.json
    try:
        db.save_aircraft_params(
            heading  = float(data['heading_deg']),
            pitch    = float(data.get('pitch_deg', 0.0)),
            roll     = float(data.get('roll_deg', 0.0)),
            altitude = float(data.get('altitude_ft', 35000.0)),
            airspeed = float(data.get('airspeed_kt', 480.0)),
        )
        engine.update_aircraft(data)
        socketio.emit('db_update', {'type': 'aircraft_updated', 'params': data})
        return jsonify({'ok': True})
    except (KeyError, ValueError) as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

@app.route('/api/controls', methods=['POST'])
def set_controls():
    data = request.json
    try:
        db.save_pilot_controls(
            mode      = data.get('mode', 'WX'),
            tilt      = float(data.get('tilt_deg', 0.0)),
            gain      = int(data.get('gain', 65)),
            range_nm  = int(data.get('range_nm', 160)),
            auto_tilt = bool(data.get('auto_tilt', True)),
        )
        engine.update_controls(data)
        socketio.emit('db_update', {'type': 'controls_updated', 'controls': data})
        return jsonify({'ok': True})
    except (KeyError, ValueError) as e:
        return jsonify({'ok': False, 'error': str(e)}), 400

FAULT_MAP = {
    'BIT_FLIP':      FaultType.BIT_FLIP,
    'WORD_DROPOUT':  FaultType.WORD_DROPOUT,
    'LATENCY':       FaultType.LATENCY,
    'PHANTOM_ECHO':  FaultType.PHANTOM_ECHO,
    'ANTENNA_STUCK': FaultType.ANTENNA_STUCK,
    'MODE_MISMATCH': FaultType.MODE_MISMATCH,
}

@app.route('/api/fault', methods=['POST'])
def toggle_fault():
    data   = request.json
    fname  = data.get('fault_type', '').upper()
    action = data.get('action', 'activate').lower()
    who    = data.get('triggered_by', 'operator')
    ft = FAULT_MAP.get(fname)
    if not ft:
        return jsonify({'ok': False, 'error': f'Unknown fault: {fname}'}), 400
    if action == 'activate':
        fi.activate(ft)
    else:
        fi.deactivate(ft)
    db.log_fault_event(fname, action, who, data.get('details', ''))
    active = [f.name for f in fi.active_faults]
    socketio.emit('fault_event', {
        'fault_type': fname, 'action': action,
        'triggered_by': who, 'active_faults': active,
        'timestamp': time.time(),
    })
    return jsonify({'ok': True, 'active_faults': active})

@app.route('/api/state', methods=['GET'])
def get_state():
    snap = engine.get_snapshot()
    snap['active_faults'] = [f.name for f in fi.active_faults]
    snap['cells']         = db.get_active_cells()
    snap['aircraft']      = db.get_latest_aircraft_params()
    snap['controls']      = db.get_latest_pilot_controls()
    snap['arinc_stats']   = db.get_arinc_stats(session_id)
    return jsonify(snap)

@app.route('/api/history/cells')
def history_cells():
    return jsonify(db.get_all_cells_history())

@app.route('/api/history/faults')
def history_faults():
    return jsonify(db.get_fault_history())

@app.route('/api/history/arinc')
def history_arinc():
    limit = int(request.args.get('limit', 50))
    return jsonify(db.get_recent_arinc_words(limit))

@app.route('/api/history/sessions')
def history_sessions():
    return jsonify(db.get_all_sessions())

@socketio.on('connect')
def on_connect():
    snap = engine.get_snapshot()
    snap['cells']         = db.get_active_cells()
    snap['aircraft']      = db.get_latest_aircraft_params()
    snap['controls']      = db.get_latest_pilot_controls()
    snap['active_faults'] = [f.name for f in fi.active_faults]
    snap['arinc_stats']   = db.get_arinc_stats(session_id)
    emit('full_state', snap)
    print("[WS] Client connected")

@socketio.on('disconnect')
def on_disconnect():
    print("[WS] Client disconnected")

def radar_push_loop():
    global session_id
    while True:
        time.sleep(0.1)
        try:
            words = engine.drain_word_queue()
            for w in words:
                db.log_arinc_word(
                    label_octal  = f"{w['label']:03o}",
                    label_name   = w['label_name'],
                    raw_hex      = w['hex'],
                    raw_binary   = w['binary'],
                    sdi          = w['sdi'],
                    data_field   = w['data'],
                    ssm          = w['ssm'],
                    parity_valid = not w['corrupt'],
                    corrupt      = w['corrupt'],
                    session_id   = session_id,
                )
                socketio.emit('arinc_word', w)
            snap = engine.get_snapshot()
            snap['active_faults'] = [f.name for f in fi.active_faults]
            snap['arinc_stats']   = db.get_arinc_stats(session_id)
            socketio.emit('radar_sweep', snap)
        except Exception as e:
            print(f"[PUSH] Error: {e}")

def start_server(host='127.0.0.1', port=5000, debug=False):
    global session_id
    db.init_db()
    db.seed_defaults()
    cells    = db.get_active_cells()
    aircraft = db.get_latest_aircraft_params()
    controls = db.get_latest_pilot_controls()
    engine.update_cells(cells)
    engine.update_aircraft(aircraft)
    engine.update_controls(controls)
    bus.start()
    engine.start()
    session_id = db.start_session("Auto-started on server launch")
    print(f"[DB] Bus session #{session_id} started")
    t = threading.Thread(target=radar_push_loop, daemon=True)
    t.start()
    print(f"\n{'='*55}")
    print(f"  Weather Radar LRU Digital Twin — LIVE SERVER")
    print(f"{'='*55}")
    print(f"  Open your browser at:  http://{host}:{port}")
    print(f"  Database file:         data/radar_twin.db")
    print(f"  Press Ctrl+C to stop")
    print(f"{'='*55}\n")
    socketio.run(app, host=host, port=port, debug=debug, use_reloader=False)

if __name__ == '__main__':
    start_server()