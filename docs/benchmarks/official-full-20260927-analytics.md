# GAP 6b：1038 条回灌数据五类查询演示实测

**只验证是否返回可展示结果、条数与耗时，不能证明答案正确。**

实测时间（UTC）：`2026-09-27T15:01:29.155617+00:00`；数据集：`6e032fb74bd345e29989e4f86e195920`（官方全量·修复后）。
代码基准：`68642fcc542a396a7224e1633d4ba0caca635b4f`；源库：`D:\ICT\bid-intel\backend\.data\datasets\6e032fb74bd345e29989e4f86e195920.sqlite`。

## 数据与输入

| 表 | 条数 |
|---|---:|
| `notices` | 1038 |
| `projects` | 1038 |
| `packages` | 1658 |
| `organizations` | 4229 |
| `procurement_items` | 6472 |
| `bid_participations` | 5009 |
| `awards` | 1315 |
| `import_receipts` | 1038 |

投标状态：{"nonwinner": 1073, "unknown": 2621, "winner": 1315}

主体选择限定为当前网页下拉前 500 可见且有关系的主体，不代表所有主体的覆盖率：

- buyer：`124` 东莞职业技术学院
- supplier：`114` 中国移动通信集团广东有限公司
- pair：`114` 中国移动通信集团广东有限公司；`157` 中国电信股份有限公司广东分公司

## 返回结果与计时

每场景 warm 重复 30 次，全部结果与首调一致。单位：ms。

| 场景 | 返回条数 | 首调 | warm p50 | warm p95 | HTTP 单次 |
|---|---|---:|---:|---:|---:|
| `buyer_awardees` | awardees=15 | 89.5438 | 88.9785 | 92.9278 | 122.4998 |
| `buyer_bidders` | top_bidders=5；co_bidder_pairs=5 | 12.5964 | 12.1403 | 13.2331 | 14.9405 |
| `supplier_co_bidders` | top_co_bidders=5；packages=25 | 11.3262 | 11.9091 | 13.1112 | 16.3286 |
| `common_buyers` | selected_suppliers=2；buyers=4 | 9.3094 | 8.9043 | 10.1982 | 11.1502 |
| `common_projects` | required_entities=2；packages=29；package_count=29；project_count=22 | 22.0929 | 22.8483 | 24.6457 | 18.9869 |

HTTP 请求使用 X-Dataset-ID 选择同一原库；已运行服务未重启，代码版本未确认。

- `buyer_awardees`：HTTP 200；结果数组一致=True；响应差异字段=[]。
- `buyer_bidders`：HTTP 200；结果数组一致=True；响应差异字段=[]。
- `supplier_co_bidders`：HTTP 200；结果数组一致=True；响应差异字段=['include_winners']。
- `common_buyers`：HTTP 200；结果数组一致=True；响应差异字段=[]。
- `common_projects`：HTTP 200；结果数组一致=True；响应差异字段=[]。

完整响应、全部重复耗时、请求路径与参数见同名 JSON。下列为每类响应的首条主结果：

### buyer_awardees

```json
[
  {
    "organization_id": 777,
    "name": "上海形宙数字技术有限公司",
    "award_package_count": 1,
    "award_amount_total": "2198000.00",
    "product_brands": [
      "9 1 4 PPB",
      "PPB",
      "PPB 2 0 2 1- 0",
      "字 数 宙PPB 形"
    ]
  }
]
```

### buyer_bidders

```json
[
  {
    "id": 385,
    "canonical_name": "哈尔滨市南岗区成拾商贸行（个体工商户）",
    "package_count": 4
  }
]
```

### supplier_co_bidders

```json
[
  {
    "organization_id": 157,
    "canonical_name": "中国电信股份有限公司广东分公司",
    "package_count": 8
  }
]
```

### common_buyers

```json
[
  {
    "buyer": {
      "id": 428,
      "canonical_name": "中山市卫生健康局三乡分局"
    },
    "suppliers": [
      {
        "organization_id": 114,
        "name": "中国移动通信集团广东有限公司",
        "award_package_count": 1,
        "award_amount_total": "22077007.00"
      },
      {
        "organization_id": 157,
        "name": "中国电信股份有限公司广东分公司",
        "award_package_count": 2,
        "award_amount_total": "6808020.33"
      }
    ],
    "award_amount_total_unique_awards": "28885027.33"
  }
]
```

### common_projects

```json
[
  {
    "package_id": 53,
    "package_code": "1",
    "package_name": null,
    "project_id": 42,
    "project_name": "民众街道民众分局视频监控运维升级改造项目",
    "project_number": "ZSJD25ZC0118",
    "announced_total_award": "3588000.000000",
    "package_award_total": "3588000.000000",
    "buyer_organization_id": 156,
    "buyer": {
      "id": 156,
      "canonical_name": "中山市公安局民众分局"
    },
    "participants": [
      {
        "organization_id": 157,
        "canonical_name": "中国电信股份有限公司广东分公司",
        "outcome": "winner"
      },
      {
        "organization_id": 114,
        "canonical_name": "中国移动通信集团广东有限公司",
        "outcome": "unknown"
      },
      {
        "organization_id": 158,
        "canonical_name": "中山万鼎科技有限公司",
        "outcome": "unknown"
      },
      {
        "organization_id": 159,
        "canonical_name": "广东布恩网络有限公司",
        "outcome": "unknown"
      }
    ],
    "award_amount_total_unique_awards": "3588000.00"
  }
]
```

## 验证边界与复现

- 本报告只验证返回结果、条数与耗时；1038 条回灌数据没有 gold，不能证明答案正确。
- 所选主体已知有关系，用于非空演示；不是所有主体/主体组合的覆盖率或准确率测量。
- 源库以 mode=ro 打开，经 SQLite backup 建临时副本；分析函数在副本执行，未注入主体或修正抽取数据。
- 函数计时包含建连、initialize 和结果组装，不含备份、选参、HTTP、网络或浏览器渲染。
- 首调为该进程该场景首次函数调用，未清理 OS 缓存；随后 30 次 warm，p50 为中位数，p95 为 nearest-rank。
- HTTP 实测若启用，单次耗时含本机 HTTP、鉴权和 JSON 传输；不代表浏览器渲染或多用户负载。
- 场景 2/3 的 include_winners=false 当前执行 outcome != 'winner'，仍含 unknown，不能等同确定未中标。
- 频次沿用当前实现按采购包统计；场景 5 同时报告去重项目数与包数，金额未按原件核验。
- 未验证 Neo4j、独立 gold 对照（6a）或第二台设备；未调用模型、未重新抽取公告。

源库 SHA-256 前后相同：`True`；`201b68f0d39166947d9c8fa502e458d50a8ee0860fd74eccf50abca825a2bf83`。

本次在固定提交的独立 checkout 中测量，避免混入其它任务正在修改的查询口径。复现时将本脚本放入同一代码基准的 scripts 目录，并在该 checkout 根目录（PowerShell）运行：

```powershell
& "D:\ICT\bid-intel\backend\.venv\Scripts\python.exe" scripts/check_analytics_display.py --database "D:\ICT\bid-intel\backend\.data\datasets\6e032fb74bd345e29989e4f86e195920.sqlite" --buyer-id 124 --supplier-id 114 --pair 114 157 --repeats 30 --output "D:\ICT\bid-intel\docs\benchmarks\official-full-20260927-analytics.json" --api-base "http://127.0.0.1:8000" --env-file "D:\ICT\bid-intel\backend\.env"
```
