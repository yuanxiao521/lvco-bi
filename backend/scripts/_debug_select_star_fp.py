"""复现 SELECT * 误报：用 SQLGlot 直接解析三条 SQL，看 Star 节点被检出情况。"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlglot
from sqlglot import exp

# 候选：LLM 在验证查询时最可能写的几条 SQL
TEST_SQLS = [
    # 1. 纯显式列（应通过）
    ('显式列+LIMIT', '''SELECT "name", "fans" FROM "douyin_1ab854fa"."data" ORDER BY "fans" DESC LIMIT 5'''),
    # 2. 带 COUNT(*) 聚合（用户场景：做行数验证或统计）——疑似误判点
    ('COUNT(*) 聚合', '''SELECT COUNT(*) AS total_rows FROM "douyin_1ab854fa"."data" LIMIT 1'''),
    # 3. 多列 + COUNT(*) 混写
    ('多列+COUNT(*)', '''SELECT "category", COUNT(*) AS cnt FROM "douyin_1ab854fa"."data" GROUP BY "category" LIMIT 10'''),
    # 4. 真·SELECT *（应继续拦截）
    ('真 SELECT *', '''SELECT * FROM "douyin_1ab854fa"."data" LIMIT 5'''),
    # 5. CTE 里 SELECT *（常见验证写法）
    ('CTE 内 SELECT *', '''WITH preview AS (SELECT * FROM "douyin_1ab854fa"."data" LIMIT 5) SELECT COUNT(*) FROM preview'''),
    # 6. table.* 误判
    ('table.*', '''SELECT d.* FROM "douyin_1ab854fa"."data" AS d LIMIT 5'''),
    # 7. SUM(1) + COUNT(col) 合法聚合（应通过）
    ('SUM(1)+COUNT(col)', '''SELECT SUM(1) AS n, COUNT("fans") AS fans_cnt FROM "douyin_1ab854fa"."data"'''),
]

print("=" * 80)
print("① SQLGlot AST: find_all(exp.Star) 检测结果")
print("=" * 80)
for label, sql in TEST_SQLS:
    parsed = sqlglot.parse(sql, dialect="duckdb")
    if not parsed or parsed[0] is None:
        print(f"  [FAIL PARSE] {label}")
        continue
    root = parsed[0]
    stars = list(root.find_all(exp.Star))
    # 打印每个 Star 节点的父路径（所在表达式类型）
    contexts = []
    for s in stars:
        # 找 Star 的直接父节点是什么
        # SQLGlot 里 Star 的父节点可以从 find_all 外层循环判断
        # 这里我们遍历 select_expr 级看位置
        parent = None
        for func in root.find_all(exp.Func):
            if any(c is s for c in func.args.values() if isinstance(c, exp.Expression)):
                parent = f"Func({func.sql_name() if hasattr(func, 'sql_name') else type(func).__name__})"
                break
        if parent is None:
            for proj in root.find_all(exp.Select):
                for expr in proj.expressions:
                    if expr is s or (isinstance(expr, exp.Alias) and expr.this is s):
                        parent = f"Select.project (真 SELECT *)"
                        break
        contexts.append(parent or "Unknown")
    print(f"\n[{label}]")
    print(f"  SQL  : {sql[:120]}")
    print(f"  Stars: {len(stars)} 个 -> 上下文: {contexts}")
    if stars:
        for i, s in enumerate(stars):
            print(f"    #{i}: {s.sql(dialect='duckdb')}  节点类型={type(s).__name__}")

# ── 再调 ast_full_check 看真实拦截结果 ──
print("\n" + "=" * 80)
print("② ast_full_check 真实判定结果")
print("=" * 80)
from app.services.sql_guard_ast import ast_full_check
for label, sql in TEST_SQLS:
    passed, reason, sanitized, details = ast_full_check(sql)
    status = "✅ 通过" if passed else "❌ 拦截"
    rule = details.get("failed_rule") if details else None
    print(f"[{label}] {status}")
    print(f"  reason: {reason or '-'}")
    if rule:
        print(f"  failed_rule: {rule}")
    if passed:
        print(f"  sanitized: {sanitized[:100]}")
