"""이미지 빌드 단계에서 로컬 임베딩 모델을 고정 리비전으로 내려받는다.

앱 코드를 import하지 않는다(설정 환경변수 없이 실행). 모델 명세는 app/services/answering/local_model.json
하나만 기준으로 삼으므로 앱이 DB에 기록하는 모델 리비전과 실제 파일이 어긋나지 않는다.
사용: python download_embedding_model.py <local_model.json> <대상 디렉터리>
"""
import json
import sys
from pathlib import Path

from huggingface_hub import snapshot_download

spec = json.loads(Path(sys.argv[1]).read_text())
target = Path(sys.argv[2])
target.mkdir(parents=True, exist_ok=True)
snapshot_download(repo_id=spec["model_name"], revision=spec["revision"], allow_patterns=spec["files"], local_dir=str(target))
missing = [name for name in spec["files"] if not (target / name).is_file()]
if missing:
    sys.exit(f"missing model files: {missing}")
(target / "MODEL_REVISION").write_text(f"{spec['model_name']}@{spec['revision']}\n")
print(f"{spec['model_name']}@{spec['revision']} -> {target}")
