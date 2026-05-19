from pathlib import Path

TIMEOUT_DOCKER = 120
WORK_DIR_DOCKER = str(Path(__file__).resolve().parent.parent / "tmp")
MODEL_NAME = "gpt-4o"