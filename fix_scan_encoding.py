#!/usr/bin/env python3
path = '/Users/rey/.openclaw/workspace/futures-panel/templates/index.html'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

uent_code = """

  function uent(s) {
    if (s == null) return '';
    if (typeof s !== 'string') return s;
    try {
      let r = s.replace(/\\\\u([0-9a-f]{4})/gi, (_, p) => String.fromCodePoint(parseInt(p, 16)));
      const t = document.createElement('textarea');
      t.innerHTML = r;
      return t.value;
    } catch(e) { return s; }
  }

"""

old1 = "  const API = window.location.origin;\n  let refreshTimer = null;"
new1 = "  const API = window.location.origin;\n" + uent_code + "  let refreshTimer = null;"
content = content.replace(old1, new1, 1)

content = content.replace(
    "if (dv) primaryBadge = '<span style=\"display:inline-flex;align-items:center;gap:3px;padding:3px 8px;border-radius:8px;font-size:0.75rem;font-weight:700;background:rgba(168,85,247,0.15);color:#a855f7;border:1px solid rgba(168,85,247,0.3)\">💊 ' + (dv.text || '背离') + '</span>';",
    "if (dv) primaryBadge = '<span style=\"display:inline-flex;align-items:center;gap:3px;padding:3px 8px;border-radius:8px;font-size:0.75rem;font-weight:700;background:rgba(168,85,247,0.15);color:#a855f7;border:1px solid rgba(168,85,247,0.3)\">💊 ' + (uent(dv.text) || '背离') + '</span>';"
)
content = content.replace(
    "if (r.type === 'div_kdj') return '<span style=\"padding:2px 6px;border-radius:8px;font-size:0.65rem;font-weight:500;background:rgba(168,85,247,0.07);color:#a855f7;border:1px solid rgba(168,85,247,0.12)\">💊' + (r.text || '').replace('+', '') + '</span>';",
    "if (r.type === 'div_kdj') return '<span style=\"padding:2px 6px;border-radius:8px;font-size:0.65rem;font-weight:500;background:rgba(168,85,247,0.07);color:#a855f7;border:1px solid rgba(168,85,247,0.12)\">💊' + uent(r.text).replace('+', '') + '</span>';"
)
content = content.replace(
    "const safeName = String(item.name || '').replace(/[\\u0000-\\u001F\\u007F-\\u009F]/g, '').replace(/[<>]/g, '');",
    "const safeName = uent(item.name || '');"
)

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)
print("done")
