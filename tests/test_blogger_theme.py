import xml.etree.ElementTree as ET

import pytest

from autopost.blogger_theme import NON_POST_CSP, improve_theme


def source():
    return """<html xmlns:b='http://www.google.com/2005/gml/b' xmlns:data='http://www.google.com/2005/gml/data'>
<head><title><data:view.title.escaped/></title>
<b:include data='blog' name='all-head-content'/>
<b:skin><![CDATA[body{}]]></b:skin>
<b:if cond='(data:widgets.AdSense.first or data:blog.adsenseClientId) and not data:blog.adsenseAutoAds'>
<script src='//pagead2.googlesyndication.com/pagead/js/adsbygoogle.js'/></b:if>
</head><body><header></header>
<b:widget id='AdSense1' visible='false'></b:widget><b:widget id='AdSense2' visible='false'></b:widget>
<b:widget cond='!data:view.isPost' id='PageList1'></b:widget>
<b:widget id='Label1' locked='false' title='태그' type='Label' visible='true'></b:widget>
<b:include name='super.defaultAdUnit'/><b:include data='post' name='super.inlineAd'/>
</body></html>"""


def test_non_post_policy_precedes_automatic_ads_but_leaves_native_metadata_and_post_ads():
    changed = improve_theme(source())
    parsed = ET.fromstring(changed)
    ns = {'b': 'http://www.google.com/2005/gml/b'}
    policy = parsed.find('.//meta')
    assert policy.attrib['content'] == NON_POST_CSP
    assert parsed.find('.//b:if', ns).attrib['cond'] == 'not data:view.isPost or data:view.isPreview'
    assert changed.index('Content-Security-Policy') < changed.index("name='all-head-content'")
    # Inline and default ad-unit rendering must also be gated, not just the loader.
    for include in parsed.findall('.//b:include', ns):
        if include.attrib.get('name') in {'super.defaultAdUnit', 'super.inlineAd'}:
            assert include.attrib['cond'] == 'data:view.isPost and not data:view.isPreview'
    assert 'googlesyndication.com' not in NON_POST_CSP
    assert "id='PageList1'" in changed and "cond='!data:view.isPost'" not in changed
    assert "type='Label' visible='false'" in changed
    assert "name='all-head-content'" in changed
    assert "aria-label='블로그 안내'" in changed


def test_refuse_repatch_and_unknown_or_enabled_ad_widgets():
    with pytest.raises(ValueError):
        improve_theme(improve_theme(source()))
    with pytest.raises(ValueError):
        improve_theme(source().replace("id='AdSense1' visible='false'", "id='AdSense1' visible='true'"))
    with pytest.raises(ValueError):
        improve_theme(source().replace("id='PageList1'", "id='PageList9'"))
