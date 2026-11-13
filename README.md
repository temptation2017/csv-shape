# csv-shape

零依赖 Python CLI：本地查看 CSV / TSV 的形状——分隔符、编码、列类型、空值、不齐的行和重复行。

## 做什么

拿到一份表格（导出、对账、对接接口）时，常常先要回答：有几行几列、用的什么分隔符、哪一列是数字或日期、哪里缺值、有没有少字段或多出来的行。`csv-shape` 只在本地读文件，不连网、无第三方依赖。

它会：

- 探测编码（UTF-8 / BOM / GB18030 / Latin-1）和换行（LF、CRLF、混合）
- 自动识别逗号、制表符、分号、竖线，也认 Excel 常见的首行 `sep=`
- 推断每列类型：`int` / `float` / `bool` / `date` / `datetime` / `string`，并给出空值数、唯一值数、最小最大值或样例
- 标出字段数不一致的行、完全重复的行、重复的表头名
- 列里如果既有 `010000` 这种前导零、又有 `200000`，会整列按编码/字符串处理，避免把邮编、编号当成整数

## 要求

- Python 3.8+
- 标准库 only（`csv` / `argparse` / `json` / `hashlib`）

## 用法

```bash
# 看一份逗号分隔的表
python3 csv_shape.py examples/orders.csv

# 自动按制表符剖析（也可以显式指定）
python3 csv_shape.py examples/messy.tsv
python3 csv_shape.py -d '\t' examples/messy.tsv

# 第一行当数据，不要当表头
python3 csv_shape.py --no-header data.csv

# 只看前 1000 行；机器可读 JSON
python3 csv_shape.py --limit 1000 --json data.csv

# 从 stdin 读取
cat examples/orders.csv | python3 csv_shape.py
python3 csv_shape.py --encoding gb18030 export.csv
```

仓库里的 `examples/orders.csv` 和 `examples/messy.tsv` 是虚构的订单 / 用户样例，没有真实姓名、地址或业务数据。`messy.tsv` 故意带了一行少字段和一行重复，用来演示 Issues。

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功读完并输出剖析结果（有 Issues 也是 0，问题写在输出里） |
| 2 | 用法错误、文件不存在、无法按指定编码解码、或文件像二进制 |

## 规则

- 默认第一行是表头；`--no-header` 时列名为 `col_1`、`col_2`……
- 空值（不计入类型推断和唯一值）：空白、`NA` / `N/A` / `null` / `None` / `NaN` / `#N/A` / `-` / `.`（大小写不敏感）
- 整数不允许前导零（`0` 本身除外），所以 `010000` 这类编号会保持为字符串
- `true` / `false` / `yes` / `no` / `y` / `n` 视为布尔值；`0` / `1` 仍按整数
- 日期支持 `YYYY-MM-DD`、`YYYY/MM/DD`、`YYYY.MM.DD`；日期时间支持常见的空格或 `T` 分隔，以及末尾 `Z` / `+08:00`
- 同一列既有整数又有小数 → `float`；既有日期又有日期时间 → `datetime`；再混入其他类型 → `string`，并加 `mixed types` 说明
- 分隔符可用 `csv.Sniffer`，失败则按前几行里逗号 / 制表符 / 分号 / 竖线的一致性猜测；仍不确定时默认逗号。可用 `-d` 强制指定
- 默认剖析全部数据行，超过 20 万行会截断并在 Issues 里说明，可用 `--limit` 改掉
- 唯一值最多跟踪 5 万个，超出后 Unique 显示为 `≥50000`

不支持（见限制）：带千分位的数字（`1,234.56`）、货币符号、多表 / 固定宽度文本、真正的 `.xlsx`。

## 示例

`python3 csv_shape.py examples/orders.csv`：

```
File:            examples/orders.csv
Size:            576 B
Encoding:        utf-8
Line endings:    LF
Delimiter:       comma (,) (sniffed)
Header:          yes
Shape:           12 rows × 7 columns

    #  Name      Type      Empty  Unique  Range / samples
    1  order_id  int           0      12  1001 .. 1012
    2  sku       string        0       5  "SKU-A", "SKU-B", "SKU-C", …
    3  qty       int           0       6  1 .. 10
    4  price     float         0       5  3.2 .. 120.0
    5  paid_at   datetime      1      11  2026-01-03 10:15:00 .. 2026-03-20 21:10:00
    6  shipped   bool          0       2  true=8, false=4
    7  note      string        8       3  "gift", "leave at door", "bulk"

Issues:          none
```

`python3 csv_shape.py examples/messy.tsv` 会把 `postal` 整列标成 string（保留前导零），并报告第 8 行少一个字段、以及第 2 行与第 6 行重复。

## 限制与后续可改进点

- 不解析千分位、货币、百分比，也不做 locale 相关的小数逗号（`1.234,56`）
- 日期不含对 `DD/MM/YYYY` 与 `MM/DD/YYYY` 的猜测，避免把 03/04/2026 弄反；这类值会落成 string
- 带时区偏移的日期时间只用来认定类型和比大小，不会换算时区
- 不是完整的 Excel 方言实现：不读 `.xlsx`，也不展开公式
- 后续可以加：列之间的候选主键、值分布直方图、按主键探测近似重复、输出简单的 JSON Schema

## 许可

MIT，见 [LICENSE](LICENSE)。
