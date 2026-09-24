"""Deterministic money calculations exposed to Copilot as tools (the model explains, the code computes)."""

from __future__ import annotations

import ast
import math
import operator
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from copilot import define_tool
from pydantic import BaseModel, Field

from .registry import ToolSpec
from .tax_params import TaxParams, load_tax_params

if TYPE_CHECKING:
    from ..context import AppContext

DISCLAIMER = "結果は目安です。専門家（税理士・FP）の助言ではありません。正確な金額は公式の情報で確認してください。"

# -- safe calculator ------------------------------------------------------------------------------------

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "round": round,
    "min": min,
    "max": max,
    "abs": abs,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
}


class CalculationError(ValueError):
    pass


def safe_eval(expression: str) -> float:
    """Evaluates an arithmetic expression without ``eval`` (numbers, + - * / // % **, parentheses, a few functions)."""
    if len(expression) > 500:
        raise CalculationError("expression is too long")
    # Remove thousands separators only ("6,000,000"), keeping commas that separate function arguments.
    expression = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", expression).replace("×", "*").replace("÷", "/")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as e:
        raise CalculationError("invalid expression") from e

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
                raise CalculationError("exponent is too large")
            try:
                return _BIN_OPS[type(node.op)](left, right)
            except ZeroDivisionError as e:
                raise CalculationError("division by zero") from e
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
            return _UNARY_OPS[type(node.op)](ev(node.operand))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FUNCS
            and not node.keywords
        ):
            try:
                return _FUNCS[node.func.id](*[ev(a) for a in node.args])
            except (TypeError, ValueError) as e:
                raise CalculationError(f"invalid arguments for {node.func.id}") from e
        raise CalculationError("unsupported expression")

    result = ev(tree)
    if isinstance(result, float) and (math.isnan(result) or math.isinf(result)):
        raise CalculationError("result is not a finite number")
    return result


# -- ふるさと納税 -------------------------------------------------------------------------------------------


def _bracket(table: list[dict], value: float) -> dict:
    for row in table:
        if row["upto"] is None or value <= row["upto"]:
            return row
    return table[-1]


def employment_income(params: TaxParams, salary: int) -> int:
    row = _bracket(params["employment_income_deduction"], salary)
    deduction = row["fixed"] if "fixed" in row else int(salary * row["rate"] + row["add"])
    return max(0, salary - deduction)


@dataclass
class FurusatoInput:
    year: int
    salary: int
    social_insurance: int | None = None
    has_spouse_deduction: bool = False
    dependents_general: int = 0
    dependents_specific: int = 0
    dependents_elderly: int = 0
    ideco_annual: int = 0
    life_insurance_deduction_income_tax: int = 0
    life_insurance_deduction_resident_tax: int = 0
    medical_deduction: int = 0
    has_housing_loan_deduction: bool = False


def estimate_furusato(params: TaxParams, inp: FurusatoInput) -> dict:
    assumptions: list[str] = []
    social = inp.social_insurance
    if social is None:
        social = int(inp.salary * 0.15)
        assumptions.append("社会保険料は給与収入の 15% で仮置きしました。")
    pd = params["personal_deductions"]
    life_max = params["life_insurance_deduction_max"]
    income = employment_income(params, inp.salary)

    common = social + inp.ideco_annual + inp.medical_deduction
    it_basic = _bracket(params["income_tax_basic_deduction"], income)["amount"]
    it_deductions = (
        common
        + it_basic
        + (pd["spouse"]["income_tax"] if inp.has_spouse_deduction else 0)
        + pd["dependent_general"]["income_tax"] * inp.dependents_general
        + pd["dependent_specific"]["income_tax"] * inp.dependents_specific
        + pd["dependent_elderly"]["income_tax"] * inp.dependents_elderly
        + min(inp.life_insurance_deduction_income_tax, life_max["income_tax"])
    )
    rt_deductions = (
        common
        + params["resident_tax_basic_deduction"]
        + (pd["spouse"]["resident_tax"] if inp.has_spouse_deduction else 0)
        + pd["dependent_general"]["resident_tax"] * inp.dependents_general
        + pd["dependent_specific"]["resident_tax"] * inp.dependents_specific
        + pd["dependent_elderly"]["resident_tax"] * inp.dependents_elderly
        + min(inp.life_insurance_deduction_resident_tax, life_max["resident_tax"])
    )
    it_taxable = max(0, (income - it_deductions) // 1000 * 1000)
    rt_taxable = max(0, (income - rt_deductions) // 1000 * 1000)
    marginal = _bracket(params["income_tax_brackets"], it_taxable)["rate"]

    # 調整控除（簡略化）: 人的控除差をもとに計算する
    diff = (
        pd["basic_diff"]
        + (pd["spouse"]["diff"] if inp.has_spouse_deduction else 0)
        + pd["dependent_general"]["diff"] * inp.dependents_general
        + pd["dependent_specific"]["diff"] * inp.dependents_specific
        + pd["dependent_elderly"]["diff"] * inp.dependents_elderly
    )
    if rt_taxable <= 2_000_000:
        adjustment = min(diff, rt_taxable) * 0.05
    else:
        adjustment = max((diff - (rt_taxable - 2_000_000)) * 0.05, 2_500)
    resident_income_levy = max(0, int(rt_taxable * params["resident_tax_rate"] - adjustment))

    denominator = 1 - params["resident_tax_income_rate_basic"] - marginal * (1 + params["reconstruction_surtax_rate"])
    limit = int(resident_income_levy * 0.20 / denominator + 2_000) if resident_income_levy > 0 else 0
    limit = limit // 1000 * 1000

    notes = [
        "住民税の所得割額の 2 割を特例分の上限として計算した目安です。",
        "上限の 8〜9 割程度で寄附すると安全です。",
    ]
    if inp.has_housing_loan_deduction:
        notes.append(
            "住宅ローン控除があるため、実際の上限はこれより少なくなる場合があります（確定申告かワンストップ特例かでも変わります）。"
        )
    return {
        "year": params.year,
        "estimated_limit_yen": limit,
        "breakdown": {
            "employment_income": income,
            "income_tax_taxable_income": it_taxable,
            "marginal_income_tax_rate": marginal,
            "resident_tax_taxable_income": rt_taxable,
            "resident_tax_income_levy": resident_income_levy,
        },
        "assumptions": assumptions,
        "notes": notes,
        "warnings": params.warnings,
        "sources": params.sources,
        "disclaimer": DISCLAIMER,
    }


# -- ライフプラン ---------------------------------------------------------------------------------------------


class LifeEvent(BaseModel):
    label: str = Field(description="イベント名（例: 住宅購入、大学入学）")
    age: int = Field(description="発生する本人の年齢")
    amount: int = Field(description="支出額（円）。収入の場合はマイナス")
    years: int = Field(default=1, ge=1, le=60, description="毎年繰り返す年数（1 なら一度だけ）")


class LifePlanParams(BaseModel):
    start_year: int = Field(description="シミュレーション開始年（西暦）")
    current_age: int = Field(ge=0, le=100)
    end_age: int = Field(default=90, ge=1, le=110)
    cash_savings: int = Field(default=0, description="現在の預貯金（円）")
    annual_net_income: int = Field(description="現在の手取り年収（円）")
    income_growth_rate: float = Field(default=0.0, ge=-0.2, le=0.2)
    retirement_age: int = Field(default=65, ge=0, le=100)
    retirement_bonus: int = Field(default=0, description="退職金（手取り、円）")
    pension_annual: int = Field(default=0, description="年金の手取り年額（円）")
    pension_start_age: int = Field(default=65, ge=50, le=80)
    annual_living_expense: int = Field(description="現在の年間生活費（住居費を含む、円）")
    inflation_rate: float = Field(default=0.01, ge=-0.05, le=0.2)
    investment_balance: int = Field(default=0, description="現在の投資残高（円）")
    annual_investment: int = Field(default=0, description="年間の積立額（円）")
    investment_end_age: int = Field(default=65, ge=0, le=110)
    investment_return_rate: float = Field(default=0.03, ge=-0.2, le=0.3)
    events: list[LifeEvent] = Field(default_factory=list)


def simulate_lifeplan(p: LifePlanParams) -> dict:
    rows = []
    cash = float(p.cash_savings)
    invest = float(p.investment_balance)
    income = float(p.annual_net_income)
    expense = float(p.annual_living_expense)
    min_total, min_age, depleted_age = math.inf, None, None
    for i, age in enumerate(range(p.current_age, p.end_age + 1)):
        year = p.start_year + i
        if i > 0:
            income *= 1 + p.income_growth_rate
            expense *= 1 + p.inflation_rate
        earned = income if age < p.retirement_age else 0.0
        if age == p.retirement_age:
            earned += p.retirement_bonus
        pension = float(p.pension_annual) if age >= p.pension_start_age else 0.0
        event_cost = sum(e.amount for e in p.events if e.age <= age < e.age + e.years)
        contribution = float(p.annual_investment) if age < p.investment_end_age else 0.0
        invest = invest * (1 + p.investment_return_rate) + contribution
        cashflow = earned + pension - expense - event_cost - contribution
        cash += cashflow
        if cash < 0 and invest > 0:
            draw = min(-cash, invest)
            invest -= draw
            cash += draw
        total = cash + invest
        if total < min_total:
            min_total, min_age = total, age
        if total < 0 and depleted_age is None:
            depleted_age = age
        rows.append(
            {
                "year": year,
                "age": age,
                "income": round(earned + pension),
                "expense": round(expense),
                "events": round(event_cost),
                "investment_contribution": round(contribution),
                "cashflow": round(cashflow),
                "cash": round(cash),
                "investment": round(invest),
                "total_assets": round(total),
            }
        )
    return {
        "rows": rows,
        "summary": {
            "min_total_assets": round(min_total) if rows else 0,
            "min_total_assets_age": min_age,
            "depleted_age": depleted_age,
            "final_total_assets": rows[-1]["total_assets"] if rows else 0,
        },
        "assumptions": [
            f"インフレ率 {p.inflation_rate:.1%}、運用利回り {p.investment_return_rate:.1%}、"
            f"収入の伸び {p.income_growth_rate:.1%}",
            "預貯金の利息は 0%。預貯金が不足した年は投資から取り崩す想定です。",
            "税金・社会保険料は手取り額に含まれている前提です。",
        ],
        "chart": {"type": "lifeplan", "x": "age", "series": ["cash", "investment", "total_assets"]},
        "disclaimer": DISCLAIMER,
    }


# -- tool definitions -----------------------------------------------------------------------------------


class CalculateParams(BaseModel):
    expression: str = Field(
        description="計算式。例: (6000000 - 1640000) * 0.1。使える関数: round, min, max, abs, floor, ceil, sqrt"
    )


class FurusatoParams(BaseModel):
    year: int = Field(description="寄附する年（西暦）")
    salary: int = Field(ge=0, description="給与収入（額面、円）")
    social_insurance: int | None = Field(
        default=None, description="社会保険料（円）。不明なら省略（収入の 15% で仮置き）"
    )
    has_spouse_deduction: bool = Field(default=False, description="配偶者控除の対象となる配偶者がいるか")
    dependents_general: int = Field(default=0, ge=0, description="一般の扶養親族の人数（16〜18 歳、23〜69 歳）")
    dependents_specific: int = Field(default=0, ge=0, description="特定扶養親族の人数（19〜22 歳）")
    dependents_elderly: int = Field(default=0, ge=0, description="老人扶養親族の人数（70 歳以上）")
    ideco_annual: int = Field(default=0, ge=0, description="iDeCo の年間掛金（円）")
    life_insurance_deduction_income_tax: int = Field(default=0, ge=0, description="生命保険料控除（所得税分、円）")
    life_insurance_deduction_resident_tax: int = Field(default=0, ge=0, description="生命保険料控除（住民税分、円）")
    medical_deduction: int = Field(default=0, ge=0, description="医療費控除（円）")
    has_housing_loan_deduction: bool = Field(default=False, description="住宅ローン控除を受けているか")


def build_tools(ctx: AppContext) -> list[ToolSpec]:
    tax_dir = ctx.settings.tax_params_dir

    @define_tool(
        name="calculate",
        description="四則演算などの計算を正確に行う。お金の計算は暗算せず必ずこのツールを使う。",
        skip_permission=True,
    )
    def calculate(params: CalculateParams) -> dict:
        try:
            return {"expression": params.expression, "result": safe_eval(params.expression)}
        except CalculationError as e:
            return {"error": str(e)}

    @define_tool(
        name="estimate_furusato_limit",
        description="給与収入・家族構成・控除から、ふるさと納税の控除上限額の目安を計算する。",
        skip_permission=True,
    )
    def estimate_furusato_limit(params: FurusatoParams) -> dict:
        tax = load_tax_params(tax_dir, params.year)
        return estimate_furusato(tax, FurusatoInput(**params.model_dump()))

    @define_tool(
        name="simulate_lifeplan",
        description="収入・支出・ライフイベント・運用から、年ごとの収支と資産残高をシミュレーションする。",
        skip_permission=True,
    )
    def simulate_lifeplan_tool(params: LifePlanParams) -> dict:
        return simulate_lifeplan(params)

    return [ToolSpec(calculate), ToolSpec(estimate_furusato_limit), ToolSpec(simulate_lifeplan_tool)]
