/* KZQ Semantic Navigation helper (injected into the page via CDP Runtime.evaluate)
 *
 * 只暴露 window.__KZQ_NAV。所有几何计算在页面内完成，服务端只拿 JSON。
 * 设计约束：
 *   - 候选 = role 白名单 + 可见 + 视口内 + 未被遮挡 + 属于当前区域；
 *   - a 链接默认不参与，只有 Profile 指定 linkZones 或 allowSelectors 才收；
 *   - 每个元素有稳定 key（语义属性 + DOM 路径），刷新后重新绑定；
 *   - 弹窗存在时导航域强制 DIALOG；
 *   - 输入框处于编辑态时由服务端把方向键交还应用，本层不抢。
 */
(function () {
  if (window.__KZQ_NAV && window.__KZQ_NAV.version >= 3) return;

  const VERSION = 3;
  const ID_ATTR = 'data-kzq-nav-id';
  const KEY_PROP = '__kzqKey';
  const ID_PROP = '__kzqId';
  const WL = new Set([
    'button', 'tab', 'textbox', 'searchbox', 'combobox', 'menuitem',
    'menuitemcheckbox', 'menuitemradio', 'checkbox', 'radio', 'switch',
    'slider', 'option', 'link'
  ]);
  let seq = 1;
  const byId = new Map();
  const keyToId = new Map();
  const REJECT = { hidden: 0, viewport: 0, occluded: 0, zone: 0, role: 0, dup: 0 };
  // 最近访问过的元素 id(最多 6 个): 导航时降权, 防止在 3~5 个元素的小圈里打转。
  // YouTube 搜索结果实测: 频道名 / 标题 / 操作菜单 三个元素互相构成"回环",
  // 连按 down 会在它们之间无限循环, 用户体感就是"按了半天没往下走"。
  const RECENT = [];

  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const txt = (el) => norm(el && el.textContent || '').slice(0, 80);

  function qsa(sel) {
    try { return Array.from(document.querySelectorAll(sel)); } catch (e) { return []; }
  }
  function matches(el, sel) {
    if (!sel) return false;
    try { return el.matches(sel); } catch (e) { return false; }
  }
  function closest(el, sel) {
    if (!sel) return null;
    try { return el.closest(sel); } catch (e) { return null; }
  }
  function joined(list) {
    return (list || []).filter(Boolean).join(',');
  }

  function rectOf(el) {
    const r = el.getBoundingClientRect();
    return { l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height };
  }
  const cx = (r) => (r.l + r.r) / 2;
  const cy = (r) => (r.t + r.b) / 2;

  function styleVisible(el, r) {
    if (!el || !el.isConnected) return false;
    if (r.w < 6 || r.h < 6) return false;
    const cs = getComputedStyle(el);
    if (!cs || cs.display === 'none' || cs.visibility === 'hidden' ||
        cs.visibility === 'collapse' || parseFloat(cs.opacity || '1') < 0.05) return false;
    if (cs.pointerEvents === 'none' && r.w * r.h < 400) return false;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
    if (el.matches(':disabled')) return false;
    if (closest(el, '[inert],[aria-hidden="true"]')) return false;
    // contenteditable 的空编辑器可能 1px 高，放宽到 4px；其余保持 6px
    if ((el.isContentEditable || el.tagName === 'TEXTAREA') && r.h < 4) return false;
    return true;
  }

  function inViewport(r) {
    if (r.b <= 0 || r.t >= innerHeight || r.r <= 0 || r.l >= innerWidth) return false;
    // 至少 35% 的面积在视口内，避免只露出 1px 的侧栏残片
    const ix = Math.max(0, Math.min(r.r, innerWidth) - Math.max(r.l, 0));
    const iy = Math.max(0, Math.min(r.b, innerHeight) - Math.max(r.t, 0));
    const area = Math.max(1, r.w * r.h);
    return (ix * iy) / area >= 0.35;
  }

  function covered(el, r) {
    // 完全在视口外的元素没法用 elementFromPoint 判定遮挡(所有采样点都在屏幕外),
    // 直接放行: 遮挡检查只在能看到的范围里有意义。
    if (r.b <= 0 || r.t >= innerHeight) return false;
    const pts = [
      [cx(r), cy(r)],
      [r.l + Math.min(8, r.w / 3), r.t + Math.min(8, r.h / 3)],
      [r.r - Math.min(8, r.w / 3), r.b - Math.min(8, r.h / 3)]
    ];
    let checked = 0;
    let blocked = 0;
    for (const [x, y] of pts) {
      if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
      const top = document.elementFromPoint(x, y);
      checked++;
      if (!top) { blocked++; continue; }
      if (top === el || el.contains(top) || top.contains(el)) continue;
      // <label for> / 透明装饰层：若遮挡物与候选共享最近可交互祖先，视为同一控件
      const a = top.closest && top.closest('button,a,input,textarea,select,[role],[contenteditable="true"]');
      if (a && (a === el || a.contains(el) || el.contains(a))) continue;
      blocked++;
    }
    // 半屏(元素跨视口边界)时只有部分采样点有效: 要求"全部有效采样点都被挡"才算遮挡,
    // 否则跨边界的大块内容会被误杀。
    if (checked === 0) return false;
    return blocked === checked;
  }

  function dialogEl(cfg) {
    const sel = joined(cfg.dialog || []);
    if (!sel) return null;
    let best = null, bestZ = -1, bestArea = 0;
    const vw = innerWidth || 1200, vh = innerHeight || 800;
    for (const el of qsa(sel)) {
      const r = rectOf(el);
      if (!styleVisible(el, r) || !inViewport(r)) continue;
      const cs = getComputedStyle(el);
      const z = parseInt(cs.zIndex || '0', 10) || 0;
      const area = r.w * r.h;
      // 真弹窗的两个硬特征(必应/京东这类站的推广浮层经常带 role=dialog 或
      // .modal 类名, 但尺寸很小、贴在角落, 判成弹窗会把焦点锁死在上面):
      //   1) 有实际尺寸(不是角落小浮标), 且四边都不贴屏幕边缘
      //   2) fixed/absolute 定位, 或显式 aria-modal
      // 面积门槛不能设太高: 站内确认框(380x200 左右)在小屏上占比不到 5%,
      // 门槛过高会把真弹窗漏掉 —— 自己写 fixture 就踩过这个坑。
      const bigEnough = r.w * r.h >= Math.min(vw * vh * 0.03, 60000);
      const centered = r.l > vw * 0.02 && r.r < vw * 0.98 &&
                       r.t > vh * 0.02 && r.b < vh * 0.98;
      const modalish = cs.position === 'fixed' || cs.position === 'absolute' ||
                       (el.getAttribute('aria-modal') === 'true') ||
                       (el.closest('[aria-modal="true"]') !== null);
      if (!(bigEnough && centered && modalish)) continue;
      if (z > bestZ || (z === bestZ && area >= bestArea)) {
        best = el; bestZ = z; bestArea = area;
      }
    }
    return best;
  }

  function roleOf(el) {
    const explicit = (el.getAttribute('role') || '').toLowerCase().trim();
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'button' || tag === 'summary') return 'button';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'select') return 'combobox';
    if (tag === 'a') return 'link';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'search') return 'searchbox';
      if (t === 'button' || t === 'submit' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'range') return 'slider';
      if (t === 'hidden') return '';
      return 'textbox';
    }
    if (el.isContentEditable) return 'textbox';
    return '';
  }

  function nameOf(el, role) {
    const isField = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable;
    const aria = norm(el.getAttribute('aria-label'));
    if (aria) return aria;
    const ph0 = norm(el.getAttribute('placeholder') || el.getAttribute('aria-placeholder') ||
                     el.getAttribute('data-placeholder'));
    const title0 = norm(el.getAttribute('title'));
    // 输入框的 aria-labelledby 经常被页面指到动态内容上（必应搜索框指向热门词容器,
    // 于是控件名变成"特朗普发30多张照片回味APEC"这种随时在变的东西 —— 名字不稳定
    // 就没法做稳定 key, 用户也看不懂"这个按钮叫什么"）。输入框只信静态标注。
    if (!isField) {
      const lab = el.getAttribute('aria-labelledby');
      if (lab) {
        const s = lab.split(/\s+/).map((id) => {
          const n = document.getElementById(id);
          return n ? txt(n) : '';
        }).filter(Boolean).join(' ');
        if (s) return s;
      }
    }
    if (ph0) return ph0;
    if (title0) return title0;
    if (isField && role !== 'button') {
      // 静态标注全空: 用 name/id 兜底, 再不济按类型给个通用名, 绝不返回空
      const nm = norm(el.getAttribute('name') || '');
      if (nm && nm.length <= 20) return nm;
      if (role === 'searchbox') return '搜索框';
      if (role === 'textbox') return '输入框';
      if (role === 'combobox') return '下拉框';
    }
    if (role === 'button' && el.tagName === 'INPUT') return norm(el.value).slice(0, 60);
    const alt = norm(el.getAttribute('alt'));
    if (alt) return alt;
    // 卡片式链接: 整块的 innerText 会把播放量/时长/悬浮按钮文案全拼进来
    // (B 站首页实测: "添加至稍后再看294.1万515529:44")。有标题元素就用标题。
    if (role === 'link' || role === 'button') {
      const head = el.querySelector('h1,h2,h3,h4,[class*=title i],[class*=Title]');
      if (head) {
        const ht = norm(head.innerText || head.textContent || '');
        if (ht) return ht.slice(0, 60);
      }
      // 卡片里的图有 alt 时, alt 往往就是标题
      const img = el.querySelector('img[alt]');
      if (img) {
        const ia = norm(img.getAttribute('alt'));
        if (ia && ia.length >= 2) return ia.slice(0, 60);
      }
    }
    let t2 = txt(el);
    // 名字是裸 URL 时毫无信息量(卡片只放了 a[aria-label=href] 或图标链接),
    // 退回 href 的路径段, 至少能看出是"哪一块"
    if (t2 && /^https?:\/\//i.test(t2)) {
      try {
        const u = new URL(t2);
        const seg = decodeURIComponent(u.pathname).split('/').filter(Boolean).pop() || u.hostname;
        t2 = seg.slice(0, 40);
      } catch (e) { t2 = t2.slice(0, 40); }
    }
    if (t2) return t2;
    if (el.tagName === 'INPUT' && el.type !== 'text' && el.type !== 'search') {
      return norm(el.value).slice(0, 60);
    }
    return '';
  }

  function isEditable(el) {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName.toLowerCase();
    if (tag === 'textarea') return !el.disabled && !el.readOnly;
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      return ['text', 'search', 'email', 'url', 'tel', 'number', 'password'].includes(t) &&
        !el.disabled && !el.readOnly;
    }
    if (el.isContentEditable) return true;
    const role = roleOf(el);
    return (role === 'textbox' || role === 'searchbox') && !el.disabled;
  }

  function domPath(el) {
    const parts = [];
    let n = el;
    let depth = 0;
    while (n && n.nodeType === 1 && depth < 4) {
      let i = 0, s = n;
      while ((s = s.previousElementSibling)) {
        if (s.tagName === n.tagName) i++;
      }
      parts.unshift(n.tagName.toLowerCase() + '[' + i + ']');
      n = n.parentElement;
      depth++;
    }
    return parts.join('>');
  }

  function stableKey(el, zone, role, name) {
    const stable = [
      el.getAttribute('data-testid') || '',
      el.getAttribute('data-e2e') || '',
      el.getAttribute('data-test-id') || '',
      el.id || '',
      el.getAttribute('name') || '',
      el.getAttribute('aria-label') || ''
    ].join('|');
    return [cfgId, zone, role, norm(name).toLowerCase(), stable, domPath(el)].join('|');
  }

  let cfgId = 'generic';

  function bind(el, zone, role, name) {
    let key = el[KEY_PROP];
    if (!key) {
      key = stableKey(el, zone, role, name);
      el[KEY_PROP] = key;
    }
    let id = keyToId.get(key);
    if (!id) {
      id = 'n' + (seq++);
      keyToId.set(key, id);
    }
    el[ID_PROP] = id;
    byId.set(id, el);
    try { el.setAttribute(ID_ATTR, id); } catch (e) { }
    return { id, key };
  }

  // 元素自身往往很窄(顶栏里一个按钮 60px), 用元素自己的矩形判「是不是横跨整屏的
  // 顶栏/侧栏」永远不成立。沿祖先链找承载它的那块条带容器, 用容器矩形 + 元素落在
  // 容器内来判断, 才是用户认知里的「这块在顶栏」。
  function bandHost(el, w, h, axis) {
    let n = el.parentElement;
    let depth = 0;
    let best = null;
    while (n && n.nodeType === 1 && depth < 5) {
      const cs = getComputedStyle(n);
      const r = n.getBoundingClientRect();
      const fixed = cs.position === 'fixed' || cs.position === 'sticky';
      if (axis === 'top') {
        const spans = r.width >= w * 0.55;
        const atTop = r.top <= Math.min(80, h * 0.12);
        const short = r.height <= Math.min(140, h * 0.22);
        if (spans && atTop && short && (fixed || depth <= 2)) best = r;
      } else if (axis === 'bottom') {
        const spans = r.width >= w * 0.45;
        const atBottom = r.bottom >= h - Math.min(90, h * 0.14);
        const short = r.height <= Math.min(180, h * 0.3);
        if (spans && atBottom && short && (fixed || depth <= 2)) best = r;
      } else {
        const tall = r.height >= h * 0.45;
        const atLeft = r.left <= w * 0.06;
        const narrow = r.width <= w * 0.42;
        if (tall && atLeft && narrow && (fixed || depth <= 2)) best = r;
      }
      if (fixed) break;   // 到固定定位的条带就是它了, 不再往上
      n = n.parentElement;
      depth++;
    }
    return best;
  }

  function zoneOf(el, cfg, r) {
    if (dialogElRef && (el === dialogElRef || dialogElRef.contains(el))) return 'DIALOG';
    const priority = cfg.zonePriority || [];
    for (const z of priority) {
      if (z === 'DIALOG') continue;
      // MAIN 是"剩下都是"的兜底区, 不能参与优先级竞争: 必应把整页内容包在
      // [role=main] 里, 让 MAIN 先命中会把搜索条上的话筒、顶栏图标全划进 MAIN,
      // 用户按方向键第一下就落到页面角落。MAIN 只在最后兜底用。
      if (z === 'MAIN') continue;
      const sel = joined((cfg.zones || {})[z]);
      if (!sel) continue;
      if (closest(el, sel)) return z;
    }
    // 语义兜底：没有 Profile 区域命中的新站点，用几何 + 角色推断
    const h = innerHeight || 800;
    const w = innerWidth || 1200;
    const isEditing = isEditable(el);
    // 搜索语义优先于位置: 顶栏里的搜索框归 SEARCH 区, 否则用户想搜索得先切区域
    if (isEditing) {
      const nm = norm(nameOf(el) || '').toLowerCase();
      const hint = ((el.type || '') + ' ' + (el.getAttribute('role') || '') + ' ' +
        (el.getAttribute('placeholder') || '') + ' ' + nm).toLowerCase();
      if (/search|搜索|查找|query/.test(hint)) return 'SEARCH';
    }
    // 搜索框旁边的辅助控件(话筒/清空/搜索按钮)也归 SEARCH: 它们视觉上属于搜索条,
    // 判成 MAIN 会让 MAIN 的首落点跑到搜索条上的话筒, 用户按方向键第一下就懵。
    try {
      const sb = el.closest('form[action*=search],[role=search],.b_searchboxForm,#sb_form');
      if (sb && !el.matches('a[href]')) {
        const sbr = sb.getBoundingClientRect();
        if (r.t >= sbr.top - 8 && r.b <= sbr.bottom + 8) return 'SEARCH';
      }
    } catch (e) { }
    // 输入区优先: 可编辑控件在屏幕下三分之一就是回复框, 别被 sticky 顶栏抢走。
    // 必须同时要求在视口内: 视口外元素的 r.t 动辄上千, 会被误判成"屏幕底部"
    // (Google 结果页把整页推下去的长问答按钮全被划进 COMPOSER 就是这么来的)。
    const inVp = r.t >= 0 && r.t < h;
    if (isEditing && inVp && r.t > h * 0.5) return 'COMPOSER';
    const topBand = bandHost(el, w, h, 'top');
    if (topBand && r.t < topBand.bottom) return 'TOPBAR';
    const sideBand = bandHost(el, w, h, 'side');
    if (sideBand && r.l < sideBand.right) return 'SIDEBAR';
    // 底部固定的回复条: 附件/工具/语音/发送都在里面, 只认 textarea 会把按钮漏到 MAIN。
    // 必须要求元素本身在视口里: 视口外的页脚(必应首页备案号链接 t=875 / 视口 865)
    // 的 bottom 也"贴近底部", 会被误判成回复框。
    const bottomBand = bandHost(el, w, h, 'bottom');
    if (bottomBand && inVp && r.b > bottomBand.top) return 'COMPOSER';
    // 兜底: 视口下部的表单元素。同样必须要求元素在视口内 —— 必应首页把整个页面
    // 包在一个 <form> 里, 页脚的备案号链接(t=875, 视口 865)会被误判成回复框按钮。
    if (inVp && r.t > h * 0.62 && r.w > w * 0.28 && el.closest('form')) return 'COMPOSER';
    return 'MAIN';
  }

  let dialogElRef = null;

  function groupOf(el, cfg) {
    const order = ((cfg.composer || {}).order) || [];
    for (let i = 0; i < order.length; i++) {
      const sel = joined((order[i].selectors) || []);
      if (!sel) continue;
      if (matches(el, sel)) return { group: order[i].name, groupIndex: i };
      const inner = closest(el, sel);
      if (inner && inner !== el && (el.contains(inner) || inner.contains(el))) {
        return { group: order[i].name, groupIndex: i };
      }
    }
    return { group: '', groupIndex: -1 };
  }

  // 链接能不能当导航目标。
  // 规范要求排除「正文链接、推荐位、评论区」，但真站(必应首页)的可点内容几乎全是
  // <a>：热搜、图片卡、导航项都是链接。之前一刀切「只在 TOPBAR/SIDEBAR 收链接」，
  // 结果整页只剩 3 个边角按钮可导航 —— 等于不可用。
  // 正确做法: 顶栏/侧栏无条件收; 其它区域按"长得像独立可点块"判:
  //   有实体尺寸(不是正文里的一段内联文字) + 有可读文本 + 自身是块级容器或带按钮语义
  function linkAllowed(el, cfg, zone) {
    if (zone && (cfg.linkZones || []).includes(zone)) return true;
    const sel = joined(cfg.allowSelectors || []);
    if (sel && matches(el, sel)) return true;
    const r = rectOf(el);
    // 正文内联链接: 很矮(小于两行文本)且窄小 —— 这是"段落里的一个蓝字", 不是控件
    const inlineLike = r.h < 22 && r.w < 120;
    const hasText = norm(el.innerText || '').length > 0;
    if (!hasText) return false;
    if (inlineLike) return false;
    // 正文容器里的小链接也不收(文章正文、评论)
    if (closest(el, 'p,li>p,blockquote,.comment,.comments,.reply,article p')) {
      if (r.h < 30) return false;
    }
    return true;
  }

  // 页脚/角落辅助信息: 备案号、版权、反馈、壁纸下载这类东西不是导航目标,
  // 但它们长度和位置都像链接, 会挤进候选把真正的热搜/结果压下去。
  // (必应首页实测: MAIN 17 个候选里 8 个是这种东西, 用户按方向键总落空)
  function isNoise(el, r, name, zone) {
    const h = innerHeight || 800;
    // 1) 贴页面底部的一条: 备案号/版权/协议链接
    const bottomStrip = r.t > h * 0.93;
    if (bottomStrip && r.h <= 24) return 'bottom-strip';
    // 2) 名字是备案/版权/许可证/反馈这类纯行政信息
    if (/ICP备|公网安备|增值电信|营业执照|许可证|版权所有|©\s*\d|反馈|意见反馈|隐私政策|使用条款|免责声明/.test(name)) {
      return 'legal';
    }
    // 3) 贴右下角的小图标按钮(40x40 以下), 通常是"反馈/更多/工具箱"
    const corner = r.t > h * 0.75 && r.l > innerWidth * 0.9;
    if (corner && r.w <= 48 && r.h <= 48) return 'corner-icon';

    // 4) MAIN 区域内的卡片悬浮/重复操作噪音过滤(用户只想要控制到按钮/标签栏/输入栏/发送等常用控件)
    if (zone === 'MAIN') {
      // 视频卡片悬浮操作与时长播放量叠加文字
      if (/^(添加至|稍后再看|添加到播放列表|watch later|add to queue)/i.test(name)) return 'card-hover';
      // 气泡/动态卡片操作按钮(点赞/投币/复制/转发等在 MAIN 区一律排除, 避免焦点在动态流里乱跳)
      if (/^(点赞|投币|转发|转推|复制|朗读|copy|like|retweet|repost|thumbs|read aloud)$/i.test(name)) return 'card-action';
      // 代码块复制按钮 / 消息气泡内的次级操作按钮
      if (el.closest && el.closest('pre, .text-token-text-secondary, [data-message-author-role], .response-container, article[data-testid=tweet]')) {
        if (el.tagName === 'BUTTON' && !el.matches('[data-testid=send-button],[data-testid=composer-plus-btn]')) {
          return 'bubble-button';
        }
      }
    }
    return null;
  }

  // 链接是否是「内容卡片」(视频封面/文章卡片/推荐位): 这些不是遥控导航目标。
  // 判据: 包含缩略图/视频、或包含时长/播放量元数据、或面积过大(整块卡片)。
  function isContentCard(el) {
    // 包含图片/视频缩略图 → 大概率是内容卡片
    if (el.querySelector('img,video,picture')) return true;
    const t = (el.innerText || '').trim();
    // 包含时长(xx:xx) → 视频卡片
    if (/\d+:\d{2}/.test(t)) return true;
    // 包含播放量/点赞量标记(万/亿/k/M/次播放/views)
    if (/\d+\.?\d*[万亿kKmM]/.test(t) || /\d+\s*(次播放|次观看|views)/i.test(t)) return true;
    // 面积大于视口的 1/6 → 卡片块而非按钮
    const r = el.getBoundingClientRect();
    if (r.width * r.height > innerWidth * innerHeight / 6) return true;
    return false;
  }

  function denied(el, cfg) {
    const sel = joined(cfg.denySelectors || []);
    if (sel && matches(el, sel)) return true;
    // 只对明确的噪音容器做祖先判定；不要把 page 级容器当成拒绝
    return !!closest(el, '[inert],[aria-hidden="true"],[hidden],.ad,.ads,.advert,[data-ad]');
  }

  // 诊断用: 说出 denied 命中的具体选择器
  function deniedWhy(el, cfg) {
    const sel = joined(cfg.denySelectors || []);
    if (sel && matches(el, sel)) return 'self:' + sel.slice(0, 60);
    const anc = closest(el, '[inert],[aria-hidden="true"],[hidden],.ad,.ads,.advert,[data-ad]');
    if (!anc) return null;
    const hit = ['[inert]', '[aria-hidden="true"]', '[hidden]', '.ad', '.ads', '.advert', '[data-ad]']
      .filter((s) => { try { return anc.matches(s); } catch (e) { return false; } });
    return 'anc:' + anc.tagName + '.' + String(anc.className || '').slice(0, 30) + ' <- ' + hit.join('|');
  }

  function scan(cfg, opts) {
    cfgId = (cfg && cfg.id) || 'generic';
    opts = opts || {};
    // 诊断开关: 打开后把每个被拒元素和原因记到 window.__KZQ_REJECTS, 排查真站
    // 「某个控件为什么没进候选」时直接用, 不用靠猜。
    const trace = !!opts.trace;
    if (trace) window.__KZQ_REJECTS = [];
    const why = (el, reason) => {
      if (!trace) return;
      const r = rectOf(el);
      window.__KZQ_REJECTS.push({
        reason,
        tag: el.tagName,
        text: String(el.innerText || el.getAttribute('aria-label') || el.value || '').trim().slice(0, 40),
        rect: [Math.round(r.l), Math.round(r.t), Math.round(r.w), Math.round(r.h)],
        display: (getComputedStyle(el) || {}).display || '',
      });
    };
    dialogElRef = dialogEl(cfg);
    const dialogOnly = !!dialogElRef;
    const wantZone = opts.zone || null;
    const sels = joined([
      'button', '[role="button"]', 'input:not([type="hidden"])', 'textarea', 'select',
      '[contenteditable="true"]', '[role="textbox"]', '[role="searchbox"]',
      '[role="combobox"]', '[role="menuitem"]', '[role="menuitemcheckbox"]',
      '[role="menuitemradio"]', '[role="tab"]', '[role="checkbox"]', '[role="radio"]',
      '[role="switch"]', '[role="slider"]', '[role="option"]', 'summary',
      'a[role="button"]', 'a[role="tab"]', 'a[role="menuitem"]',
      // 链接只有 Profile 指定了 linkZones 才收集: 不配就完全不来, 配了也要再过
      // linkAllowed 的区域判定(正文里的链接、推荐位链接一律不算导航目标)
      ...((cfg.linkZones || []).length ? ['a[href]'] : [])
    ].concat(cfg.allowSelectors || []));
    if (!sels) return { candidates: [], zones: {}, dialog: false, error: 'no-selectors' };

    const raw = [];
    for (const el of qsa(sels)) {
      const role = roleOf(el);
      if (!role || !WL.has(role)) { REJECT.role++; why(el, 'role:' + role); continue; }
      if (denied(el, cfg)) { REJECT.role++; why(el, 'denied:' + (deniedWhy(el, cfg) || '?')); continue; }
      const r = rectOf(el);
      if (!styleVisible(el, r)) { REJECT.hidden++; why(el, 'hidden'); continue; }
      // 视口外的元素不丢: 只是排在后面。搜索结果/AI 概览占满首屏时, 真正的结果
      // 全在视口下方, 一律过滤掉等于"往下按十次都没东西" —— 用户感知就是死锁。
      // 由 rank 按距离罚分决定优先级, 选中后 focusElement 会 scrollIntoView 滚过去。
      const offscreen = !inViewport(r);
      if (offscreen) {
        // 只保留「一屏半」以内的, 避免把整页几千个元素全拖进候选
        const reach = innerHeight * 1.5;
        if (r.t > innerHeight + reach || r.b < -reach) { REJECT.viewport++; why(el, 'far'); continue; }
      }
      const zone = zoneOf(el, cfg, r);
      if (role === 'link' && !linkAllowed(el, cfg, zone)) { REJECT.role++; why(el, 'link-denied:' + zone); continue; }
      if (dialogOnly && zone !== 'DIALOG') { REJECT.zone++; why(el, 'dialog-fence'); continue; }
      if (wantZone && zone !== wantZone && !dialogOnly) { REJECT.zone++; why(el, 'zone:' + zone); continue; }
      if (covered(el, r)) { REJECT.occluded++; why(el, 'occluded'); continue; }
      const name = nameOf(el, role);
      // 无名 link/按钮可能是图标，但必须至少有一个可识别语义；纯装饰排除
      if (!name && !['textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'switch', 'slider'].includes(role)) {
        REJECT.role++;
        why(el, 'no-name:' + role);
        continue;
      }
      // 页脚/角落的行政信息与辅助图标: 不是用户想遥控的内容
      const noise = isNoise(el, r, name, zone);
      if (noise && !isEditable(el)) { REJECT.role++; why(el, 'noise:' + noise); continue; }
      const g = groupOf(el, cfg);
      if (trace) {
        window.__KZQ_REJECTS.push({
          reason: 'ACCEPTED',
          tag: el.tagName,
          text: String(el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 40),
          rect: [Math.round(r.l), Math.round(r.t), Math.round(r.w), Math.round(r.h)],
          display: (getComputedStyle(el) || {}).display || '',
        });
      }
      raw.push({ el, role, name, zone, rect: r, group: g.group,
                 groupIndex: g.groupIndex, offscreen });
    }

    // 去重 1：嵌套的同类语义元素（textarea 包在 role=textbox 里、图标包在 button 里）
    raw.sort((a, b) => (a.rect.w * a.rect.h) - (b.rect.w * b.rect.h));
    const kept = [];
    for (const c of raw) {
      let dup = false;
      for (const k of kept) {
        if (k.zone !== c.zone) continue;
        // 同一个可点区域的多种语义包装: 典型是 <li role=menuitem><a>图片</a></li>,
        // 外层 LI 和内层 A 都把「图片」报成候选。
        // 判据必须严格: 只有"真正互相包含"才算同一控件 —— 不能用矩形重叠率,
        // 因为同一行里并列的两个元素(搜索结果标题 228x44 和它右边的「翻译此页」
        // 48x18)矩形也大量重叠, 用重叠率会把大的那个当重复删掉, 结果整页
        // 搜索结果一个都不剩。
        const root = k.el === c.el || k.el.contains(c.el) || c.el.contains(k.el);
        const areaK = k.rect.w * k.rect.h;
        const areaC = c.rect.w * c.rect.h;
        const big = Math.max(areaK, areaC);
        const sameBox = big > 0 && Math.abs(areaK - areaC) / big < 0.12 &&
          rectOverlapRatio(k.rect, c.rect) > 0.92;
        if (root || sameBox) {
          // 保留「更内层 / 更具体」的那个: c 优先就删掉 k, 否则丢掉 c
          const cInner = k.el.contains(c.el) || (sameBox && isInner(c, k));
          if (cInner) {
            const idx = kept.indexOf(k);
            if (idx >= 0) kept.splice(idx, 1);
          } else {
            dup = true;
          }
          if (dup) break;
          continue;
        }
      }
      if (dup) {
        REJECT.dup++;
        if (trace) {
          window.__KZQ_REJECTS.push({
            reason: 'dedup',
            tag: c.el.tagName,
            text: String(c.el.innerText || '').trim().slice(0, 40),
            rect: [Math.round(c.rect.l), Math.round(c.rect.t),
                   Math.round(c.rect.w), Math.round(c.rect.h)],
            display: '',
          });
        }
        continue;
      }
      kept.push(c);
    }

    // 密度上限: 单区域候选超过 DENSITY_CAP 个时, 自动剥离该区域的 link,
    // 只保留 button/tab/textbox 等真控件。防止视频卡片/文章链接淹没有用的按钮,
    // 是「焦点乱跳」的核心止血措施。
    const DENSITY_CAP = 20;
    const zoneCounts = {};
    for (const c of kept) zoneCounts[c.zone] = (zoneCounts[c.zone] || 0) + 1;
    const dense = new Set();
    for (const z in zoneCounts) if (zoneCounts[z] > DENSITY_CAP) dense.add(z);
    if (dense.size > 0) {
      const before = kept.length;
      const trimmed = kept.filter((c) => {
        if (!dense.has(c.zone)) return true;
        if (c.role === 'link') {
          // 链接是内容卡片(含图片/视频/时长/播放量): 不是遥控目标
          if (isContentCard(c.el)) return false;
          // 大面积链接块(超过视口 1/8): 大概率是内容卡片
          if (c.rect.w * c.rect.h > innerWidth * innerHeight / 8) return false;
          // 密度仍然过高时, 所有链接一律降级剥离
          return false;
        }
        return true;
      });
      // 收紧后至少保留 3 个控件才采纳, 否则回退全量(宁可多不可空)
      if (trimmed.length >= 3) {
        kept.length = 0;
        kept.push(...trimmed);
      }
    }

    const out = [];
    const zones = {};
    for (const c of kept) {
      const b = bind(c.el, c.zone, c.role, c.name);
      zones[c.zone] = (zones[c.zone] || 0) + 1;
      out.push({
        id: b.id, key: b.key, role: c.role, name: c.name.slice(0, 60),
        zone: c.zone, group: c.group, groupIndex: c.groupIndex,
        rect: c.rect, center: { x: cx(c.rect), y: cy(c.rect) },
        screen: screenRect(c.rect), editable: isEditable(c.el),
        offscreen: !!c.offscreen
      });
    }
    return {
      candidates: out,
      zones,
      dialog: !!dialogOnly,
      zone: wantZone,
      activeZone: dialogOnly ? 'DIALOG' : (wantZone || ''),
      viewport: { w: innerWidth, h: innerHeight },
      url: location.href,
      title: document.title,
      rejects: REJECT
    };
  }

  // 只返回「视口内 CSS 坐标」。屏幕坐标一律由服务端算:
  //   screen = 客户区原点 + css * dpr
  // 客户端拿不到可靠的客户区原点(screenX/screenY 在不同 Chrome 版本里语义漂移:
  // 有的是窗口左上角、有的是视口左上角), 之前 here 里叠 outerHeight-innerHeight
  // 会把标题栏偏移算两遍, 实测框整体下移 96px、跑到元素外面 —— 用户看到的
  // 「框太大 / 框乱飘」。几何真值只留一份, 在服务端。
  function screenRect(r) {
    return { l: r.l, t: r.t, r: r.r, b: r.b };
  }

  // 两个矩形的交叠比例(相对较小那个的面积): 1 = 完全重合
  function rectOverlapRatio(a, b) {
    const w = Math.min(a.r, b.r) - Math.max(a.l, b.l);
    const h = Math.min(a.b, b.b) - Math.max(a.t, b.t);
    if (w <= 0 || h <= 0) return 0;
    const inter = w * h;
    const small = Math.min(a.w * a.h, b.w * b.h);
    return small > 0 ? inter / small : 0;
  }

  // 矩形重合时判断谁"更内层"。
  // 关键: 真控件(link/button/input 等可操作语义)优先于包装元素保留 —— 否则
  // Google 结果标题的 <a>(228x44) 会被它内部更小的文本节点挤掉, 结果整页
  // 搜索结果一个都进不了候选(实测 dedup 删掉了全部结果链接)。
  function isInner(c, k) {
    const REAL = new Set(['link', 'button', 'textbox', 'searchbox', 'combobox',
                          'checkbox', 'radio', 'switch', 'slider', 'tab',
                          'menuitem', 'menuitemcheckbox', 'menuitemradio', 'option']);
    const cReal = REAL.has(c.role);
    const kReal = REAL.has(k.role);
    if (cReal !== kReal) return cReal;      // 真控件赢
    const areaC = c.rect.w * c.rect.h;
    const areaK = k.rect.w * k.rect.h;
    if (Math.abs(areaC - areaK) > 4) return areaC < areaK;
    return false;
  }

  function rank(start, list, dir) {
    const s = start.rect;
    const scx = cx(s), scy = cy(s);
    const vertical = dir === 'up' || dir === 'down';
    const vw = innerWidth || 1200, vh = innerHeight || 800;
    const scored = [];
    for (const c of list) {
      if (c.id === start.id) continue;
      const r = c.rect;
      if (r.w <= 0 || r.h <= 0) continue;
      const ccx = cx(r), ccy = cy(r);
      let fwd, main, side;
      if (vertical) {
        fwd = dir === 'down' ? ccy - scy : scy - ccy;
        main = dir === 'down' ? r.t - s.b : s.t - r.b;
        side = Math.max(0, s.l - r.r, r.l - s.r);
      } else {
        fwd = dir === 'right' ? ccx - scx : scx - ccx;
        main = dir === 'right' ? r.l - s.r : s.l - r.r;
        side = Math.max(0, s.t - r.b, r.t - s.b);
      }
      if (fwd <= 0) continue;
      const sideCenter = vertical ? ccx - scx : ccy - scy;

      // 水平移动: 严格同行约束, 防止斜向跳行
      if (!vertical) {
        const startH = Math.max(1, s.h);
        const drift = Math.abs(sideCenter) / startH;
        if (drift > 1.0) continue;   // 超过 1 倍高度: 不同行
      } else {
        // 垂直移动: 60° 方向锥, 允许较大水平偏移
        if (side > 0 && Math.abs(sideCenter) > fwd * 1.73) continue;
      }

      const gap = Math.max(0, main);
      // 副轴重叠量: 正前方的元素优先
      const overlap = vertical
        ? Math.max(0, Math.min(s.r, r.r) - Math.max(s.l, r.l))
        : Math.max(0, Math.min(s.b, r.b) - Math.max(s.t, r.t));

      // 评分: 主轴距离 + 副轴偏移权重, 有重叠的加分
      let score = gap + Math.abs(sideCenter) * 0.3;
      if (overlap > 4) score -= Math.min(50, overlap * 0.2);

      // 远距离惩罚: 超过一个视口不跨区
      const limit = vertical ? vh : vw;
      if (gap > limit * 0.5) score += (gap - limit * 0.5) * 2.5;
      if (gap > limit * 0.95) score += limit * 2;

      // 同一功能组优先
      if (start.group && c.group === start.group) score -= 24;
      // 可编辑控件略优先
      if (c.editable) score -= 6;
      // 视口外排后面
      if (c.offscreen) score += 260;
      // 最近访问过的降权(防小圈回环)
      const ri = RECENT.indexOf(c.id);
      if (ri >= 0) score += 600 + (6 - ri) * 120;
      scored.push({ c, score });
    }
    scored.sort((a, b) => a.score - b.score);
    return scored.map((x) => x.c);
  }

  function info(c, moved, reason) {
    if (!c) return { moved: false, reason: reason || 'zone-end' };
    return {
      moved: !!moved,
      reason: reason || '',
      id: c.id, key: c.key, name: c.name, role: c.role, zone: c.zone,
      group: c.group, rect: c.rect, screen: c.screen,
      editing: isEditable(c.el)
    };
  }

  function findById(id) {
    const el = byId.get(id);
    if (el && el.isConnected) return el;
    return null;
  }

  function focusElement(el, cfg) {
    if (!el || !el.isConnected) return null;
    try { el.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' }); } catch (e) { }
    try { el.focus({ preventScroll: true }); } catch (e) {
      try { el.focus(); } catch (e2) { return null; }
    }
    const active = document.activeElement;
    if (active !== el && !el.contains(active)) {
      // 某些自定义控件只响应 click 才进入焦点
      try { el.dispatchEvent(new FocusEvent('focus', { bubbles: false })); } catch (e) { }
    }
    return rectOf(el);
  }

  function state(cfg) {
    cfgId = (cfg && cfg.id) || 'generic';
    const d = dialogEl(cfg);
    dialogElRef = d;
    const ae = document.activeElement;
    const editing = isEditable(ae);
    let active = null;
    if (ae && ae.nodeType === 1) {
      const r = rectOf(ae);
      active = {
        id: ae[ID_PROP] || '', key: ae[KEY_PROP] || '', role: roleOf(ae),
        name: (nameOf(ae, roleOf(ae)) || '').slice(0, 60),
        rect: r, screen: screenRect(r), editing
      };
    }
    let sel = null;
    if (editing) {
      try {
        if (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA') {
          sel = { start: ae.selectionStart, end: ae.selectionEnd, length: (ae.value || '').length };
        } else {
          const s = window.getSelection();
          sel = { start: s && s.anchorOffset, end: s && s.focusOffset, length: (ae.innerText || '').length };
        }
      } catch (e) { }
    }
    return {
      editing, dialog: !!d, active, selection: sel,
      // 输入框里是否有可编辑内容: 引擎层据此决定方向键归谁(空框放行导航)
      editing_room: editing ? fieldHasRoom(ae) : false,
      url: location.href, title: document.title,
      viewport: { w: innerWidth, h: innerHeight },
      dpr: window.devicePixelRatio || 1,
      // 客户端自报的视口原点, 只作参考; 服务端会用 UIA 客户区原点覆盖它
      origin: { x: window.screenX || 0, y: window.screenY || 0 }
    };
  }

  // 输入框里"有没有东西可以给方向键玩"。
  // 空输入框里按左右根本无处可去、按上下本来也是滚页面 —— 这时候把方向键全吞掉
  // 就是"按什么都没反应"。必应首页一进来焦点就在搜索框里(editing=true), 之前
  // 整页导航直接瘫痪, 用户第一句反馈就是"完全不可用"。
  // 规则: 有内容/有选中/有光标可移 => 让权给文本编辑; 空 => 放行导航。
  function fieldHasRoom(el) {
    if (!el) return false;
    try {
      const tag = el.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA') {
        const v = el.value || '';
        if (v.length > 0) return true;                 // 有内容
        return false;                                  // 空: 没有可移动的东西
      }
      if (el.isContentEditable) {
        const t = (el.innerText || '').trim();
        return t.length > 0;
      }
    } catch (e) { }
    return false;
  }

  function nav(cfg, dir, zone, currentId, mode) {
    cfgId = (cfg && cfg.id) || 'generic';
    // 编辑态保护: 焦点在输入控件里、且框里确实有文字可编辑时, 方向键属于文本编辑
    // (移光标), 不抢焦点。空输入框放行 —— 否则页面自动聚焦搜索框时整页遥控失效。
    const ae0 = document.activeElement;
    if (mode !== 'free' && isEditable(ae0) && fieldHasRoom(ae0)) {
      return { moved: false, reason: 'editing', zone: zone || '', editing: true,
               id: ae0[ID_PROP] || '' };
    }
    const d = dialogEl(cfg);
    dialogElRef = d;
    const free = !zone || zone === '*';
    const activeZone = d ? 'DIALOG' : (free ? '*' : zone);
    const scanRes = scan(cfg, { zone: free ? null : activeZone });
    let list = scanRes.candidates;
    if (d) list = list.filter((c) => c.zone === 'DIALOG');
    if (!list.length) return { moved: false, reason: 'zone-empty', zone: activeZone, dialog: !!d };

    let start = null;
    if (currentId) {
      const el = findById(currentId);
      if (el && el.isConnected) {
        const r = rectOf(el);
        const hit = list.find((c) => c.id === currentId);
        if (hit) start = hit;
        else if (r.w > 0 && r.h > 0) {
          start = { id: currentId, rect: r, group: '', groupIndex: -1 };
        }
      }
    }
    if (!start) {
      const ae = document.activeElement;
      if (ae && ae[ID_PROP]) {
        const hit = list.find((c) => c.id === ae[ID_PROP]);
        if (hit) start = hit;
      }
    }
    if (!start) {
      // 没有起点：只落到当前区域左上角，不跳到别的区域
      const first = list.slice().sort((a, b) => (a.rect.t - b.rect.t) || (a.rect.l - b.rect.l))[0];
      const el = findById(first.id);
      if (!el) return { moved: false, reason: 'stale', zone: activeZone };
      const r = focusElement(el, cfg);
      return info({ ...first, rect: r || first.rect, screen: screenRect(r || first.rect) }, true, 'initial');
    }

    // 输入区横向优先走功能组顺序：附件 → 工具 → 输入框 → 语音 → 发送
    if (activeZone === 'COMPOSER' && (dir === 'left' || dir === 'right') &&
        start.groupIndex >= 0) {
      const same = list.filter((c) => c.groupIndex >= 0 && c.zone === 'COMPOSER');
      const step = dir === 'right' ? 1 : -1;
      let target = null;
      for (let i = start.groupIndex + step; i >= 0 && i <= 16; i += step) {
        target = same.find((c) => c.groupIndex === i);
        if (target) break;
      }
      if (target) {
        const el = findById(target.id);
        if (el) {
          const r = focusElement(el, cfg);
          return info({ ...target, rect: r || target.rect, screen: screenRect(r || target.rect) }, true, 'group');
        }
      }
      // 功能组到头：不在输入区内部绕圈，也不跨区
      return { moved: false, reason: 'group-end', zone: activeZone };
    }

    const ranked = rank(start, list, dir);
    if (!ranked.length) return { moved: false, reason: 'zone-end', zone: activeZone };
    const target = ranked[0];
    const el = findById(target.id);
    if (!el) return { moved: false, reason: 'stale', zone: activeZone };
    const r = focusElement(el, cfg);
    if (!r) return { moved: false, reason: 'focus-failed', zone: activeZone };
    noteVisited(target.id);
    return info({ ...target, rect: r, screen: screenRect(r) }, true, 'spatial');
  }

  // 记录导航落点, 供 rank 做"别在原地打转"的降权
  function noteVisited(id) {
    if (!id) return;
    const i = RECENT.indexOf(id);
    if (i >= 0) RECENT.splice(i, 1);
    RECENT.unshift(id);
    if (RECENT.length > 6) RECENT.length = 6;
  }

  function zoneFirst(cfg, zone) {
    cfgId = (cfg && cfg.id) || 'generic';
    const d = dialogEl(cfg);
    dialogElRef = d;
    const z = d ? 'DIALOG' : zone;
    const res = scan(cfg, { zone: z });
    const list = res.candidates;
    if (!list.length) return { moved: false, reason: 'zone-empty', zone: z };
    let first;
    if (z === 'COMPOSER') {
      // 输入区按 Profile 功能组顺序落第一个: 附件 → 工具 → 输入框 → 语音 → 发送
      // (几何最靠上的那个可能正好是中间的大 textarea, 不是规范要求的入口)
      const grouped = list.filter((c) => c.groupIndex >= 0)
        .sort((a, b) => a.groupIndex - b.groupIndex);
      first = grouped[0] ||
        list.slice().sort((a, b) => (a.rect.t - b.rect.t) || (a.rect.l - b.rect.l))[0];
    } else {
      // 落「第一行内容」而不是「最靠上的那一个小图标」。
      // 真站(Google 结果页)每条结果都挂一个 34x20 的「详细信息」小按钮, 坐标比
      // 结果标题更靠上更靠左, 按纯几何排序会先撞上它 —— 用户第一下方向键落在
      // 一个几乎看不见的小图标上, 就是"乱跳"的体感来源。
      // 规则: 先按 t 取行基准, 容差内算同一行; 行内优先「面积够大」的内容块,
      // 小图标(宽<60 或 高<24)只在没有大块时兜底。
      const sorted = list.slice().sort((a, b) => (a.rect.t - b.rect.t) || (a.rect.l - b.rect.l));
      const top = sorted[0].rect.t;
      const tol = Math.max(28, sorted[0].rect.h * 0.9);
      const row = sorted.filter((c) => c.rect.t - top <= tol);
      // 「内容块」优先于「叠在内容上的小按钮」:
      // B 站首页视频卡上盖着「添加至稍后再看60.2万345417:00」这类悬浮按钮,
      // 位置和标题一样靠上, 纯几何排序会先选中它。用三个信号区分:
      //   · 面积够大(是真块, 不是图标)
      //   · 文本不是"操作动词 + 播放量/时长"的堆叠(悬浮角标的典型特征)
      //   · 高度够(单行标题 20px 也可能很大, 但悬浮角标通常更矮)
      const isBlock = (c) => {
        if (c.rect.w < 60 || c.rect.h < 24) return false;
        const n = c.name || '';
        if (n.length > 24 && c.rect.h < 48) return false;
        // 数字占比高 = 播放量/时长/点赞数堆在一起的无障碍名, 不是内容标题
        const digits = (n.match(/[0-9]/g) || []).length;
        if (n.length >= 8 && digits / n.length > 0.25) return false;
        // 以操作动词开头的小控件(添加至/收藏/稍后再看/分享/点赞...)
        if (/^(添加至|添加|收藏|稍后再看|分享|点赞|投币|举报|更多)/.test(n) && c.rect.h < 40) return false;
        return true;
      };
      const blocks = row.filter(isBlock).sort((a, b) => (a.rect.l - b.rect.l));
      first = blocks[0] || row.sort((a, b) => (a.rect.l - b.rect.l))[0] || sorted[0];
    }
    const el = findById(first.id);
    if (!el) return { moved: false, reason: 'stale', zone: z };
    const r = focusElement(el, cfg);
    noteVisited(first.id);
    return info({ ...first, rect: r || first.rect, screen: screenRect(r || first.rect) }, true, 'zone-first');
  }

  function restore(cfg, key, zone) {
    cfgId = (cfg && cfg.id) || 'generic';
    const d = dialogEl(cfg);
    dialogElRef = d;
    const z = d ? 'DIALOG' : zone;
    const res = scan(cfg, { zone: z });
    let hit = res.candidates.find((c) => c.key === key);
    if (!hit && key) {
      const parts = key.split('|');
      const role = parts[2], name = (parts[3] || '').toLowerCase();
      hit = res.candidates.find((c) => c.role === role && c.name.toLowerCase() === name);
    }
    if (!hit) return { moved: false, reason: 'key-gone', zone: z };
    const el = findById(hit.id);
    if (!el) return { moved: false, reason: 'stale', zone: z };
    const r = focusElement(el, cfg);
    return info({ ...hit, rect: r || hit.rect, screen: screenRect(r || hit.rect) }, true, 'restore');
  }

  function activate(cfg, id) {
    cfgId = (cfg && cfg.id) || 'generic';
    const ae = document.activeElement;
    if (isEditable(ae)) return { editing: true, ok: false };
    const el = (id && findById(id)) || ae;
    if (!el || !el.isConnected) return { ok: false, reason: 'stale' };
    try {
      el.click();
      return { ok: true, role: roleOf(el), name: (nameOf(el, roleOf(el)) || '').slice(0, 60) };
    } catch (e) {
      return { ok: false, reason: String(e && e.message || e) };
    }
  }

  function listZones(cfg) {
    cfgId = (cfg && cfg.id) || 'generic';
    const d = dialogEl(cfg);
    dialogElRef = d;
    const res = scan(cfg, { zone: null });
    return { zones: res.zones, dialog: !!d, count: res.candidates.length };
  }

  window.__KZQ_NAV = {
    version: VERSION,
    scan, state, nav, zoneFirst, restore, activate, listZones,
    _debug: { REJECT, byId: () => byId.size, keys: () => keyToId.size,
              linkAllowed, zoneOf, nameOf, roleOf, covered, styleVisible }
  };
})();
