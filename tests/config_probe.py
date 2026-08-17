"""별도 프로세스에서 봇의 환경설정 초기화 결과를 JSON으로 출력한다."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mafia_bot import DATA_DIR, DEV_PASSWORD, PROJECT_DIR, TELEGRAM_TOKEN

print(
    json.dumps(
        {
            "project_dir": str(PROJECT_DIR),
            "token": TELEGRAM_TOKEN,
            "password": DEV_PASSWORD,
            "data_dir": str(DATA_DIR),
        }
    )
)
