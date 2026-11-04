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
parser.add_argument("--ecg", action="store_true", help="TEST ONLY: known 60 bpm ECG, nSeq and EDA")
args = parser.parse_args()
name = "OpenSignals-SYNTHETIC-ECG" if args.ecg else "OpenSignals-SYNTHETIC-TEST"
count = 3 if args.ecg else 6
info = StreamInfo(name, "NOT-A-REAL-DEVICE", count, 1000,
                  "float32", "synthetic-" + uuid.uuid4().hex)
channels = info.desc().append_child("channels")
for index in range(count):
    channel = channels.append_child("channel")
    channel.append_child_value("label", ("nSeq", "ECGBIT0", "EDA")[index] if args.ecg else f"SYNTHETIC_{index+1}")
    channel.append_child_value("unit", ("", "mV", "uS")[index] if args.ecg else "arbitrary")
outlet = StreamOutlet(info, chunk_size=10)
print(f"TEST ONLY: {name}, 1000 Hz, {count} channels" + (", ECG 60 bpm" if args.ecg else ""), flush=True)
anchor = local_clock()
index = 0
try:
    while local_clock() - anchor < args.seconds:
        now = local_clock()
        while anchor + index / 1000 <= now:
            if args.ecg:
                t = index/1000
                phase = (t-.6+.5) % 1 - .5
                ecg = (math.exp(-.5*(phase/.012)**2) - .20*math.exp(-.5*((phase-.025)/.01)**2)
                       + .25*math.exp(-.5*((phase-.22)/.045)**2) + .02*math.sin(2*math.pi*.3*t))
                values = [index % 16, ecg, 5 + .01*math.sin(t/10)]
            else:
                values = [math.sin(index / (10 + c)) for c in range(6)]
            outlet.push_sample(values, anchor + index / 1000)
            index += 1
        time.sleep(.002)
finally:
    print("Synthetic stream stopped.", flush=True)
