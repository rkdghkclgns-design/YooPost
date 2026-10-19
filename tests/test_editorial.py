import json

import pytest

from autopost.content import parse_post
from autopost.editorial import editorial_problems
from fakes import post_dict


A = "공식 가격표의 입력 단위가 같아도 캐시 적용 여부에 따라 비교 결과가 달라지므로 이 조건을 먼저 확인해야 합니다."
B = "팀에서 외부 전송을 허용하지 않는다면 비용보다 보관 조건을 먼저 확인하고 조건이 맞지 않으면 도입을 보류하는 판단 기준입니다."


def sample():
    post = post_dict()
    post['sections'] = [
        {'heading': '⚖️ 조건 비교', 'blocks': [{'type': 'paragraph', 'text': A}]},
        {'heading': '🔍 선택 기준', 'blocks': [{'type': 'paragraph', 'text': B}]},
    ]
    content = parse_post(json.dumps(post, ensure_ascii=False))
    research = {'sources': [{'id': 'S1', 'url': 'https://example.com/a'}, {'id': 'S2', 'url': 'https://example.com/b'}],
                'editorial': {'standard_version': 1, 'reader_question': '팀에서 어떤 조건을 먼저 확인해야 하나요?',
                              'limitations': '실측하지 않았으며 제공 조건은 변경될 수 있습니다.',
                              'contributions': [
                                  {'kind': 'comparison', 'section': '⚖️ 조건 비교', 'excerpt': A,
                                   'source_ids': ['S1'], 'added_value': '수치 비교 전에 맞춰야 할 조건'},
                                  {'kind': 'decision_guide', 'section': '🔍 선택 기준', 'excerpt': B,
                                   'source_ids': ['S2'], 'added_value': '도입을 보류해야 할 조건'},
                              ]}}
    return content, research


def test_manifest_traces_two_distinct_contributions_to_actual_sections():
    content, research = sample()
    assert editorial_problems(content, research, '20261021') == []
    # Formatting differences must not detach the evidence from the text.
    research['editorial']['contributions'][0]['excerpt'] = A.replace('공식', '**공식**')
    assert editorial_problems(content, research, '20261021') == []


@pytest.mark.parametrize('change', ['absent_text', 'wrong_section', 'unknown_source', 'duplicate_kind', 'no_limit', 'bad_type'])
def test_declarations_without_traceable_value_fail(change):
    content, research = sample()
    items = research['editorial']['contributions']
    if change == 'absent_text':
        items[0]['excerpt'] = '이 문장은 본문에 존재하지 않는 추가 가치라고 주장하는 문장으로 충분한 길이만 가진 예시입니다.'
    elif change == 'wrong_section':
        items[0]['section'] = '🔍 선택 기준'
    elif change == 'unknown_source':
        items[0]['source_ids'] = ['S99']
    elif change == 'duplicate_kind':
        items[1]['kind'] = 'comparison'
    elif change == 'no_limit':
        research['editorial']['limitations'] = ''
    else:
        items[0]['kind'] = []
    assert editorial_problems(content, research, '20261021')


def test_synthesis_requires_two_distinct_urls_not_two_ids_for_the_same_source():
    content, research = sample()
    research['editorial']['contributions'][0].update(kind='source_synthesis', source_ids=['S1', 'S2'])
    assert not editorial_problems(content, research, '20261021')
    research['sources'][1]['url'] = 'https://example.com/a#part'
    assert any('서로 다른 출처' in p for p in editorial_problems(content, research, '20261021'))


def test_cutover_does_not_block_in_flight_legacy_drafts_but_new_day_cannot_omit_record():
    content, research = sample()
    del research['editorial']
    assert editorial_problems(content, research, '20261020') == []
    assert editorial_problems(content, research, '20261021')
    # A version declaration is checked immediately, even before the cutoff.
    research['editorial'] = {'standard_version': 1}
    assert editorial_problems(content, research, '20261020')
