# lab — WhisperX experiments

Throwaway environment for evaluating transcription settings on real recordings before the CLI is written.

Setup (Windows, RTX 5090 / CUDA 12.8):

```
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe whisperx
# whisperx pulls CPU torch; replace with CUDA builds matching its torch~=2.8 constraint
uv pip install --python .venv/Scripts/python.exe --reinstall torch==2.8.0 torchaudio==2.8.0 torchvision==0.23.0 --index-url https://download.pytorch.org/whl/cu128
```

`requirements-lab.txt` is the resulting freeze. `samples/` and `out/` are git-ignored.
Diarization needs `HF_TOKEN` and accepted terms for `pyannote/speaker-diarization-3.1` and `pyannote/segmentation-3.0`.
