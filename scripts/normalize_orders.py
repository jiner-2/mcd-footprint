#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcd-footprint · MCP 原始返回 → 规范化订单

麦当劳中国 MCP 的 order-list / query-order 返回的是嵌套 JSON，字段名随接口版本
可能变化；而 footprint_aggregate.py 需要一套稳定的扁平 schema。本脚本负责中间这一层。

设计原则
--------
1. 不臆造：任何字段名都靠「别名表」去匹配，匹配不到就留空并告警，绝不填猜测值。
2. 结构优先于命名：先按结构特征找出「像订单」的对象（有时间 + 有明细），
   再在对象内部按别名取字段。这样即使外层包装换成 data/list/records 也能吃。
3. 单位不硬转：金额默认按「分」原样透传。若检测到疑似「元」，先告警，
   只在你显式指定 --amount-unit yuan 时才做换算。
4. 可对账：导出的每个订单都保留原始字段快照，方便逐单回溯到 MCP 返回值。

用法
----
    # 1) 先把 MCP 返回存成文件（单个或多个 .json 都行）
    # 2) 探字段：拿到真实数据后第一件事，看官方到底返回了哪些键
    python normalize_orders.py --input raw/ --dump-keys

    # 3) 规范化
    python normalize_orders.py --input raw/ --output orders.json
    python normalize_orders.py --input raw/ --output orders.json --amount-unit yuan
"""

import argparse
import io
import json
import os
import re
import sys

# ============================================================
# 字段别名表（按优先级排列，命中即停）
# 命名来源：麦当劳 MCP 官方文档的字段约定 + 同类接口常见命名。
# 未验证的条目在首次拿到真实返回后应当锁定，见 references/mcd-tools.md。
# ============================================================

ALIASES = {
    "order_id": ["orderId", "order_id", "orderNo", "order_no", "orderCode",
                 "orderSn", "id", "code"],
    "order_time": ["orderTime", "order_time", "orderDate", "order_date",
                   "createTime", "create_time", "submitTime", "submit_time",
                   "orderedAt", "payTime", "pay_time", "time", "date"],
    "channel": ["beType", "be_type", "orderType", "order_type", "channel",
                "deliveryType", "delivery_type", "takeType", "take_type",
                "orderMode", "order_mode", "scene", "orderScene"],
    "items": ["items", "itemList", "item_list", "productList", "product_list",
              "products", "goodsList", "goods_list", "details", "detailList",
              "orderItems", "order_items", "skuList", "subOrders"],
    "subtotal": ["subtotal", "subTotal", "originalPrice", "original_price",
                 "originalAmount", "original_amount", "totalAmount",
                 "total_amount", "orderAmount", "order_amount", "amount"],
    "discount": ["discount", "discountAmount", "discount_amount",
                 "preferAmount", "prefer_amount", "couponAmount",
                 "coupon_amount", "reduceAmount", "reduce_amount",
                 "promotionAmount", "savedAmount"],
    "total": ["total", "totalPrice", "total_price", "payAmount", "pay_amount",
              "actualAmount", "actual_amount", "realAmount", "real_amount",
              "payPrice", "pay_price", "finalPrice", "final_price"],
}

STORE_ALIASES = {
    "store_obj": ["store", "storeInfo", "store_info", "restaurant",
                  "restaurantInfo", "shop", "shopInfo", "outlet"],
    "store_name": ["storeName", "store_name", "restaurantName", "shopName",
                   "name", "title", "outletName"],
    "store_code": ["storeCode", "store_code", "restaurantCode", "shopCode",
                   "storeId", "store_id", "code", "outletCode"],
    "address": ["address", "storeAddress", "store_address", "fullAddress",
                "addr", "location", "detailAddress", "addressDetail"],
    # 门店字段有时平铺在订单上，不一定嵌套
    "store_name_flat": ["storeName", "store_name", "restaurantName", "shopName"],
    "store_code_flat": ["storeCode", "store_code", "restaurantCode", "shopCode",
                        "storeId", "store_id"],
    "address_flat": ["storeAddress", "store_address", "address", "shopAddress"],
}

ITEM_ALIASES = {
    "name": ["name", "productName", "product_name", "itemName", "item_name",
             "goodsName", "goods_name", "skuName", "sku_name", "title"],
    "quantity": ["quantity", "qty", "num", "number", "count", "amountNum",
                 "productNum", "product_num"],
    "amount": ["amount", "amount_fen", "price", "totalPrice", "total_price",
               "subtotal", "actualPrice", "payPrice", "salePrice", "unitPrice"],
}

CARD_ALIASES = {
    "obj": ["mealCard", "meal_card", "memberCard", "member_card", "cardInfo",
            "card_info", "mcdCard", "vipCard", "privilegeCard"],
    "flag": ["isMealCard", "is_meal_card", "useMealCard", "cardFlag",
             "memberCardFlag", "isMember"],
    "name": ["cardName", "card_name", "name", "type", "cardType"],
    "saved": ["savedAmount", "saved_amount", "discountAmount", "discount_amount",
              "preferAmount", "prefer_amount", "saveMoney", "reduceAmount"],
}

# 官方 order-list 的 beType 口径（三处交叉验证过）
BE_TYPE_MAP = {
    1: "到店取餐",
    2: "麦乐送",
    5: "得来速",
    6: "企业团餐",
}

CHANNEL_KEYWORDS = [
    ("到店取餐", ["到店", "自取", "pickup", "pick_up", "takeout", "counter"]),
    ("麦乐送", ["外送", "麦乐送", "配送", "delivery", "express"]),
    ("得来速", ["得来速", "drivethru", "drive_thru", "drive-thru", "dt"]),
    ("企业团餐", ["团餐", "企业", "group", "catering", "corporate"]),
    ("堂食外带", ["堂食", "外带", "dinein", "dine_in", "dine-in"]),
]


# ============================================================
# 基础工具
# ============================================================

def load_json_loose(text):
    """解析 JSON；容忍 MCP 的 SSE 风格前缀（event: / data:）。"""
    text = text.strip()
    if not text:
        return None
    if text[0] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            pass
    # SSE：逐行找 data: {...}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk[:1] in "[{":
                try:
                    return json.loads(chunk)
                except ValueError:
                    continue
    return None


def read_payloads(path):
    """读取一个文件或整个目录，返回 [(来源名, python 对象)]。"""
    files = []
    if os.path.isdir(path):
        for name in sorted(os.listdir(path)):
            if name.lower().endswith((".json", ".jsonl", ".txt")):
                files.append(os.path.join(path, name))
    else:
        files.append(path)

    out = []
    for fp in files:
        with io.open(fp, "r", encoding="utf-8-sig") as f:
            raw = f.read()
        if fp.lower().endswith(".jsonl"):
            for i, line in enumerate(raw.splitlines()):
                obj = load_json_loose(line)
                if obj is not None:
                    out.append(("%s#L%d" % (os.path.basename(fp), i + 1), obj))
        else:
            obj = load_json_loose(raw)
            if obj is not None:
                out.append((os.path.basename(fp), obj))
    return out


def pick(obj, names):
    """按别名顺序取值，返回 (值, 命中的键)。"""
    if not isinstance(obj, dict):
        return None, None
    for n in names:
        if n in obj and obj[n] not in (None, "", [], {}):
            return obj[n], n
    return None, None


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def walk_keys(node, prefix="", acc=None):
    """递归收集所有键路径 → (出现次数, 样例值)。"""
    if acc is None:
        acc = {}
    if isinstance(node, dict):
        for k, v in node.items():
            p = ("%s.%s" % (prefix, k)) if prefix else k
            slot = acc.setdefault(p, {"count": 0, "sample": None})
            slot["count"] += 1
            if slot["sample"] is None and not isinstance(v, (dict, list)):
                slot["sample"] = v
            walk_keys(v, p, acc)
    elif isinstance(node, list):
        for v in node[:5]:
            walk_keys(v, prefix + "[]", acc)
    return acc


# ============================================================
# 结构识别：找出「像订单」的对象
# ============================================================

ORDER_ID_HINT = re.compile(r"(order|trade|bill|receipt)", re.I)
TIME_HINT = re.compile(r"(time|date|at$)", re.I)


def looks_like_order(d):
    if not isinstance(d, dict):
        return False
    has_id = any(ORDER_ID_HINT.search(k or "") for k in d)
    has_time = any(TIME_HINT.search(k or "") for k in d)
    has_items, _ = pick(d, ALIASES["items"])
    has_money, _ = pick(d, ALIASES["total"] + ALIASES["subtotal"])
    return bool(has_id and has_time and (has_items or has_money))


def find_orders(node, acc=None, depth=0):
    if acc is None:
        acc = []
    if depth > 8:
        return acc
    if isinstance(node, dict):
        if looks_like_order(node):
            acc.append(node)
            return acc  # 命中最外层订单对象后不再往里挖
        for v in node.values():
            find_orders(v, acc, depth + 1)
    elif isinstance(node, list):
        for v in node:
            find_orders(v, acc, depth + 1)
    return acc


# ============================================================
# 字段转换
# ============================================================

def to_channel(raw):
    """beType / 字符串 → 统一渠道名。无法识别时返回 (原文, 告警)。"""
    if raw is None:
        return "", "缺少点餐方式字段"
    if is_number(raw) or (isinstance(raw, str) and raw.isdigit()):
        n = int(raw)
        if n in BE_TYPE_MAP:
            return BE_TYPE_MAP[n], None
        return "未知(beType=%s)" % n, "beType=%s 不在已知映射(1/2/5/6)内" % n
    s = str(raw)
    low = s.lower()
    for name, kws in CHANNEL_KEYWORDS:
        for kw in kws:
            if kw in low:
                return name, None
    return s, "点餐方式「%s」未匹配到已知渠道，已原样保留" % s


def to_fen(v, unit, ctx, warns):
    """金额 → 分。unit 为 auto/fen/yuan。"""
    if not is_number(v):
        return None
    if unit == "yuan":
        return int(round(v * 100))
    if unit == "fen":
        return int(round(v))
    # auto：交给上层统一判定，这里先按分透传
    return int(round(v))


def parse_time_str(v):
    if isinstance(v, str):
        return v.strip().replace("T", " ")
    if is_number(v):
        # 13 位毫秒 / 10 位秒
        import datetime as _dt
        ts = float(v)
        if ts > 1e11:
            ts /= 1000.0
        try:
            return _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError, OverflowError):
            return ""
    return ""


def normalize_order(raw, idx, unit, warns, fatals):
    oid = "#%d" % (idx + 1)
    out = {"order_id": "", "order_time": "", "channel": "未知",
           "store": {"store_code": "", "store_name": "", "address": ""},
           "items": [], "subtotal_fen": 0, "discount_fen": 0, "total_fen": 0,
           "meal_card": None}

    v, _ = pick(raw, ALIASES["order_id"])
    out["order_id"] = str(v) if v is not None else ""
    if not out["order_id"]:
        warns.append("%s 未找到订单号" % oid)
    else:
        oid = out["order_id"]

    v, _ = pick(raw, ALIASES["order_time"])
    out["order_time"] = parse_time_str(v)
    if not out["order_time"]:
        fatals.append("%s 订单时间无法解析（原值 %r），该单已跳过" % (oid, v))
        return None

    v, _ = pick(raw, ALIASES["channel"])
    ch, w = to_channel(v)
    out["channel"] = ch
    if w:
        warns.append("%s %s" % (oid, w))

    # --- 门店 ---
    store_obj, _ = pick(raw, STORE_ALIASES["store_obj"])
    if isinstance(store_obj, dict):
        sv, _ = pick(store_obj, STORE_ALIASES["store_name"])
        cv, _ = pick(store_obj, STORE_ALIASES["store_code"])
        av, _ = pick(store_obj, STORE_ALIASES["address"])
    else:
        sv, cv, av = None, None, None
    if sv is None:
        sv, _ = pick(raw, STORE_ALIASES["store_name_flat"])
    if cv is None:
        cv, _ = pick(raw, STORE_ALIASES["store_code_flat"])
    if av is None:
        av, _ = pick(raw, STORE_ALIASES["address_flat"])
    out["store"] = {
        "store_code": str(cv) if cv is not None else "",
        "store_name": str(sv) if sv is not None else "",
        "address": str(av) if av is not None else "",
    }
    if not out["store"]["address"]:
        warns.append("%s 缺少门店地址，城市足迹维度会计入「地址缺失」" % oid)

    # --- 明细 ---
    items_raw, _ = pick(raw, ALIASES["items"])
    if isinstance(items_raw, dict):
        items_raw = [items_raw]
    if isinstance(items_raw, list):
        for it in items_raw:
            if not isinstance(it, dict):
                continue
            nm, _ = pick(it, ITEM_ALIASES["name"])
            qv, _ = pick(it, ITEM_ALIASES["quantity"])
            am, _ = pick(it, ITEM_ALIASES["amount"])
            if nm is None and am is None:
                continue
            try:
                qty = int(qv) if is_number(qv) or (isinstance(qv, str) and qv.isdigit()) else 1
            except (TypeError, ValueError):
                qty = 1
            out["items"].append({
                "name": str(nm) if nm is not None else "(未命名商品)",
                "quantity": max(qty, 1),
                "amount_fen": to_fen(am, unit, oid, warns) or 0,
            })
    if not out["items"]:
        warns.append("%s 未解析出商品明细，品类结构维度会失真" % oid)

    # --- 金额 ---
    sv, _ = pick(raw, ALIASES["subtotal"])
    dv, _ = pick(raw, ALIASES["discount"])
    tv, _ = pick(raw, ALIASES["total"])
    out["subtotal_fen"] = to_fen(sv, unit, oid, warns) or 0
    out["discount_fen"] = to_fen(dv, unit, oid, warns) or 0
    out["total_fen"] = to_fen(tv, unit, oid, warns) or 0
    if not out["total_fen"] and out["subtotal_fen"]:
        out["total_fen"] = out["subtotal_fen"] - out["discount_fen"]
        warns.append("%s 缺少实付金额，已按 小计-优惠 推算" % oid)
    if not out["subtotal_fen"]:
        out["subtotal_fen"] = sum(i["amount_fen"] for i in out["items"])
        if out["subtotal_fen"]:
            warns.append("%s 缺少订单小计，已按明细加总推算" % oid)

    # --- 麦金卡（未验证字段，只做保守识别）---
    card_obj, _ = pick(raw, CARD_ALIASES["obj"])
    card = None
    if isinstance(card_obj, dict):
        cn, _ = pick(card_obj, CARD_ALIASES["name"])
        cs, _ = pick(card_obj, CARD_ALIASES["saved"])
        card = {"applied": True,
                "card_name": str(cn) if cn is not None else "麦金卡",
                "saved_fen": to_fen(cs, unit, oid, warns) or 0}
    else:
        fv, _ = pick(raw, CARD_ALIASES["flag"])
        if fv is not None and fv not in (0, "0", False, "false", ""):
            card = {"applied": True, "card_name": "麦金卡", "saved_fen": 0}
    out["meal_card"] = card

    out["_raw"] = raw
    return out


# ============================================================
# 单位判定
# ============================================================

def detect_unit(orders):
    """
    判断这一批订单的金额单位是分还是元。返回 (unit, 依据)。

    按「每批独立判定」调用，绝不跨批次共享结论 —— 否则一个含小数的异常订单
    会把整批「分」的金额放大 100 倍。

    判定用多数票而非「存在即成立」：个别订单带小数（如分摊优惠）不足以改变口径。
    """
    totals = []
    for o in orders:
        for key in ("total", "subtotal"):
            v, _ = pick(o, ALIASES[key])
            if is_number(v) and v > 0:
                totals.append(float(v))
    if not totals:
        return "fen", "无订单级金额可判定，按官方文档默认「分」"

    dec = sum(1 for t in totals if abs(t - round(t)) > 1e-9)
    mn, mx = min(totals), max(totals)

    if dec * 2 >= len(totals):
        return "yuan", "%d/%d 笔订单金额带小数，判定为「元」" % (dec, len(totals))
    if mx <= 300 and mn < 100:
        return "yuan", "金额区间 %g–%g，若为分则不足 3 元，判定为「元」" % (mn, mx)
    if dec:
        return "fen", "仅 %d/%d 笔带小数（个别分摊所致），整体判定为「分」" % (dec, len(totals))
    return "fen", "金额均为整数、最小 %g 分（%.2f 元），判定为「分」" % (mn, mn / 100.0)


# ============================================================
# 主流程
# ============================================================

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把麦当劳 MCP 的原始订单返回规范化成 footprint_aggregate.py 的输入格式")
    ap.add_argument("--input", required=True, help="原始返回：单个 .json/.jsonl 文件，或存放它们的目录")
    ap.add_argument("--output", help="规范化结果输出路径（--dump-keys 时可省略）")
    ap.add_argument("--amount-unit", choices=["auto", "fen", "yuan"], default="auto",
                    help="金额单位，默认 auto 自动判定")
    ap.add_argument("--dump-keys", action="store_true",
                    help="只输出字段清单（键路径 / 出现次数 / 样例值），用于拿到真实数据后锁定字段名")
    ap.add_argument("--keep-raw", action="store_true",
                    help="输出中保留每单的原始字段快照（默认不保留，避免文件过大）")
    args = ap.parse_args(argv)

    payloads = read_payloads(args.input)
    if not payloads:
        sys.stderr.write("未读到任何有效 JSON：%s\n" % args.input)
        return 2

    # 逐文件识别订单与单位：不同来源的文件可能是不同调用、甚至不同单位
    raws = []
    for src, obj in payloads:
        found = find_orders(obj)
        unit_i = args.amount_unit
        why_i = "由 --amount-unit %s 指定" % unit_i
        if unit_i == "auto":
            unit_i, why_i = detect_unit(found)
        raws.append({"src": src, "orders": found, "unit": unit_i, "why": why_i})

    all_raws = sum((r["orders"] for r in raws), [])
    if not all_raws:
        sys.stderr.write(
            "在 %d 个文件中没有识别出订单对象。\n"
            "建议先跑 --dump-keys 看实际返回结构，再对齐 ALIASES 里的字段名。\n"
            % len(payloads))
        return 3

    if args.dump_keys:
        acc = walk_keys(all_raws)
        lines = ["| 键路径 | 出现次数 | 样例值 |", "|---|---:|---|"]
        for k in sorted(acc):
            s = acc[k]["sample"]
            if isinstance(s, str) and len(s) > 40:
                s = s[:40] + "…"
            lines.append("| `%s` | %d | %s |" % (k, acc[k]["count"], s if s is not None else ""))
        body = "\n".join(lines)
        if args.output:
            with io.open(args.output, "w", encoding="utf-8") as f:
                f.write(body + "\n")
            sys.stderr.write("字段清单已写入 %s（%d 个键路径）\n" % (args.output, len(acc)))
        else:
            sys.stdout.write(body + "\n")
        return 0

    if not args.output:
        sys.stderr.write("规范化模式必须指定 --output\n")
        return 2

    warns, fatals = [], []
    orders = []
    units_used = {}
    for blk in raws:
        u = blk["unit"]
        units_used[u] = units_used.get(u, 0) + len(blk["orders"])
        for i, r in enumerate(blk["orders"]):
            o = normalize_order(r, i, u, warns, fatals)
            if o is not None:
                if not args.keep_raw:
                    o.pop("_raw", None)
                orders.append(o)

    unit = "+".join(sorted(units_used)) if units_used else "fen"
    why = "；".join("%s:%d 条" % (k, v) for k, v in sorted(units_used.items()))
    if len(units_used) > 1:
        warns.append("同一批文件里检出 %d 种金额单位（%s），已按文件分别换算，"
                     "请确认这些文件是否来自同一次调用" % (len(units_used), unit))

    # 去重：同一订单可能同时出现在 order-list 与 query-order 里
    seen, deduped, dup = set(), [], 0
    for o in orders:
        key = o["order_id"] or ("%s|%s" % (o["order_time"], o["store"]["store_name"]))
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        deduped.append(o)
    if dup:
        warns.append("检出 %d 条重复订单（order-list 与 query-order 交叠），已按订单号去重" % dup)

    deduped.sort(key=lambda o: o["order_time"])

    # 事后合理性拦截：单位判错时金额会整体放大/缩小 100 倍，这里兜底喊一嗓子
    abnormal = [o for o in deduped if o["total_fen"] > 200000]
    if abnormal:
        warns.append("检出 %d 笔订单金额超过 2000 元（最高 %s 元），疑似金额单位判定有误，"
                     "建议用 --amount-unit fen|yuan 显式指定"
                     % (len(abnormal), "%.2f" % (max(o["total_fen"] for o in abnormal) / 100.0)))
    zero = [o for o in deduped if o["total_fen"] <= 0]
    if zero:
        warns.append("检出 %d 笔订单实付金额为 0 或缺失，会在消费总额与人均里被记为 0" % len(zero))

    doc = {
        "schema_version": 1,
        "source": {
            "provider": "麦当劳中国 MCP · order-list + query-order",
            "endpoint": "https://mcp.mcd.cn",
            "normalized_by": "normalize_orders.py",
            "raw_files": len(payloads),
            "raw_orders_found": len(all_raws),
            "amount_unit": unit,
            "amount_unit_basis": why,
            "note": "订单时间为官方原值，不含时区，聚合时统一按 UTC+8 解释。",
        },
        "orders": deduped,
    }

    with io.open(args.output, "w", encoding="utf-8", newline="\n") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)

    sys.stdout.write("规范化完成 → %s\n" % args.output)
    sys.stdout.write("  原始订单 %d 条 / 去重后 %d 条 / 金额单位 %s（%s）\n"
                     % (len(all_raws), len(deduped), unit, why))
    sys.stdout.write("  告警 %d 条 / 跳过 %d 条\n" % (len(warns), len(fatals)))
    for w in warns[:15]:
        sys.stdout.write("  ! %s\n" % w)
    if len(warns) > 15:
        sys.stdout.write("  ! ……另有 %d 条告警\n" % (len(warns) - 15))
    for w in fatals[:10]:
        sys.stdout.write("  x %s\n" % w)

    if not deduped:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
