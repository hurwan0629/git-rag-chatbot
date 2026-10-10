"""저장된 모든 repo/run을 서버 없이 조회하는 단일 HTML을 생성한다."""

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'repo_ingestion'

from .config import OUTPUT_DIR, safe_error
from .storage import atomic_bytes, read_files, read_jsonl


def build_report(output=OUTPUT_DIR):
    output = Path(output)
    runs, warnings = [], []
    for path in sorted(output.glob('*/*/manifest.json'), reverse=True):
        try:
            manifest = json.loads(path.read_text(encoding='utf-8'))
            if manifest.get('schema_version') != '1.0':
                raise ValueError('지원하지 않는 schema version')
            folder = path.parent
            runs.append(dict(manifest=manifest,
                             files=read_files(folder / 'files.csv'),
                             documents=read_jsonl(folder / 'documents.jsonl'),
                             chunks=read_jsonl(folder / 'chunks.jsonl')))
        except (ValueError, OSError, KeyError) as error:
            warnings.append(str(path.relative_to(output)) + ': ' + safe_error(error))
    # JSON script의 종료 태그·HTML 문자·Unicode line separator를 안전하게 직렬화한다.
    data = json.dumps(dict(runs=runs, warnings=warnings), ensure_ascii=False)
    for character, escape in [('<', '\\u003c'), ('>', '\\u003e'), ('&', '\\u0026'),
                              ('\u2028', '\\u2028'), ('\u2029', '\\u2029')]:
        data = data.replace(character, escape)
    template = Path(__file__).with_name('report_template.html').read_text(encoding='utf-8')
    atomic_bytes(output / 'index.html', template.replace('__REPORT_DATA__', data).encode('utf-8'))
    return output / 'index.html', warnings


def main():
    parser = argparse.ArgumentParser(description='기존 output/으로 단일 HTML을 다시 만듭니다. API·모델 호출은 없습니다.')
    parser.parse_args()
    try:
        path, warnings = build_report()
        print(f'HTML: {path}')
        for warning in warnings:
            print('주의: ' + warning, file=sys.stderr)
        return 1 if warnings else 0
    except Exception as error:
        print('HTML 생성 실패: ' + safe_error(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
