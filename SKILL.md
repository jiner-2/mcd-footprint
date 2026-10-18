---
name: mcd-footprint
description: This skill should be used when the user wants to see their own McDonald's China consumption footprint — where they ate, when they ate, how they ordered, what they ordered, and whether their 麦金卡 (McDonald's China paid membership) actually paid off. It pulls the user's recent order history from the McDonald's China MCP server, aggregates it locally into five dimensions, and renders a self-contained HTML report plus a 1080×1920 shareable card. All personal order data stays on the user's machine and is never uploaded.
agent_created: true
---

# 麦麦足迹 · MCD Footprint

把用户本人的麦当劳订单历史，聚合成一张**消费行为画像**：在哪吃、什么时候吃、怎么点、爱吃什么、麦金卡值不值。

产出两份东西：一份自包含 HTML 报告，一张可分享的竖版卡片。**全程本地计算，订单数据不出本机。**

## 何时使用

用户想复盘、回顾、可视化**自己的**麦当劳消费记录时触发。典型说法：

- "看看我在麦当劳花了多少钱、都吃了什么"
- "把我的麦当劳订单做成一张消费画像 / 年度报告"
- "我经常吃哪家店？一般什么时间吃？"
- "我这张麦金卡办得值不值，省回来没有"
- "生成一张我的麦当劳足迹分享卡"

## 何时不使用

- **用户问的是麦当劳通用知识**（营收、选址策略、品牌历史、新品味评）——不属于本 skill，直接回答，不要触发。
- **用户想要全国门店地图 / 门店热度排行**——本 skill 只处理"用户本人的订单"。全国门店坐标官方接口并不返回。
- **用户想点餐、领券、兑换积分、预约主题活动**——这些是下单类场景，不在本 skill 范围内。
- **用户没有配置 MCP Token**——先引导去 <https://open.mcd.cn/mcp> 申请，不要用假数据糊弄。

## 前置检查

1. **MCP 已连接**：`mcd-mcp` 可用。调用返回 401 表示 Token 失效，提示用户重新激活，**不要反复重试**。
2. **明确告知数据边界**（这一点必须主动说，不要等用户问）：
   - 官方 `order-list` **只返回近期订单**，不是全年完整历史；
   - 接口**不返回门店经纬度**，所以地点维度只能做到「城市 / 区 / 门店」层级；
   - 订单时间字段**不含时区**，统一按 UTC+8 解释。

## 工作流

严格按顺序执行。第 3 步的规范化是**判断性工作，必须由你完成**，不要指望脚本猜。

### 1. 取时间基准

调用 `now-time-info`。后续所有"最近""本月"的相对时间都要基于它，**不要用模型自己猜的日期**。

### 2. 拉取订单

1. `order-list` 取近期订单列表。
2. 对列表中**每一笔**订单调用 `query-order` 取明细（商品、金额、点餐方式）。
3. 结果落盘为 `orders-raw.json`（**不要提交到任何公开仓库**；`.gitignore` 已忽略 `snapshots/` 与 `*.snapshot.json`）。

若失败：

- 429 → 触发了每分钟 600 次限流，退避后重试；`query-order` 逐条调用时**主动控制节奏**。
- 订单数为 0 → 直接告诉用户"近期没有可读取的订单"，不要制造空报告。

### 3. 规范化（关键步骤）

把官方返回转成本 skill 的输入格式。**先跑脚本，不要手工搬字段**：

```bash
# 拿到真实返回后，第一件事：把实际字段名探出来
python scripts/normalize_orders.py --input orders-raw.json --dump-keys

# 再规范化（--input 可以是单个文件，也可以是存放多次返回的目录）
python scripts/normalize_orders.py --input orders-raw.json --output orders.json
```

脚本按「结构特征」识别订单对象（有时间 + 有明细即视为订单），再在对象内部按别名表取字段，
因此外层包成 `data.list` / `records` / 直接数组都能吃；金额单位按**每个文件独立判定**，
并自动去重（`order-list` 与 `query-order` 常有交叠）。

- 若 stdout 报「没有识别出订单对象」→ 先看 `--dump-keys` 的实际键名，再把键补进
  `scripts/normalize_orders.py` 顶部的 `ALIASES`，**不要手工编一份 JSON 绕过去**。
- 若报「疑似金额单位判定有误」→ 用 `--amount-unit fen|yuan` 显式指定后重跑。

输出格式（也就是下游聚合器的输入契约）：

```json
{
  "schema_version": 1,
  "source": { "provider": "McDonald's China MCP", "endpoint": "https://mcp.mcd.cn" },
  "orders": [
    {
      "order_id": "订单号",
      "order_time": "2026-09-14 12:37:00",
      "channel": "到店取餐",
      "store": {
        "store_code": "门店编码",
        "store_name": "门店名称",
        "address": "门店完整地址（用于解析城市与区）"
      },
      "items": [
        { "name": "巨无霸", "quantity": 1, "amount_fen": 2500 }
      ],
      "subtotal_fen": 4850,
      "discount_fen": 400,
      "total_fen": 4450,
      "meal_card": { "applied": true, "card_name": "麦金卡", "saved_fen": 400 }
    }
  ]
}
```

只有脚本识别不了、必须手工补字段时，才按下表守住四条规则：

| 规则 | 说明 |
|---|---|
| **金额单位** | 接口金额是**分**，直接填 `amount_fen` / `total_fen`，**不要预先除以 100**，换算由脚本统一做。 |
| **点餐方式** | 归一到五种：`到店取餐` / `麦乐送` / `得来速` / `堂食外带` / `企业团餐`。无法判定时填「未知」，**不要猜**。 |
| **城市与区** | **不要自己填 `city` / `district` 字段**——脚本会从 `address` 文本解析。你只需保证 `address` 是完整的门店地址原文。 |
| **麦金卡** | 若返回中确实没有持卡 / 权益字段，填 `null`；不要因为"用户可能持卡"就编一个。报告会自动降级说明。 |

`subtotal_fen` 是可选的自检字段：填了，脚本会拿它跟明细加总比对；对不上会在报告里生成告警。**能填就填**，这是给数据上保险。

### 4. 聚合

```bash
python scripts/footprint_aggregate.py --input orders.json --output footprint.json
```

脚本输出五维聚合结果与口径元信息。**检查 stdout 里的告警条数**：

- 订单 < 15 单 → 分布结论稳定性有限，报告里自带降级说明，**你回复用户时也要提一句**。
- 出现「小计对不上」告警 → 说明第 3 步映射有错，**回到第 3 步修**，不要带着错误数字往下走。

### 5. 渲染

```bash
python scripts/render_report.py --input footprint.json --output report.html --card-output card.html
```

产出两个文件：完整报告与独立分享卡（均为自包含 HTML，无外部依赖，可离线打开）。

### 6. 交付

用 `present_files` 把 `report.html` 与 `card.html` 一起交给用户。回复时：

- 给出**三到五个最关键的结论**（常去门店、活跃时段、最爱单品、麦金卡是否回本）；
- 说明数据边界（近期订单、无坐标、统一 UTC+8）；
- **不要**把订单明细原样贴进聊天窗口——那是个人消费记录。

## 决策点速查

| 情况 | 决策 |
|---|---|
| 用户没配 Token | 引导申请，不生成假报告 |
| `order-list` 返回 0 单 | 如实告知无可分析数据，不造空壳报告 |
| 订单 < 15 单 | 照常生成，但明确提示"样本偏小，分布仅供参照" |
| 门店地址解析不出城市 | 保留订单，该单不计入城市维度；报告会告警 |
| 拿不到点餐方式字段 | 填「未知」，不要按订单类型反推成具体场景 |
| 拿不到麦金卡字段 | 填 `null`，报告自动降级为"本次未见持卡记录" |
| 出现小计对不上告警 | 回到规范化步骤修映射，不要绕过 |
| 用户想对比多人 / 全网 | 超出范围，说明本 skill 只处理本人订单 |

## 输出规范

- 金额一律**元、两位小数**；份数用整数。
- 报告必须包含**口径与存疑提示**区块——这是本项目的硬要求，不可省略。
- 涉及麦金卡"回本"结论时，必须同时给出**节省额、月费成本、持有月数**三个数字，让用户能自己验算。
- 结尾固定说明：数据来自用户本人的麦当劳订单，仅存于本机；本项目为参赛作品，非麦当劳官方产品。

## 资源索引

- `references/mcd-tools.md` —— 本 skill 依赖的 MCP 工具与字段提取要点
- `references/playbooks.md` —— 完整调用序列与失败分支
- `references/metrics.md` —— **五维口径定义**（每个数字怎么算出来的）
- `references/pitfalls.md` —— 实测踩坑：无坐标、仅近期订单、无时区、分→元、字段可见性
- `scripts/normalize_orders.py` —— MCP 原始返回 → 规范化订单（含 `--dump-keys` 字段探测）
- `scripts/footprint_aggregate.py` —— 五维聚合器
- `scripts/render_report.py` —— HTML 报告 + 分享卡渲染器
