"""
main.py — Weather Radar LRU Digital Twin (Live Edition)
---------------------------------------------------------
Run this file to start everything:
  python main.py

Then open your browser at:
  http://localhost:5000
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from backend.server import start_server

if __name__ == '__main__':
  start_server(host='0.0.0.0', port=5000, debug=False)