import sqlite3
import os
import time
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'radar_twin.db')

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS weather_cells (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            cell_name    TEXT    NOT NULL DEFAULT 'Cell',
            azimuth_deg  REAL    NOT NULL,
            range_nm     REAL    NOT NULL,
            dbz          REAL    NOT NULL,
            width_deg    REAL    NOT NULL DEFAULT 15.0,
            altitude_ft  REAL    NOT NULL DEFAULT 25000.0,
            active       INTEGER NOT NULL DEFAULT 1,
            added_by     TEXT    NOT NULL DEFAULT 'operator',
            created_at   REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
            updated_at   REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec'))
        );
        CREATE TABLE IF NOT EXISTS aircraft_params (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            heading_deg  REAL    NOT NULL DEFAULT 45.0,
            pitch_deg    REAL    NOT NULL DEFAULT 0.0,
            roll_deg     REAL    NOT NULL DEFAULT 0.0,
            altitude_ft  REAL    NOT NULL DEFAULT 35000.0,
            airspeed_kt  REAL    NOT NULL DEFAULT 480.0,
            updated_at   REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec'))
        );
        CREATE TABLE IF NOT EXISTS pilot_controls (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            mode         TEXT    NOT NULL DEFAULT 'WX',
            tilt_deg     REAL    NOT NULL DEFAULT 0.0,
            gain         INTEGER NOT NULL DEFAULT 65,
            range_nm     INTEGER NOT NULL DEFAULT 160,
            auto_tilt    INTEGER NOT NULL DEFAULT 1,
            updated_at   REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec'))
        );
        CREATE TABLE IF NOT EXISTS fault_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            fault_type   TEXT    NOT NULL,
            action       TEXT    NOT NULL,
            triggered_by TEXT    NOT NULL DEFAULT 'operator',
            details      TEXT,
            created_at   REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec'))
        );
        CREATE TABLE IF NOT EXISTS arinc_words (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            label_octal  TEXT    NOT NULL,
            label_name   TEXT    NOT NULL,
            raw_hex      TEXT    NOT NULL,
            raw_binary   TEXT    NOT NULL,
            sdi          INTEGER NOT NULL,
            data_field   INTEGER NOT NULL,
            ssm          TEXT    NOT NULL,
            parity_valid INTEGER NOT NULL,
            corrupt      INTEGER NOT NULL DEFAULT 0,
            session_id   INTEGER,
            created_at   REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec'))
        );
        CREATE TABLE IF NOT EXISTS bus_sessions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
            stopped_at    REAL,
            total_words   INTEGER DEFAULT 0,
            corrupt_words INTEGER DEFAULT 0,
            notes         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_arinc_session ON arinc_words(session_id);
        CREATE INDEX IF NOT EXISTS idx_arinc_created ON arinc_words(created_at);
        CREATE INDEX IF NOT EXISTS idx_cells_active  ON weather_cells(active);
        CREATE INDEX IF NOT EXISTS idx_faults_time   ON fault_events(created_at);
        """)
    print(f"[DB] Initialised at {DB_PATH}")

def upsert_cell(az, rng, dbz, width=15.0, alt=25000.0,
                name="Cell", added_by="operator", cell_id=None):
    with get_db() as conn:
        if cell_id:
            conn.execute("""
                UPDATE weather_cells
                SET azimuth_deg=?, range_nm=?, dbz=?, width_deg=?,
                    altitude_ft=?, cell_name=?, added_by=?, updated_at=?
                WHERE id=?
            """, (az, rng, dbz, width, alt, name, added_by, time.time(), cell_id))
            return cell_id
        else:
            cur = conn.execute("""
                INSERT INTO weather_cells
                (azimuth_deg, range_nm, dbz, width_deg, altitude_ft,
                 cell_name, added_by, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, (az, rng, dbz, width, alt, name, added_by, time.time(), time.time()))
            return cur.lastrowid

def get_active_cells():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM weather_cells WHERE active=1
            ORDER BY azimuth_deg
        """).fetchall()
    return [dict(r) for r in rows]

def deactivate_cell(cell_id):
    with get_db() as conn:
        conn.execute("""
            UPDATE weather_cells SET active=0, updated_at=?
            WHERE id=?
        """, (time.time(), cell_id))

def get_all_cells_history():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM weather_cells ORDER BY created_at DESC LIMIT 200
        """).fetchall()
    return [dict(r) for r in rows]

def save_aircraft_params(heading, pitch, roll, altitude=35000, airspeed=480):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO aircraft_params
            (heading_deg, pitch_deg, roll_deg, altitude_ft, airspeed_kt, updated_at)
            VALUES (?,?,?,?,?,?)
        """, (heading, pitch, roll, altitude, airspeed, time.time()))

def get_latest_aircraft_params():
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM aircraft_params ORDER BY updated_at DESC LIMIT 1
        """).fetchone()
    if row:
        return dict(row)
    return {"heading_deg": 45.0, "pitch_deg": 0.0, "roll_deg": 0.0,
            "altitude_ft": 35000.0, "airspeed_kt": 480.0}

def save_pilot_controls(mode, tilt, gain, range_nm, auto_tilt=True):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO pilot_controls
            (mode, tilt_deg, gain, range_nm, auto_tilt, updated_at)
            VALUES (?,?,?,?,?,?)
        """, (mode, tilt, gain, range_nm, int(auto_tilt), time.time()))

def get_latest_pilot_controls():
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM pilot_controls ORDER BY updated_at DESC LIMIT 1
        """).fetchone()
    if row:
        return dict(row)
    return {"mode": "WX", "tilt_deg": 0.0, "gain": 65,
            "range_nm": 160, "auto_tilt": 1}

def log_fault_event(fault_type, action, triggered_by="operator", details=""):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO fault_events
            (fault_type, action, triggered_by, details, created_at)
            VALUES (?,?,?,?,?)
        """, (fault_type, action, triggered_by, details, time.time()))

def get_fault_history(limit=100):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM fault_events ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]

def log_arinc_word(label_octal, label_name, raw_hex, raw_binary,
                   sdi, data_field, ssm, parity_valid,
                   corrupt=False, session_id=None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO arinc_words
            (label_octal, label_name, raw_hex, raw_binary, sdi,
             data_field, ssm, parity_valid, corrupt, session_id, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (label_octal, label_name, raw_hex, raw_binary, sdi,
              data_field, ssm, int(parity_valid), int(corrupt),
              session_id, time.time()))

def get_recent_arinc_words(limit=50, session_id=None):
    with get_db() as conn:
        if session_id:
            rows = conn.execute("""
                SELECT * FROM arinc_words WHERE session_id=?
                ORDER BY created_at DESC LIMIT ?
            """, (session_id, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT * FROM arinc_words
                ORDER BY created_at DESC LIMIT ?
            """, (limit,)).fetchall()
    return [dict(r) for r in rows]

def get_arinc_stats(session_id=None):
    with get_db() as conn:
        if session_id:
            row = conn.execute("""
                SELECT COUNT(*) as total, SUM(corrupt) as corrupt_count
                FROM arinc_words WHERE session_id=?
            """, (session_id,)).fetchone()
        else:
            row = conn.execute("""
                SELECT COUNT(*) as total, SUM(corrupt) as corrupt_count
                FROM arinc_words
            """).fetchone()
    if row:
        total = row["total"] or 0
        corrupt = row["corrupt_count"] or 0
        return {"total": total, "corrupt": corrupt,
                "clean": total - corrupt,
                "corrupt_pct": round(100 * corrupt / total, 1) if total else 0}
    return {"total": 0, "corrupt": 0, "clean": 0, "corrupt_pct": 0}

def start_session(notes=""):
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO bus_sessions (started_at, notes)
            VALUES (?, ?)
        """, (time.time(), notes))
        return cur.lastrowid

def end_session(session_id, total_words, corrupt_words):
    with get_db() as conn:
        conn.execute("""
            UPDATE bus_sessions
            SET stopped_at=?, total_words=?, corrupt_words=?
            WHERE id=?
        """, (time.time(), total_words, corrupt_words, session_id))

def get_all_sessions():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM bus_sessions ORDER BY started_at DESC
        """).fetchall()
    return [dict(r) for r in rows]

def seed_defaults():
    with get_db() as conn:
        if not conn.execute("SELECT 1 FROM aircraft_params LIMIT 1").fetchone():
            conn.execute("""
                INSERT INTO aircraft_params
                (heading_deg, pitch_deg, roll_deg, altitude_ft, airspeed_kt, updated_at)
                VALUES (45.0, 0.0, 0.0, 35000.0, 480.0, ?)
            """, (time.time(),))
        if not conn.execute("SELECT 1 FROM pilot_controls LIMIT 1").fetchone():
            conn.execute("""
                INSERT INTO pilot_controls
                (mode, tilt_deg, gain, range_nm, auto_tilt, updated_at)
                VALUES ('WX', 0.0, 65, 160, 1, ?)
            """, (time.time(),))
    print("[DB] Default state seeded.")