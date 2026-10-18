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
#
# 2026-10-18 用真实 Token 跑通 order-list / query-order 后，把推测值换成了实测值。
# 实测口径（10 单真实数据，逐字段核对）：
#
#   order-list    orderId / createTime / beType(str) / beCode / storeCode / storeName
#                 orderStatus / orderProductList[] / realTotalAmount(str,元)
#                 orderProductList[]: productCode productName quantity comboItemList[]
#                 comboItemList[]   : productCode name quantity
#
#   query-order   orderId / createTime / storeName / storeAddress / takeWay
#                 orderStatus / status / orderProductList[] / couponList[]
#                 totalAmount / realTotalAmount / totalDiscountAmount / productPrice
#                 deliveryPrice / realDeliveryPrice / needTableware / pickupCode
#                 orderProductList[]: productName price quantity productImage comboItemList[]
#                 comboItemList[]   : itemName itemQuantity
#                 couponList[]      : couponName discountAmount
#
# 两个接口**同名不同构**，是这张表最容易被绕进去的地方：
#   · orderProductList 元素：order-list 有 productCode 无 price；query-order 有 price 无 productCode
#   · comboItemList 元素  ：order-list 用 name/quantity；query-order 用 itemName/itemQuantity
#   所以下面把两套键名并列，谁先命中用谁，不能假设「同名即同构」。
#
# 另外两个实测硬结论：
#   · 金额是「元」且为**字符串**（"14.9"），不是分。分只出现在 query-meals 这类菜单接口。
#   · 没有任何持卡标识字段（全量搜 card / 麦金 / 会员 / vip 均无命中），
#     所以麦金卡账本维度不可用，改为优惠账本 —— 见 footprint_aggregate.py。
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
    # 堂食/外带：只有 query-order 有，order-list 没有。与 beType 是两个维度，
    # beType 说「怎么取餐」，takeWay 说「在哪吃」。
    "takeway": ["takeWay", "takeway", "take_way", "diningType", "dining_type",
                "eatType", "eat_type"],
    "items": ["orderProductList", "items", "itemList", "item_list",
              "productList", "product_list", "products", "goodsList",
              "goods_list", "details", "detailList", "orderItems",
              "order_items", "skuList", "subOrders"],
    # order-list 的 subtotal 侧没有字段；query-order 的 totalAmount 是**折前原价**
    "subtotal": ["subtotal", "subTotal", "originalPrice", "original_price",
                 "originalAmount", "original_amount", "totalAmount",
                 "total_amount", "orderAmount", "order_amount", "productPrice",
                 "amount"],
    "discount": ["totalDiscountAmount", "total_discount_amount",
                 "discount", "discountAmount", "discount_amount",
                 "preferAmount", "prefer_amount", "couponAmount",
                 "coupon_amount", "reduceAmount", "reduce_amount",
                 "promotionAmount", "savedAmount"],
    "total": ["realTotalAmount", "real_total_amount", "total", "totalPrice",
              "total_price", "payAmount", "pay_amount", "actualAmount",
              "actual_amount", "realAmount", "real_amount", "payPrice",
              "pay_price", "finalPrice", "final_price"],
    "coupons": ["couponList", "coupon_list", "coupons"],
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
    # order-list 用 productName，query-order 的套餐子项用 itemName
    "name": ["productName", "product_name", "itemName", "item_name", "name",
             "goodsName", "goods_name", "skuName", "sku_name", "title"],
    "quantity": ["quantity", "qty", "num", "number", "count", "amountNum",
                 "itemQuantity", "item_quantity", "productNum", "product_num"],
    "amount": ["price", "amount", "amount_fen", "totalPrice", "total_price",
               "subtotal", "actualPrice", "payPrice", "salePrice", "unitPrice"],
    # 套餐子项：order-list 叫 comboItemList，字段是 name/quantity；
    #          query-order 也叫 comboItemList，字段却是 itemName/itemQuantity
    "combo": ["comboItemList", "combo_item_list", "comboList", "combo_list",
              "children", "subItems", "sub_items"],
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

# query-order 的 takeWay 口径（2026-10-18 实测 10 单：堂食 9 / 外带 1）
TAKEWAY_MAP = [
    ("堂食", ["堂食", "dinein", "dine_in", "dine-in"]),
    ("外带", ["外带", "打包", "自取", "takeaway", "take_away", "takeout"]),
]

# ------------------------------------------------------------
# 城市名表：只用于从**门店名**里切城市，不参与任何地理推断。
#
# 为什么需要它：实测 storeAddress 不含城市（如「东山街道玖力大厦1号楼1层101…」），
# 城市只出现在 storeName 里（「麦当劳南京万泰路得来速餐厅」）。门店名是「麦当劳+城市+
# 店名+餐厅」，但城市名 2~4 字不定长，没有词表就无法可靠切分。
# 宁可只认表内的城市并把表外的留空告警，也不用「取前两个字」这种猜法
# —— 那会把「麦当劳哈尔滨中央大街餐厅」切成「哈尔」。
# ------------------------------------------------------------
CITY_NAMES = (
    # 直辖市
    "北京 上海 天津 重庆 "
    # 省会 / 自治区首府
    "广州 成都 杭州 南京 武汉 西安 长沙 郑州 沈阳 济南 合肥 福州 昆明 哈尔滨 "
    "长春 石家庄 太原 南昌 贵阳 南宁 兰州 西宁 银川 乌鲁木齐 呼和浩特 拉萨 海口 "
    # 计划单列 / 经济强市
    "深圳 青岛 大连 宁波 厦门 苏州 无锡 东莞 佛山 珠海 温州 泉州 南通 常州 徐州 "
    "烟台 潍坊 唐山 洛阳 襄阳 宜昌 惠州 中山 汕头 湛江 江门 肇庆 茂名 "
    # 其他常见地级市
    "嘉兴 绍兴 金华 台州 湖州 衢州 丽水 舟山 扬州 镇江 泰州 盐城 淮安 连云港 "
    "宿迁 芜湖 蚌埠 阜阳 安庆 马鞍山 九江 赣州 上饶 宜春 淄博 枣庄 东营 济宁 "
    "泰安 威海 日照 德州 聊城 菏泽 临沂 开封 平顶山 安阳 新乡 焦作 许昌 南阳 "
    "商丘 信阳 周口 驻马店 黄石 十堰 荆州 孝感 黄冈 岳阳 常德 衡阳 郴州 株洲 "
    "湘潭 邵阳 益阳 永州 怀化 韶关 梅州 清远 揭阳 汕尾 河源 阳江 云浮 潮州 "
    "桂林 柳州 北海 玉林 自贡 泸州 内江 乐山 南充 眉山 德阳 绵阳 宜宾 遂宁 "
    "遵义 六盘水 安顺 曲靖 玉溪 大理 丽江 咸阳 宝鸡 渭南 榆林 延安 汉中 天水 "
    "保定 廊坊 邯郸 秦皇岛 张家口 承德 沧州 邢台 衡水 大同 临汾 运城 包头 "
    "鄂尔多斯 鞍山 抚顺 锦州 营口 盘锦 吉林 齐齐哈尔 大庆 牡丹江 佳木斯 "
    "克拉玛依 喀什 三亚 昆山 义乌 慈溪 常熟 张家港 江阴 晋江 石狮"
).split()

_STORE_PREFIX = re.compile(r"^(麦当劳|McDonald'?s|McDonald’s|麦当劳中国)[\s·]*", re.I)


def city_from_store_name(name):
    """
    从门店名切出城市。返回 (city, 依据)。切不出返回 ("", 原因)。

    只做「前缀匹配城市表」这一件事：去前缀后最长的命中城市优先（先试 4 字再 3 字再 2 字）。
    不在表内的城市宁可留空 —— 留空会在报告里体现为覆盖率不足，猜错则会让城市维度静默出错。
    """
    if not name or not isinstance(name, str):
        return "", "门店名为空"
    s = _STORE_PREFIX.sub("", name.strip())
    if not s:
        return "", "门店名去掉「麦当劳」前缀后为空"
    # 名字长度短于 2 显然不是「城市+店名」，直接放弃
    if len(s) < 3:
        return "", "门店名过短，无法切分城市"
    for n in (4, 3, 2):
        head = s[:n]
        if head in CITY_NAMES:
            return head, "门店名前缀命中城市表"
    return "", "门店名前缀「%s」不在内置城市表内" % s[:6]


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


_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def to_num(v):
    """
    把金额取成 float。官方返回的金额是**字符串**（"14.9"），早期版本只认数字，
    结果真实数据里所有金额都会被判成 0 —— 报告照常生成，但金额全是 0。
    """
    if is_number(v):
        return float(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("¥", "").replace("￥", "")
        if s and _NUM_RE.match(s):
            return float(s)
    return None


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
        # query-order 不返回 beType，合并后才能知道是否真的缺失。
        # 这里不报警，由合并后的统一巡检负责。
        return "", None
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


_BRACKET = re.compile(r"[（(【\[](?:[^）)】\]]*)[）)】\]]")
# 「大杯鲜萃咖啡 奶油球/白砂糖」这种「主商品 + 点单选项」的尾巴
_MOD_TAIL = re.compile(r"\s+[^\s]*(?:奶油球|白砂糖|糖浆|餐具|吸管|酱)[^\s]*$")


def clean_item_name(name):
    """
    去掉商品名里的小括号备注，例如
      「大杯鲜萃咖啡 奶油球(不要)/白砂糖(不要)」→「大杯鲜萃咖啡」
      「奥利奥麦旋风(无小卡)」→「奥利奥麦旋风」

    实测这类备注会直接污染品类归类和「常点单品」榜单：同一杯咖啡因为备注不同
    会变成好几个不同单品。只剥括号内内容，剥完为空或过短时保留原名，不做别的改写。
    """
    s = (name or "").strip()
    cleaned = _BRACKET.sub("", s).strip()
    # 括号删掉后常留下「 奶油球/白砂糖」这类点单选项尾巴，把整段去掉
    cleaned = _MOD_TAIL.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -/·")
    if len(cleaned) < 2:
        return s
    return cleaned


def to_fen(v, unit, ctx, warns):
    """金额 → 分。unit 为 auto/fen/yuan。支持数字与数字字符串（官方返回是字符串）。"""
    x = to_num(v)
    if x is None:
        return None
    if unit == "yuan":
        return int(round(x * 100))
    if unit == "fen":
        return int(round(x))
    # auto：交给上层统一判定，这里先按分透传
    return int(round(x))


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
    out = {"order_id": "", "order_time": "", "channel": "未知", "takeway": "未知",
           "store": {"store_code": "", "store_name": "", "address": "", "city_hint": ""},
           "items": [], "subtotal_fen": 0, "discount_fen": 0, "total_fen": 0,
           "coupons": [], "meal_card": None}

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

    # --- 堂食 / 外带（只有 query-order 有）---
    tv, _ = pick(raw, ALIASES["takeway"])
    if tv is not None:
        s = str(tv).strip()
        low = s.lower()
        hit = None
        for name, kws in TAKEWAY_MAP:
            if any(k in low for k in kws):
                hit = name
                break
        out["takeway"] = hit or s
        if hit is None:
            warns.append("%s 用餐方式「%s」未匹配到堂食/外带，已原样保留" % (oid, s))

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
    _sname = str(sv) if sv is not None else ""
    city_hint, city_why = city_from_store_name(_sname)
    out["store"] = {
        "store_code": str(cv) if cv is not None else "",
        "store_name": _sname,
        "address": str(av) if av is not None else "",
        "city_hint": city_hint,
    }
    # order-list 不返回地址、query-order 才返回，合并后可能就有了；
    # 城市同理。这里不报警，交给合并完成后的统一巡检。
    out["_city_why"] = city_why if not city_hint else ""

    # --- 明细 ---
    #
    # 套餐要展开成子项：order-list 里「泰式炭烤风味猪猪堡随心配」这种套餐商品，
    # 真正的品类信息（双层吉士汉堡 / 菠萝派）只存在于 comboItemList 里。
    # 不展开的话，「奥利奥麦旋风」这类甜品会被整单归进「小食随心选」，品类结构直接失真。
    #
    # 金额处理：套餐只有一个总价，没有子项单价。这里按子项数量均摊，并保证
    # 末位承担余数，使子项金额之和**精确等于**父商品金额 —— 宁可分摊口径粗糙，
    # 也不让总额对不上。这个近似会写进 references/pitfalls.md。
    items_raw, _ = pick(raw, ALIASES["items"])
    if isinstance(items_raw, dict):
        items_raw = [items_raw]
    combo_flat = 0
    if isinstance(items_raw, list):
        for it in items_raw:
            if not isinstance(it, dict):
                continue
            nm, _ = pick(it, ITEM_ALIASES["name"])
            qv, _ = pick(it, ITEM_ALIASES["quantity"])
            am, _ = pick(it, ITEM_ALIASES["amount"])
            if nm is None and am is None:
                continue
            qty = 1
            if is_number(qv) or (isinstance(qv, str) and qv.isdigit()):
                qty = int(qv)
            qty = max(qty, 1)
            parent_fen = to_fen(am, unit, oid, warns) or 0

            kids_raw, _ = pick(it, ITEM_ALIASES["combo"])
            kids = []
            if isinstance(kids_raw, list):
                for k in kids_raw:
                    if not isinstance(k, dict):
                        continue
                    kn, _ = pick(k, ITEM_ALIASES["name"])
                    kq, _ = pick(k, ITEM_ALIASES["quantity"])
                    if kn is None:
                        continue
                    kqty = 1
                    if is_number(kq) or (isinstance(kq, str) and kq.isdigit()):
                        kqty = int(kq)
                    kids.append((str(kn), max(kqty, 1)))

            if kids:
                combo_flat += 1
                child_total_qty = sum(k[1] for k in kids)
                # 子项 amount_fen 是**单价**（下游按 单价×数量 汇总），
                # 所以要把「一个套餐的总价」摊到「所有子项单位数」上：
                # 单价 = 套餐价 / (套餐数 × 子项单位数)，余数挂在最后一个子项上。
                total_units = qty * child_total_qty
                per_unit = parent_fen // total_units if total_units else 0
                left = parent_fen - per_unit * total_units
                for pos, (kname, kqty) in enumerate(kids):
                    amt = per_unit
                    if pos == len(kids) - 1:
                        amt += left
                    out["items"].append({
                        "name": clean_item_name(kname),
                        "quantity": qty * kqty,
                        "amount_fen": amt,
                        "from_combo": str(nm) if nm is not None else "",
                    })
                continue

            out["items"].append({
                "name": clean_item_name(str(nm)) if nm is not None else "(未命名商品)",
                "quantity": qty,
                "amount_fen": parent_fen,
            })
    if not out["items"]:
        warns.append("%s 未解析出商品明细，品类结构维度会失真" % oid)
    out["_combo_flat"] = combo_flat

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

    # --- 优惠券明细（query-order 才有）---
    cl, _ = pick(raw, ALIASES["coupons"])
    if isinstance(cl, list):
        for c in cl:
            if not isinstance(c, dict):
                continue
            cn, _ = pick(c, ["couponName", "coupon_name", "name", "title"])
            ca, _ = pick(c, ["discountAmount", "discount_amount", "amount", "price"])
            if cn is None:
                continue
            out["coupons"].append({
                "name": str(cn),
                "amount_fen": to_fen(ca, unit, oid, warns) or 0,
            })

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
            num = to_num(v)
            if num is not None and num > 0:
                totals.append(num)
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

    # 合并：同一订单会同时出现在 order-list 与 query-order 里，两个接口字段互补 ——
    # order-list 有 storeCode 没有地址、没有优惠；query-order 有地址、堂食/外带、优惠金额，
    # 却没有 storeCode。早期版本是「后到的丢掉」，等于白跑一遍 query-order。
    def _blank(v):
        return v in (None, "", "未知", 0, [], {})

    def merge_two(a, b):
        """把 b 里有、a 里没有的东西补进 a。返回补了几个字段。"""
        filled = 0

        for k in ("order_time", "channel", "takeway"):
            if _blank(a.get(k)) and not _blank(b.get(k)):
                a[k] = b[k]
                filled += 1
            elif k == "takeway" and a.get(k) == "未知" and not _blank(b.get(k)):
                a[k] = b[k]
                filled += 1

        for k in ("store_code", "store_name", "address", "city_hint"):
            av, bv = a["store"].get(k), b["store"].get(k)
            if _blank(av) and not _blank(bv):
                a["store"][k] = bv
                filled += 1

        # 明细：优先保留「带金额」的那份。order-list 的明细没有单价，
        # 只有数量；query-order 才有 price。金额全为 0 的明细等同不可用。
        a_amt = sum(i.get("amount_fen", 0) for i in a.get("items") or [])
        b_amt = sum(i.get("amount_fen", 0) for i in b.get("items") or [])
        if b_amt > a_amt or (b_amt == a_amt and len(b.get("items") or []) > len(a.get("items") or [])):
            if b.get("items"):
                a["items"] = b["items"]
                filled += 1

        for k in ("subtotal_fen", "discount_fen", "total_fen"):
            if _blank(a.get(k)) and not _blank(b.get(k)):
                a[k] = b[k]
                filled += 1

        if len(b.get("coupons") or []) > len(a.get("coupons") or []):
            a["coupons"] = b.get("coupons") or []
            filled += 1

        if a.get("meal_card") is None and b.get("meal_card") is not None:
            a["meal_card"] = b["meal_card"]
            filled += 1

        return filled

    index, deduped, dup, merged_fields = {}, [], 0, 0
    for o in orders:
        key = o["order_id"] or ("%s|%s" % (o["order_time"], o["store"]["store_name"]))
        if key in index:
            dup += 1
            merged_fields += merge_two(index[key], o)
            continue
        index[key] = o
        deduped.append(o)
    if dup:
        warns.append("检出 %d 条重复订单（order-list 与 query-order 交叠），"
                     "已按订单号合并，互补字段 %d 个" % (dup, merged_fields))
    no_addr = [o for o in deduped if not o["store"]["address"]]
    if no_addr:
        warns.append("%d 单没有门店地址（order-list 不返回，需 query-order 补齐）"
                     % len(no_addr))

    # 合并完成后的统一巡检：到这一步还缺的，才是真的缺
    combo_orders = 0
    for o in deduped:
        o.pop("_city_why", None)
        if o.pop("_combo_flat", 0):
            combo_orders += 1
        if _blank(o.get("channel")):
            warns.append("%s 合并后仍无点餐方式，该单在渠道维度计为「未知」" % o["order_id"])
        if not o["store"]["city_hint"] and not o["store"]["address"]:
            warns.append("%s 门店名切不出城市、也没有地址，该单不计入城市足迹" % o["order_id"])
    if combo_orders:
        warns.append("%d 单含套餐，已展开为子项以便归类；子项金额按数量均摊，合计与套餐价一致"
                     % combo_orders)

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
        "schema_version": 2,
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
