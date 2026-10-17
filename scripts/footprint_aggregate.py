#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcd-footprint · 消费足迹五维聚合器

把麦当劳中国 MCP（order-list + query-order）返回的历史订单，
聚合成一张消费画像的五维数据，供 render_report.py 渲染成 HTML 报告。

设计原则
--------
1. 口径先行：所有判定规则集中在本文件顶部的常量区，任何结论都能回溯到规则。
2. 可加总核对：脚本自己重算每一单的小计并与其声明值比对，不一致即告警。
3. 不臆造：门店坐标、结构化城市字段官方并不返回，本脚本只从地址文本解析，
   解析不到就留空并告警，绝不填充猜测值。
4. 样本量诚实：样本不足时主动降级告警，不给出看起来精确、实则无意义的分布结论。

用法
----
    python footprint_aggregate.py --input orders.json --output footprint.json
    python footprint_aggregate.py --input orders.json \\
        --card-start 2026-08 --card-fee-fen 1900
"""

import argparse
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

# ============================================================
# 口径定义区（修改口径请只改这里）
# ============================================================

# --- 时段划分：左闭右开，单位为「当日第几分钟」 ---
# 深夜 00:00-05:59 / 早餐 06:00-10:29 / 午市 10:30-13:59
# 下午茶 14:00-16:59 / 晚市 17:00-20:59 / 夜宵 21:00-23:59
SLOT_BOUNDS = [
    ("深夜", 0, 360),
    ("早餐", 360, 630),
    ("午市", 630, 840),
    ("下午茶", 840, 1020),
    ("晚市", 1020, 1260),
    ("夜宵", 1260, 1440),
]
SLOT_NAMES = [s[0] for s in SLOT_BOUNDS]

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# --- 点餐方式：官方 MCP 的点餐场景口径 ---
KNOWN_CHANNELS = ["到店取餐", "麦乐送", "得来速", "堂食外带", "企业团餐"]

# --- 品类归类：按商品名称关键词做启发式归类，非官方分类 ---
# 顺序即优先级，命中即停。早餐与汉堡规则靠前，避免「满福堡」「麦满分」被「堡」字误伤。
CATEGORY_RULES = [
    ("早餐", ["麦满分", "满福堡", "热香饼", "薯饼", "早餐", "油条", "蛋堡", "粥"]),
    ("汉堡", ["巨无霸", "麦辣鸡腿汉堡", "板烧", "麦香鸡", "吉士", "安格斯", "鳕鱼",
              "不素之霸", "虾堡", "脆汁鸡", "双牛", "汉堡", "堡"]),
    ("咖啡", ["麦咖啡", "拿铁", "美式", "卡布", "摩卡", "澳白", "浓缩", "冷萃"]),
    ("冰品甜点", ["新地", "麦旋风", "圆筒", "冰淇淋", "冰激凌", "甜筒", "圣代", "蛋糕", "派"]),
    ("小食", ["薯条", "麦乐鸡", "鸡块", "鸡翅", "香骨鸡", "玉米杯", "苹果片", "沙拉", "扭扭"]),
    ("饮料", ["可乐", "雪碧", "芬达", "橙汁", "柠檬茶", "冰红茶", "豆浆", "矿泉水", "果汁", "茶"]),
]
DEFAULT_CATEGORY = "其他"

# --- 地址解析 ---
MUNICIPALITIES = ["北京市", "上海市", "天津市", "重庆市"]

# --- 样本量阈值 ---
MIN_SAMPLE_OK = 15   # 低于此值：分布类结论稳定性有限
MIN_SAMPLE_HARD = 5  # 低于此值：不足以支撑分布类结论

# --- 默认麦金卡月费（分） ---
DEFAULT_CARD_FEE_FEN = 1900

# ============================================================
# 基础工具
# ============================================================


def to_yuan(fen):
    """分 → 元，保留两位小数。"""
    return round(fen / 100.0, 2)


def parse_time(raw):
    """解析订单时间。官方字段不含时区，统一按 UTC+8 解释（不做换算，仅作口径声明）。"""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def slot_of(dt):
    """返回该时刻所属时段名。"""
    minutes = dt.hour * 60 + dt.minute
    for name, lo, hi in SLOT_BOUNDS:
        if lo <= minutes < hi:
            return name
    return SLOT_NAMES[0]


def categorize(item_name):
    """按关键词把商品归入品类。命中即停，未命中归「其他」。"""
    if not item_name:
        return DEFAULT_CATEGORY
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in item_name:
                return category
    return DEFAULT_CATEGORY


def parse_location(address):
    """
    从门店地址文本解析 (province, city, district)。

    官方 MCP 不返回结构化的城市字段，也没有经纬度，这里只做文本切分。
    解析不到的部分返回空串——不做任何猜测填充。
    """
    if not address or not isinstance(address, str):
        return "", "", ""
    addr = address.strip()
    province = ""
    city = ""
    district = ""
    rest = addr

    # 直辖市优先
    for muni in MUNICIPALITIES:
        if addr.startswith(muni):
            city = muni
            rest = addr[len(muni):]
            break
    else:
        m = re.match(r"^(.{2,3}?(?:省|自治区|特别行政区))", addr)
        if m:
            province = m.group(1)
            rest = addr[len(province):]
        m = re.match(r"^(.{2,5}?(?:市|自治州|地区|盟))", rest)
        if m:
            city = m.group(1)
            rest = rest[len(city):]

    m = re.match(r"^(.{2,5}?(?:区|县|市|旗))", rest)
    if m:
        district = m.group(1)

    return province, city, district


def load_orders(path):
    """读取订单文件，返回 (source_meta, orders)。"""
    with io.open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {}, data
    return data.get("source", {}) or {}, data.get("orders", []) or []


# ============================================================
# 聚合
# ============================================================


def build_report(orders, card_start=None, card_fee_fen=DEFAULT_CARD_FEE_FEN, source_meta=None):
    warnings = []
    parsed = []

    for idx, o in enumerate(orders):
        dt = parse_time(o.get("order_time"))
        if dt is None:
            warnings.append({
                "level": "warn",
                "message": "订单 %s 的下单时间无法解析（原值 %r），已跳过该单的时间维度。"
                           % (o.get("order_id", "#%d" % idx), o.get("order_time")),
            })
            continue

        store = o.get("store", {}) or {}
        address = store.get("address", "")
        province, city, district = parse_location(address)
        if not city:
            warnings.append({
                "level": "warn",
                "message": "订单 %s 的门店地址无法解析出城市：%r。该单不计入城市维度。"
                           % (o.get("order_id", "#%d" % idx), address),
            })

        items = o.get("items", []) or []
        calc_subtotal = 0
        norm_items = []
        for it in items:
            qty = int(it.get("quantity", 1) or 1)
            amt = int(it.get("amount_fen", 0) or 0)
            calc_subtotal += amt * qty
            norm_items.append({
                "name": it.get("name", ""),
                "quantity": qty,
                "amount_fen": amt,
                "category": categorize(it.get("name", "")),
            })

        declared = o.get("subtotal_fen")
        if isinstance(declared, int) and declared != calc_subtotal:
            warnings.append({
                "level": "warn",
                "message": "订单 %s 的小计对不上：明细加总 %d 分，订单声明 %d 分，差 %d 分。"
                           % (o.get("order_id", "#%d" % idx), calc_subtotal, declared,
                              declared - calc_subtotal),
            })

        discount = int(o.get("discount_fen", 0) or 0)
        total = o.get("total_fen")
        if not isinstance(total, int):
            total = calc_subtotal - discount
        elif calc_subtotal - discount != total:
            warnings.append({
                "level": "warn",
                "message": "订单 %s 的实付对不上：小计 %d − 优惠 %d ≠ 实付 %d。"
                           % (o.get("order_id", "#%d" % idx), calc_subtotal, discount, total),
            })

        parsed.append({
            "order_id": o.get("order_id", ""),
            "dt": dt,
            "channel": o.get("channel", "未知"),
            "store_code": store.get("store_code", ""),
            "store_name": store.get("store_name", ""),
            "address": address,
            "province": province,
            "city": city,
            "district": district,
            "items": norm_items,
            "subtotal_fen": calc_subtotal,
            "discount_fen": discount,
            "total_fen": total,
            "meal_card": o.get("meal_card") or None,
        })

    n = len(parsed)
    if n == 0:
        warnings.append({"level": "error", "message": "没有任何可解析的订单，未生成任何分布结论。"})
    elif n < MIN_SAMPLE_HARD:
        warnings.append({
            "level": "error",
            "message": "样本量仅 %d 单（<%d），不足以支撑时间热力与品类分布结论，"
                       "以下分布数据仅供参考。" % (n, MIN_SAMPLE_HARD),
        })
    elif n < MIN_SAMPLE_OK:
        warnings.append({
            "level": "warn",
            "message": "样本量 %d 单（<%d），时间热力与分布结论的稳定性有限。" % (n, MIN_SAMPLE_OK),
        })

    # ---------- 概览 ----------
    total_fen = sum(p["total_fen"] for p in parsed)
    saved_fen = sum(p["discount_fen"] for p in parsed)
    item_qty = sum(i["quantity"] for p in parsed for i in p["items"])
    cities = set(p["city"] for p in parsed if p["city"])
    stores = set(p["store_code"] or p["store_name"] for p in parsed if (p["store_code"] or p["store_name"]))
    times = sorted(p["dt"] for p in parsed)

    summary = {
        "order_count": n,
        "total_yuan": to_yuan(total_fen),
        "saved_yuan": to_yuan(saved_fen),
        "item_qty": item_qty,
        "city_count": len(cities),
        "store_count": len(stores),
        "first_order": times[0].strftime("%Y-%m-%d %H:%M") if times else "",
        "last_order": times[-1].strftime("%Y-%m-%d %H:%M") if times else "",
        "span_days": (times[-1] - times[0]).days + 1 if times else 0,
    }

    # ---------- 时间热力：周 × 时段 ----------
    matrix = [[0] * len(SLOT_NAMES) for _ in range(7)]
    for p in parsed:
        matrix[p["dt"].weekday()][SLOT_NAMES.index(slot_of(p["dt"]))] += 1
    heat_max = max((max(row) for row in matrix), default=0)

    # ---------- 小时分布 ----------
    hour_hist = [0] * 24
    for p in parsed:
        hour_hist[p["dt"].hour] += 1

    # ---------- 星期 / 月度 ----------
    wd_counter = Counter()
    wd_spend = Counter()
    month_counter = Counter()
    month_spend = Counter()
    for p in parsed:
        wd = WEEKDAY_NAMES[p["dt"].weekday()]
        wd_counter[wd] += 1
        wd_spend[wd] += p["total_fen"]
        mk = p["dt"].strftime("%Y-%m")
        month_counter[mk] += 1
        month_spend[mk] += p["total_fen"]

    weekday_mix = [{"weekday": w, "orders": wd_counter.get(w, 0),
                    "spend_yuan": to_yuan(wd_spend.get(w, 0))} for w in WEEKDAY_NAMES]
    monthly = [{"month": m, "orders": month_counter[m], "spend_yuan": to_yuan(month_spend[m])}
               for m in sorted(month_counter)]

    # ---------- 点餐方式 ----------
    ch_counter = Counter()
    ch_spend = Counter()
    for p in parsed:
        ch_counter[p["channel"]] += 1
        ch_spend[p["channel"]] += p["total_fen"]
    channel_mix = []
    for ch, cnt in ch_counter.most_common():
        channel_mix.append({
            "channel": ch,
            "orders": cnt,
            "spend_yuan": to_yuan(ch_spend[ch]),
            "share": round(cnt / n, 4) if n else 0,
            "known": ch in KNOWN_CHANNELS,
        })

    # ---------- 品类 ----------
    cat_qty = Counter()
    cat_amt = Counter()
    for p in parsed:
        for i in p["items"]:
            cat_qty[i["category"]] += i["quantity"]
            cat_amt[i["category"]] += i["amount_fen"] * i["quantity"]
    total_cat_amt = sum(cat_amt.values()) or 1
    category_mix = [{
        "category": c,
        "quantity": cat_qty[c],
        "amount_yuan": to_yuan(cat_amt[c]),
        "share": round(cat_amt[c] / total_cat_amt, 4),
    } for c in sorted(cat_qty, key=lambda x: -cat_amt[x])]

    # ---------- 单品 TOP ----------
    item_qty_c = Counter()
    item_amt_c = Counter()
    item_order_c = defaultdict(set)
    for p in parsed:
        for i in p["items"]:
            item_qty_c[i["name"]] += i["quantity"]
            item_amt_c[i["name"]] += i["amount_fen"] * i["quantity"]
            item_order_c[i["name"]].add(p["order_id"])
    top_items = [{
        "name": name,
        "quantity": item_qty_c[name],
        "amount_yuan": to_yuan(item_amt_c[name]),
        "orders": len(item_order_c[name]),
    } for name in sorted(item_qty_c, key=lambda x: (-item_qty_c[x], -item_amt_c[x]))[:12]]

    # ---------- 地理足迹（城市 → 门店） ----------
    city_agg = {}
    for p in parsed:
        if not p["city"]:
            continue
        c = city_agg.setdefault(p["city"], {
            "city": p["city"], "province": p["province"],
            "orders": 0, "spend_fen": 0,
            "districts": Counter(), "stores": {},
        })
        c["orders"] += 1
        c["spend_fen"] += p["total_fen"]
        if p["district"]:
            c["districts"][p["district"]] += 1
        skey = p["store_code"] or p["store_name"]
        s = c["stores"].setdefault(skey, {
            "store_name": p["store_name"], "address": p["address"],
            "orders": 0, "spend_fen": 0,
        })
        s["orders"] += 1
        s["spend_fen"] += p["total_fen"]

    city_list = []
    for c in sorted(city_agg.values(), key=lambda x: (-x["orders"], -x["spend_fen"])):
        city_list.append({
            "city": c["city"],
            "province": c["province"],
            "orders": c["orders"],
            "spend_yuan": to_yuan(c["spend_fen"]),
            "districts": [d for d, _ in c["districts"].most_common()],
            "stores": [{
                "store_name": s["store_name"],
                "address": s["address"],
                "orders": s["orders"],
                "spend_yuan": to_yuan(s["spend_fen"]),
            } for s in sorted(c["stores"].values(), key=lambda x: (-x["orders"], -x["spend_fen"]))],
        })
    geo = {
        "cities": city_list,
        "max_city_orders": max((c["orders"] for c in city_list), default=0),
    }

    # ---------- 麦金卡账本 ----------
    card_orders = [p for p in parsed if p["meal_card"] and p["meal_card"].get("applied")]
    mealcard = None
    if card_orders:
        card_saved = sum(int((p["meal_card"] or {}).get("saved_fen", 0) or 0) for p in card_orders)
        card_times = sorted(p["dt"] for p in card_orders)
        first_card = card_times[0]
        last_time = times[-1] if times else first_card
        months = (last_time.year - first_card.year) * 12 + (last_time.month - first_card.month) + 1
        months = max(months, 1)
        cost_fen = months * card_fee_fen
        net_fen = card_saved - cost_fen
        mealcard = {
            "has_card": True,
            "card_name": (card_orders[0]["meal_card"] or {}).get("card_name", "麦金卡"),
            "first_card_order": first_card.strftime("%Y-%m-%d"),
            "months_held": months,
            "monthly_fee_yuan": to_yuan(card_fee_fen),
            "cost_yuan": to_yuan(cost_fen),
            "saved_yuan": to_yuan(card_saved),
            "net_yuan": to_yuan(net_fen),
            "roi": round(card_saved / cost_fen, 2) if cost_fen else None,
            "card_orders": len(card_orders),
            "plain_orders": n - len(card_orders),
            "note": "持卡月数按「首笔用卡订单所在月 → 末笔订单所在月」的自然月数估算，"
                    "月费取默认值，可在参数中调整。",
        }
    else:
        mealcard = {
            "has_card": False,
            "note": "本次返回的订单中未见麦金卡权益记录。可能是未持卡，"
                    "也可能是官方 order-list 未回传该字段（该字段的可见性需以实际返回为准）。",
        }

    # ---------- 自动洞察 ----------
    highlights = []
    if city_list:
        top_city = city_list[0]
        highlights.append("足迹覆盖 %d 座城市、%d 家门店。" % (len(city_list), len(stores)))
        top_store = top_city["stores"][0] if top_city["stores"] else None
        if top_store:
            highlights.append("去得最多的是「%s」，共 %d 单（%s）。"
                              % (top_store["store_name"], top_store["orders"], top_city["city"]))
    if heat_max > 0:
        flat = [(matrix[r][c], r, c) for r in range(7) for c in range(len(SLOT_NAMES))]
        flat.sort(reverse=True)
        cnt, r, c = flat[0]
        highlights.append("最活跃的时段是 %s %s，共 %d 单。" % (WEEKDAY_NAMES[r], SLOT_NAMES[c], cnt))
    if channel_mix:
        highlights.append("最常用的点餐方式是「%s」，占 %.0f%%。"
                          % (channel_mix[0]["channel"], channel_mix[0]["share"] * 100))
    if top_items:
        highlights.append("点得最多的单品是「%s」，累计 %d 份。"
                          % (top_items[0]["name"], top_items[0]["quantity"]))
    if mealcard and mealcard.get("has_card"):
        if mealcard["net_yuan"] >= 0:
            highlights.append("持卡 %d 个月，累计省下 %s 元，扣掉 %s 元月费后净赚 %s 元（%.2f 倍回本）。"
                              % (mealcard["months_held"], mealcard["saved_yuan"],
                                 mealcard["cost_yuan"], mealcard["net_yuan"], mealcard["roi"]))
        else:
            highlights.append("持卡 %d 个月，累计省下 %s 元，仍未覆盖 %s 元月费，净支出 %s 元。"
                              % (mealcard["months_held"], mealcard["saved_yuan"],
                                 mealcard["cost_yuan"], abs(mealcard["net_yuan"])))

    # ---------- 口径元信息 ----------
    meta = {
        "skill": "mcd-footprint",
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data_source": (source_meta or {}).get("provider", "麦当劳中国 MCP · order-list + query-order"),
        "endpoint": (source_meta or {}).get("endpoint", "https://mcp.mcd.cn"),
        "sample_count": n,
        "coverage_note": "官方 order-list 仅返回近期订单，不是全年完整历史；"
                         "本报告的分布结论仅代表该样本区间。",
        "timezone_note": "order-list 返回的下单时间字段不含时区，本报告统一按 UTC+8 解释。",
        "amount_note": "接口金额原始单位为分，本报告已统一换算为元（保留两位小数）。",
        "category_note": "品类由商品名称关键词启发式归类，非麦当劳官方分类。",
        "city_note": "城市与区由门店地址文本解析，官方不返回结构化城市字段，也不返回经纬度。",
        "slot_note": "时段划分：深夜 00:00-05:59 / 早餐 06:00-10:29 / 午市 10:30-13:59 / "
                     "下午茶 14:00-16:59 / 晚市 17:00-20:59 / 夜宵 21:00-23:59。",
    }

    return {
        "meta": meta,
        "summary": summary,
        "highlights": highlights,
        "time_heatmap": {
            "slots": SLOT_NAMES,
            "weekdays": WEEKDAY_NAMES,
            "matrix": matrix,
            "max": heat_max,
        },
        "hour_hist": hour_hist,
        "weekday_mix": weekday_mix,
        "monthly": monthly,
        "channel_mix": channel_mix,
        "category_mix": category_mix,
        "top_items": top_items,
        "geo": geo,
        "mealcard": mealcard,
        "warnings": warnings,
    }


# ============================================================
# 入口
# ============================================================


def main():
    ap = argparse.ArgumentParser(
        description="把麦当劳 MCP 历史订单聚合成消费足迹五维数据")
    ap.add_argument("--input", required=True, help="规范化订单 JSON 路径")
    ap.add_argument("--output", required=True, help="聚合结果 JSON 输出路径")
    ap.add_argument("--card-start", default=None,
                    help="麦金卡起始月（YYYY-MM），仅用于覆盖自动推算，一般无需指定")
    ap.add_argument("--card-fee-fen", type=int, default=DEFAULT_CARD_FEE_FEN,
                    help="麦金卡月费（分），默认 1900 即 19 元/月")
    args = ap.parse_args()

    if not os.path.exists(args.input):
        sys.stderr.write("找不到输入文件：%s\n" % args.input)
        return 2

    source_meta, orders = load_orders(args.input)
    report = build_report(orders, card_start=args.card_start,
                          card_fee_fen=args.card_fee_fen, source_meta=source_meta)

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir and not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    with io.open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    s = report["summary"]
    lines = [
        "聚合完成 → %s" % args.output,
        "订单 %d 单 | 消费 %s 元 | 节省 %s 元 | %d 城 %d 店 | %s ~ %s（%d 天）"
        % (s["order_count"], s["total_yuan"], s["saved_yuan"], s["city_count"],
           s["store_count"], s["first_order"], s["last_order"], s["span_days"]),
        "告警 %d 条" % len(report["warnings"]),
    ]
    for w in report["warnings"]:
        lines.append("  [%s] %s" % (w["level"], w["message"]))
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
