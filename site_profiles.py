# -*- coding: utf-8 -*-
"""站点 Profile —— 语义导航的站点知识与回退链。

设计原则
--------
1. 只描述「语义」：ARIA role、accessible name、label、placeholder、少量稳定
   data-testid / id。不依赖 hash 化的 CSS 类名（网站改版后 class 最先变）。
2. 每个站点有独立的区域定义（TOPBAR/SIDEBAR/MAIN/COMPOSER/DIALOG/PLAYER/SEARCH），
   Profile 只负责「这个站点把这些语义控件放在哪个区域」。
3. 选择器全部是 CSS；JS 侧用 `el.closest(sel)` 做区域归属，复杂度 O(区域数)。
4. 匹配失败时返回 generic profile（仍启用 role 白名单 + 几何区域），
   保证新站点/改版站点不会退化成「全 DOM 导航」。

数据结构
--------
profile = {
  id, name, url_patterns, default_zone,
  zones: {ZONE: [css, ...]}, zone_priority: [...],
  link_zones: [...], allow_selectors: [...], deny_selectors: [...],
  composer: {zone, input, order:[{name, selectors}], ok},
  dialog: [css, ...],
  scroll_ok: bool,
}
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

ZONES = ("TOPBAR", "SIDEBAR", "MAIN", "COMPOSER", "DIALOG", "PLAYER", "SEARCH")

# 通用角色白名单（与 JS 侧保持一致；a 默认不在内）
ROLE_WHITELIST = (
    "button", "tab", "textbox", "searchbox", "combobox", "menuitem",
    "menuitemcheckbox", "menuitemradio", "checkbox", "radio", "switch",
    "slider", "option", "link",
)

_COMMON_DIALOG = [
    "[role=dialog]:not([aria-hidden=true])",
    "dialog[open]",
    "[aria-modal=true]",
    "[data-testid=dialog]",
    "[data-testid=modal]",
    ".modal.show",
]

_GENERIC = {
    "id": "generic",
    "name": "通用网页",
    "url_patterns": [],
    "default_zone": "MAIN",
    "zones": {
        "TOPBAR": ["header", "[role=banner]", "#masthead", ".topbar", ".top-bar"],
        "SIDEBAR": ["nav", "[role=navigation]", "aside", "[role=complementary]"],
        "MAIN": ["main", "[role=main]", "article", "#content", "#main"],
        "COMPOSER": [
            "form[role=search]", "[role=search]",
            "form:has(textarea)", "form:has([contenteditable=true])",
        ],
        "DIALOG": _COMMON_DIALOG,
        "PLAYER": ["video", "[role=application]", ".player", ".video-player"],
        "SEARCH": [
            # 只认搜索"控件"本身, 不要整个 form —— 必应的 form 把热搜、推荐图
            # 全包在里面, 用 form 当区域会把首页内容全划进 SEARCH, MAIN 变空。
            "input[type=search]", "[role=searchbox]",
            "[role=search] > input", "[role=search] > textarea",
            "form[action*=search] input", "form[action*=search] textarea",
            "form[action*=search] button[type=submit]",
        ],
    },
    "zone_priority": ["DIALOG", "COMPOSER", "SEARCH", "PLAYER", "TOPBAR", "SIDEBAR", "MAIN"],
    "link_zones": ["TOPBAR", "SIDEBAR"],
    # 内容卡片整卡选择器: 命中的容器整块算一个候选(按 OK 打开),
    # 卡片内部的小字/频道/菜单一律不收。简单控制: 只选大目标。
    "card_selectors": [],
    "allow_selectors": ["[role=tab]", "[role=menuitem]", "[role=option]"],
    "deny_selectors": [
        # 只留「确定不是控件」的: 隐藏/禁用/广告容器。
        # 不要放 pre / code / [class*=advert] 这一类 ——
        #   · pre/code 是正文排版标签, 放进来会把正文里所有行内链接连带拒掉
        #     (Google 结果页有 173 个元素因此被误杀, 搜索结果一个都进不了候选)
        #   · [class*=advert] 是模糊匹配, 很容易撞上正常容器的类名
        "[aria-hidden=true]", "[hidden]", "[inert]", "[disabled]",
        ".ad", ".ads", ".advert", ".advertisement",
        "[data-ad]", "[data-testid*=ad-]",
    ],
    "composer": {
        "zone": "COMPOSER",
        "input": ["textarea", "[contenteditable=true]", "[role=textbox]"],
        "order": [
            {"name": "附件", "selectors": [
                "button[aria-label*='Attach' i]", "button[aria-label*='附件']",
                "button[aria-label*='上传']", "button[title*='附件']",
                "button[title*='上传' i]", "button[title*='Attach' i]"]},
            {"name": "工具", "selectors": [
                "button[aria-label*='Tool' i]", "button[aria-label*='工具']",
                "button[title*='工具']", "button[title*='Tool' i]"]},
            {"name": "输入框", "selectors": ["textarea", "[contenteditable=true]",
                                            "[role=textbox]", "[role=searchbox]"]},
            {"name": "语音", "selectors": [
                "button[aria-label*='Voice' i]", "button[aria-label*='Mic' i]",
                "button[aria-label*='语音']", "button[title*='语音']",
                "button[title*='Voice' i]"]},
            {"name": "发送", "selectors": [
                "button[aria-label*='Send' i]", "button[aria-label*='发送']",
                "button[title*='发送']", "button[title*='Send' i]",
                "button[type=submit]"]},
        ],
        "ok": "enter",
    },
    "dialog": _COMMON_DIALOG,
    "scroll_ok": True,
}


def _p(**kw) -> Dict[str, Any]:
    """基于 generic 叠加站点差异，避免每个 Profile 重复样板。"""
    p = {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in _GENERIC.items()}
    for k, v in kw.items():
        if k in ("zones", "composer") and isinstance(v, dict):
            merged = {kk: (list(vv) if isinstance(vv, list) else vv) for kk, vv in p[k].items()}
            merged.update(v)
            p[k] = merged
        elif k == "composer" and isinstance(v, dict):
            p[k] = {**p[k], **v}
        else:
            p[k] = v
    return p


PROFILES: List[Dict[str, Any]] = [
    _p(
        id="chatgpt",
        name="ChatGPT",
        url_patterns=[r"(^|[./])chatgpt\.com", r"(^|[./])chat\.openai\.com"],
        default_zone="COMPOSER",
        zones={
            "TOPBAR": ["header", "[role=banner]", "[data-testid*=topbar]", "[data-testid='navigation-header']",
                       "a[href='/']", "button[aria-label*='New chat' i]"],
            "SIDEBAR": [
                "nav[aria-label*='Chat' i]", "nav[aria-label*='对话']",
                "#stage-slideover-sidebar", "nav[aria-label*='History' i]",
            ],
            "MAIN": ["main", "[role=main]", "#thread", "[data-testid*=conversation]"],
            "COMPOSER": [
                "form[data-type=unified-composer]", "#thread-bottom",
                "[data-testid=composer]", "form:has(#prompt-textarea)",
                "form:has([data-testid=send-button])", "div[class*='composer']",
            ],
            "SEARCH": ["[role=search]", "form[role=search]"],
        },
        zone_priority=["DIALOG", "COMPOSER", "SEARCH", "TOPBAR", "SIDEBAR", "MAIN"],
        # MAIN 不收链接: 对话内容里的引用链接不是遥控目标
        link_zones=["TOPBAR", "SIDEBAR"],
        deny_selectors=_GENERIC["deny_selectors"] + [
            "#thread a",
            # 对话气泡里的操作按钮(复制/反馈/引用)不是用户想遥控到的控件
            "[data-message-id] button", "[data-message-author-role] button",
            "article button:not([data-testid=send-button]):not([data-testid=composer-plus-btn])",
            "button[aria-label*='Copy' i]", "button[aria-label*='复制' i]",
            "button[aria-label*='Good' i]", "button[aria-label*='Bad' i]",
            "button[aria-label*='Read aloud' i]", "button[aria-label*='朗读' i]",
        ],
        composer={
            "zone": "COMPOSER",
            "input": ["#prompt-textarea", "[data-testid=prompt-textarea]",
                      "form [contenteditable=true]", "form textarea"],
            "order": [
                {"name": "附件", "selectors": ["button[aria-label*='Attach' i]", "button[aria-label*='附件']",
                                              "button[aria-label*='Upload' i]", "input[type=file]"]},
                {"name": "工具", "selectors": ["button[data-testid=composer-plus-btn]",
                                              "button[aria-label*='Tool' i]", "button[aria-label*='工具']"]},
                {"name": "输入框", "selectors": ["#prompt-textarea", "[data-testid=prompt-textarea]",
                                                "form [contenteditable=true]", "form textarea"]},
                {"name": "语音", "selectors": ["button[aria-label*='Dictate' i]", "button[aria-label*='Voice' i]",
                                              "button[aria-label*='语音']"]},
                {"name": "发送", "selectors": ["button[data-testid=send-button]", "button[aria-label*='Send' i]",
                                              "button[aria-label*='发送']", "form button[type=submit]"]},
            ],
            "ok": "enter",
        },
    ),
    _p(
        id="gemini",
        name="Gemini",
        url_patterns=[r"(^|[./])gemini\.google\.com"],
        default_zone="COMPOSER",
        zones={
            "TOPBAR": ["header", "[role=banner]", "top-bar", ".top-bar"],
            "SIDEBAR": ["bard-sidenav", "nav", "[role=navigation]", "side-navigation"],
            "MAIN": ["main", "[role=main]", "chat-window", ".conversation-container"],
            "COMPOSER": ["input-container", "rich-textarea", "[data-test-id*=composer]",
                         "form:has(rich-textarea)", ".input-area-container", ".chat-input-container"],
            "SEARCH": ["[role=search]", "search-area"],
        },
        zone_priority=["DIALOG", "COMPOSER", "SEARCH", "TOPBAR", "SIDEBAR", "MAIN"],
        # MAIN 不收链接: 回答内容里的引用链接不是遥控目标
        link_zones=["TOPBAR", "SIDEBAR"],
        deny_selectors=_GENERIC["deny_selectors"] + [
            "model-response a", "message-content a",
            # 回答气泡里的反馈/复制按钮
            "model-response button", "message-content button",
            "button[aria-label*='Copy' i]", "button[aria-label*='复制' i]",
            "button[aria-label*='Good' i]", "button[aria-label*='Bad' i]",
            "button[aria-label*='Share' i]", "button[aria-label*='分享' i]",
        ],
        composer={
            "zone": "COMPOSER",
            "input": ["rich-textarea [contenteditable=true]", "rich-textarea .ql-editor",
                      "div[contenteditable=true][role=textbox]", "textarea"],
            "order": [
                {"name": "附件", "selectors": ["button[aria-label*='Add' i]", "button[aria-label*='Upload' i]",
                                              "button[aria-label*='添加']", "button[aria-label*='上传']"]},
                {"name": "工具", "selectors": ["button[aria-label*='Tool' i]", "button[aria-label*='工具']",
                                              "button[aria-label*='More' i]"]},
                {"name": "输入框", "selectors": ["rich-textarea [contenteditable=true]", "rich-textarea .ql-editor",
                                                "div[contenteditable=true][role=textbox]", "textarea"]},
                {"name": "语音", "selectors": ["button[aria-label*='Mic' i]", "button[aria-label*='Voice' i]",
                                              "button[aria-label*='麦克风']"]},
                {"name": "发送", "selectors": ["button[aria-label*='Send' i]", "button[aria-label*='发送']",
                                              "button.send-button", "button[aria-label*='Submit' i]"]},
            ],
            "ok": "enter",
        },
    ),
    _p(
        id="x",
        name="X",
        url_patterns=[r"(^|[./])x\.com", r"(^|[./])twitter\.com"],
        # 默认落点直接在侧栏/标签栏(Home/Explore/Notifications), 避免直接冲入充满推文的信息流
        default_zone="SIDEBAR",
        zones={
            "TOPBAR": ["header[role=banner]", "header"],
            "SIDEBAR": ["nav[aria-label*='Primary' i]", "header nav[role=navigation]",
                        "[data-testid=SideNav_AccountSwitcher_Button]"],
            "MAIN": ["main", "[role=main]", "[data-testid=primaryColumn]"],
            "COMPOSER": ["[data-testid=tweetTextarea_0]", "[data-testid=dmComposerTextInput]",
                         "[data-testid=toolBar]", "[role=dialog] [data-testid=tweetTextarea_0]"],
            "SEARCH": ["[data-testid=SearchBox_Search_Input]", "form[role=search]"],
        },
        zone_priority=["DIALOG", "SIDEBAR", "SEARCH", "COMPOSER", "TOPBAR", "MAIN"],
        # MAIN 不收链接: timeline 推文链接不是遥控目标
        link_zones=["TOPBAR", "SIDEBAR"],
        allow_selectors=[
            "[role=tab]",
            "[data-testid=AppTabBar_Home_Link]",
            "[data-testid=AppTabBar_Explore_Link]",
            "[data-testid=AppTabBar_Notifications_Link]",
            "[data-testid=AppTabBar_DirectMessage_Link]",
            "[data-testid=SideNav_NewTweet_Button]",
        ],
        deny_selectors=_GENERIC["deny_selectors"] + [
            "a[href*='/hashtag/']",
            # timeline 推文里的点赞/转发/评论/分享按钮(发推按钮除外)
            "article[data-testid=tweet] button:not([data-testid=tweetButtonInline])",
            "[data-testid=cellInnerDiv] a",
            "[data-testid=UserAvatar-Container]",
            "[data-testid=caret]",
            "[data-testid=like]", "[data-testid=retweet]", "[data-testid=reply]",
        ],
        composer={
            "zone": "COMPOSER",
            "input": ["[data-testid=tweetTextarea_0]", "[data-testid=dmComposerTextInput]",
                      "[contenteditable=true][role=textbox]"],
            "order": [
                {"name": "媒体", "selectors": ["[data-testid=attachments]", "button[aria-label*='Media' i]",
                                              "button[aria-label*='媒体']"]},
                {"name": "GIF", "selectors": ["button[aria-label*='GIF' i]"]},
                {"name": "投票", "selectors": ["button[aria-label*='Poll' i]", "button[aria-label*='投票']"]},
                {"name": "表情", "selectors": ["button[aria-label*='Emoji' i]", "button[aria-label*='表情']"]},
                {"name": "输入框", "selectors": ["[data-testid=tweetTextarea_0]",
                                                "[data-testid=dmComposerTextInput]",
                                                "[contenteditable=true][role=textbox]"]},
                {"name": "发送", "selectors": ["[data-testid=tweetButtonInline]", "[data-testid=tweetButton]",
                                              "button[aria-label*='Post' i]", "button[aria-label*='回复']"]},
            ],
            "ok": "enter",
        },
    ),
    _p(
        id="youtube",
        name="YouTube",
        url_patterns=[r"(^|[./])youtube\.com", r"(^|[./])youtu\.be"],
        default_zone="SEARCH",
        zones={
            "TOPBAR": ["#masthead", "ytd-masthead", "header"],
            "SIDEBAR": ["#guide", "ytd-guide-renderer", "nav", "[role=navigation]"],
            "MAIN": ["#primary", "ytd-watch-flexy #primary", "main", "[role=main]"],
            "COMPOSER": [],
            "PLAYER": ["#movie_player", ".html5-video-player", "ytd-player", "#player"],
            "SEARCH": ["ytd-searchbox", "form[role=search]", "input#search"],
        },
        zone_priority=["DIALOG", "SEARCH", "PLAYER", "TOPBAR", "SIDEBAR", "MAIN"],
        # MAIN 不收链接: 视频卡片/推荐链接不是遥控目标
        link_zones=["TOPBAR", "SIDEBAR"],
        # 视频整卡一个候选: 方向键在卡与卡之间走, 不进卡内小字
        card_selectors=["ytd-rich-item-renderer", "ytd-video-renderer",
                        "ytd-compact-video-renderer", "ytd-grid-video-renderer"],
        allow_selectors=["yt-chip-cloud-chip-renderer", "button[aria-label*='Search' i]"],
        deny_selectors=_GENERIC["deny_selectors"] + [
            "ytd-comment-thread-renderer a", "#comments a",
            # 视频卡片链接与卡片浮层操作
            "ytd-rich-item-renderer a", "ytd-video-renderer a",
            "ytd-compact-video-renderer a", "ytd-thumbnail",
            "ytd-menu-renderer button",
            "button[aria-label*='Watch later' i]", "button[aria-label*='稍后再看' i]",
            "button[aria-label*='Add to queue' i]", "button[aria-label*='添加到播放列表' i]",
        ],
        composer={
            "zone": "SEARCH",
            "input": ["input#search", "ytd-searchbox input", "input[name=search_query]"],
            "order": [
                {"name": "搜索框", "selectors": ["input#search", "ytd-searchbox input", "input[name=search_query]"]},
                {"name": "搜索按钮", "selectors": ["button#search-icon-legacy", "button[aria-label*='Search' i]"]},
            ],
            "ok": "enter",
        },
    ),
    _p(
        id="google",
        name="Google Search",
        url_patterns=[r"(^|[./])google\.[a-z.]+"],
        default_zone="SEARCH",
        zones={
            "TOPBAR": ["#gb", "#searchform", "header"],
            "SIDEBAR": [ "#gb", "nav", "[role=navigation]"],
            "MAIN": ["#center_col", "#rcnt", "main", "[role=main]"],
            "COMPOSER": [],
            "SEARCH": ["#searchform", "form[role=search]", "textarea[name=q]", "input[name=q]"],
        },
        zone_priority=["DIALOG", "SEARCH", "TOPBAR", "SIDEBAR", "MAIN"],
        # 搜索结果本身就是链接, MAIN 必须允许链接, 否则整页只剩几个小图标可导航
        link_zones=["TOPBAR", "SIDEBAR", "MAIN"],
        deny_selectors=_GENERIC["deny_selectors"] + [
            # 排除底部版权/帮助、翻译此页、快照等杂项
            "#fbar a", ".fbar a", "a[href*='support.google.com']",
            "a[href*='policies.google.com']", "a[href*='webcache.googleusercontent.com']",
        ],
        composer={
            "zone": "SEARCH",
            "input": ["textarea[name=q]", "input[name=q]", "input[type=search]"],
            "order": [
                {"name": "搜索框", "selectors": ["textarea[name=q]", "input[name=q]", "input[type=search]"]},
                {"name": "语音搜索", "selectors": ["div[aria-label*='Search by voice' i]", "button[aria-label*='voice' i]"]},
                {"name": "搜索按钮", "selectors": ["button[aria-label*='Search' i]", "input[type=submit]"]},
            ],
            "ok": "enter",
        },
    ),
    _p(
        id="bilibili",
        name="哔哩哔哩",
        url_patterns=[r"(^|[./])bilibili\.com", r"(^|[./])b23\.tv"],
        default_zone="SEARCH",
        zones={
            "TOPBAR": [".bili-header", "#biliHeader", "header"],
            "SIDEBAR": [".bili-header__bar", "nav", "[role=navigation]", ".nav-mask"],
            "MAIN": ["#app", ".main-container", "main", "[role=main]"],
            "COMPOSER": ["#comment", ".comment-send", ".reply-box", ".bili-comment-box"],
            "PLAYER": [".bpx-player-container", "#bilibili-player", ".bilibili-player",
                       ".bpx-player-video-area"],
            "SEARCH": [".nav-search", ".bili-header .nav-search", "form[role=search]",
                       "input.nav-search-input"],
        },
        zone_priority=["DIALOG", "SEARCH", "PLAYER", "COMPOSER", "TOPBAR", "SIDEBAR", "MAIN"],
        # MAIN 不收链接: 视频卡片链接不是遥控目标
        link_zones=["TOPBAR", "SIDEBAR"],
        # 视频整卡一个候选: 方向键在卡与卡之间走, 不进卡内小字
        card_selectors=[".bili-video-card", ".video-card", ".feed-card"],
        deny_selectors=_GENERIC["deny_selectors"] + [
            ".reply-item a", ".comment a",
            # 视频卡片和评论区操作按钮
            ".bili-video-card a", ".video-card a", ".feed-card a",
            ".reply-item button",
            "button.bili-video-card__stats--watch-later", ".watch-later-video",
            "button[aria-label*='稍后再看']", ".carousel-indicators",
        ],
        composer={
            "zone": "COMPOSER",
            "input": [".comment-send [contenteditable=true]", ".reply-box textarea",
                      "#comment [contenteditable=true]", "textarea"],
            "order": [
                {"name": "表情", "selectors": ["button[aria-label*='表情']", ".emoji-btn"]},
                {"name": "输入框", "selectors": [".comment-send [contenteditable=true]", ".reply-box textarea",
                                                "#comment [contenteditable=true]", "textarea"]},
                {"name": "发送", "selectors": ["button[class*=send]", "button[aria-label*='发布']",
                                              "button[aria-label*='发送']"]},
            ],
            "ok": "enter",
        },
    ),
    _p(
        id="douyin",
        name="抖音",
        url_patterns=[r"(^|[./])douyin\.com", r"(^|[./])iesdouyin\.com"],
        # 默认进视频流(MAIN), 不进搜索框——进门落在搜索框会被编辑态吞方向键(真机实证)
        default_zone="MAIN",
        zones={
            "TOPBAR": ["header", "[role=banner]", ".header"],
            "SIDEBAR": ["nav", "[role=navigation]", ".sidebar"],
            "MAIN": ["main", "[role=main]", "#main-content", ".main-content"],
            "COMPOSER": ["[data-e2e=comment-input]", ".comment-input", ".comment-editor"],
            "PLAYER": ["xg-video-container", "[data-e2e=video-player]", ".xgplayer",
                       ".video-player", "[data-e2e=feed-active-video]"],
            "SEARCH": ["[data-e2e=searchbar]", "input[data-e2e=searchbar-input]",
                       "form[role=search]", "input[placeholder*='搜索']"],
        },
        zone_priority=["DIALOG", "SEARCH", "PLAYER", "COMPOSER", "TOPBAR", "SIDEBAR", "MAIN"],
        # MAIN 不收链接: 视频卡片链接不是遥控目标
        link_zones=["TOPBAR", "SIDEBAR"],
        # 视频整卡一个候选: 方向键在卡与卡之间走, 不进卡内小字
        card_selectors=["[data-e2e=video-card]", ".recommend-video"],
        deny_selectors=_GENERIC["deny_selectors"] + [
            ".comment a",
            # 推荐流视频卡片
            "[data-e2e=video-card] a", ".recommend-video a",
            "a[href*='/user/']", "a[href*='/music/']",
        ],
        composer={
            "zone": "COMPOSER",
            "input": ["[data-e2e=comment-input] [contenteditable=true]", ".comment-input [contenteditable=true]",
                      ".comment-editor [contenteditable=true]", "textarea"],
            "order": [
                {"name": "表情", "selectors": ["button[aria-label*='表情']", "[data-e2e=emoji]"]},
                {"name": "输入框", "selectors": ["[data-e2e=comment-input] [contenteditable=true]",
                                                ".comment-input [contenteditable=true]",
                                                ".comment-editor [contenteditable=true]", "textarea"]},
                {"name": "发送", "selectors": ["[data-e2e=comment-publish]", "button[aria-label*='发送']",
                                              "button[class*=send]"]},
            ],
            "ok": "enter",
        },
    ),
]

_BY_ID = {p["id"]: p for p in PROFILES}


def match_profile(url: str = "", title: str = "") -> Dict[str, Any]:
    """URL/标题 -> Profile。URL 优先；标题只在前 160 字符里做弱匹配。"""
    u = (url or "").lower()
    t = (title or "").lower()[:160]
    for p in PROFILES:
        for pat in p["url_patterns"]:
            if u and re.search(pat, u):
                return p
    # 标题回退：站点名出现且 URL 为空（某些浏览器 UIA 读不到地址栏时）
    if not u and t:
        for p in PROFILES:
            if p["id"] == "generic":
                continue
            if p["name"].lower() in t:
                return p
    return _GENERIC


def get_profile(pid: str) -> Dict[str, Any]:
    return _BY_ID.get(pid, _GENERIC)


def generic_profile() -> Dict[str, Any]:
    return _GENERIC


def profile_for_js(profile: Dict[str, Any]) -> Dict[str, Any]:
    """裁掉 Python 端专用字段，给页面内 JS 的配置。"""
    return {
        "id": profile.get("id") or "generic",
        "name": profile.get("name") or "",
        "defaultZone": profile.get("default_zone") or "MAIN",
        "zones": profile.get("zones") or {},
        "zonePriority": profile.get("zone_priority") or list(ZONES),
        "linkZones": profile.get("link_zones") or [],
        "cardSelectors": profile.get("card_selectors") or [],
        "allowSelectors": profile.get("allow_selectors") or [],
        "denySelectors": profile.get("deny_selectors") or [],
        "composer": profile.get("composer") or {},
        "dialog": profile.get("dialog") or _COMMON_DIALOG,
        "scrollOk": bool(profile.get("scroll_ok", True)),
    }


def describe(profile: Dict[str, Any]) -> str:
    return f"{profile.get('name') or profile.get('id')}({profile.get('id')})"


def zone_css(profile: Dict[str, Any], zone: str) -> str:
    sels = (profile.get("zones") or {}).get(zone) or []
    return ",".join(sels) if sels else ""


def composer_order(profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    c = profile.get("composer") or {}
    return list(c.get("order") or [])


def composer_input_selectors(profile: Dict[str, Any]) -> List[str]:
    c = profile.get("composer") or {}
    return list(c.get("input") or [])


def site_key(url: str) -> str:
    """站点身份键：host（不含端口/路径），用于焦点记忆与 Profile 缓存。"""
    v = (url or "").strip().lower()
    if not v:
        return ""
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#:]+)", v)
    if not m:
        m = re.match(r"^([^/?#:]+)", v)
    return m.group(1) if m else ""


def page_key(url: str) -> str:
    """页面身份键：host + path（去掉 query/hash），用于刷新/换页检测。"""
    v = (url or "").strip().lower()
    if not v:
        return ""
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]+)([^?#]*)", v)
    if not m:
        m = re.match(r"^([^/?#]+)([^?#]*)", v)
    if not m:
        return ""
    return (m.group(1) + (m.group(2) or "")).rstrip("/")
