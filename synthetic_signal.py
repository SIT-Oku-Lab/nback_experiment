"""TEST ONLY: advertise a clearly labelled synthetic stream for GUI validation."""
import argparse
import math
from pathlib import Path
import sys
import time
import uuid

deps = Path(__file__).resolve().parent / ".build_deps"
if deps.exists():
    sys.path.insert(0, str(deps))
from pylsl import StreamInfo, StreamOutlet, local_clock

parser = argparse.ArgumentParser(description="TEST ONLY synthetic LSL; not a physiological device")
parser.add_argument("--seconds", type=float, default=180)
args = parser.parse_args()
info = StreamInfo("OpenSignals-SYNTHETIC-TEST", "NOT-A-REAL-DEVICE", 6, 1000,
                  "float32", "synthetic-" + uuid.uuid4().hex)
channels = info.desc().append_child("channels")
for index in range(6):
    channel = channels.append_child("channel")
    channel.append_child_value("label", f"SYNTHETIC_{index+1}")
    channel.append_child_value("unit", "arbitrary")
outlet = StreamOutlet(info, chunk_size=10)
print("TEST ONLY: OpenSignals-SYNTHETIC-TEST, 1000 Hz, 6 channels", flush=True)
anchor = local_clock()
index = 0
try:
    while local_clock() - anchor < args.seconds:
        now = local_clock()
        while anchor + index / 1000 <= now:
            outlet.push_sample([math.sin(index / (10 + c)) for c in range(6)], anchor + index / 1000)
            index += 1
        time.sleep(.002)
finally:
    print("Synthetic stream stopped.", flush=True)
