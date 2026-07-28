"""The 3-agent advisory graph — topology makes the deterministic → LLM seam explicit.

    FeatureBuilderNode (DETERMINISTIC, no model)
        → IrrigationNode (LLM) → SprayNode (LLM) → CoordinatorNode (LLM) → End[DailyFarmAdvisory]

Why a graph (not agent-as-tool / programmatic hand-off)? The graph makes the LLM/deterministic
boundary a first-class topology element — the non-agent FeatureBuilderNode is visible in the node
list and the mermaid diagram — and gives fixed, testable ordering with one typed shared state.
Agent-as-tool would let the model decide control flow and hide the coordinator's reconcile inside a
tool call; programmatic hand-off is the valid runner-up but doesn't surface the boundary as structure.

State vs deps: FarmState = per-run mutable data (rows + accumulating advices); Deps (crop config) =
injected read-only params, so a new crop/region is a deps change, not a code change.

API note: uses the class-based `BaseNode`/`Graph` API (pinned `pydantic-graph<2`) because it expresses
the deterministic-node boundary most directly. pydantic-graph 1.107 steers new code toward the
builder-based `GraphBuilder`; migrating to it is the v2 path (TODO) — the topology is unchanged.

Note: Load+Validate is done by `ingest.load_weather` *before* the graph (I/O kept at the edge); the
graph's first node is the deterministic FeatureBuilder. Irrigation & Spray are independent — chained
sequentially here; true concurrency would be `asyncio.gather` in one node (an upgrade, not needed for
a nightly batch). TODO(B2): thread a shared RunUsage through state for graph-wide token totals.
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass
from datetime import date

# We pin pydantic-graph<2 and knowingly use the class-based Graph API (see module docstring).
warnings.filterwarnings("ignore", message=r"Importing .Graph. from .pydantic_graph.")
from pydantic_graph import BaseNode, End, Graph, GraphRunContext  # noqa: E402

from .agents import run_coordinator_agent, run_irrigation_agent, run_spray_agent
from .contracts import DailyFarmAdvisory, FarmFeatures, IrrigationAdvice, SprayAdvice
from .deps import WINE_GRAPES, Deps
from .features import build_features
from .ingest import DataQuality, WeatherRow
from .reconcile import build_conflict_facts


@dataclass
class FarmState:
    """Mutable shared state threaded through every node (slots fill as the graph progresses)."""

    history: list[WeatherRow]
    forecast: list[WeatherRow]
    run_date: date
    data_quality: DataQuality
    features: FarmFeatures | None = None
    irrigation: IrrigationAdvice | None = None
    spray: SprayAdvice | None = None


@dataclass
class FeatureBuilderNode(BaseNode[FarmState, Deps]):
    # DETERMINISTIC — no model call. Computes the FAO-56 water balance + spray windows.
    async def run(self, ctx: GraphRunContext[FarmState, Deps]) -> IrrigationNode:
        ctx.state.features = build_features(
            ctx.state.history, ctx.state.forecast, ctx.state.data_quality, ctx.state.run_date, ctx.deps
        )
        return IrrigationNode()


@dataclass
class IrrigationNode(BaseNode[FarmState, Deps]):
    async def run(self, ctx: GraphRunContext[FarmState, Deps]) -> SprayNode:
        f = ctx.state.features
        ctx.state.irrigation = await run_irrigation_agent(
            ctx.deps, f.irrigation, f.data_quality, f.target_date, ctx.state.run_date
        )
        return SprayNode()


@dataclass
class SprayNode(BaseNode[FarmState, Deps]):
    async def run(self, ctx: GraphRunContext[FarmState, Deps]) -> CoordinatorNode:
        f = ctx.state.features
        ctx.state.spray = await run_spray_agent(
            ctx.deps, f.spray, f.data_quality, f.target_date, ctx.state.run_date
        )
        return CoordinatorNode()


@dataclass
class CoordinatorNode(BaseNode[FarmState, Deps, DailyFarmAdvisory]):
    async def run(self, ctx: GraphRunContext[FarmState, Deps]) -> End[DailyFarmAdvisory]:
        f = ctx.state.features
        facts = build_conflict_facts(
            ctx.state.forecast, ctx.state.irrigation, ctx.state.spray, ctx.deps, f.target_date
        )
        advisory = await run_coordinator_agent(
            ctx.deps, ctx.state.irrigation, ctx.state.spray, facts, f.data_quality, f.target_date, ctx.state.run_date
        )
        return End(advisory)


advisory_graph = Graph(
    nodes=(FeatureBuilderNode, IrrigationNode, SprayNode, CoordinatorNode),
    state_type=FarmState,
    run_end_type=DailyFarmAdvisory,
    name="vineyard_advisor",
)


def _state(history, forecast, data_quality, run_date) -> FarmState:
    return FarmState(history=history, forecast=forecast, run_date=run_date, data_quality=data_quality)


async def run_advisory(
    history: list[WeatherRow], forecast: list[WeatherRow], data_quality: DataQuality,
    run_date: date, deps: Deps = WINE_GRAPES,
) -> DailyFarmAdvisory:
    result = await advisory_graph.run(
        FeatureBuilderNode(), state=_state(history, forecast, data_quality, run_date), deps=deps
    )
    return result.output


def run_advisory_sync(
    history: list[WeatherRow], forecast: list[WeatherRow], data_quality: DataQuality,
    run_date: date, deps: Deps = WINE_GRAPES,
) -> DailyFarmAdvisory:
    return asyncio.run(run_advisory(history, forecast, data_quality, run_date, deps))


def mermaid() -> str:
    """Topology diagram (committed in the README so the LLM/deterministic seam is visible)."""
    return advisory_graph.mermaid_code(start_node=FeatureBuilderNode)
