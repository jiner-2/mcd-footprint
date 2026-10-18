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
    ("咖啡", ["麦咖啡", "咖啡", "拿铁", "美式", "卡布", "摩卡", "澳白", "浓缩", "冷萃"]),
    ("冰品甜点", ["新地", "麦旋风", "圆筒", "冰淇淋", "冰激凌", "甜筒", "圣代", "蛋糕", "派"]),
    ("小食", ["薯条", "麦乐鸡", "鸡块", "鸡翅", "翅", "香骨鸡", "玉米杯", "苹果片",
              "沙拉", "扭扭", "鸡排", "鸡腿"]),
    ("饮料", ["可乐", "雪碧", "芬达", "橙汁", "柠檬茶", "冰红茶", "豆浆", "矿泉水", "果汁", "茶"]),
    # 配料/自定义项：这些不该算进「其他」，否则「其他」会被调味包撑起来，
    # 让真正的未归类商品藏在一个虚高的数字后面
    ("配料", ["奶油球", "白砂糖", "糖浆", "酱", "餐具", "打包费", "配送费"]),
]
DEFAULT_CATEGORY = "其他"

# --- 地址解析 ---
MUNICIPALITIES = ["北京市", "上海市", "天津市", "重庆市"]

# --- 样本量阈值 ---
MIN_SAMPLE_OK = 15   # 低于此值：分布类结论稳定性有限
MIN_SAMPLE_HARD = 5  # 低于此值：不足以支撑分布类结论

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


def build_report(orders, source_meta=None):
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
        city_source = "地址文本" if city else ""
        # 实测 storeAddress 不含城市（如「东山街道玖力大厦…」），城市只出现在门店名里
        # （「麦当劳南京万泰路得来速餐厅」）。地址解析不出时用门店名切出的城市兜底，
        # 并在口径里标明来源，不把两种来源混为一谈。
        if not city:
            hint = (store.get("city_hint") or "").strip()
            if hint:
                city = hint
                city_source = "门店名前缀"
        if not city:
            warnings.append({
                "level": "warn",
                "message": "订单 %s 的门店地址无法解析出城市（地址 %r），门店名也切不出城市。"
                           "该单不计入城市维度。" % (o.get("order_id", "#%d" % idx), address),
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
            "takeway": o.get("takeway", "未知"),
            "store_code": store.get("store_code", ""),
            "store_name": store.get("store_name", ""),
            "address": address,
            "province": province,
            "city": city,
            "city_source": city_source,
            "district": district,
            "items": norm_items,
            "subtotal_fen": calc_subtotal,
            "discount_fen": discount,
            "total_fen": total,
            "coupons": o.get("coupons") or [],
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

    # ---------- 优惠账本 ----------
    #
    # 原设计是「麦金卡账本」。2026-10-18 拿到真实 Token 后确认：
    # order-list 与 query-order 的返回里**没有任何持卡标识字段**
    # （对 10 单真实返回全量搜索 card / 麦金 / 会员 / vip，零命中），
    # 该维度无法支撑，硬做只能编。
    #
    # 所以换成同位置、同目的、但真实可算的「优惠账本」：
    # 原价 → 实付 → 省下的钱到底来自哪张券。数据全部来自 query-order 的
    # totalAmount / realTotalAmount / totalDiscountAmount / couponList。
    original_fen = sum(p["subtotal_fen"] for p in parsed)
    paid_fen = sum(p["total_fen"] for p in parsed)
    saved_fen = sum(p["discount_fen"] for p in parsed)
    coupon_orders = [p for p in parsed if p["coupons"]]
    no_coupon_orders = n - len(coupon_orders)

    coupon_name_qty = Counter()
    coupon_name_amt = Counter()
    for p in parsed:
        for c in p["coupons"]:
            coupon_name_qty[c["name"]] += 1
            coupon_name_amt[c["name"]] += int(c.get("amount_fen", 0) or 0)
    top_coupons = [{
        "name": nm,
        "times": coupon_name_qty[nm],
        "saved_yuan": to_yuan(coupon_name_amt[nm]),
    } for nm in sorted(coupon_name_qty,
                       key=lambda x: (-coupon_name_amt[x], -coupon_name_qty[x]))[:8]]

    best = max(parsed, key=lambda p: p["discount_fen"]) if parsed else None
    discount_ledger = {
        "original_yuan": to_yuan(original_fen),
        "paid_yuan": to_yuan(paid_fen),
        "saved_yuan": to_yuan(saved_fen),
        "save_rate": round(saved_fen / original_fen, 4) if original_fen else None,
        "avg_paid_yuan": to_yuan(paid_fen // n) if n else 0,
        "coupon_orders": len(coupon_orders),
        "no_coupon_orders": no_coupon_orders,
        "coupon_order_rate": round(len(coupon_orders) / n, 4) if n else None,
        "top_coupons": top_coupons,
        "best_saving": None if not best else {
            "date": best["dt"].strftime("%Y-%m-%d"),
            "store_name": best["store_name"],
            "original_yuan": to_yuan(best["subtotal_fen"] or 0),
            "saved_yuan": to_yuan(best["discount_fen"]),
            "paid_yuan": to_yuan(best["total_fen"]),
            "save_rate": (round(best["discount_fen"] / best["subtotal_fen"], 4)
                          if best["subtotal_fen"] else None),
        },
        "note": "省下的钱来自订单返回的优惠金额合计（totalDiscountAmount），"
                "券名来自 couponList。接口不返回持卡标识，故本维度不区分麦金卡/非麦金卡。",
    }

    # ---------- 堂食 / 外带 ----------
    tw_counter = Counter()
    tw_spend = Counter()
    for p in parsed:
        tw_counter[p["takeway"]] += 1
        tw_spend[p["takeway"]] += p["total_fen"]
    takeway_mix = [{
        "takeway": tw,
        "orders": cnt,
        "spend_yuan": to_yuan(tw_spend[tw]),
        "share": round(cnt / n, 4) if n else 0,
    } for tw, cnt in tw_counter.most_common()]

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
    if discount_ledger["saved_yuan"] > 0:
        highlights.append("原价 %.2f 元、实付 %.2f 元，一共省下 %.2f 元（%.0f%%）。"
                          % (discount_ledger["original_yuan"], discount_ledger["paid_yuan"],
                             discount_ledger["saved_yuan"],
                             (discount_ledger["save_rate"] or 0) * 100))
        if discount_ledger["top_coupons"]:
            t = discount_ledger["top_coupons"][0]
            highlights.append("最值钱的一张券是「%s」，用过 %d 次，省下 %.2f 元。"
                              % (t["name"], t["times"], t["saved_yuan"]))
    if takeway_mix and len(takeway_mix) > 1:
        highlights.append("用餐方式以「%s」为主，占 %.0f%%。"
                          % (takeway_mix[0]["takeway"], takeway_mix[0]["share"] * 100))

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
        "amount_note": "order-list / query-order 返回的金额是**元**且为字符串（如 \"14.9\"），"
                       "不是分；本报告内部统一折算为分计算，展示时再换回元。"
                       "菜单类接口（query-meals 等）的金额单位不同，不要混用。",
        "category_note": "品类由商品名称关键词启发式归类，非麦当劳官方分类。",
        "city_note": "官方不返回城市字段与经纬度。城市优先由门店地址文本解析，"
                     "地址不含城市时改用门店名（如「麦当劳南京万泰路…」）前缀匹配内置城市表；"
                     "两者都拿不到时留空，不猜。",
        "combo_note": "套餐商品已展开为子项归类，子项金额按数量均摊，合计与套餐价一致。",
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
        "takeway_mix": takeway_mix,
        "category_mix": category_mix,
        "top_items": top_items,
        "geo": geo,
        "discount_ledger": discount_ledger,
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
    args = ap.parse_args()

    if not os.path.exists(args.input):
        sys.stderr.write("找不到输入文件：%s\n" % args.input)
        return 2

    source_meta, orders = load_orders(args.input)
    report = build_report(orders, source_meta=source_meta)

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
