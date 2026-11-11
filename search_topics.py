"""Search coverage topics, not a hard industry whitelist or official classification."""
ALL_DIRECTIONS = (
    "银行业", "证券", "保险", "多元金融", "房地产", "建筑材料", "建筑装饰",
    "电力", "公用事业", "环保", "煤炭", "石油石化", "化工", "钢铁", "有色金属",
    "农林牧渔", "食品饮料", "家用电器", "纺织服饰", "轻工制造", "商贸零售",
    "社会服务", "美容护理", "医药生物", "医疗服务", "机械设备", "汽车", "交通运输",
    "国防军工", "电子", "半导体", "计算机", "通信", "传媒", "电力设备",
    "光伏", "风电", "储能", "人工智能", "互联网", "教育", "文化娱乐",
    "宏观经济", "货币政策", "财政政策", "资本市场监管", "国际贸易",
    "债券市场", "外汇市场", "大宗商品", "公司治理", "市场情绪与机构观点",
)

# Search hints only: translations do not define issuer classifications or stock mappings.
ENGLISH_DIRECTION_HINTS = dict(zip(ALL_DIRECTIONS, (
    'Banking','Securities and brokerage','Insurance','Diversified financials','Real estate','Building materials','Construction',
    'Electric utilities','Utilities','Environmental services','Coal','Oil and petrochemicals','Chemicals','Steel','Nonferrous metals',
    'Agriculture forestry and fisheries','Food and beverages','Home appliances','Textiles and apparel','Light manufacturing','Wholesale and retail',
    'Consumer services','Beauty and personal care','Pharmaceuticals and biotechnology','Healthcare services','Machinery','Automobiles','Transportation',
    'Aerospace and defense','Electronics','Semiconductors','Computers and software','Telecommunications','Media','Electrical equipment',
    'Solar energy','Wind energy','Energy storage','Artificial intelligence','Internet services','Education','Culture and entertainment',
    'Macroeconomics','Monetary policy','Fiscal policy','Capital market regulation','International trade',
    'Bond markets','Foreign exchange','Commodities','Corporate governance','Market sentiment and institutional views',
)))
ENGLISH_DIRECTION_HINTS.update({'医药':'Pharmaceuticals','银行':'Banking','新能源车':'Electric vehicles'})


def english_direction_hint(direction):
    if direction in ENGLISH_DIRECTION_HINTS:return ENGLISH_DIRECTION_HINTS[direction]
    return direction if direction.isascii() else 'Financial and industry developments; translate the supplied sector label'
