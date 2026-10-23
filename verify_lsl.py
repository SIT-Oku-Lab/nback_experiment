"""Optional local LSL round-trip check; does not connect to BITalino."""
import json
from pathlib import Path
import sys
import time

root = Path(__file__).resolve().parent
if (root / ".build_deps").exists():
    sys.path.insert(0, str(root / ".build_deps"))
from pylsl import StreamInlet, resolve_byprop
from engine import Config
from storage import Recorder

rec = Recorder(root / "verification_results", Config(participant="LSL_CHECK", mode="demo", lsl=True))
try:
    streams = resolve_byprop("source_id", rec.session_id, timeout=5)
    assert len(streams) == 1, "Marker stream was not discovered"
    inlet = StreamInlet(streams[0])
    inlet.open_stream(timeout=5)
    rec.event("roundtrip_check", trial=3, position=5)
    sample, stamp = inlet.pull_sample(timeout=5)
    assert sample, "No marker received"
    payload = json.loads(sample[0])
    assert payload["event"] == "roundtrip_check", payload
    assert payload["trial"] == 3 and payload["position"] == 5
    assert payload["session_id"] == rec.session_id
    journal = [json.loads(line) for line in (rec.path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    expected = next(r for r in journal if r["event"] == "roundtrip_check")["lsl_timestamp"]
    assert abs(stamp - expected) < .001, (stamp, expected)
    rec.finish("completed")
    report = {"result": "passed", "stream": "NBackMarkers", "session_id": rec.session_id,
              "timestamp_difference_ms": (stamp - expected) * 1000,
              "scope": "local software loopback; not BITalino/OpenSignals/LabRecorder hardware validation"}
    (root / "verification_results" / "lsl_roundtrip.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
finally:
    if not rec.closed:
        rec.finish("error", "roundtrip test failed")
