#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcd-footprint · 报告渲染器

把 footprint_aggregate.py 产出的聚合 JSON，渲染成：
  1. 一份自包含的 HTML 消费画像报告（零外部依赖、离线可开）
  2. 报告内嵌一张 1080×1440 的竖版分享卡（可直接截图/存图分享）

用法
----
    python render_report.py --input footprint.json --output report.html
"""

import argparse
import io
import json
import os
import sys
from datetime import datetime

# ============================================================
# 工具
# ============================================================


def esc(s):
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def money(y):
    try:
        return "¥{:,.2f}".format(float(y))
    except (TypeError, ValueError):
        return "¥0.00"


def pct(x, digits=0):
    try:
        fmt = "{:." + str(digits) + "f}%"
        return fmt.format(float(x) * 100)
    except (TypeError, ValueError):
        return "0%"


# ============================================================
# 样式
# ============================================================

CSS = r"""
:root{
  --bg:#faf8f5; --card:#fffdfb; --ink:#2b2320; --ink2:#6f625a; --ink3:#a3968d;
  --line:#efe7df; --brand:#DA291C; --brand-d:#a8160d; --gold:#f0b429; --brown:#8c3a2b;
  --shadow:0 1px 2px rgba(80,50,40,.04), 0 8px 24px rgba(80,50,40,.05);
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{
  background:var(--bg); color:var(--ink); line-height:1.65;
  font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:920px;margin:0 auto;padding:40px 20px 96px}

/* ---------- 页头 ---------- */
.masthead{margin-bottom:28px}
.eyebrow{
  display:inline-flex;align-items:center;gap:8px;font-size:12px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--brand);font-weight:700;margin-bottom:12px;
}
.eyebrow::before{content:"";width:22px;height:2px;background:var(--brand);border-radius:2px}
h1{font-size:34px;line-height:1.25;letter-spacing:-.5px;font-weight:800;margin-bottom:10px}
h1 .accent{background:linear-gradient(100deg,var(--brand),var(--brown));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.lede{font-size:15px;color:var(--ink2);max-width:640px}
.lede b{color:var(--ink);font-weight:700}

/* ---------- 通用卡片 ---------- */
.panel{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:24px;box-shadow:var(--shadow);margin-bottom:18px}
.panel h2{font-size:15px;font-weight:800;letter-spacing:.02em;margin-bottom:4px;display:flex;align-items:center;gap:8px}
.panel h2 .dot{width:7px;height:7px;border-radius:50%;background:var(--brand);flex:none}
.panel .sub{font-size:12.5px;color:var(--ink3);margin-bottom:18px}

/* ---------- Hero 数字 ---------- */
.hero{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:18px}
.hero .cell{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 16px;box-shadow:var(--shadow)}
.hero .num{font-size:26px;font-weight:800;letter-spacing:-.5px;line-height:1.2;font-variant-numeric:tabular-nums}
.hero .num small{font-size:13px;font-weight:700;color:var(--ink3);margin-left:3px}
.hero .cap{font-size:12px;color:var(--ink3);margin-top:6px}
.hero .cell.hi{background:linear-gradient(150deg,#fff6f4,#fffdfb);border-color:#f6ded8}
.hero .cell.hi .num{color:var(--brand)}

/* ---------- 亮点 ---------- */
.highlights{display:flex;flex-direction:column;gap:10px}
.highlights li{list-style:none;display:flex;gap:10px;font-size:14px;color:var(--ink2)}
.highlights li::before{content:"";width:5px;height:5px;border-radius:50%;background:var(--gold);margin-top:9px;flex:none}
.highlights li b{color:var(--ink);font-weight:700}

/* ---------- 时间热力 ---------- */
.heat{display:grid;grid-template-columns:58px repeat(6,1fr);gap:5px}
.heat .hd{font-size:11.5px;color:var(--ink3);text-align:center;padding-bottom:4px;font-weight:600}
.heat .rl{font-size:11.5px;color:var(--ink3);display:flex;align-items:center;font-weight:600}
.heat .cl{height:38px;border-radius:7px;display:flex;align-items:center;justify-content:center;
  font-size:12.5px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--ink3)}
.heat .cl.on{color:#fff}
.heat-legend{display:flex;align-items:center;gap:8px;margin-top:14px;font-size:11.5px;color:var(--ink3)}
.heat-legend .sw{width:16px;height:12px;border-radius:3px}

/* ---------- 柱状（月度） ---------- */
.months{display:flex;align-items:flex-end;gap:14px;height:150px;padding-top:8px}
.month-col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;gap:6px}
.month-bar{width:100%;max-width:52px;border-radius:8px 8px 3px 3px;
  background:linear-gradient(180deg,var(--brand),#e8574a);min-height:4px;position:relative}
.month-bar span{position:absolute;top:-19px;left:0;right:0;text-align:center;font-size:11.5px;font-weight:700;color:var(--ink2)}
.month-cap{font-size:11.5px;color:var(--ink3);font-weight:600}

/* ---------- 条形 ---------- */
.bars{display:flex;flex-direction:column;gap:11px}
.bar-row{display:grid;grid-template-columns:104px 1fr 128px;align-items:center;gap:12px}
.bar-name{font-size:13px;font-weight:600;color:var(--ink)}
.bar-track{height:12px;border-radius:6px;background:#f3ece6;overflow:hidden}
.bar-fill{height:100%;border-radius:6px;background:linear-gradient(90deg,var(--brand),#ec6a52)}
.bar-fill.gold{background:linear-gradient(90deg,var(--gold),#f6cd6a)}
.bar-val{font-size:12.5px;color:var(--ink3);text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.bar-foot{margin-top:14px;padding-top:12px;border-top:1px dashed var(--line);font-size:12.5px;color:var(--ink2);line-height:1.75}
.bar-foot b{color:var(--ink);font-weight:700}
.bar-foot .dim{color:var(--ink3)}

/* ---------- 城市 ---------- */
.cities{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:12px}
.city{border:1px solid var(--line);border-radius:13px;padding:15px 16px;background:#fffefd}
.city .top{display:flex;align-items:baseline;justify-content:space-between;gap:8px;margin-bottom:6px}
.city .name{font-size:15px;font-weight:800}
.city .badge{font-size:11.5px;font-weight:700;color:var(--brand);background:#fdeeeb;border-radius:20px;padding:2px 9px;white-space:nowrap}
.city .meta{font-size:12px;color:var(--ink3);margin-bottom:8px}
.city .store{font-size:12.5px;color:var(--ink2);line-height:1.5}
.city .store b{color:var(--ink);font-weight:600}
.city .spend{font-size:12px;color:var(--ink3);margin-top:8px;padding-top:8px;border-top:1px dashed var(--line)}

/* ---------- 优惠账本 ---------- */
.ledger{background:linear-gradient(140deg,#2f2622,#4a352c);color:#f7efe7;border:none;border-radius:16px;padding:26px;box-shadow:var(--shadow)}
.ledger h2{color:#fff}
.ledger h2 .dot{background:var(--gold)}
.ledger .sub{color:#c4b3a6}
.ledger-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:6px}
.ledger-grid .k{font-size:11.5px;color:#c4b3a6;margin-bottom:4px}
.ledger-grid .v{font-size:21px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.3px}
.ledger-grid .v.good{color:#7fdc9a}
.ledger-grid .v.bad{color:#f2a08f}
.ledger .verdict{margin-top:18px;padding-top:16px;border-top:1px solid rgba(255,255,255,.14);font-size:14px;color:#e8dcd2}
.ledger .verdict b{color:var(--gold)}
.cp-title{margin-top:20px;font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;color:#c4b3a6}
.cp-list{margin-top:10px;display:flex;flex-direction:column;gap:9px}
.cp{display:grid;grid-template-columns:150px 1fr 130px;gap:12px;align-items:center}
.cp .cp-n{font-size:13px;color:#f4e9df;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cp .cp-bar{height:8px;border-radius:4px;background:rgba(255,255,255,.10);overflow:hidden}
.cp .cp-bar i{display:block;height:100%;border-radius:4px;background:var(--gold)}
.cp .cp-v{font-size:12px;color:#c4b3a6;text-align:right;font-variant-numeric:tabular-nums}

/* ---------- 口径 ---------- */
.notes{list-style:none;display:flex;flex-direction:column;gap:8px}
.notes li{font-size:12.5px;color:var(--ink2);line-height:1.6;padding-left:16px;position:relative}
.notes li::before{content:"·";position:absolute;left:4px;color:var(--ink3);font-weight:800}
.notes li b{color:var(--ink);font-weight:700}
.warns{list-style:none;display:flex;flex-direction:column;gap:8px;margin-top:14px}
.warns li{font-size:12.5px;padding:10px 13px;border-radius:9px;line-height:1.6}
.warns li.warn{background:#fdf7e8;color:#8a6a19;border:1px solid #f5e6bd}
.warns li.error{background:#fdeeeb;color:#a8331f;border:1px solid #f6d8d2}
.warns li b{font-weight:800}
.ok-line{font-size:12.5px;color:#3e7d55;background:#eef8f1;border:1px solid #d6ecdd;border-radius:9px;padding:10px 13px}

/* ---------- 分享卡 ---------- */
.card-section{margin-top:26px}
.card-hint{font-size:12.5px;color:var(--ink3);margin-bottom:12px}
.card-scroll{overflow:auto;border-radius:18px}
.share-card{
  width:1080px;min-height:1920px;zoom:.5;
  background:linear-gradient(170deg,#fffdfa 0%,#faf4ec 58%,#f6ece2 100%);
  padding:96px 76px;display:flex;flex-direction:column;gap:48px;
  position:relative;overflow:hidden;border:1px solid #eadfd2;border-radius:18px;
}
.share-card::before{content:"";position:absolute;top:-180px;right:-160px;width:620px;height:620px;border-radius:50%;
  background:radial-gradient(circle,rgba(218,41,28,.10),rgba(218,41,28,0) 68%)}
.share-card::after{content:"";position:absolute;bottom:-200px;left:-140px;width:520px;height:520px;border-radius:50%;
  background:radial-gradient(circle,rgba(240,180,41,.14),rgba(240,180,41,0) 70%)}
.sc-brand{font-size:20px;font-weight:800;letter-spacing:.18em;color:var(--brand);text-transform:uppercase;position:relative}
.sc-title{font-size:70px;font-weight:900;letter-spacing:-2px;line-height:1.1;position:relative}
.sc-title em{font-style:normal;background:linear-gradient(100deg,var(--brand),var(--brown));-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.sc-sub{font-size:24px;color:var(--ink2);position:relative;margin-top:-18px}
.sc-nums{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;position:relative}
.sc-num{background:rgba(255,255,255,.72);border:1px solid #efe2d4;border-radius:20px;padding:22px 24px}
.sc-num .n{font-size:48px;font-weight:900;letter-spacing:-1.5px;font-variant-numeric:tabular-nums;line-height:1.1}
.sc-num .n small{font-size:22px;font-weight:800;color:var(--ink3);margin-left:4px}
.sc-num .c{font-size:19px;color:var(--ink3);margin-top:4px}
.sc-num.hi .n{color:var(--brand)}
.sc-block{position:relative}
.sc-block-t{font-size:19px;font-weight:800;color:var(--ink2);margin-bottom:14px;letter-spacing:.02em}
.sc-heat{display:grid;grid-template-columns:76px repeat(6,1fr);gap:7px}
.sc-heat .c{height:34px;border-radius:7px}
.sc-heat .l{font-size:16px;color:var(--ink3);display:flex;align-items:center;font-weight:600}
.sc-chips{display:flex;flex-wrap:wrap;gap:11px}
.sc-chip{font-size:21px;padding:9px 20px;border-radius:999px;background:rgba(218,41,28,.09);
  color:var(--brand-d);font-weight:700;border:1px solid rgba(218,41,28,.16)}
.sc-chip.plain{background:rgba(240,180,41,.14);color:#8a6412;border-color:rgba(240,180,41,.3)}
.sc-items{display:flex;gap:14px}
.sc-item{flex:1;background:rgba(255,255,255,.72);border:1px solid #efe2d4;border-radius:16px;padding:16px 18px}
.sc-item .r{font-size:15px;font-weight:800;color:var(--gold)}
.sc-item .nm{font-size:24px;font-weight:800;margin:4px 0 2px;line-height:1.25}
.sc-item .q{font-size:17px;color:var(--ink3)}
.sc-ledger{background:linear-gradient(120deg,#2f2622,#4a352c);border-radius:20px;padding:26px 30px;color:#f7efe7;
  display:flex;align-items:center;justify-content:space-between;gap:24px}
.sc-ledger .t{font-size:19px;color:#c4b3a6;margin-bottom:6px}
.sc-ledger .m{font-size:34px;font-weight:900;letter-spacing:-.8px}
.sc-ledger .m b{color:var(--gold)}
.sc-ledger .r{text-align:right;font-size:19px;color:#c4b3a6;line-height:1.5}
.sc-ledger .r b{color:#7fdc9a;font-size:26px;font-weight:900;display:block}
.sc-foot{margin-top:auto;display:flex;align-items:center;justify-content:space-between;
  font-size:17px;color:var(--ink3);padding-top:20px;border-top:1px solid #eadfd2;position:relative}
.sc-foot b{color:var(--brand);font-weight:800}

.foot{text-align:center;font-size:12px;color:var(--ink3);margin-top:40px;line-height:1.9}

@media (max-width:760px){
  h1{font-size:26px}
  .hero{grid-template-columns:repeat(2,1fr)}
  .bar-row{grid-template-columns:84px 1fr;gap:8px 10px}
  .bar-val{grid-column:2;text-align:left}
  .ledger-grid{grid-template-columns:repeat(2,1fr)}
  .heat{grid-template-columns:44px repeat(6,1fr);gap:3px}
  .heat .cl{height:32px;font-size:11px}
  .share-card{zoom:.32}
}
"""

# ============================================================
# 片段生成
# ============================================================


def heat_color(count, mx):
    if not count or mx <= 0:
        return "rgba(218,41,28,0.045)"
    a = 0.14 + 0.78 * (count / mx)
    return "rgba(218,41,28,%.3f)" % a


def sec_heatmap(r):
    hm = r["time_heatmap"]
    mx = hm["max"]
    slots = hm["slots"]
    rows = hm["matrix"]
    out = ['<div class="panel">']
    out.append('<h2><span class="dot"></span>时间热力 · 一周 × 时段</h2>')
    out.append('<div class="sub">每格数字是落在「该星期几 + 该时段」的订单数。'
               '时段划分见文末口径。</div>')
    out.append('<div class="heat">')
    out.append('<div class="hd"></div>')
    for s in slots:
        out.append('<div class="hd">%s</div>' % esc(s))
    for i, wd in enumerate(hm["weekdays"]):
        out.append('<div class="rl">%s</div>' % esc(wd))
        for j in range(len(slots)):
            c = rows[i][j]
            bg = heat_color(c, mx)
            cls = "cl on" if (mx and c >= mx * 0.55) else "cl"
            out.append('<div class="%s" style="background:%s">%s</div>'
                       % (cls, bg, c if c else ""))
    out.append('</div>')
    out.append('<div class="heat-legend"><span>少</span>')
    for f in (0.045, 0.28, 0.5, 0.72, 0.92):
        out.append('<span class="sw" style="background:rgba(218,41,28,%.3f)"></span>' % f)
    out.append('<span>多</span><span style="margin-left:10px">峰值 %d 单</span></div>' % mx)
    out.append('</div>')
    return "".join(out)


def sec_monthly(r):
    rows = r["monthly"]
    if not rows:
        return ""
    mx = max(x["orders"] for x in rows) or 1
    out = ['<div class="panel">']
    out.append('<h2><span class="dot"></span>月度节律</h2>')
    out.append('<div class="sub">柱高表示当月订单数，柱顶数字为单量。</div>')
    out.append('<div class="months">')
    for x in rows:
        h = max(4, int(round(x["orders"] / mx * 100)))
        label = x["month"].split("-")
        cap = "%s年%s月" % (label[0][2:], label[1]) if len(label) == 2 else x["month"]
        out.append('<div class="month-col">'
                   '<div class="month-bar" style="height:%d%%"><span>%d</span></div>'
                   '<div class="month-cap">%s</div></div>'
                   % (h, x["orders"], esc(cap)))
    out.append('</div></div>')
    return "".join(out)


def sec_bars(title, sub, rows, fmt, cls="", foot=""):
    if not rows:
        return ""
    out = ['<div class="panel">']
    out.append('<h2><span class="dot"></span>%s</h2>' % esc(title))
    if sub:
        out.append('<div class="sub">%s</div>' % esc(sub))
    out.append('<div class="bars">')
    mx = max(x["_w"] for x in rows) or 1
    for x in rows:
        w = max(2, int(round(x["_w"] / mx * 100)))
        out.append('<div class="bar-row">'
                   '<div class="bar-name">%s</div>'
                   '<div class="bar-track"><div class="bar-fill %s" style="width:%d%%"></div></div>'
                   '<div class="bar-val">%s</div></div>'
                   % (esc(x["_name"]), cls, w, fmt(x)))
    out.append('</div>')
    if foot:
        out.append('<div class="bar-foot">%s</div>' % foot)
    out.append('</div>')
    return "".join(out)


def sec_channels(r):
    rows = []
    for c in r["channel_mix"]:
        rows.append({"_name": c["channel"], "_w": c["orders"], "_d": c,
                     "_o": c["orders"], "_s": c["share"], "_y": c["spend_yuan"]})

    # 堂食/外带是另一个轴：beType 说「怎么取餐」，takeWay 说「在哪吃」。
    # 实测 beType 常常全场只有一个值（10 单全是「到店取餐」），
    # 单靠它这一维度就没有信息量，所以把 takeWay 补在下方。
    tw = r.get("takeway_mix") or []
    foot = ""
    if tw:
        parts = " · ".join("%s %d 单（%.0f%%）"
                           % (esc(t["takeway"]), t["orders"], t["share"] * 100) for t in tw)
        foot = '<b>堂食 / 外带</b>：%s' % parts
        if len(r["channel_mix"]) == 1:
            foot += '<br><span class="dim">本次样本里点餐方式只有一个取值，' \
                    '该维度的比较意义有限。</span>'

    return sec_bars(
        "点餐方式 · 你更常怎么吃",
        "五种点餐场景的订单占比。到店取餐 / 麦乐送 / 得来速 / 堂食外带 / 企业团餐。",
        rows,
        lambda x: "%d 单 · %s · %s" % (x["_o"], pct(x["_s"]), money(x["_y"])),
        foot=foot,
    )


def sec_categories(r):
    rows = []
    for c in r["category_mix"]:
        rows.append({"_name": c["category"], "_w": c["amount_yuan"], "_d": c,
                     "_q": c["quantity"], "_s": c["share"], "_y": c["amount_yuan"]})
    return sec_bars(
        "口味结构 · 钱花在哪一类",
        "按累计金额排序。品类由商品名称关键词启发式归类，非麦当劳官方分类。",
        rows,
        lambda x: "%d 份 · %s · %s" % (x["_q"], pct(x["_s"]), money(x["_y"])),
        cls="gold",
    )


def sec_top_items(r):
    rows = []
    for c in r["top_items"]:
        rows.append({"_name": c["name"], "_w": c["quantity"], "_d": c,
                     "_q": c["quantity"], "_y": c["amount_yuan"], "_o": c["orders"]})
    return sec_bars(
        "常点单品 TOP 12",
        "按累计份数排序，括号内为出现在多少笔订单中。",
        rows,
        lambda x: "%d 份 · %d 单 · %s" % (x["_q"], x["_o"], money(x["_y"])),
    )


def sec_geo(r):
    cities = r["geo"]["cities"]
    if not cities:
        return ""
    out = ['<div class="panel">']
    out.append('<h2><span class="dot"></span>城市足迹 · 吃过的店</h2>')
    out.append('<div class="sub">官方接口不返回门店经纬度，因此这一维按「城市 → 区 → 门店」的'
               '行政区划层级呈现，不做地理坐标绘制。</div>')
    out.append('<div class="cities">')
    for c in cities:
        provinces = ("%s · " % c["province"]) if c["province"] else ""
        ds = c["districts"]
        if ds:
            districts = " · ".join(ds[:4]) + (" 等 %d 个区" % len(ds) if len(ds) > 4 else "")
        else:
            districts = "—"
        top = c["stores"][0] if c["stores"] else None
        out.append('<div class="city"><div class="top">'
                   '<span class="name">%s</span>'
                   '<span class="badge">%d 单</span></div>'
                   '<div class="meta">%s%s</div>'
                   % (esc(c["city"]), c["orders"], esc(provinces), esc(districts)))
        if top:
            out.append('<div class="store">常去：<b>%s</b>（%d 单）<br>%s</div>'
                       % (esc(top["store_name"]), top["orders"], esc(top["address"])))
        out.append('<div class="spend">累计 %s · %d 家门店</div></div>'
                   % (money(c["spend_yuan"]), len(c["stores"])))
    out.append('</div></div>')
    return "".join(out)


def sec_discount(r):
    d = r["discount_ledger"]
    out = ['<div class="panel ledger">']
    out.append('<h2><span class="dot"></span>优惠账本</h2>')
    out.append('<div class="sub">这一节回答的不是「卡值不值」，而是「省下的钱从哪来」：'
               '原价多少、实付多少、差额由哪几张券贡献。'
               '接口不返回持卡标识，因此不区分麦金卡/非麦金卡订单。</div>')

    out.append('<div class="ledger-grid">')
    out.append('<div><div class="k">折前原价</div><div class="v">%s</div></div>'
               % money(d["original_yuan"]))
    out.append('<div><div class="k">实际支付</div><div class="v">%s</div></div>'
               % money(d["paid_yuan"]))
    out.append('<div><div class="k">累计省下</div><div class="v good">%s</div></div>'
               % money(d["saved_yuan"]))
    out.append('<div><div class="k">节省比例</div><div class="v">%.1f%%</div></div>'
               % ((d["save_rate"] or 0) * 100))
    out.append('</div>')

    if d["top_coupons"]:
        out.append('<div class="cp-title">券的贡献</div>')
        out.append('<div class="cp-list">')
        top_amt = d["top_coupons"][0]["saved_yuan"] or 1
        for c in d["top_coupons"]:
            w = max(2.0, min(100.0, (c["saved_yuan"] / top_amt) * 100))
            out.append('<div class="cp"><div class="cp-n">%s</div>'
                       '<div class="cp-bar"><i style="width:%.1f%%"></i></div>'
                       '<div class="cp-v">%d 次 · %s</div></div>'
                       % (esc(c["name"]), w, c["times"], money(c["saved_yuan"])))
        out.append('</div>')

    if d["best_saving"]:
        b = d["best_saving"]
        orig = b.get("original_yuan") or 0
        fold = ((b["paid_yuan"] or 0) / orig * 10) if orig else None
        if fold and fold < 9.95:
            out.append('<div class="verdict">省钱最多的一单是 <b>%s</b> 在「%s」：'
                       '原价 <b>%s</b> → 只花了 <b>%s</b>，省下 <b>%s</b>，'
                       '相当于 <b>%.1f</b> 折。</div>'
                       % (b["date"], esc(b["store_name"]), money(orig),
                          money(b["paid_yuan"]), money(b["saved_yuan"]), fold))
        else:
            out.append('<div class="verdict">省钱最多的一单是 <b>%s</b> 在「%s」：'
                       '只花了 <b>%s</b>，省下 <b>%s</b>。</div>'
                       % (b["date"], esc(b["store_name"]), money(b["paid_yuan"]),
                          money(b["saved_yuan"])))
    if d["no_coupon_orders"]:
        no_rate = (1 - (d["coupon_order_rate"] or 0)) * 100
        out.append('<div class="verdict">%d 单没用券，占 <b>%.0f%%</b>。</div>'
                   % (d["no_coupon_orders"], no_rate))
    out.append('</div>')
    return "".join(out)


def sec_notes(r):
    meta = r["meta"]
    out = ['<div class="panel">']
    out.append('<h2><span class="dot"></span>口径与存疑提示</h2>')
    out.append('<div class="sub">读任何数字之前先看这里——这些是数字成立的前提。</div>')
    out.append('<ul class="notes">')
    items = [
        ("数据来源", "%s（%s），调用工具 %s。" % (
            meta.get("data_source", ""), meta.get("endpoint", ""),
            "now-time-info / order-list / query-order")),
        ("样本区间", "本次共 %d 单，覆盖 %s ~ %s。%s" % (
            meta.get("sample_count", 0), r["summary"]["first_order"],
            r["summary"]["last_order"], meta.get("coverage_note", ""))),
        ("时区", meta.get("timezone_note", "")),
        ("金额", meta.get("amount_note", "")),
        ("品类", meta.get("category_note", "")),
        ("城市", meta.get("city_note", "")),
        ("时段", meta.get("slot_note", "")),
    ]
    for k, v in items:
        out.append('<li><b>%s：</b>%s</li>' % (esc(k), esc(v)))
    out.append('</ul>')

    ws = r.get("warnings", [])
    if ws:
        out.append('<ul class="warns">')
        for w in ws:
            out.append('<li class="%s"><b>[%s]</b> %s</li>'
                       % (esc(w.get("level", "warn")),
                          esc(w.get("level", "warn").upper()), esc(w.get("message", ""))))
        out.append('</ul>')
    else:
        out.append('<div class="ok-line" style="margin-top:14px">'
                   '数据一致性检查通过：每一单的商品明细加总与其声明小计一致，未发现对不上的数字。'
                   '</div>')
    out.append('</div>')
    return "".join(out)


def share_card_inner(r):
    """分享卡的内部内容（不含外层容器）。报告内嵌与独立卡片文件共用同一份。"""
    s = r["summary"]
    hm = r["time_heatmap"]
    mx = hm["max"]

    out = []
    out.append('<div class="sc-brand">MCD · Footprint</div>')
    out.append('<div class="sc-title">我的<em>麦当劳</em>足迹</div>')
    out.append('<div class="sc-sub">%s ~ %s · 共 %d 天</div>'
               % (esc(s["first_order"][:10]), esc(s["last_order"][:10]), s["span_days"]))

    out.append('<div class="sc-nums">')
    out.append('<div class="sc-num hi"><div class="n">%d<small>单</small></div>'
               '<div class="c">累计订单</div></div>' % s["order_count"])
    out.append('<div class="sc-num"><div class="n">%s</div><div class="c">总消费</div></div>'
               % money(s["total_yuan"]))
    out.append('<div class="sc-num"><div class="n">%d<small>城</small></div>'
               '<div class="c">足迹城市</div></div>' % s["city_count"])
    out.append('<div class="sc-num"><div class="n">%d<small>家</small></div>'
               '<div class="c">吃过门店</div></div>' % s["store_count"])
    out.append('</div>')

    # 热力
    out.append('<div class="sc-block"><div class="sc-block-t">一周吃麦当劳的时间分布</div>')
    out.append('<div class="sc-heat">')
    out.append('<div class="l"></div>')
    for sl in hm["slots"]:
        out.append('<div class="l" style="justify-content:center;font-size:14px">%s</div>' % esc(sl))
    for i, wd in enumerate(hm["weekdays"]):
        out.append('<div class="l">%s</div>' % esc(wd))
        for j in range(len(hm["slots"])):
            c = hm["matrix"][i][j]
            out.append('<div class="c" style="background:%s"></div>' % heat_color(c, mx))
    out.append('</div></div>')

    # 城市
    cities = r["geo"]["cities"][:7]
    if cities:
        out.append('<div class="sc-block"><div class="sc-block-t">足迹城市</div>')
        out.append('<div class="sc-chips">')
        for c in cities:
            cls = "sc-chip" if c["orders"] >= 3 else "sc-chip plain"
            out.append('<span class="%s">%s · %d 单</span>'
                       % (cls, esc(c["city"]), c["orders"]))
        out.append('</div></div>')

    # 最爱单品
    tops = r["top_items"][:3]
    if tops:
        out.append('<div class="sc-block"><div class="sc-block-t">常点单品 TOP 3</div>')
        out.append('<div class="sc-items">')
        for i, t in enumerate(tops):
            out.append('<div class="sc-item"><div class="r">NO.%d</div>'
                       '<div class="nm">%s</div><div class="q">%d 份 · %d 单</div></div>'
                       % (i + 1, esc(t["name"]), t["quantity"], t["orders"]))
        out.append('</div></div>')

    # 优惠账本
    d = r["discount_ledger"]
    if d["saved_yuan"] > 0:
        out.append('<div class="sc-ledger">')
        out.append('<div><div class="t">累计省下</div>'
                   '<div class="m"><b>%s</b></div></div>' % money(d["saved_yuan"]))
        out.append('<div class="r">原价 %s · 实付 %s<br>节省 <b>%.0f%%</b></div>'
                   % (money(d["original_yuan"]), money(d["paid_yuan"]),
                      (d["save_rate"] or 0) * 100))
        out.append('</div>')

    out.append('<div class="sc-foot"><span>数据源：麦当劳中国 MCP · order-list</span>'
               '<span><b>mcd-footprint</b></span></div>')
    return "".join(out)


def sec_share_card(r):
    out = ['<div class="card-section">']
    out.append('<h2 style="font-size:15px;font-weight:800;margin-bottom:6px">分享卡</h2>')
    out.append('<div class="card-hint">下面这张 1080×1920 的竖版卡片（9:16，手机全屏比例）'
               '可以直接截图或长按保存，用于发朋友圈 / 微博 / 放进 README。'
               '页面里按 50% 缩放显示；加 --card-output 参数还能单独导出一份'
               '「打开即整卡」的 HTML。</div>')
    out.append('<div class="card-scroll"><div class="share-card">')
    out.append(share_card_inner(r))
    out.append('</div></div></div>')
    return "".join(out)


def build_card_html(r):
    """独立分享卡页面：打开就是一张 1:1 的竖版卡片，不需要滚动，直接截图即可。"""
    return ('<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>麦麦足迹 · 分享卡</title><style>' + CSS + '</style></head>'
            '<body style="background:#efe7dd;display:flex;align-items:flex-start;'
            'justify-content:center;min-height:100vh;padding:40px 20px">'
            '<div class="share-card" style="zoom:1">' + share_card_inner(r) + '</div>'
            '</body></html>')


# ============================================================
# 主装配
# ============================================================


def build_html(r):
    s = r["summary"]

    out = ['<!DOCTYPE html>', '<html lang="zh-CN"><head>',
           '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           '<title>麦麦足迹 · 我的麦当劳消费画像</title>',
           '<style>', CSS, '</style>', '</head><body><div class="wrap">']

    # 页头
    out.append('<div class="masthead">')
    out.append('<div class="eyebrow">McDonald\'s China MCP · Footprint</div>')
    out.append('<h1>我的<span class="accent">麦当劳足迹</span></h1>')
    out.append('<p class="lede">把 <b>%d 笔</b>真实订单，变成一张消费画像——'
               '在哪吃、什么时候吃、怎么点、爱吃什么、省下的钱从哪来。'
               '数据全部来自你本人的麦当劳 MCP 订单，不联网、不上传。</p>' % s["order_count"])
    out.append('</div>')

    # Hero
    out.append('<div class="hero">')
    out.append('<div class="cell hi"><div class="num">%d<small>单</small></div>'
               '<div class="cap">累计订单</div></div>' % s["order_count"])
    out.append('<div class="cell"><div class="num">%s</div><div class="cap">总消费</div></div>'
               % money(s["total_yuan"]))
    out.append('<div class="cell"><div class="num">%d<small>城 %d 店</small></div>'
               '<div class="cap">足迹覆盖</div></div>' % (s["city_count"], s["store_count"]))
    out.append('<div class="cell"><div class="num">%s</div><div class="cap">优惠累计省下</div></div>'
               % money(s["saved_yuan"]))
    out.append('</div>')

    # 亮点
    if r.get("highlights"):
        out.append('<div class="panel"><h2><span class="dot"></span>一眼看点</h2>')
        out.append('<div class="sub">由聚合结果自动生成，每一条都能在上方数字里核对到。</div>')
        out.append('<ul class="highlights">')
        for h in r["highlights"]:
            out.append('<li>%s</li>' % esc(h))
        out.append('</ul></div>')

    # 主体
    out.append(sec_heatmap(r))
    out.append(sec_monthly(r))
    out.append(sec_channels(r))
    out.append(sec_categories(r))
    out.append(sec_top_items(r))
    out.append(sec_geo(r))
    out.append(sec_discount(r))
    out.append(sec_notes(r))
    out.append(sec_share_card(r))

    out.append('<div class="foot">本报告由 <b>mcd-footprint</b> 本地生成 · 生成时间 %s<br>'
               '本项目为「2026 麦当劳程序员创意开发大赛」参赛作品，非麦当劳官方产品 · MIT License</div>'
               % esc(r["meta"].get("generated_at", "")))

    out.append('</div></body></html>')
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description="把聚合 JSON 渲染成 HTML 报告")
    ap.add_argument("--input", required=True, help="聚合结果 JSON 路径")
    ap.add_argument("--output", required=True, help="HTML 报告输出路径")
    ap.add_argument("--card-output", default=None,
                    help="可选：额外导出一份只含分享卡的独立 HTML（打开即可截图）")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        sys.stderr.write("找不到输入文件：%s\n" % args.input)
        return 2

    with io.open(args.input, "r", encoding="utf-8") as f:
        r = json.load(f)

    html = build_html(r)
    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with io.open(args.output, "w", encoding="utf-8") as f:
        f.write(html)

    size_kb = os.path.getsize(args.output) / 1024.0
    sys.stdout.write("报告已生成 → %s（%.1f KB，自包含、无外部依赖）\n"
                     % (args.output, size_kb))

    if args.card_output:
        card = build_card_html(r)
        cdir = os.path.dirname(os.path.abspath(args.card_output))
        if cdir and not os.path.isdir(cdir):
            os.makedirs(cdir)
        with io.open(args.card_output, "w", encoding="utf-8") as f:
            f.write(card)
        sys.stdout.write("分享卡已生成 → %s（%.1f KB）\n"
                         % (args.card_output, os.path.getsize(args.card_output) / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
