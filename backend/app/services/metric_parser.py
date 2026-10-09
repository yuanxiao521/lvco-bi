import re
from dataclasses import dataclass


@dataclass(frozen=True)
class MetricRef:
    metric_key: str
    metric_id: str | None = None


class InvalidFormulaError(ValueError):
    pass


def parse_derived_formula(formula: str | None) -> list[MetricRef]:
    if not formula or not formula.strip():
        return []
    pattern = r"metric\(['\"]([^'\"]*)['\"]\)"
    matches = re.findall(pattern, formula)
    if not matches:
        if re.search(r"metric\s*\(", formula):
            raise InvalidFormulaError(f"Invalid metric reference syntax in formula: {formula}")
        return []
    seen = set()
    result = []
    for key in matches:
        if not key.strip():
            raise InvalidFormulaError(f"Empty metric key in formula: {formula}")
        if key not in seen:
            seen.add(key)
            result.append(MetricRef(metric_key=key))
    return result


def validate_formula_syntax(formula: str | None) -> bool:
    if not formula:
        return True
    stack = 0
    for ch in formula:
        if ch == '(':
            stack += 1
        elif ch == ')':
            stack -= 1
        if stack < 0:
            return False
    return stack == 0


def detect_cycle(metric_id: str, all_metrics: dict[str, str]) -> list[list[str]]:
    cycles = []
    visited = set()
    recursion_stack = []

    def dfs(current: str) -> None:
        if current in recursion_stack:
            idx = recursion_stack.index(current)
            cycles.append(recursion_stack[idx:] + [current])
            return
        if current in visited:
            return
        visited.add(current)
        recursion_stack.append(current)

        formula = all_metrics.get(current, "")
        if formula:
            refs = parse_derived_formula(formula)
            for ref in refs:
                dfs(ref.metric_key)

        recursion_stack.pop()

    dfs(metric_id)
    return cycles