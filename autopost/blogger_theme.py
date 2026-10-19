"""Backed-up Contempo theme changes; refuse unknown or already-patched inputs."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

NON_POST_CSP = "script-src 'self' 'unsafe-inline' https://www.gstatic.com https://resources.blogblog.com https://www.blogger.com;"


def improve_theme(source: str) -> str:
    if "yp-non-post-ad-policy" in source:
        raise ValueError("Theme already patched; back up the current version before another change")
    def once(old: str, new: str) -> None:
        nonlocal source
        if source.count(old) != 1:
            raise ValueError(f"Unexpected theme structure: {old[:80]}")
        source = source.replace(old, new, 1)
    once("<title><data:view.title.escaped/></title>", """<b:if cond='not data:view.isPost or data:view.isPreview'>
      <meta content=""" + '"' + NON_POST_CSP.replace("'", "&#39;") + '"' + """ http-equiv='Content-Security-Policy' id='yp-non-post-ad-policy'/>
    </b:if>
    <title><data:view.title.escaped/></title>""")
    once("<b:if cond='(data:widgets.AdSense.first or data:blog.adsenseClientId) and not data:blog.adsenseAutoAds'>",
         "<b:if cond='data:view.isPost and not data:view.isPreview and (data:widgets.AdSense.first or data:blog.adsenseClientId) and not data:blog.adsenseAutoAds'>")
    # Keep native SEO metadata, ownership markers and the Blogger-hosted account.
    # Both are disabled in the backup; their obsolete settings prevent any theme save.
    for name in ("AdSense1", "AdSense2"):
        pattern = rf"<b:widget id='{name}'[^>]*visible='false'>[\s\S]*?</b:widget>"
        source, count = re.subn(pattern, "", source)
        if count != 1:
            raise ValueError(f"Expected one disabled widget: {name}")
    source = source.replace("<b:include name='super.defaultAdUnit'/>",
                            "<b:include cond='data:view.isPost and not data:view.isPreview' name='super.defaultAdUnit'/>")
    source = source.replace("<b:include data='post' name='super.inlineAd'/>",
                            "<b:include cond='data:view.isPost and not data:view.isPreview' data='post' name='super.inlineAd'/>")
    once("<b:widget cond='!data:view.isPost' id='PageList1'", "<b:widget id='PageList1'")
    once("<b:widget id='Label1' locked='false' title='태그' type='Label' visible='true'>",
         "<b:widget id='Label1' locked='false' title='태그' type='Label' visible='false'>")
    guide = """
            <nav aria-label='블로그 안내' class='yp-site-guide'>
              <span>공식 근거로 읽는 AI의 변화와 선택 기준</span>
              <a href='/'>홈</a>
              <a href='/p/about.html'>소개·편집 기준</a>
              <a href='/p/contact.html'>문의·정정 요청</a>
              <a href='/p/privacy-policy.html'>개인정보처리방침</a>
            </nav>"""
    once("</header>", "</header>" + guide)
    css = """
.yp-site-guide{display:flex;flex-wrap:wrap;align-items:center;gap:10px 18px;padding:16px 24px;margin:0 0 20px;background:#f4f6fb;border:1px solid #e0e5ef;border-radius:10px;font-size:13px;line-height:1.6}
.yp-site-guide span{flex:1 1 260px;color:#414b59}
.yp-site-guide a{color:#244f9d;text-decoration:none;white-space:nowrap}
.yp-site-guide a:hover,.yp-site-guide a:focus{text-decoration:underline}
"""
    once("]]></b:skin>", css + "]]></b:skin>")
    ET.fromstring(source)  # XML syntax check before uploading through Blogger UI.
    return source
