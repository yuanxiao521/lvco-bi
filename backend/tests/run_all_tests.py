#!/usr/bin/env python3
"""
================================================================================
Lvco BI 全方位自动化测试运行器（增强版）
================================================================================
统一入口，整合所有测试层级：
- Layer 1: pytest 单元测试（Service / Repository 业务逻辑）
- Layer 2: full_auto_test.py 端到端集成测试（13 大类业务流程）
- Layer 3: 测试报告汇总（Markdown + JSON）

运行方式：
    python tests/run_all_tests.py                    # 运行所有测试
    python tests/run_all_tests.py --skip-unit        # 跳过单元测试
    python tests/run_all_tests.py --skip-e2e         # 跳过端到端测试
    python tests/run_all_tests.py --unit-only        # 只跑单元测试
    python tests/run_all_tests.py --e2e-only         # 只跑端到端测试
    python tests/run_all_tests.py --base-url URL     # 指定后端地址
    python tests/run_all_tests.py --report-path PATH # 指定报告输出路径

环境要求：
    pip install pytest pytest-asyncio requests
================================================================================
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ============================================================================
# 颜色与输出
# ============================================================================
class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    CYAN = "\033[96m"
    MAGENTA = "\033[95m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def log_info(msg: str) -> None:
    print(f"{Colors.CYAN}[INFO]{Colors.RESET} {msg}")


def log_success(msg: str) -> None:
    print(f"{Colors.GREEN}[PASS]{Colors.RESET} {msg}")


def log_error(msg: str) -> None:
    print(f"{Colors.RED}[FAIL]{Colors.RESET} {msg}")


def log_warning(msg: str) -> None:
    print(f"{Colors.YELLOW}[WARN]{Colors.RESET} {msg}")


def log_section(msg: str) -> None:
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*70}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}  {msg}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'='*70}{Colors.RESET}\n")


# ============================================================================
# 测试阶段定义
# ============================================================================

class TestStage:
    """单个测试阶段的执行结果"""

    def __init__(self, name: str, command: list[str], cwd: str, timeout: int = 600):
        self.name = name
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.start_time: float = 0.0
        self.duration: float = 0.0
        self.returncode: int = -1
        self.stdout: str = ""
        self.stderr: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "command": " ".join(self.command),
            "duration_s": round(self.duration, 1),
            "returncode": self.returncode,
            "passed": self.returncode == 0,
        }


# ============================================================================
# 阶段 1：pytest 单元测试
# ============================================================================

def run_pytest_unit_tests(
    backend_dir: str,
    test_files: list[str] | None = None,
) -> TestStage:
    """运行 pytest 单元测试。

    Args:
        backend_dir: 后端根目录绝对路径
        test_files: 指定的测试文件列表，None 表示运行 tests/test_*.py

    Returns:
        TestStage 对象，包含执行结果
    """
    stage = TestStage(
        name="pytest 单元测试",
        command=[sys.executable, "-m", "pytest"],
        cwd=backend_dir,
        timeout=600,
    )

    if test_files:
        stage.command.extend(["-v", "--tb=short"] + test_files)
    else:
        # 默认运行 Service + Repository 单元测试
        stage.command.extend([
            "-v",
            "--tb=short",
            "tests/test_dashboard_service.py",
            "tests/test_dashboard_repository.py",
        ])

    log_info(f"执行命令: {' '.join(stage.command)}")
    log_info(f"工作目录: {stage.cwd}")

    stage.start_time = time.time()
    try:
        proc = subprocess.run(
            stage.command,
            cwd=stage.cwd,
            capture_output=True,
            text=True,
            timeout=stage.timeout,
            encoding="utf-8",
            errors="replace",
        )
        stage.returncode = proc.returncode
        stage.stdout = proc.stdout
        stage.stderr = proc.stderr
    except subprocess.TimeoutExpired:
        stage.returncode = 124
        stage.stderr = f"pytest 单元测试超时 ({stage.timeout}s)"
    except Exception as e:
        stage.returncode = -1
        stage.stderr = f"执行异常: {type(e).__name__}: {e}"
    finally:
        stage.duration = time.time() - stage.start_time

    return stage


def parse_pytest_summary(stdout: str) -> dict:
    """从 pytest 输出解析测试摘要。"""
    import re
    summary = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "duration_s": 0.0,
    }
    for line in stdout.splitlines():
        line = line.strip()
        # pytest 最终摘要行示例：
        #   "===== 40 passed in 4.71s ====="
        #   "===== 5 failed, 3 passed in 1.2s ====="
        #   "===== 1 passed, 1 warning in 0.1s ====="
        if line.startswith("=") and "passed" in line and " in " in line:
            # 提取数字+状态词
            tokens = re.findall(r"(\d+)\s+(passed|failed|error|skipped|warning|warnings)", line)
            for count, key in tokens:
                count = int(count)
                if key == "passed":
                    summary["passed"] = count
                elif key in ("failed", "error"):
                    summary["failed"] += count
                elif key == "skipped":
                    summary["skipped"] = count
                # warning 不计入 total
            # 提取耗时
            m = re.search(r"in\s+([\d.]+)s", line)
            if m:
                summary["duration_s"] = float(m.group(1))
    summary["total"] = summary["passed"] + summary["failed"] + summary["skipped"]
    return summary


# ============================================================================
# 阶段 2：端到端集成测试（full_auto_test.py）
# ============================================================================

def run_e2e_tests(
    backend_dir: str,
    base_url: str | None = None,
) -> TestStage:
    """运行端到端集成测试。

    Args:
        backend_dir: 后端根目录绝对路径
        base_url: 后端服务地址，None 表示使用环境变量或默认值

    Returns:
        TestStage 对象，包含执行结果
    """
    full_auto_test_path = os.path.join(backend_dir, "tests", "full_auto_test.py")

    if not os.path.exists(full_auto_test_path):
        stage = TestStage(
            name="端到端集成测试",
            command=[],
            cwd=backend_dir,
        )
        stage.returncode = -1
        stage.stderr = f"找不到测试脚本: {full_auto_test_path}"
        return stage

    env = os.environ.copy()
    if base_url:
        env["TEST_BASE_URL"] = base_url

    # 直接 import 并调用 main()，避免等待 input() 阻塞
    stage = TestStage(
        name="端到端集成测试",
        command=[sys.executable, full_auto_test_path],
        cwd=backend_dir,
        timeout=1800,  # 端到端测试耗时较长
    )

    log_info(f"执行命令: {' '.join(stage.command)}")
    log_info(f"目标环境: {base_url or env.get('TEST_BASE_URL', 'http://127.0.0.1:8000')}")

    stage.start_time = time.time()
    try:
        proc = subprocess.run(
            stage.command,
            cwd=stage.cwd,
            capture_output=True,
            text=True,
            timeout=stage.timeout,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        stage.returncode = proc.returncode
        stage.stdout = proc.stdout
        stage.stderr = proc.stderr
    except subprocess.TimeoutExpired:
        stage.returncode = 124
        stage.stderr = f"端到端测试超时 ({stage.timeout}s)"
    except Exception as e:
        stage.returncode = -1
        stage.stderr = f"执行异常: {type(e).__name__}: {e}"
    finally:
        stage.duration = time.time() - stage.start_time

    return stage


def parse_e2e_summary(stdout: str) -> dict:
    """从端到端测试输出解析测试摘要。"""
    summary = {
        "total": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "pass_rate": 0.0,
    }
    for line in stdout.splitlines():
        line = line.strip()
        if "总计:" in line and "通过:" in line:
            # 格式: "总计: N | 通过: X | 失败: Y | 跳过: Z"
            parts = line.split("|")
            for p in parts:
                p = p.strip()
                if p.startswith("总计:"):
                    try:
                        summary["total"] = int(p.split(":")[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif p.startswith("通过:"):
                    try:
                        summary["passed"] = int(p.split(":")[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif p.startswith("失败:"):
                    try:
                        summary["failed"] = int(p.split(":")[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif p.startswith("跳过:"):
                    try:
                        summary["skipped"] = int(p.split(":")[1].strip())
                    except (ValueError, IndexError):
                        pass
        elif "通过率:" in line:
            try:
                rate_str = line.split("通过率:")[1].strip().rstrip("%")
                summary["pass_rate"] = float(rate_str)
            except (ValueError, IndexError):
                pass
    return summary


# ============================================================================
# 综合报告生成
# ============================================================================

def generate_combined_report(
    unit_stage: TestStage | None,
    e2e_stage: TestStage | None,
    unit_summary: dict,
    e2e_summary: dict,
    report_path: str,
) -> None:
    """生成综合测试报告（Markdown + JSON）。"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total_passed = unit_summary["passed"] + e2e_summary["passed"]
    total_failed = unit_summary["failed"] + e2e_summary["failed"]
    total_skipped = unit_summary["skipped"] + e2e_summary["skipped"]
    total = unit_summary["total"] + e2e_summary["total"]
    pass_rate = (total_passed / (total_passed + total_failed) * 100) if (total_passed + total_failed) > 0 else 0
    overall = "通过" if total_failed == 0 else f"存在 {total_failed} 个失败"

    lines = []
    lines.append(f"# Lvco BI 全方位自动化测试报告")
    lines.append("")
    lines.append(f"**测试时间**: {now_str}")
    lines.append(f"")
    lines.append(f"## 总体概况")
    lines.append("")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 测试总数 | {total} |")
    lines.append(f"| 通过 | {total_passed} |")
    lines.append(f"| 失败 | {total_failed} |")
    lines.append(f"| 跳过 | {total_skipped} |")
    lines.append(f"| 通过率 | {pass_rate:.1f}% |")
    lines.append(f"| 综合结论 | **{overall}** |")
    lines.append("")

    # 各阶段明细
    lines.append("## 各测试阶段")
    lines.append("")
    lines.append("| 阶段 | 总数 | 通过 | 失败 | 跳过 | 通过率 | 耗时 |")
    lines.append("|------|------|------|------|------|--------|------|")

    unit_pass_rate = (unit_summary["passed"] / (unit_summary["passed"] + unit_summary["failed"]) * 100) if (unit_summary["passed"] + unit_summary["failed"]) > 0 else 100
    e2e_pass_rate = (e2e_summary["passed"] / (e2e_summary["passed"] + e2e_summary["failed"]) * 100) if (e2e_summary["passed"] + e2e_summary["failed"]) > 0 else 100
    lines.append(
        f"| 单元测试 | {unit_summary['total']} | {unit_summary['passed']} | "
        f"{unit_summary['failed']} | {unit_summary['skipped']} | "
        f"{unit_pass_rate:.1f}% | {unit_summary['duration_s']:.1f}s |"
    )
    lines.append(
        f"| 端到端集成测试 | {e2e_summary['total']} | {e2e_summary['passed']} | "
        f"{e2e_summary['failed']} | {e2e_summary['skipped']} | "
        f"{e2e_pass_rate:.1f}% | {e2e_stage.duration:.1f}s |"
        if e2e_stage else f"| 端到端集成测试 | 跳过 | - | - | - | - | - |"
    )
    lines.append("")

    # 测试范围说明
    lines.append("## 测试覆盖范围")
    lines.append("")
    lines.append("### Layer 1: 单元测试（pytest）")
    lines.append("")
    lines.append("- DashboardService 业务逻辑（CRUD + get_dashboard_data + 缓存）")
    lines.append("- DashboardRepository 数据访问（Protocol + SQLAlchemy）")
    lines.append("- Mock Repository 与 Mock AsyncSession 完全隔离数据库")
    lines.append("")
    lines.append("### Layer 2: 端到端集成测试（full_auto_test.py）")
    lines.append("")
    lines.append("- **01 Baseline**: 服务健康检查、OpenAPI、CORS")
    lines.append("- **02 Smoke**: 注册→登录→刷新Token→登出→重新登录")
    lines.append("- **03 Auth**: 改密/换密码/更新资料/错误密码/短密码/未认证")
    lines.append("- **04 DataSource**: 列表/上传/预览/边界/Sync/AI清洗")
    lines.append("- **05 Canvas**: 创建/查询/Block/查询/AIRecommend/PDF/存为报表")
    lines.append("- **06 Dashboard**: 创建/布局/图表/刷新/分享")
    lines.append("- **07 Report**: 创建/状态/分享/PDF导出")
    lines.append("- **08 AI**: 会话CRUD/Query/Insights/Polish/Clean/SSE流")
    lines.append("- **09 Statistics**: describe/correlation/ranking/summary/comparison/preview")
    lines.append("- **10 Boundary**: SQL注入/XSS/限流/并发/超参/路径遍历")
    lines.append("- **11 Notification**: 列表/未读/已读/SSE")
    lines.append("- **12 Permission&Audit**: 用户/角色/审计/CSV导出")
    lines.append("- **13 Trash&Public**: 软删/恢复/彻底删/公开分享")
    lines.append("")

    # 失败项详情
    failed_lines = []
    if unit_stage and unit_summary["failed"] > 0:
        failed_lines.append("### 单元测试失败")
        failed_lines.append("")
        failed_lines.append("```")
        # 从 pytest 输出提取 FAILED 行
        for line in unit_stage.stdout.splitlines():
            if "FAILED" in line or "ERROR" in line:
                failed_lines.append(line)
        failed_lines.append("```")
        failed_lines.append("")
    if e2e_stage and e2e_summary["failed"] > 0:
        failed_lines.append("### 端到端测试失败")
        failed_lines.append("")
        failed_lines.append("详见 `test_report.md` 详细报告")
        failed_lines.append("")

    if failed_lines:
        lines.append("## 失败项详情")
        lines.append("")
        lines.extend(failed_lines)

    # 建议
    lines.append("## 建议与备注")
    lines.append("")
    if total_failed == 0:
        lines.append("- 所有测试均通过，系统运行正常，可以交付答辩。")
    else:
        lines.append(f"- 存在 {total_failed} 个失败项，建议在答辩前修复。")
    if total_skipped > 0:
        lines.append(
            f"- {total_skipped} 个测试项被跳过（多为缺少数据源或LLM不可用），不影响核心功能评估。"
        )
    lines.append("- AI相关测试可能因LLM余额不足(402)或服务不可用(503)而跳过，属于外部依赖。")
    lines.append("- 单元测试使用 Mock，完全隔离数据库，可重复运行。")
    lines.append("- 端到端集成测试需要后端服务运行中，会创建/删除实际数据。")
    lines.append("")

    # 写入 Markdown
    content = "\n".join(lines)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)

    # 写入 JSON
    json_path = report_path.replace(".md", ".json")
    json_data = {
        "meta": {
            "test_time": now_str,
            "report_type": "combined",
        },
        "summary": {
            "total": total,
            "passed": total_passed,
            "failed": total_failed,
            "skipped": total_skipped,
            "pass_rate": round(pass_rate, 1),
            "overall": overall,
        },
        "unit_tests": {
            "summary": unit_summary,
            "stage": unit_stage.to_dict() if unit_stage else None,
        },
        "e2e_tests": {
            "summary": e2e_summary,
            "stage": e2e_stage.to_dict() if e2e_stage else None,
        },
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)

    log_success(f"综合报告已生成:")
    log_info(f"  Markdown: {report_path}")
    log_info(f"  JSON:     {json_path}")


# ============================================================================
# 主入口
# ============================================================================

def parse_args() -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Lvco BI 全方位自动化测试运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--skip-unit", action="store_true", help="跳过 pytest 单元测试"
    )
    parser.add_argument(
        "--skip-e2e", action="store_true", help="跳过端到端集成测试"
    )
    parser.add_argument(
        "--unit-only", action="store_true", help="只跑单元测试"
    )
    parser.add_argument(
        "--e2e-only", action="store_true", help="只跑端到端测试"
    )
    parser.add_argument(
        "--base-url", default=None, help="后端服务地址（如 http://127.0.0.1:8000）"
    )
    parser.add_argument(
        "--report-path", default=None, help="综合报告输出路径"
    )
    parser.add_argument(
        "--unit-test-files",
        nargs="*",
        default=None,
        help="指定的单元测试文件路径（多个用空格分隔）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    # 定位后端目录
    script_dir = Path(__file__).resolve().parent
    backend_dir = script_dir.parent  # tests/ 的父目录就是 backend/
    tests_dir = backend_dir / "tests"
    report_path = args.report_path or str(tests_dir / "combined_report.md")

    if not (backend_dir / "app").exists():
        log_error(f"未找到 backend/app 目录: {backend_dir}")
        return 1

    log_section("Lvco BI 全方位自动化测试")
    log_info(f"后端目录: {backend_dir}")
    log_info(f"测试目录: {tests_dir}")
    log_info(f"报告路径: {report_path}")
    log_info(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    unit_stage: TestStage | None = None
    e2e_stage: TestStage | None = None
    unit_summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "duration_s": 0.0}
    e2e_summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "pass_rate": 0.0}

    run_unit = not args.skip_unit
    run_e2e = not args.skip_e2e

    if args.unit_only:
        run_e2e = False
    if args.e2e_only:
        run_unit = False

    overall_start = time.time()

    # ── Layer 1: 单元测试 ─────────────────────────────────────────────────
    if run_unit:
        log_section("Layer 1 · 单元测试（pytest）")
        unit_stage = run_pytest_unit_tests(
            backend_dir=str(backend_dir),
            test_files=args.unit_test_files,
        )
        unit_summary = parse_pytest_summary(unit_stage.stdout)

        if unit_stage.returncode == 0:
            log_success(
                f"单元测试通过: {unit_summary['passed']}/{unit_summary['total']} 个，"
                f"耗时 {unit_stage.duration:.1f}s"
            )
        else:
            log_error(
                f"单元测试失败: 退出码 {unit_stage.returncode}，"
                f"通过 {unit_summary['passed']}/{unit_summary['total']}"
            )
            if unit_stage.stderr:
                print(f"\n{Colors.YELLOW}--- stderr ---{Colors.RESET}")
                print(unit_stage.stderr[-1000:])
        # 输出最后 30 行 stdout（pytest summary 通常在末尾）
        if unit_stage.stdout:
            tail = "\n".join(unit_stage.stdout.splitlines()[-30:])
            print(f"\n{Colors.CYAN}--- pytest 最后 30 行 ---{Colors.RESET}")
            print(tail)

    # ── Layer 2: 端到端集成测试 ───────────────────────────────────────────
    if run_e2e:
        log_section("Layer 2 · 端到端集成测试（13 大类业务流程）")
        log_warning("提示：端到端测试会调用真实后端 API，请确保服务已启动")
        e2e_stage = run_e2e_tests(
            backend_dir=str(backend_dir),
            base_url=args.base_url,
        )
        e2e_summary = parse_e2e_summary(e2e_stage.stdout)

        if e2e_stage.returncode == 0:
            log_success(
                f"端到端测试通过: {e2e_summary['passed']}/{e2e_summary['total']} 个，"
                f"耗时 {e2e_stage.duration:.1f}s"
            )
        elif e2e_stage.returncode == 1:
            log_warning(
                f"端到端测试部分失败: 通过 {e2e_summary['passed']}，"
                f"失败 {e2e_summary['failed']}，跳过 {e2e_summary['skipped']}"
            )
        elif e2e_stage.returncode in (2, 124):
            log_error(f"端到端测试异常: 退出码 {e2e_stage.returncode}")
            if e2e_stage.stderr:
                print(f"\n{Colors.YELLOW}--- stderr ---{Colors.RESET}")
                print(e2e_stage.stderr[-1000:])
        else:
            log_warning(f"端到端测试退出码: {e2e_stage.returncode}")

        # 输出最后 50 行 stdout（含测试摘要）
        if e2e_stage.stdout:
            tail = "\n".join(e2e_stage.stdout.splitlines()[-50:])
            print(f"\n{Colors.CYAN}--- E2E 最后 50 行 ---{Colors.RESET}")
            print(tail)

    # ── 综合报告 ───────────────────────────────────────────────────────────
    log_section("生成综合测试报告")
    generate_combined_report(
        unit_stage=unit_stage,
        e2e_stage=e2e_stage,
        unit_summary=unit_summary,
        e2e_summary=e2e_summary,
        report_path=report_path,
    )

    overall_elapsed = time.time() - overall_start
    total_passed = unit_summary["passed"] + e2e_summary["passed"]
    total_failed = unit_summary["failed"] + e2e_summary["failed"]
    total_skipped = unit_summary["skipped"] + e2e_summary["skipped"]
    total = unit_summary["total"] + e2e_summary["total"]

    log_section("测试完成")
    log_info(f"总耗时: {overall_elapsed:.1f}s")
    log_info(f"总计: {total}")
    log_success(f"通过: {total_passed}")
    if total_failed > 0:
        log_error(f"失败: {total_failed}")
    if total_skipped > 0:
        log_warning(f"跳过: {total_skipped}")

    if total == 0:
        log_warning("没有任何测试被执行，请检查参数配置")

    # 返回非零退出码如果有失败
    return 0 if total_failed == 0 and total > 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log_warning("测试被用户中断")
        sys.exit(130)
    except Exception as e:
        log_error(f"未捕获异常: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)