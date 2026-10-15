"""Extract speaker embedding from one WAV. Prints JSON to stdout."""
from __future__ import annotations

import argparse
import json
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", help="16 kHz mono WAV path")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()

    if args.offline:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    cache = os.environ.get("PYANNOTE_CACHE")
    if cache:
        os.environ.setdefault("PYANNOTE_CACHE", cache)

    from pyannote.audio import Inference

    inference = Inference(model="pyannote/wespeaker-voxceleb-resnet34-LM", window="whole")
    result = inference(args.audio)
    vector = [round(float(x), 6) for x in result]
    print(json.dumps({"vector": vector, "dimension": len(vector)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())