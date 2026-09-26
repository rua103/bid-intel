"""Local Chinese OCR with reusable models and content-addressed results."""
from __future__ import annotations

import hashlib
import io
import json
import threading
from functools import lru_cache

from filelock import FileLock
from PIL import Image

from app.config import settings

_lock = threading.Lock()


@lru_cache(maxsize=1)
def engine():
    from rapidocr import RapidOCR
    return RapidOCR(params={
        'Global.log_level': 'error',
        'EngineConfig.onnxruntime.intra_op_num_threads': 2,
        'EngineConfig.onnxruntime.inter_op_num_threads': 1,
    })


def recognize(content: bytes) -> tuple[str, list[str]]:
    root = settings.resolved_database_path.parent / 'ocr-cache'
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(b'rapidocr-3.9.2-v1' + content).hexdigest()
    path = root / (key + '.json')
    with FileLock(str(path.with_suffix('.lock')), timeout=300):
        if path.exists():
            try:
                result = json.loads(path.read_text(encoding='utf-8'))
                return result['text'], result['warnings']
            except (ValueError, KeyError):
                pass
        with Image.open(io.BytesIO(content)) as image:
            image.load()
            image = image.convert('RGB')
            image.thumbnail((2400, 2400))
            with _lock:
                result = engine()(image)
        rows = []
        boxes = []
        if result.txts:
            for box, text, score in zip(result.boxes, result.txts, result.scores, strict=True):
                if score < 0.5:
                    continue
                boxes.append((float(box[:, 1].mean()), float(box[:, 0].min()),
                              float(box[:, 1].max() - box[:, 1].min()), text))
        for y, x, height, text in sorted(boxes):
            if rows and abs(rows[-1][0] - y) <= max(5, height * 0.5):
                rows[-1][1].append((x, text))
            else:
                rows.append((y, [(x, text)]))
        text = '\n'.join('\t'.join(value for _, value in sorted(row)) for _, row in rows)
        warnings = ['本地 RapidOCR 已识别，文本和表格需人工核验']
        if not text:
            warnings.append('OCR 未识别到可信文本')
        result = {'text': text, 'warnings': warnings}
        pending = path.with_suffix('.pending')
        pending.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        pending.replace(path)
        return text, warnings
