"""선택한 날의 YooPost 발행 글에 노출된 Markdown 링크를 같은 글에서 보정한다.

    python scripts/repair_inline_links.py --day 20261013          # 변경 계획·백업
    python scripts/repair_inline_links.py --day 20261013 --apply  # 본문만 수정
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from autopost.blog_publisher import BloggerPublisher  # noqa: E402
from autopost.cli import http_session  # noqa: E402
from autopost.config import load_config  # noqa: E402
from autopost.draft_store import DraftStore  # noqa: E402
from autopost.google_auth import build_blogger_service, build_credentials  # noqa: E402
from autopost.history import read_marker  # noqa: E402
from autopost.inline_repair import repair_html_links  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--day', required=True, help='원고 작성일 YYYYMMDD')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    datetime.strptime(args.day, '%Y%m%d')
    load_dotenv(ROOT / '.env')
    cfg = load_config()
    service = build_blogger_service(build_credentials(cfg.google))
    blogger = BloggerPublisher(service, cfg.blogger_blog_id)
    store = DraftStore(cfg.image_host_url, cfg.image_upload_token, http_session())
    output = ROOT / 'output' / 'inline_link_repairs' / datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for post in blogger.editable_posts():
        original = str(post.get('content') or '')
        draft_id = str(read_marker(original).get('draft_id') or '')
        if not draft_id.startswith(args.day + '_'):
            continue
        repaired = repair_html_links(original)
        if not repaired.changes:
            continue
        post_id, url = str(post['id']), str(post.get('url') or '')
        row = {'draft_id': draft_id, 'post_id': post_id, 'url': url, 'links': repaired.changes,
               'before_sha256': hashlib.sha256(original.encode()).hexdigest(), 'applied': False}
        (output / f'{draft_id}.before.html').write_text(original, encoding='utf-8')
        (output / f'{draft_id}.after.html').write_text(repaired.html, encoding='utf-8')
        if args.apply:
            current = service.posts().get(blogId=cfg.blogger_blog_id, postId=post_id).execute(num_retries=2)
            if current.get('content') != original:
                raise RuntimeError(f'조회 후 본문이 바뀌어 수정 중단: {draft_id}')
            blogger.update_content(post_id, repaired.html)
            verified = service.posts().get(blogId=cfg.blogger_blog_id, postId=post_id).execute(num_retries=2)
            if verified.get('content') != repaired.html or verified.get('url') != url:
                raise RuntimeError(f'본문·주소 수정 결과 확인 실패: {draft_id}')
            record = store.get(draft_id)
            if record.get('status') == 'published' and record.get('url') == url:
                stored = repair_html_links(str(record.get('html') or ''))
                if stored.changes:
                    store.put(draft_id, {**record, 'html': stored.html})
            row.update(applied=True, api_verified=True)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False))
    (output / 'report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'day': args.day, 'mode': 'apply' if args.apply else 'preview',
                      'posts': len(results), 'backup': str(output)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
