# 麦当劳 MCP Tool 参考 · mcd-footprint 视角

来源：麦当劳中国 MCP Server，**2026-10-18 用真实 Token 实测**，`tools/list` 返回 **35 个 Tool**
（文档早期口径写的是 33 个，实测多出 2 个，以 `tools/list` 为准）。
接入：`https://mcp.mcd.cn`，Streamable HTTP，请求头 `Authorization: Bearer <token>`。
Token 申请：<https://open.mcd.cn/mcp>（手机号登录 → 控制台 → 激活 → 复制，免费）。
限流：**600 次/分钟/token**，超限返回 429。

> 本文所有字段名、类型、单位均来自 10 单真实订单的实测返回，不是推测。

---

## 一、本 skill 依赖的工具

### `now-time-info` —— 时间基准

- **用途**：取服务端当前时间，作为所有相对时间（"最近""本月"）的基准。
- **入参**：无。
- **用法**：工作流第 1 步调用**一次**即可，不要每步都调。
- **为什么必须用它**：模型自己推算的日期不可靠，涉及时间跨度的统计会全盘偏移。

### `order-list` —— 历史订单列表

- **用途**：查询用户的**近期**到店 / 外送历史订单。
- **入参**：**无**（`inputSchema` 为空，不接受分页、时间范围等参数）。
- **实测返回**：一次返回 **10 条**，字段如下（全部为字符串，除标注外）：

  | 字段 | 含义 | 备注 |
  |---|---|---|
  | `orderId` | 订单号 | 25 位数字串，用于去重 |
  | `orderType` | 订单类型 | `"1"` 到店 / `"2"` 外送 |
  | `createTime` | 下单时间 | `"2026-09-07 12:15:46"`，**无时区** |
  | `beType` | 门店类型 | `"1"` 到店取餐 / `"2"` 麦乐送 / `"5"` 得来速 / `"6"` 企业团餐 |
  | `beCode` | BE 编码 | 实测为空串 |
  | `storeCode` | 门店编码 | 纯数字，如 `"1450442"` |
  | `storeName` | 门店名 | 如「麦当劳上海虹桥万创中心餐厅」，**含城市名** |
  | `orderStatus` | 订单状态 | 如「订单已完成」 |
  | `orderProductList[]` | 商品列表 | 元素含 `productCode` / `productName` / `quantity` / `comboItemList[]` |
  | `realTotalAmount` | 实付金额 | `"14.9"` —— **单位是元，且为字符串** |

- **关键限制**：官方口径明确——**「近期订单不是全年完整历史」**。实测 10 条覆盖约 3 个月。
- **注意**：**不含**麦麦商城订单；商城订单需用 `mall-order-list`，与本 skill 无关。
- **注意**：**不返回门店地址**，也没有任何金额拆分字段（没有原价、没有优惠）。

### `query-order` —— 订单详情

- **用途**：查单笔订单的状态、内容、金额拆分、用餐方式。
- **入参**：`orderId`（必填）。
- **实测返回**（`data` 下的字段，共 20 个，全部 10/10 出现）：

  | 字段 | 含义 | 备注 |
  |---|---|---|
  | `orderId` / `createTime` / `orderStatus` / `status` | 订单标识与状态 | `status` 为数字串，如 `"40"` |
  | `storeName` | 门店名 | 与 `order-list` 一致 |
  | `storeAddress` | 门店地址 | **不含城市名**（如「东山街道玖力大厦1号楼1层101…」） |
  | `takeWay` | 用餐方式 | 实测取值 `堂食` / `外带` |
  | `orderProductList[]` | 商品列表 | 元素含 `productName` / `price` / `quantity` / `productImage` / `comboItemList[]`，**没有 `productCode`** |
  | `totalAmount` | 折前原价 | `"72.5"`，单位元 |
  | `realTotalAmount` | 实付金额 | `"38"`，单位元 |
  | `totalDiscountAmount` | 优惠合计 | `"34.5"`，单位元 |
  | `couponList[]` | 券明细 | 元素含 `couponName` / `discountAmount` |
  | `productPrice` / `deliveryPrice` / `realDeliveryPrice` | 商品与配送价 | 单位元 |
  | `pickupCode` / `payId` / `expirePayTime` / `remark` / `needTableware` | 取餐码、支付、备注等 | 本 skill 不使用 |

- **用法**：对 `order-list` 返回的**每一笔**逐条调用，补齐地址、用餐方式与金额拆分。
- **注意**：逐条调用会快速消耗限流额度，务必控制节奏（实测 10 单约 3 秒）。

### `campaign-calendar` —— 活动日历（可选）

- **用途**：查当月营销活动日历。
- **用法**：仅当用户想把订单和当期活动对上时调用一次作背景注解。**非必需**，主流程不依赖。

---

## 二、本 skill 明确不调用的工具

本 skill 是**只读分析**工具，与下单链路完全隔离。

| 工具组 | 为什么不用 |
|---|---|
| `query-nearby-stores` / `delivery-query-stores` | 面向"下单选店"，且**不返回经纬度**；本 skill 的门店信息直接来自订单本身。 |
| `query-meals` / `query-meal-detail` | 菜单与套餐详情是点餐输入，与历史订单分析无关。 |
| `calculate-price` / `create-order` / `cancel-order` | 下单链路，会产生**真实交易**。本 skill 绝不触碰。 |
| `available-coupons` / `auto-bind-coupons` / `query-my-coupons` | 券库操作会**修改用户券包**，不需要。 |
| `query-my-account` / `mall-*` / `draw-lottery` / `query-party-*` | 积分、商城、抽奖、主题活动预约，均超出范围。 |

> **红线**：不调用任何写操作工具。一次误调就可能替用户下单或改动券包。

---

## 三、字段提取要点

规范化脚本 `normalize_orders.py` 的 `ALIASES` 已按实测字段名锁定下表。

| 目标 | order-list | query-order | 备注 |
|---|---|---|---|
| 订单号 | `orderId` | `orderId` | 两接口一致，用于合并与去重 |
| 下单时间 | `createTime` | `createTime` | **不含时区**，按 UTC+8 解释 |
| 门店编码 | `storeCode` | **无** | 只有 order-list 有 |
| 门店名称 | `storeName` | `storeName` | 两接口都有，**城市只能从这里切** |
| 门店地址 | **无** | `storeAddress` | 只有 query-order 有，且**不含城市** |
| 点餐方式 | `beType` | **无** | 只有 order-list 有 |
| 用餐方式 | **无** | `takeWay` | 堂食 / 外带，只有 query-order 有 |
| 商品名称 | `productName` | `productName` | 一致 |
| 商品数量 | `quantity` | `quantity` | 一致 |
| 商品单价 | **无** | `price` | 只有 query-order 有 |
| 套餐子项 | `comboItemList[].name` / `.quantity` | `comboItemList[].itemName` / `.itemQuantity` | **同名不同键**，见下 |
| 优惠金额 | **无** | `totalDiscountAmount` | 只有 query-order 有 |
| 券明细 | **无** | `couponList[].couponName` / `.discountAmount` | 只有 query-order 有 |
| 折前原价 | **无** | `totalAmount` | 只有 query-order 有 |
| 实付金额 | `realTotalAmount` | `realTotalAmount` | 两接口都有，**单位是元** |
| 持卡 / 权益标识 | **无** | **无** | 全量搜 card / 麦金 / 会员 / vip 均无命中 |

**两个最容易踩的坑**：

1. **同名不同构**。`orderProductList` 在两个接口里名字一样，但：
   - order-list 的元素有 `productCode`、没有 `price`；
   - query-order 的元素有 `price`、没有 `productCode`。
   同理 `comboItemList` 在 order-list 里是 `name`/`quantity`，在 query-order 里是
   `itemName`/`itemQuantity`。**不能假设"同名字段 = 同构"**。

2. **金额单位是「元」不是「分」**。`realTotalAmount` 返回 `"14.9"` 这样的**字符串**。
   「分」只出现在 `query-meals` / `calculate-price` 这类菜单接口里。混用会把金额差 100 倍。

---

## 四、已知的接口能力缺口

以下四条不是"没想到"，是**官方接口目前确实给不了**，实测确认：

1. **不返回门店经纬度** —— 所以做不了真正的地理热力图，地点维度只能停在行政区划层级。
2. **`storeAddress` 不含城市** —— 城市只能从 `storeName` 前缀切，或依赖地址里恰好写了城市。
   `storeName` 的格式是「麦当劳 + 城市 + 店名 + 餐厅」，但城市名 2~4 字不定长，
   没有词表就无法可靠切分。本项目内置约 180 个常见城市名做最长前缀匹配，不在表内就留空。
3. **历史订单只有"近期"** —— `order-list` 无参数、约 10 条，`query-order` 需逐个订单号。
   样本量天然受限，分布类结论必须带降级说明。
4. **没有任何持卡 / 会员权益字段** —— 所以做不了"麦金卡值不值"这个维度。
   本项目改用真实可算的**优惠账本**（原价 / 实付 / 优惠合计 / 券明细）替代。

---

## 五、只读声明

本 skill 只调用 `now-time-info`、`order-list`、`query-order`，
可选调用 `campaign-calendar`。**全部为只读工具**，不会下单、取消、领券、兑换或预约。
